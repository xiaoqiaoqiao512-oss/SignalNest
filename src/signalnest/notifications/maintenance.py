"""Explicit policy changes and bounded, replayable local event re-evaluation.

Callers hold the instance writer lock for writes. All extraction and rule
evaluation finishes outside write transactions. No historical event is created,
no frozen mail is changed and this module never sends mail.
"""

import json
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import sqlalchemy as sa
from pydantic import ValidationError
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import SQLAlchemyError

from signalnest.contracts import NoticeContent
from signalnest.errors import IngestError
from signalnest.notifications.contracts import (
    Decision,
    EventContext,
    NoticeFacts,
    Profile,
    canonical_json,
    canonical_sha256,
)
from signalnest.notifications.decision import deadline_for_profile, decide, policy_manifest
from signalnest.notifications.facts import extract_facts
from signalnest.notifications.state import (
    SHANGHAI,
    channel_on,
    next_digest_at,
    notification_time,
)
from signalnest.schema import (
    documents,
    email_outbox,
    mail_message_members,
    notice_versions,
    notification_channel_state,
    notification_decisions,
    notification_events,
    notification_observations,
    notification_operations,
    notification_policy_revisions,
)

_OPERATIONS = notification_operations
_CONTEXT_KEYS = (
    "installation_id",
    "source_id",
    "policy_revision_id",
    "notification_mode",
    "initial_recent_review",
    "digest_hour",
    "digest_minute",
    "sender",
    "recipient",
)


def _error(code: str, *, document_id: int | None = None):
    return IngestError(code, "notification", document_id=document_id)


def _operation_id(operation_id: str):
    if not isinstance(operation_id, str) or not re.fullmatch(
        r"[A-Za-z0-9_.:-]{1,64}", operation_id
    ):
        raise _error("notification_operation_id_invalid")


def _row(connection, table, *where):
    row = connection.execute(sa.select(table).where(*where)).mappings().one_or_none()
    return dict(row) if row is not None else None


def _channel(connection: Connection, source_id: str, at: int | None = None) -> dict:
    state = channel_on(connection)
    if state is None:
        raise _error("notification_not_enabled")
    if state["source_id"] != source_id:
        raise _error("notification_source_mismatch")
    if at is not None and at < state["activation_at"]:
        raise _error("notification_time_invalid")
    return state


def _context(state: dict) -> dict:
    # A send pause is not a rule/routing change and does not invalidate a replay.
    return {key: state[key] for key in _CONTEXT_KEYS}


def _policy(connection: Connection, state: dict) -> dict:
    row = _row(
        connection,
        notification_policy_revisions,
        notification_policy_revisions.c.id == state["policy_revision_id"],
    )
    if row is None:
        raise _error("notification_policy_missing")
    try:
        profile = Profile.model_validate(row["manifest"]["profile"])
    except (ValidationError, KeyError, TypeError) as exc:
        raise _error("notification_policy_invalid") from exc
    if canonical_sha256(row["manifest"]) != row["policy_sha256"]:
        raise _error("notification_policy_invalid")
    if canonical_sha256(policy_manifest(profile)) != row["policy_sha256"]:
        raise _error("notification_policy_outdated")
    return row


def _operation(connection: Connection, source_id: str, operation_id: str, kind: str):
    row = _row(connection, _OPERATIONS, _OPERATIONS.c.id == operation_id)
    if row is not None:
        if row["source_id"] != source_id or row["kind"] != kind:
            raise _error("notification_operation_parameters_mismatch")
        _validate_operation(row)
        if kind == "reevaluate":
            items = {item["event_id"]: item for item in row["snapshot"]["payload"]["items"]}
            for result in row["results"]:
                if result["status"] != "applied":
                    continue
                decision = _row(
                    connection,
                    notification_decisions,
                    notification_decisions.c.id == result["decision_id"],
                    notification_decisions.c.event_id == result["event_id"],
                )
                if (
                    decision is None
                    or decision["evaluation_key"] != "operation:" + row["id"]
                    or decision["policy_revision_id"] != row["parameters"]["policy_revision_id"]
                    or decision["decision"] != items[result["event_id"]]["decision"]
                    or decision["decision"]["effective_route"] != result["effective_route"]
                ):
                    raise _error("notification_operation_invalid")
                if result["effective_route"] == "immediate":
                    outbox = _row(
                        connection,
                        email_outbox,
                        email_outbox.c.id == result["outbox_id"],
                        email_outbox.c.event_id == result["event_id"],
                        email_outbox.c.decision_id == result["decision_id"],
                    )
                    if outbox is None:
                        raise _error("notification_operation_invalid")
    return row


def _positive(value) -> bool:
    return type(value) is int and 1 <= value <= 2**63 - 1


def _digest(value) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def _validate_operation(row: dict) -> None:
    """Validate durable JSON shape/bindings before any replay or result reporting."""
    try:
        notification_time(row["created_at"])
    except IngestError as exc:
        raise _error("notification_operation_invalid") from exc
    if row["finished_at"] is not None and (
        type(row["finished_at"]) is not int or row["finished_at"] < row["created_at"]
    ):
        raise _error("notification_operation_invalid")
    parameters, snapshot, results = row["parameters"], row["snapshot"], row["results"]
    if (
        not isinstance(parameters, dict)
        or canonical_sha256(parameters) != row["parameters_sha256"]
        or not isinstance(snapshot, dict)
        or set(snapshot) != {"payload", "sha256"}
        or not isinstance(snapshot["payload"], dict)
        or canonical_sha256(snapshot["payload"]) != snapshot["sha256"]
        or not isinstance(results, list)
    ):
        raise _error("notification_operation_invalid")
    payload = snapshot["payload"]
    if (
        parameters.get("source_id") != row["source_id"]
        or type(parameters.get("evaluated_at")) is not int
        or parameters["evaluated_at"] != row["created_at"]
        or not _digest(parameters.get("policy_sha256"))
        or parameters["policy_sha256"] != payload.get("policy_sha256")
        or not _positive(payload.get("policy_revision_id"))
    ):
        raise _error("notification_operation_invalid")
    if row["kind"] == "policy_update":
        if (
            set(parameters) != {"source_id", "policy_sha256", "evaluated_at"}
            or set(payload)
            != {"policy_revision_id", "policy_sha256", "previous_policy_revision_id"}
            or not _positive(payload["previous_policy_revision_id"])
            or results != [dict(status="applied", policy_revision_id=payload["policy_revision_id"])]
            or row["finished_at"] is None
        ):
            raise _error("notification_operation_invalid")
        return
    context = parameters.get("routing_context")
    ids = parameters.get("event_ids")
    items = payload.get("items")
    if (
        set(parameters)
        != {
            "source_id",
            "event_ids",
            "evaluated_at",
            "policy_revision_id",
            "policy_sha256",
            "routing_context",
        }
        or parameters["policy_revision_id"] != payload["policy_revision_id"]
        or not _positive(parameters["policy_revision_id"])
        or not isinstance(context, dict)
        or set(context) != set(_CONTEXT_KEYS)
        or context["installation_id"] != row["installation_id"]
        or context["source_id"] != row["source_id"]
        or context["policy_revision_id"] != payload["policy_revision_id"]
        or not isinstance(ids, list)
        or not 1 <= len(ids) <= 100
        or any(not _positive(value) for value in ids)
        or ids != sorted(set(ids))
        or set(payload) != {"policy_revision_id", "policy_sha256", "items"}
        or not isinstance(items, list)
        or len(items) != len(ids)
    ):
        raise _error("notification_operation_invalid")
    for event_id, item in zip(ids, items, strict=True):
        if (
            not isinstance(item, dict)
            or set(item) != {"event_id", "input_token", "decision", "facts", "context"}
            or item["event_id"] != event_id
            or not _positive(item["event_id"])
            or not _digest(item["input_token"])
        ):
            raise _error("notification_operation_invalid")
        try:
            decision = Decision.model_validate(item["decision"])
            facts = NoticeFacts.model_validate(item["facts"])
            event_context = EventContext.model_validate(item["context"])
        except ValidationError as exc:
            raise _error("notification_operation_invalid") from exc
        if (
            decision.policy_sha256 != parameters["policy_sha256"]
            or decision.content_sha256 != facts.content_sha256
            or decision.evaluated_at != datetime.fromtimestamp(row["created_at"], UTC)
            or event_context.notification_mode != context["notification_mode"]
            or event_context.next_digest_at <= decision.evaluated_at
        ):
            raise _error("notification_operation_invalid")
    for index, result in enumerate(results):
        if (
            index >= len(ids)
            or not isinstance(result, dict)
            or result.get("event_id") != ids[index]
            or not _positive(result.get("event_id"))
            or result.get("status") not in {"applied", "stale_input", "stale_context"}
            or (result["status"] == "applied" and not _positive(result.get("decision_id")))
        ):
            raise _error("notification_operation_invalid")
        if result["status"] == "applied":
            if (
                set(result) != {"event_id", "status", "decision_id", "effective_route", "outbox_id"}
                or result["effective_route"] not in {"none", "immediate", "digest"}
                or (result["effective_route"] == "immediate" and not _positive(result["outbox_id"]))
                or (result["effective_route"] != "immediate" and result["outbox_id"] is not None)
            ):
                raise _error("notification_operation_invalid")
        elif set(result) != {"event_id", "status"}:
            raise _error("notification_operation_invalid")
    if (row["finished_at"] is not None) != (len(results) == len(ids)):
        raise _error("notification_operation_invalid")


def _freeze(snapshot: dict) -> dict:
    return dict(payload=snapshot, sha256=canonical_sha256(snapshot))


def _summary(row: dict, *, reused: bool = False) -> dict:
    payload = row["snapshot"]["payload"]
    return dict(
        source_id=row["source_id"],
        operation_id=row["id"],
        kind=row["kind"],
        evaluated_at=row["created_at"],
        policy_revision_id=payload["policy_revision_id"],
        policy_sha256=payload["policy_sha256"],
        complete=row["finished_at"] is not None,
        completed_at=row["finished_at"],
        reused=reused,
        results=list(row["results"]),
    )


def preview_policy_update(engine: Engine, source_id: str, profile: Profile, *, at: int) -> dict:
    """Read-only comparison; it never updates events or creates delivery work."""
    notification_time(at)
    manifest = policy_manifest(profile)
    with engine.connect() as connection:
        state = _channel(connection, source_id, at)
        current = _row(
            connection,
            notification_policy_revisions,
            notification_policy_revisions.c.id == state["policy_revision_id"],
        )
        if current is None:
            raise _error("notification_policy_missing")
    digest = canonical_sha256(manifest)
    return dict(
        source_id=source_id,
        evaluated_at=at,
        current_policy_revision_id=current["id"],
        current_policy_sha256=current["policy_sha256"],
        policy_sha256=digest,
        changed=current["policy_sha256"] != digest,
        profile=profile.model_dump(mode="json"),
        creates_historical_mail=False,
    )


def update_policy(
    engine: Engine, source_id: str, profile: Profile, operation_id: str, *, at: int
) -> dict:
    """Publish an immutable manifest; an identical operation replay never reverts it."""
    _operation_id(operation_id)
    notification_time(at)
    manifest = policy_manifest(profile)
    digest = canonical_sha256(manifest)
    parameters = dict(source_id=source_id, policy_sha256=digest, evaluated_at=at)
    try:
        with engine.begin() as connection:
            existing = _operation(connection, source_id, operation_id, "policy_update")
            if existing is not None:
                if existing["parameters"] != parameters:
                    raise _error("notification_operation_parameters_mismatch")
                return _summary(existing, reused=True)
            state = _channel(connection, source_id, at)
            connection.execute(
                insert(notification_policy_revisions)
                .values(policy_sha256=digest, manifest=manifest, created_at=at)
                .on_conflict_do_nothing(
                    index_elements=[notification_policy_revisions.c.policy_sha256]
                )
            )
            policy = _row(
                connection,
                notification_policy_revisions,
                notification_policy_revisions.c.policy_sha256 == digest,
            )
            if policy is None or canonical_sha256(policy["manifest"]) != digest:
                raise _error("notification_policy_invalid")
            connection.execute(
                notification_channel_state.update()
                .where(notification_channel_state.c.id == state["id"])
                .values(policy_revision_id=policy["id"])
            )
            values = dict(
                id=operation_id,
                installation_id=state["installation_id"],
                source_id=source_id,
                kind="policy_update",
                parameters_sha256=canonical_sha256(parameters),
                parameters=parameters,
                snapshot=_freeze(
                    dict(
                        policy_revision_id=policy["id"],
                        policy_sha256=digest,
                        previous_policy_revision_id=state["policy_revision_id"],
                    )
                ),
                results=[dict(status="applied", policy_revision_id=policy["id"])],
                created_at=at,
                finished_at=at,
            )
            connection.execute(_OPERATIONS.insert().values(**values))
    except SQLAlchemyError as exc:
        raise _error("notification_database_write_failed") from exc
    return _summary(values)


def _ids(event_ids) -> tuple[int, ...]:
    if not isinstance(event_ids, (tuple, list)) or not 1 <= len(event_ids) <= 100:
        raise _error("notification_event_selection_invalid")
    if any(type(value) is not int or not 1 <= value <= 2**63 - 1 for value in event_ids):
        raise _error("notification_event_selection_invalid")
    return tuple(sorted(set(event_ids)))


def _jsonable(value):
    if hasattr(value, "isoformat"):
        return value.isoformat()
    if isinstance(value, dict):
        return {key: _jsonable(item) for key, item in value.items()}
    return value


def _input(connection: Connection, state: dict, event_id: int) -> dict:
    event = _row(connection, notification_events, notification_events.c.id == event_id)
    if event is None or event["installation_id"] != state["installation_id"]:
        raise _error("notification_event_missing")
    doc = _row(connection, documents, documents.c.id == event["document_id"])
    observation = _row(
        connection,
        notification_observations,
        notification_observations.c.installation_id == state["installation_id"],
        notification_observations.c.document_id == event["document_id"],
    )
    selected = _row(
        connection,
        notification_decisions,
        notification_decisions.c.id == event["selected_decision_id"],
        notification_decisions.c.event_id == event_id,
    )
    version = _row(connection, notice_versions, notice_versions.c.id == event["version_id"])
    previous_version = (
        _row(connection, notice_versions, notice_versions.c.id == event["previous_version_id"])
        if event["previous_version_id"] is not None
        else None
    )
    allocated = connection.execute(
        sa.select(mail_message_members.c.event_id).where(
            mail_message_members.c.event_id == event_id
        )
    ).scalar_one_or_none()
    return _jsonable(
        dict(
            event=event,
            document=doc,
            observation=observation,
            selected=selected,
            version=version,
            previous_version=previous_version,
            allocated=allocated,
        )
    )


def _validate_candidate(inputs: dict, source_id: str, *, allow_locked: bool = False):
    event, doc, observation = inputs["event"], inputs["document"], inputs["observation"]
    if (
        doc is None
        or doc["source_id"] != source_id
        or observation is None
        or inputs["selected"] is None
        or inputs["version"] is None
        or inputs["version"]["document_id"] != doc["id"]
        or inputs["version"]["id"] != doc["current_version_id"]
        or observation["version_id"] != event["version_id"]
        or observation["event_seq"] != event["event_seq"]
    ):
        raise _error("notification_event_not_current", document_id=event["document_id"])
    if not allow_locked and (
        event["effective_route"] != "none"
        or event["delivery_intent_registered_at"] is not None
        or event["outbox_id"] is not None
        or inputs["allocated"] is not None
    ):
        raise _error("notification_event_route_locked", document_id=event["document_id"])


def _content(version) -> NoticeContent:
    try:
        content = NoticeContent.model_validate(version["normalized_content"])
    except ValidationError as exc:
        raise _error("notification_version_invalid") from exc
    if content.content_sha256() != version["content_sha256"]:
        raise _error("notification_version_digest")
    return content


def _validate_frozen_decision(connection: Connection, operation: dict, item: dict, inputs: dict):
    """Check evidence hashes without rerunning extraction or current-version rules."""
    policy = _row(
        connection,
        notification_policy_revisions,
        notification_policy_revisions.c.id == operation["parameters"]["policy_revision_id"],
    )
    if (
        policy is None
        or policy["policy_sha256"] != operation["parameters"]["policy_sha256"]
        or canonical_sha256(policy["manifest"]) != policy["policy_sha256"]
    ):
        raise _error("notification_operation_invalid")
    try:
        profile = Profile.model_validate(policy["manifest"]["profile"])
        versions = policy["manifest"]["versions"]
        decision = Decision.model_validate(item["decision"])
        facts = NoticeFacts.model_validate(item["facts"])
        context = EventContext.model_validate(item["context"])
        content = _content(inputs["version"])
        facts = facts.model_copy(update=dict(body_text=content.body_text))
        if context.previous_facts is not None:
            if inputs["previous_version"] is None:
                raise _error("notification_operation_invalid")
            previous = _content(inputs["previous_version"])
            if context.previous_facts.content_sha256 != previous.content_sha256():
                raise _error("notification_operation_invalid")
            context = context.model_copy(
                update=dict(
                    previous_facts=context.previous_facts.model_copy(
                        update=dict(body_text=previous.body_text)
                    )
                )
            )
        elif inputs["previous_version"] is not None:
            raise _error("notification_operation_invalid")
        if (
            decision.content_sha256 != content.content_sha256()
            or facts.title != content.title
            or facts.published_date != content.published_date
            or decision.profile_sha256 != profile.sha256()
            or decision.facts_sha256 != facts.sha256()
            or context.kind != inputs["event"]["kind"]
            or decision.facts_extractor_version != versions["facts_extractor"]
            or decision.rules_version != versions["rules"]
            or decision.decision_engine_version != versions["decision_engine"]
            or decision.routing_version != versions["routing"]
            or decision.input_sha256
            != canonical_sha256(
                dict(
                    profile=profile.model_dump(mode="json"),
                    facts=facts.model_dump(mode="json"),
                    context=context.model_dump(mode="json"),
                    evaluated_at=decision.evaluated_at.isoformat(),
                    policy_sha256=policy["policy_sha256"],
                )
            )
        ):
            raise _error("notification_operation_invalid")
    except (KeyError, TypeError, ValidationError) as exc:
        raise _error("notification_operation_invalid") from exc


@dataclass(frozen=True)
class PreparedReevaluation:
    operation_id: str
    source_id: str
    installation_id: str
    parameters_json: str
    snapshot_json: str
    evaluated_at: int


def prepare_reevaluation(
    engine: Engine,
    source_id: str,
    operation_id: str,
    event_ids: tuple[int, ...],
    *,
    at: int,
    preview: bool = False,
) -> PreparedReevaluation:
    """Freeze exact bounded inputs and decisions, with no write transaction or file I/O."""
    _operation_id(operation_id)
    selected_ids = _ids(event_ids)
    notification_time(at)
    with engine.connect() as connection:
        state = _channel(connection, source_id, at)
        policy = _policy(connection, state)
        inputs = [_input(connection, state, event_id) for event_id in selected_ids]
    for item in inputs:
        _validate_candidate(item, source_id, allow_locked=preview)
    profile = Profile.model_validate(policy["manifest"]["profile"])
    now = datetime.fromtimestamp(at, UTC)
    today = now.astimezone(SHANGHAI).date()
    frozen_items = []
    for item in inputs:
        facts = extract_facts(_content(item["version"]))
        effective_time = deadline_for_profile(facts, profile)
        recent = today - timedelta(days=6) <= facts.published_date <= today
        opened = (
            facts.category == "opportunity"
            and effective_time.deadline_at is not None
            and effective_time.deadline_at >= now
            and (facts.opening_confirmed or facts.opens_at is not None)
            and (facts.opens_at is None or facts.opens_at <= now)
        )
        if not preview and not recent and not opened:
            raise _error(
                "notification_event_not_candidate", document_id=item["event"]["document_id"]
            )
        if at < max(item["event"]["occurred_at"], item["observation"]["observed_at"]):
            raise _error("notification_time_invalid")
        try:
            context = EventContext.model_validate(item["selected"]["context"])
        except ValidationError as exc:
            raise _error("notification_context_invalid") from exc
        previous = (
            extract_facts(_content(item["previous_version"]))
            if item["previous_version"] is not None
            else None
        )
        context = context.model_copy(
            update=dict(
                notification_mode=state["notification_mode"],
                previous_facts=previous,
                next_digest_at=next_digest_at(state, at),
            )
        )
        decision = decide(profile, facts, context, now=now)
        context_json = context.model_dump(mode="json", exclude={"previous_facts"})
        context_json["previous_facts"] = (
            previous.model_dump(mode="json", exclude={"body_text"})
            if previous is not None
            else None
        )
        frozen_items.append(
            dict(
                event_id=item["event"]["id"],
                input_token=canonical_sha256(item),
                decision=decision.model_dump(mode="json"),
                facts=facts.model_dump(mode="json", exclude={"body_text"}),
                context=context_json,
            )
        )
    parameters = dict(
        source_id=source_id,
        event_ids=list(selected_ids),
        evaluated_at=at,
        policy_revision_id=policy["id"],
        policy_sha256=policy["policy_sha256"],
        routing_context=_context(state),
    )
    return PreparedReevaluation(
        operation_id=operation_id,
        source_id=source_id,
        installation_id=state["installation_id"],
        parameters_json=canonical_json(parameters),
        snapshot_json=canonical_json(
            _freeze(
                dict(
                    policy_revision_id=policy["id"],
                    policy_sha256=policy["policy_sha256"],
                    items=frozen_items,
                )
            )
        ),
        evaluated_at=at,
    )


def register_reevaluation_in_transaction(
    connection: Connection, prepared: PreparedReevaluation
) -> dict:
    """Store a prepared operation after token checks; caller owns the transaction."""
    try:
        parameters = json.loads(prepared.parameters_json)
        snapshot = json.loads(prepared.snapshot_json)
    except (json.JSONDecodeError, TypeError) as exc:
        raise _error("notification_operation_invalid") from exc
    values = dict(
        id=prepared.operation_id,
        installation_id=prepared.installation_id,
        source_id=prepared.source_id,
        kind="reevaluate",
        parameters_sha256=canonical_sha256(parameters),
        parameters=parameters,
        snapshot=snapshot,
        results=[],
        created_at=prepared.evaluated_at,
        finished_at=None,
    )
    _validate_operation(values)
    existing = _operation(connection, prepared.source_id, prepared.operation_id, "reevaluate")
    if existing is not None:
        if existing["parameters"] != parameters:
            raise _error("notification_operation_parameters_mismatch")
        return existing
    state = _channel(connection, prepared.source_id, prepared.evaluated_at)
    if _context(state) != parameters["routing_context"]:
        raise _error("notification_reevaluation_stale_context")
    for item in snapshot["payload"]["items"]:
        current = _input(connection, state, item["event_id"])
        if canonical_sha256(current) != item["input_token"]:
            raise _error("notification_reevaluation_stale_input")
        _validate_candidate(current, prepared.source_id)
        _validate_frozen_decision(connection, values, item, current)
    connection.execute(_OPERATIONS.insert().values(**values))
    return values


def _apply_member(connection: Connection, operation: dict, item: dict) -> dict:
    state = _channel(connection, operation["source_id"])
    if _context(state) != operation["parameters"]["routing_context"]:
        return dict(event_id=item["event_id"], status="stale_context")
    try:
        inputs = _input(connection, state, item["event_id"])
    except IngestError as exc:
        if exc.code == "notification_event_missing":
            return dict(event_id=item["event_id"], status="stale_input")
        raise
    if canonical_sha256(inputs) != item["input_token"]:
        return dict(event_id=item["event_id"], status="stale_input")
    _validate_candidate(inputs, operation["source_id"])
    _validate_frozen_decision(connection, operation, item, inputs)
    try:
        decision = Decision.model_validate(item["decision"])
    except ValidationError as exc:
        raise _error("notification_operation_invalid") from exc
    if decision.policy_sha256 != operation["parameters"]["policy_sha256"]:
        raise _error("notification_operation_invalid")
    at = operation["created_at"]
    event, doc = inputs["event"], inputs["document"]
    decision_id = connection.execute(
        notification_decisions.insert().values(
            event_id=event["id"],
            policy_revision_id=operation["parameters"]["policy_revision_id"],
            # N1 reserves "initial"; namespace caller IDs instead of silently
            # colliding when a caller chooses that otherwise valid operation ID.
            evaluation_key="operation:" + operation["id"],
            evaluated_at=at,
            decision=item["decision"],
            facts=item["facts"],
            context=item["context"],
        )
    ).inserted_primary_key[0]
    outbox_id = None
    if decision.effective_route == "immediate":
        delivery_key = canonical_sha256(
            dict(
                installation_id=state["installation_id"],
                source_id=state["source_id"],
                source_document_id=doc["source_document_id"],
                event_seq=event["event_seq"],
                recipient_key="primary",
            )
        )
        outbox_id = connection.execute(
            email_outbox.insert().values(
                event_id=event["id"],
                decision_id=decision_id,
                delivery_key=delivery_key,
                sender=state["sender"],
                recipient=state["recipient"],
                created_at=at,
            )
        ).inserted_primary_key[0]
    connection.execute(
        notification_events.update()
        .where(notification_events.c.id == event["id"])
        .values(
            selected_decision_id=decision_id,
            effective_route=decision.effective_route,
            delivery_intent_registered_at=at if decision.effective_route != "none" else None,
            outbox_id=outbox_id,
        )
    )
    return dict(
        event_id=event["id"],
        status="applied",
        decision_id=decision_id,
        effective_route=decision.effective_route,
        outbox_id=outbox_id,
    )


def _resume(
    engine: Engine,
    source_id: str,
    operation_id: str,
    *,
    reused: bool,
    clock: Callable[[], int],
) -> dict:
    while True:
        try:
            with engine.begin() as connection:
                operation = _operation(connection, source_id, operation_id, "reevaluate")
                if operation is None:
                    raise _error("notification_operation_missing")
                if operation["finished_at"] is not None:
                    return _summary(operation, reused=reused)
                completed = {result["event_id"] for result in operation["results"]}
                pending = [
                    item
                    for item in operation["snapshot"]["payload"]["items"]
                    if item["event_id"] not in completed
                ]
                if not pending:
                    raise _error("notification_operation_invalid")
                result = _apply_member(connection, operation, pending[0])
                updates = dict(results=[*operation["results"], result])
                if len(pending) == 1:
                    finished_at = clock()
                    notification_time(finished_at)
                    if finished_at < operation["created_at"]:
                        raise _error("notification_completion_clock_invalid")
                    # Diagnostic completion time is separate from the fixed
                    # decision/routing clock and never changes those inputs.
                    updates["finished_at"] = finished_at
                connection.execute(
                    _OPERATIONS.update().where(_OPERATIONS.c.id == operation_id).values(**updates)
                )
                operation.update(updates)
                if len(pending) == 1:
                    return _summary(operation, reused=reused)
        except SQLAlchemyError as exc:
            raise _error("notification_database_write_failed") from exc


def reevaluate_events(
    engine: Engine,
    source_id: str,
    operation_id: str,
    *,
    event_ids: tuple[int, ...] | None = None,
    at: int | None = None,
    preview: bool = False,
    clock: Callable[[], int] | None = None,
) -> dict:
    """Explicit bounded create/apply, or ID-only resume of fixed inputs/results.

    A route already registered for immediate or digest mail is locked. Resume
    returns completed results before checking current event eligibility.
    """
    _operation_id(operation_id)
    if type(preview) is not bool or (event_ids is None) != (at is None):
        raise _error("notification_operation_parameters_invalid")
    if preview and event_ids is None:
        raise _error("notification_operation_parameters_invalid")
    if event_ids is not None:
        selected_ids = _ids(event_ids)
        notification_time(at)
    else:
        selected_ids = None
    with engine.connect() as connection:
        existing = _operation(connection, source_id, operation_id, "reevaluate")
        if existing is not None and selected_ids is not None:
            state = _channel(connection, source_id, at)
            policy = _policy(connection, state)
            parameters = dict(
                source_id=source_id,
                event_ids=list(selected_ids),
                evaluated_at=at,
                policy_revision_id=policy["id"],
                policy_sha256=policy["policy_sha256"],
                routing_context=_context(state),
            )
            if existing["parameters"] != parameters:
                raise _error("notification_operation_parameters_mismatch")
    if existing is not None:
        if preview:
            return _summary(existing, reused=True) | dict(preview=True)
        return _resume(
            engine,
            source_id,
            operation_id,
            reused=True,
            clock=clock if clock is not None else lambda: int(time.time()),
        )
    if selected_ids is None:
        raise _error("notification_operation_missing")
    prepared = prepare_reevaluation(
        engine, source_id, operation_id, selected_ids, at=at, preview=preview
    )
    if preview:
        payload = json.loads(prepared.snapshot_json)["payload"]
        return dict(
            preview=True,
            source_id=source_id,
            operation_id=operation_id,
            kind="reevaluate",
            evaluated_at=at,
            policy_revision_id=payload["policy_revision_id"],
            policy_sha256=payload["policy_sha256"],
            results=[
                dict(event_id=item["event_id"], decision=item["decision"], context=item["context"])
                for item in payload["items"]
            ],
        )
    try:
        with engine.begin() as connection:
            register_reevaluation_in_transaction(connection, prepared)
    except SQLAlchemyError as exc:
        raise _error("notification_database_write_failed") from exc
    return _resume(
        engine,
        source_id,
        operation_id,
        reused=False,
        clock=clock if clock is not None else lambda: int(time.time()),
    )
