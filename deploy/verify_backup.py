"""Read-only verification of a stopped/restored SignalNest database and raw directory.

Run with the installed project Python. This script never creates storage, upgrades a
schema, downloads missing files, modifies evidence, or deletes orphan files.
"""

import argparse
import hashlib
import json
import re
import sqlite3
import stat
import sys
from contextlib import closing
from email import policy
from email.parser import BytesParser
from email.utils import parsedate_to_datetime
from pathlib import Path

from alembic.script import ScriptDirectory
from pydantic import TypeAdapter, ValidationError

from signalnest.cache import reuse_reason, validation_reason
from signalnest.contracts import (
    REFERENCE_NORMALIZATION_VERSION,
    NoticeContent,
    RequestProfile,
    WebUrl,
)
from signalnest.ingestion_state import ScanCompletion
from signalnest.mail.contracts import SendErrorCode, SendOutcome, SendResult, SendStage
from signalnest.notifications.contracts import Decision, canonical_sha256
from signalnest.rawstore import RawStore, RawStoreError
from signalnest.storage import migration_config


class BackupVerificationError(RuntimeError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _json(value, code):
    try:
        return json.loads(value)
    except (ValueError, TypeError) as exc:
        raise BackupVerificationError(code) from exc


def _records(connection, table, key="id"):
    return {row[key]: dict(row) for row in connection.execute(f"SELECT * FROM {table}")}


def _search_schema(connection):
    """Require rebuildable 0007 structure, but allow missing/stale derived rows.

    Rebuild repairs postings/content, not a dropped schema object at Alembic head.
    These checks only read sqlite_master/PRAGMA; FTS's write-style integrity command
    deliberately does not run while validating a read-only backup.
    """
    code = "backup_search_schema_missing"
    names = (
        "search_documents",
        "search_fts",
        "search_documents_insert",
        "search_documents_delete",
        "search_documents_update",
    )
    placeholders = ",".join("?" for _ in names)
    records = {
        row["name"]: dict(row)
        for row in connection.execute(
            "SELECT name,type,tbl_name,sql FROM sqlite_master WHERE name IN (" + placeholders + ")",
            names,
        )
    }
    if set(records) != set(names):
        raise BackupVerificationError(code)
    virtual = re.compile(r"^\s*CREATE\s+VIRTUAL\s+TABLE\b", re.IGNORECASE)
    regular = records["search_documents"]
    fts = records["search_fts"]
    if (
        regular["type"] != "table"
        or regular["tbl_name"] != "search_documents"
        or not regular["sql"]
        or virtual.match(regular["sql"])
        or fts["type"] != "table"
        or fts["tbl_name"] != "search_fts"
        or not fts["sql"]
        or not virtual.match(fts["sql"])
        or not re.search(r"\bUSING\s+fts5\s*\(", fts["sql"], re.IGNORECASE)
    ):
        raise BackupVerificationError(code)
    columns = [tuple(row) for row in connection.execute("PRAGMA table_info(search_documents)")]
    if [row[1] for row in columns] != [
        "document_id",
        "version_id",
        "title_text",
        "body_text",
        "index_version",
    ]:
        raise BackupVerificationError(code)
    if [row[1] for row in connection.execute("PRAGMA table_info(search_fts)")] != [
        "title_text",
        "body_text",
    ]:
        raise BackupVerificationError(code)
    for name in names[2:]:
        trigger = records[name]
        if (
            trigger["type"] != "trigger"
            or trigger["tbl_name"] != "search_documents"
            or not trigger["sql"]
        ):
            raise BackupVerificationError(code)


def _numbers(row, fields, code, *, minimum=0, optional=()):
    """SQLite affinity is not a type constraint; corrupted text must fail finitely."""
    for field in fields:
        value = row[field]
        if value is None and field in optional:
            continue
        if type(value) is not int or not minimum <= value <= 2**63 - 1:
            raise BackupVerificationError(code)


def _booleans(row, fields, code):
    if any(type(row[field]) is not int or row[field] not in {0, 1} for field in fields):
        raise BackupVerificationError(code)


def _mail_payload(row):
    """Validate the stored bytes, rather than render them again with current rules."""
    code = "backup_mail_payload"
    _numbers(row, ("id", "part", "max_bytes", "max_events"), code, minimum=1)
    _numbers(row, ("date_at", "frozen_at"), code)
    payload = row["payload_bytes"]
    if (
        not isinstance(payload, bytes)
        or not 0 < len(payload) <= row["max_bytes"] <= 1024 * 1024
        or hashlib.sha256(payload).hexdigest() != row["payload_sha256"]
        or not payload.endswith(b"\r\n")
        or b"\n" in payload.replace(b"\r\n", b"")
        or b"\r" in payload.replace(b"\r\n", b"")
        or any(byte > 127 for byte in payload)
    ):
        raise BackupVerificationError(code)
    try:
        message = BytesParser(policy=policy.SMTP).parsebytes(payload)
        date = parsedate_to_datetime(message["Date"])
        if (
            message.defects
            or message.is_multipart()
            or message.get_content_type() != "text/plain"
            or message.get_content_charset() != "utf-8"
            or any(message.get_all(name, []) for name in ("Cc", "Bcc", "Resent-To"))
            or len(message.get_all("Date", [])) != 1
            or date.tzinfo is None
            or int(date.timestamp()) != row["date_at"]
            or any(
                len(message.get_all(header, [])) != 1 or str(message[header]) != row[column]
                for header, column in (
                    ("From", "sender"),
                    ("To", "recipient"),
                    ("Subject", "subject"),
                    ("Message-ID", "message_id"),
                )
            )
        ):
            raise BackupVerificationError(code)
        message.get_content()
    except (ValueError, TypeError, KeyError, AttributeError, UnicodeError) as exc:
        raise BackupVerificationError(code) from exc


def _observed_response(observed, body, records):
    """Validate historical evidence without consulting mutable latest-resource pointers."""
    code = "backup_mail_evidence"
    for response in (observed, body):
        _numbers(response, ("id", "status_code", "document_id"), code, minimum=1)
        _numbers(response, ("fetched_at",), code)
    if (
        observed["source_id"] != body["source_id"]
        or observed["document_id"] != body["document_id"]
        or observed["page_type"] != "notice"
        or observed["fetched_at"] < body["fetched_at"]
    ):
        raise BackupVerificationError(code)
    if observed["status_code"] == 200:
        if observed["id"] != body["id"]:
            raise BackupVerificationError(code)
        return
    resource = records["http_resources"].get(body["resource_id"])
    if (
        observed["status_code"] != 304
        or observed["validated_response_id"] != body["id"]
        or observed["resource_id"] != body["resource_id"]
        or resource is None
        or observed["requested_url"] != body["requested_url"]
        or observed["final_url"] != body["final_url"]
        or observed["body_path"] is not None
        or observed["body_sha256"] is not None
        or observed["body_state"] != "unavailable"
    ):
        raise BackupVerificationError(code)
    for response in (observed, body):
        if any(
            response[field] is not None and not isinstance(response[field], str)
            for field in ("etag", "last_modified", "vary", "cache_control", "content_encoding")
        ):
            raise BackupVerificationError(code)
    try:
        profile = RequestProfile.model_validate(_json(resource["request_profile"], code))
    except (ValidationError, ValueError, TypeError) as exc:
        raise BackupVerificationError(code) from exc
    if (
        profile.sha256() != resource["profile_sha256"]
        or reuse_reason(body, resource) is not None
        or validation_reason(observed, body) is not None
    ):
        raise BackupVerificationError(code)


def _mail_members(connection, row, records):
    code = "backup_mail_members"
    members = [
        dict(member)
        for member in connection.execute(
            "SELECT * FROM mail_message_members WHERE mail_id = ? ORDER BY position", (row["id"],)
        )
    ]
    manifest = [_json(member["snapshot"], code) for member in members]
    if (
        not 0 < len(members) <= row["max_events"]
        or [member["position"] for member in members] != list(range(len(members)))
        or canonical_sha256(manifest) != row["members_sha256"]
        or (row["kind"] == "immediate" and len(members) != 1)
    ):
        raise BackupVerificationError(code)
    for member, snapshot in zip(members, manifest, strict=True):
        if (
            not isinstance(snapshot, dict)
            or snapshot.get("event_id") != member["event_id"]
            or snapshot.get("decision_id") != member["decision_id"]
        ):
            raise BackupVerificationError(code)
        try:
            event = records["notification_events"][member["event_id"]]
            doc = records["documents"][event["document_id"]]
            version = records["notice_versions"][event["version_id"]]
            decision = records["notification_decisions"][member["decision_id"]]
            body = records["raw_responses"][event["body_response_id"]]
            observed = records["raw_responses"][event["observed_response_id"]]
            channel = records["notification_channel_state"][row["installation_id"]]
            content = NoticeContent.model_validate(_json(version["normalized_content"], code))
            decision_json = _json(decision["decision"], code)
            selected = Decision.model_validate(decision_json)
            policy_record = records["notification_policy_revisions"][decision["policy_revision_id"]]
        except (KeyError, ValidationError, TypeError, ValueError) as exc:
            raise BackupVerificationError("backup_mail_evidence") from exc
        _numbers(event, ("id", "document_id", "version_id"), code, minimum=1)
        _numbers(
            event, ("event_seq",), code, minimum=0 if event["kind"] == "opportunity_review" else 1
        )
        if event["kind"] == "opportunity_review" and event["event_seq"] != 0:
            raise BackupVerificationError(code)
        _numbers(event, ("occurred_at",), code)
        _numbers(decision, ("id", "event_id", "policy_revision_id"), code, minimum=1)
        _numbers(decision, ("evaluated_at",), code)
        _numbers(version, ("id", "document_id", "raw_response_id"), code, minimum=1)
        _numbers(version, ("parsed_at",), code)
        _observed_response(observed, body, records)
        expected = dict(
            event_id=event["id"],
            decision_id=decision["id"],
            document_id=doc["id"],
            source_id=doc["source_id"],
            source_document_id=doc["source_document_id"],
            version_id=version["id"],
            content_sha256=version["content_sha256"],
            parser_version=version["parser_version"],
            body_response_id=event["body_response_id"],
            observed_response_id=event["observed_response_id"],
            page_url=body["final_url"],
            event_kind=event["kind"],
            occurred_at=event["occurred_at"],
            decision=decision_json,
            policy_revision_id=decision["policy_revision_id"],
        )
        # Bind to the decision frozen into the mail, not a later policy or the
        # document's mutable current-version/URL. Historical mail remains valid.
        if (
            snapshot != expected
            or event["installation_id"] != row["installation_id"]
            or doc["source_id"] != channel["source_id"]
            or version["document_id"] != doc["id"]
            or decision["event_id"] != event["id"]
            or content.content_sha256() != version["content_sha256"]
            or selected.content_sha256 != version["content_sha256"]
            or selected.effective_route != row["kind"]
            or int(selected.evaluated_at.timestamp()) != decision["evaluated_at"]
            or selected.policy_sha256 != policy_record["policy_sha256"]
            or canonical_sha256(_json(policy_record["manifest"], code))
            != policy_record["policy_sha256"]
            or body["document_id"] != doc["id"]
            or body["source_id"] != doc["source_id"]
            or body["page_type"] != "notice"
            or body["status_code"] != 200
            or body["body_state"] != "complete"
            or body["body_path"] is None
        ):
            raise BackupVerificationError("backup_mail_evidence")
        if row["kind"] == "immediate":
            intent = records["email_outbox"].get(row["immediate_intent_id"])
            if intent is None or any(
                intent[key] != value
                for key, value in dict(
                    event_id=event["id"],
                    decision_id=decision["id"],
                    sender=row["sender"],
                    recipient=row["recipient"],
                ).items()
            ):
                raise BackupVerificationError("backup_mail_evidence")
    return len(members)


def _mail_attempts(connection, mail, delivery):
    code = "backup_mail_attempts"
    attempts = [
        dict(row)
        for row in connection.execute(
            "SELECT * FROM mail_attempts WHERE mail_id = ? ORDER BY attempt_no", (mail["id"],)
        )
    ]
    if len(attempts) != delivery["attempt_count"] or any(
        attempt["attempt_no"] != index for index, attempt in enumerate(attempts, start=1)
    ):
        raise BackupVerificationError(code)
    # Validate the whole sequence before semantic checks that inspect a later
    # attempt (notably the uncertain cooldown). SQLite affinity permits text.
    for attempt in attempts:
        _numbers(attempt, ("mail_id", "attempt_no"), code, minimum=1)
        _numbers(
            attempt,
            ("started_at", "finished_at", "uncertain_until"),
            code,
            optional=("finished_at", "uncertain_until"),
        )
        _booleans(attempt, ("manual", "recovered", "cleanup_failed"), code)
    previous_finish = mail["frozen_at"]
    for index, attempt in enumerate(attempts):
        if attempt["started_at"] < previous_finish:
            raise BackupVerificationError(code)
        if attempt["finished_at"] is None:
            if (
                index != len(attempts) - 1
                or delivery["state"] != "sending"
                or any(
                    attempt[key] is not None
                    for key in (
                        "outcome",
                        "stage",
                        "error_code",
                        "smtp_code",
                        "scope",
                        "uncertain_until",
                    )
                )
                or attempt["recovered"]
                or attempt["cleanup_failed"]
            ):
                raise BackupVerificationError(code)
            continue
        previous_finish = attempt["finished_at"]
        if previous_finish < attempt["started_at"] or (
            attempt["outcome"] == "accepted" and index != len(attempts) - 1
        ):
            raise BackupVerificationError(code)
        if attempt["recovered"]:
            if (
                attempt["outcome"] != "uncertain"
                or attempt["stage"] != "unknown"
                or attempt["error_code"] != "process_interrupted"
                or attempt["scope"] != "message"
                or attempt["smtp_code"] is not None
                or attempt["cleanup_failed"]
            ):
                raise BackupVerificationError(code)
        else:
            try:
                SendResult(
                    SendOutcome(attempt["outcome"]),
                    SendStage(attempt["stage"]),
                    SendErrorCode(attempt["error_code"]) if attempt["error_code"] else None,
                    attempt["smtp_code"],
                    attempt["scope"],
                    bool(attempt["cleanup_failed"]),
                )
            except ValueError as exc:
                raise BackupVerificationError(code) from exc
        if attempt["outcome"] == "uncertain":
            if (
                attempt["uncertain_until"] is None
                or attempt["uncertain_until"] < previous_finish + 1800
            ):
                raise BackupVerificationError(code)
            if (
                index + 1 < len(attempts)
                and attempts[index + 1]["started_at"] < attempt["uncertain_until"]
            ):
                raise BackupVerificationError(code)
        elif attempt["uncertain_until"] is not None:
            raise BackupVerificationError(code)
    return attempts


def _mail_delivery(connection, mail, row):
    code = "backup_mail_delivery"
    if row is None:
        raise BackupVerificationError(code)
    _numbers(row, ("mail_id",), code, minimum=1)
    _numbers(
        row,
        ("attempt_count", "updated_at", "next_attempt_at", "accepted_at"),
        code,
        optional=("next_attempt_at", "accepted_at"),
    )
    _booleans(row, ("manual_retry_pending",), code)
    if row["updated_at"] < mail["frozen_at"]:
        raise BackupVerificationError(code)
    attempts = _mail_attempts(connection, mail, row)
    last = attempts[-1] if attempts else None
    state = row["state"]
    if last and row["updated_at"] < (last["finished_at"] or last["started_at"]):
        raise BackupVerificationError(code)
    if state == "sending":
        valid = (
            last is not None
            and last["finished_at"] is None
            and row["updated_at"] == last["started_at"]
            and not row["manual_retry_pending"]
        )
    elif state == "accepted":
        valid = (
            last is not None
            and last["outcome"] == "accepted"
            and row["accepted_at"] == row["updated_at"] == last["finished_at"]
            and not row["manual_retry_pending"]
        )
    elif state in {"retry", "uncertain"}:
        outcome = "retryable" if state == "retry" else "uncertain"
        valid = (
            last is not None
            and last["outcome"] == outcome
            and not last["manual"]
            and row["updated_at"] == last["finished_at"]
            and row["next_attempt_at"] > last["finished_at"]
            and not row["manual_retry_pending"]
            and (state != "uncertain" or row["next_attempt_at"] >= last["uncertain_until"])
        )
    elif state == "pending":
        valid = (
            row["next_attempt_at"] >= row["updated_at"]
            and (
                row["manual_retry_pending"]
                or (last is None and row["updated_at"] == mail["frozen_at"])
            )
            and (last is None or last["finished_at"] is not None)
            and (last is None or last["outcome"] != "accepted")
            and (
                last is None
                or last["outcome"] != "uncertain"
                or row["next_attempt_at"] >= last["uncertain_until"]
            )
        )
    else:
        reason = row["blocked_reason"]
        valid = not row["manual_retry_pending"] and (
            reason == "frozen_corrupt"
            or (
                last is not None
                and last["finished_at"] is not None
                and (
                    (reason == "permanent" and last["outcome"] == "permanent")
                    or (
                        reason == "retry_exhausted"
                        and last["outcome"] in {"retryable", "uncertain"}
                    )
                    or (
                        reason == "manual_attempt_failed"
                        and last["manual"]
                        and last["outcome"] in {"retryable", "uncertain"}
                    )
                )
            )
        )
    if not valid or (last and last["outcome"] == "accepted" and state != "accepted"):
        raise BackupVerificationError(code)
    return len(attempts)


def _verify_mail(connection):
    records = {
        table: _records(connection, table, key)
        for table, key in (
            ("notification_channel_state", "installation_id"),
            ("notification_events", "id"),
            ("notification_decisions", "id"),
            ("notification_policy_revisions", "id"),
            ("documents", "id"),
            ("notice_versions", "id"),
            ("raw_responses", "id"),
            ("email_outbox", "id"),
            ("http_resources", "id"),
        )
    }
    channels = records["notification_channel_state"].values()
    errors = {error.value for error in SendErrorCode}
    for channel in channels:
        _numbers(
            channel,
            ("activation_at", "paused_at"),
            "backup_mail_channel_pause",
            optional=("paused_at",),
        )
        _booleans(channel, ("paused",), "backup_mail_channel_pause")
        # Pre-N4 paused channels can legitimately have an unknown reason/time.
        if (
            not channel["paused"]
            and (channel["pause_reason"] is not None or channel["paused_at"] is not None)
        ) or (
            channel["paused"]
            and (
                (channel["pause_reason"] is None) != (channel["paused_at"] is None)
                or (
                    channel["pause_reason"] is not None
                    and channel["pause_reason"] not in errors | {"manual"}
                )
            )
        ):
            raise BackupVerificationError("backup_mail_channel_pause")
    delivery = _records(connection, "mail_delivery", "mail_id")
    counts = dict.fromkeys(("pending", "sending", "retry", "uncertain", "accepted", "blocked"), 0)
    members = attempts = 0
    messages = _records(connection, "mail_messages")
    for mail in messages.values():
        _mail_payload(mail)
        members += _mail_members(connection, mail, records)
        row = delivery.get(mail["id"])
        attempts += _mail_delivery(connection, mail, row)
        counts[row["state"]] += 1
    return {
        "mail_messages": len(messages),
        "mail_message_members": members,
        "mail_attempts": attempts,
        "mail_paused_channels": sum(bool(channel["paused"]) for channel in channels),
        **{f"mail_{state}": count for state, count in counts.items()},
    }


def _list_binding(records, source_id, body_id, observed_id):
    body, observed = records.get(body_id), records.get(observed_id)
    if (
        body is None
        or observed is None
        or any(
            row["source_id"] != source_id or row["page_type"] != "list" for row in (body, observed)
        )
        or body["status_code"] != 200
        or body["body_path"] is None
    ):
        raise BackupVerificationError("backup_list_evidence_invalid")
    if observed_id != body_id and (
        observed["status_code"] != 304
        or observed["validated_response_id"] != body_id
        or any(
            observed[field] != body[field]
            for field in ("resource_id", "requested_url", "final_url")
        )
    ):
        raise BackupVerificationError("backup_list_evidence_invalid")
    return body, observed


def _verify_references(connection):
    responses = _records(connection, "raw_responses")
    references = _records(connection, "discovered_references")
    validator = TypeAdapter(WebUrl)
    for row in references.values():
        try:
            uri = str(validator.validate_python(row["resolved_url"]))
        except ValidationError as exc:
            raise BackupVerificationError("backup_reference_invalid") from exc
        if (
            row["normalization_version"] != REFERENCE_NORMALIZATION_VERSION
            or uri != row["resolved_url"]
            or row["candidate_key"] != "link:v1:" + hashlib.sha256(uri.encode("utf-8")).hexdigest()
            or row["status"] != "pending_adapter"
        ):
            raise BackupVerificationError("backup_reference_invalid")
        for prefix in ("first", "last"):
            _, observed = _list_binding(
                responses,
                row["source_id"],
                row[f"{prefix}_body_response_id"],
                row[f"{prefix}_observed_response_id"],
            )
            if row[f"{prefix}_seen_at"] < observed["fetched_at"]:
                raise BackupVerificationError("backup_reference_invalid")
    ledgers = 0
    for run in connection.execute(
        "SELECT * FROM ingestion_runs WHERE coverage_evidence IS NOT NULL"
    ):
        try:
            completion = ScanCompletion.model_validate(
                _json(run["coverage_evidence"], "backup_scan_evidence_invalid")
            )
        except ValidationError as exc:
            raise BackupVerificationError("backup_scan_evidence_invalid") from exc
        if run["coverage"] != "complete":
            raise BackupVerificationError("backup_scan_evidence_invalid")
        if completion.registrations is None:
            continue  # Explicit legacy-style attestation, not an invented response ledger.
        for page in (*completion.registrations, completion.home_recheck):
            body, _ = _list_binding(
                responses, run["source_id"], page.body_response_id, page.observed_response_id
            )
            if body["requested_url"] != str(page.response_requested_url) or body[
                "final_url"
            ] != str(page.final_url):
                raise BackupVerificationError("backup_scan_evidence_invalid")
            for kind, key in page.rows:
                table, column = (
                    ("documents", "source_document_id")
                    if kind == "notice"
                    else ("discovered_references", "candidate_key")
                )
                if (
                    connection.execute(
                        f"SELECT 1 FROM {table} WHERE source_id=? AND {column}=?",
                        (run["source_id"], key),
                    ).fetchone()
                    is None
                ):
                    raise BackupVerificationError("backup_scan_evidence_invalid")
        ledgers += 1
    return {"discovered_references": len(references), "complete_scan_ledgers": ledgers}


def verify_backup(database: Path, data_dir: Path) -> dict[str, int | str]:
    """Check relational integrity and every raw reference without opening a writer."""
    database = database.absolute()
    data_dir = data_dir.absolute()
    if database.is_symlink() or not database.is_file():
        raise BackupVerificationError("backup_database_missing_or_unsafe")
    expected_revision = ScriptDirectory.from_config(migration_config()).get_current_head()
    try:
        with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as connection:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA query_only=ON")
            if [tuple(row) for row in connection.execute("PRAGMA integrity_check")] != [("ok",)]:
                raise BackupVerificationError("backup_database_integrity")
            if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
                raise BackupVerificationError("backup_foreign_keys")
            revisions = [
                tuple(row) for row in connection.execute("SELECT version_num FROM alembic_version")
            ]
            if revisions != [(expected_revision,)]:
                raise BackupVerificationError("backup_revision_mismatch")
            _search_schema(connection)
            invalid_success = connection.execute(
                "SELECT d.id FROM documents d LEFT JOIN notice_versions v "
                "ON v.id = d.current_version_id AND v.document_id = d.id "
                "WHERE (d.current_version_id IS NULL) != (d.last_success_at IS NULL) "
                "OR (d.current_version_id IS NOT NULL AND v.id IS NULL) LIMIT 1"
            ).fetchone()
            if invalid_success is not None:
                raise BackupVerificationError("backup_success_pointer")
            bodies = connection.execute(
                "SELECT DISTINCT body_path, body_sha256 FROM raw_responses "
                "WHERE body_path IS NOT NULL"
            ).fetchall()
            counts = {
                table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                for table in ("documents", "notice_versions", "raw_responses", "ingestion_runs")
            }
            counts.update(_verify_mail(connection))
            counts.update(_verify_references(connection))
        raw_store = RawStore(data_dir)
        raw_dir = data_dir / "raw"
        if not raw_dir.is_dir() or any(path.is_symlink() for path in (raw_dir, *raw_dir.parents)):
            raise BackupVerificationError("backup_raw_directory_missing_or_unsafe")
        for path, digest in bodies:
            raw_store.read(path, digest)
        referenced = {Path(path).name for path, _ in bodies}
        raw_files = 0
        temporary_files = 0
        orphan_files = 0
        for entry in raw_dir.iterdir():
            if not stat.S_ISREG(entry.lstat().st_mode):
                raise BackupVerificationError("backup_raw_unsafe_entry")
            if entry.name.startswith(".tmp-"):
                temporary_files += 1
            else:
                raw_files += 1
                orphan_files += entry.name not in referenced
    except RawStoreError as exc:
        raise BackupVerificationError(exc.code) from exc
    except (sqlite3.Error, OSError) as exc:
        raise BackupVerificationError("backup_unreadable") from exc
    return {
        "revision": expected_revision,
        **counts,
        "referenced_bodies": len(bodies),
        "raw_files": raw_files,
        "orphan_files": orphan_files,
        "temporary_files": temporary_files,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--data-dir", type=Path, required=True)
    args = parser.parse_args()
    try:
        report = verify_backup(args.database, args.data_dir)
    except BackupVerificationError as exc:
        print(f"备份校验失败: {exc.code}", file=sys.stderr)
        return 1
    print(json.dumps(report, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
