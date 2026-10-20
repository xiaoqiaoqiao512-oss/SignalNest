"""Replay fixed HTML, archived content or explicit synthetic inputs through rules.

This offline diagnostic does not open a database, read the personal Profile, or
send mail. The engineering expectations are regression checks, not human gold.
"""

import argparse
import hashlib
import json
import platform
import subprocess
import sys
from collections import Counter
from datetime import UTC, datetime
from importlib.metadata import version
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CASES = ROOT / "docs/validation/notification-production-cases.json"
SOURCE_FILES = (
    "deploy/evaluate_notifications.py",
    "src/signalnest/contracts.py",
    "src/signalnest/parsing.py",
    "src/signalnest/ems_parsing.py",
    "src/signalnest/cs_parsing.py",
    "src/signalnest/notifications/contracts.py",
    "src/signalnest/notifications/facts.py",
    "src/signalnest/notifications/decision.py",
    "src/signalnest/notifications/profile.py",
)


class EvaluationError(ValueError):
    """A finite input or reproducibility error without raw page/exception text."""


def source_hashes():
    return {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in SOURCE_FILES}


def _fixture(path):
    candidate = Path(path)
    if candidate.is_absolute() or ".." in candidate.parts:
        raise EvaluationError("evaluation_fixture_path_invalid")
    resolved = (ROOT / candidate).resolve()
    if not resolved.is_relative_to(ROOT / "research/fixtures"):
        raise EvaluationError("evaluation_fixture_path_invalid")
    return resolved


def _archived_content(case, notice_contract):
    """Read a digest-bound existing parser product; do not claim a new parse/fetch."""
    candidate = Path(case["archive"])
    if candidate.is_absolute() or ".." in candidate.parts:
        raise EvaluationError("evaluation_archive_path_invalid")
    resolved = (ROOT / candidate).resolve()
    if not resolved.is_relative_to(ROOT / "tests/fixtures") or resolved.suffix != ".json":
        raise EvaluationError("evaluation_archive_path_invalid")
    payload = resolved.read_bytes()
    if hashlib.sha256(payload).hexdigest() != case["archive_sha256"]:
        raise EvaluationError("evaluation_archive_digest_mismatch")
    try:
        archive = json.loads(payload)
        if archive["format"] != "signalnest-notification-archive-v1":
            raise EvaluationError("evaluation_archive_invalid")
        selected = [row for row in archive["cases"] if row["case_id"] == case["archive_case_id"]]
        if len(selected) != 1 or "notice_content" not in selected[0]:
            raise EvaluationError("evaluation_archive_case_invalid")
        row = selected[0]
        content = notice_contract.model_validate(row["notice_content"])
        digest = content.content_sha256()
        if digest != row["content_sha256"] or digest != case["content_sha256"]:
            raise EvaluationError("evaluation_content_digest_mismatch")
        provenance = {
            name: row[name]
            for name in (
                "source_id",
                "source_document_id",
                "source_url",
                "response_requested_url",
                "response_final_url",
                "raw_body_sha256",
                "parser_version",
                "fetched_at",
                "parsed_at",
                "provenance",
            )
        }
        if any(
            not isinstance(value, str) or not value
            for name, value in provenance.items()
            if name not in {"fetched_at", "parsed_at"}
        ) or any(
            type(provenance[name]) is not int or provenance[name] < 0
            for name in ("fetched_at", "parsed_at")
        ):
            raise EvaluationError("evaluation_archive_invalid")
    except (KeyError, TypeError, json.JSONDecodeError) as exc:
        raise EvaluationError("evaluation_archive_invalid") from exc
    return content, provenance


def _expectation_mismatches(decision, expectation, facts):
    mismatches = []
    for field in ("action", "effective_route", "eligibility", "time_status", "needs_review"):
        if field in expectation and decision[field] != expectation[field]:
            mismatches.append(field)
    rules = set(decision["matched_rules"])
    if any(rule not in rules for rule in expectation.get("matched_rules_present", ())):
        mismatches.append("matched_rules_present")
    if any(rule in rules for rule in expectation.get("matched_rules_absent", ())):
        mismatches.append("matched_rules_absent")
    expected_facts = expectation.get("facts", {})
    if "deadline_at" in expected_facts and facts["deadline_at"] != expected_facts["deadline_at"]:
        mismatches.append("facts.deadline_at")
    unknown_proofs = [proof["excerpt"] for item in facts["unknowns"] for proof in item["evidence"]]
    if any(
        not any(phrase in proof for proof in unknown_proofs)
        for phrase in expected_facts.get("unknown_evidence_contains", ())
    ):
        mismatches.append("facts.unknown_evidence_contains")
    if any(
        constraint["field"] in expected_facts.get("no_hard_constraint_fields", ())
        and constraint["operator"] != "unsupported"
        for constraint in facts["constraints"]
    ):
        mismatches.append("facts.no_hard_constraint_fields")
    return mismatches


def evaluate_cases(case_file=DEFAULT_CASES):
    """Return auditable production output for fixed Profiles and explicit clocks."""
    before = source_hashes()
    # Capture before import and after replay so a concurrent edit cannot silently
    # produce a report bearing source hashes from another revision.
    from pydantic import ValidationError

    from signalnest.contracts import NoticeContent, PageInput
    from signalnest.cs_parsing import PARSER_VERSION as CS_PARSER_VERSION
    from signalnest.cs_parsing import parse_cs_notice
    from signalnest.ems_parsing import PARSER_VERSION as EMS_PARSER_VERSION
    from signalnest.ems_parsing import parse_ems_notice
    from signalnest.notifications.contracts import (
        DECISION_ENGINE_VERSION,
        FACTS_EXTRACTOR_VERSION,
        ROUTING_VERSION,
        RULES_VERSION,
        EventContext,
        Profile,
        aware_time,
    )
    from signalnest.notifications.decision import decide
    from signalnest.notifications.facts import extract_facts
    from signalnest.parsing import PARSER_VERSION, ParseError, parse_notice

    if source_hashes() != before:
        raise EvaluationError("evaluation_source_changed")
    manifest_bytes = Path(case_file).read_bytes()
    try:
        manifest = json.loads(manifest_bytes)
        if manifest["schema_version"] != 1 or not manifest["cases"]:
            raise EvaluationError("evaluation_manifest_invalid")
        profiles = {
            name: Profile.model_validate(values) for name, values in manifest["profiles"].items()
        }
    except (KeyError, TypeError, json.JSONDecodeError, ValidationError) as exc:
        raise EvaluationError("evaluation_manifest_invalid") from exc
    rows = []
    identifiers = set()
    for case in manifest["cases"]:
        try:
            identifier = case["case_id"]
            if not isinstance(identifier, str) or not identifier or identifier in identifiers:
                raise EvaluationError("evaluation_case_invalid")
            identifiers.add(identifier)
            input_kind = case.get("input_kind", "fixture_html")
            if input_kind == "fixture_html":
                if "synthetic_notice_content" in case:
                    raise EvaluationError("evaluation_case_invalid")
                body = _fixture(case["fixture"]).read_bytes()
                if hashlib.sha256(body).hexdigest() != case["sha256"]:
                    raise EvaluationError("evaluation_fixture_digest_mismatch")
                page = PageInput(content=body, page_url=case["source_url"])
                selected_parser = case.get("parser", "whu-student-notices")
                if selected_parser not in {
                    "whu-student-notices",
                    "ems-notices",
                    "cs-undergrad-notices",
                }:
                    raise EvaluationError("evaluation_parser_invalid")
                content = None
            elif input_kind == "synthetic_notice_content":
                if any(
                    name in case
                    for name in ("fixture", "sha256", "source_url", "research_case", "parser")
                ):
                    raise EvaluationError("evaluation_case_invalid")
                content = NoticeContent.model_validate(case["synthetic_notice_content"])
                if content.content_sha256() != case["content_sha256"]:
                    raise EvaluationError("evaluation_content_digest_mismatch")
                page = None
            elif input_kind == "archived_notice_content":
                if any(
                    name in case
                    for name in (
                        "fixture",
                        "sha256",
                        "source_url",
                        "research_case",
                        "parser",
                        "synthetic_notice_content",
                    )
                ):
                    raise EvaluationError("evaluation_case_invalid")
                content, archive_provenance = _archived_content(case, NoticeContent)
                page = None
            else:
                raise EvaluationError("evaluation_input_kind_invalid")
            profile = profiles[case["profile"]]
            try:
                now = aware_time(datetime.fromisoformat(case["evaluated_at"]))
            except ValueError as exc:
                raise EvaluationError("evaluation_clock_invalid") from exc
            context = EventContext.model_validate(case["context"])
            if context.next_digest_at <= now:
                raise EvaluationError("evaluation_clock_invalid")
            expectation = case["engineering_expectation"]
            description = case["description"]
        except (KeyError, TypeError, ValidationError) as exc:
            raise EvaluationError("evaluation_case_invalid") from exc
        row = {
            "case_id": identifier,
            "research_case": case.get("research_case"),
            "description": description,
            "input_kind": input_kind,
            "profile": profile.model_dump(mode="json"),
            "context": context.model_dump(mode="json"),
            "evaluated_at": case["evaluated_at"],
            "engineering_expectation": expectation,
        }
        if page is not None:
            row.update(
                fixture=case["fixture"],
                fixture_sha256=case["sha256"],
                source_url=case["source_url"],
                parser=selected_parser,
            )
        elif input_kind == "synthetic_notice_content":
            row.update(
                synthetic_notice_content=content.model_dump(mode="json"),
                content_sha256=content.content_sha256(),
                parser_applied=False,
            )
        else:
            row.update(
                archive=case["archive"],
                archive_sha256=case["archive_sha256"],
                archive_case_id=case["archive_case_id"],
                archived_notice_content=content.model_dump(mode="json"),
                content_sha256=content.content_sha256(),
                source_id=archive_provenance["source_id"],
                source_document_id=archive_provenance["source_document_id"],
                source_url=archive_provenance["source_url"],
                archived_provenance=archive_provenance,
                parser_applied=False,
            )
        if page is not None:
            try:
                if selected_parser == "cs-undergrad-notices":
                    notice = parse_cs_notice(page)
                elif selected_parser == "ems-notices":
                    notice = parse_ems_notice(page)
                else:
                    notice = parse_notice(page)
            except ParseError as exc:
                row.update(
                    parse_error={"code": exc.code.value, "field": exc.field},
                    engineering_mismatches=["parse_success"],
                )
            else:
                content = notice.content
                row.update(
                    source_document_id=notice.source_document_id,
                    parser_version=notice.parser_version,
                    parser_applied=True,
                )
        if content is not None:
            facts = extract_facts(content)
            decision = decide(profile, facts, context, now=now).model_dump(mode="json")
            public_facts = facts.model_dump(mode="json", exclude={"body_text"})
            row.update(
                # Offsets/excerpts remain available without copying the full body.
                facts=public_facts,
                decision=decision,
                engineering_mismatches=_expectation_mismatches(decision, expectation, public_facts),
            )
        rows.append(row)
    if source_hashes() != before:
        raise EvaluationError("evaluation_source_changed")
    decisions = [row["decision"] for row in rows if "decision" in row]
    return {
        "schema_version": 1,
        "kind": "offline_production_notification_replay",
        "note": manifest["note"],
        "adaptations": manifest["adaptations"],
        "recorded_at": datetime.now(UTC).isoformat(),
        "git_head": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
        ).strip(),
        "source_sha256": before,
        "manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
        "versions": {
            "parser": PARSER_VERSION,
            "ems_parser": EMS_PARSER_VERSION,
            "cs_parser": CS_PARSER_VERSION,
            "facts_extractor": FACTS_EXTRACTOR_VERSION,
            "rules": RULES_VERSION,
            "decision_engine": DECISION_ENGINE_VERSION,
            "routing": ROUTING_VERSION,
        },
        "environment": {
            "python": sys.version.split()[0],
            "platform": platform.platform(),
            "dependencies": {
                name: version(name) for name in ("httpx", "beautifulsoup4", "pydantic")
            },
        },
        "summary": {
            "cases": len(rows),
            "fixture_cases": sum(row["input_kind"] == "fixture_html" for row in rows),
            "synthetic_cases": sum(row["input_kind"] == "synthetic_notice_content" for row in rows),
            "archived_cases": sum(row["input_kind"] == "archived_notice_content" for row in rows),
            "unique_pages": len({row["fixture_sha256"] for row in rows if "fixture_sha256" in row}),
            "parse_success": sum(row.get("parser_applied", False) for row in rows),
            "decision_success": len(decisions),
            "actions": dict(sorted(Counter(d["action"] for d in decisions).items())),
            "routes": dict(sorted(Counter(d["effective_route"] for d in decisions).items())),
            "needs_review": sum(d["needs_review"] for d in decisions),
            "engineering_mismatch_cases": [
                row["case_id"] for row in rows if row["engineering_mismatches"]
            ],
            "human_gold": False,
        },
        "results": rows,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASES)
    parser.add_argument("--output", type=Path, help="create a new JSON snapshot; never overwrite")
    parser.add_argument("--check", action="store_true", help="exit 1 for regression mismatches")
    args = parser.parse_args(argv)
    try:
        result = evaluate_cases(args.cases)
        payload = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
        if args.output is None:
            print(payload, end="")
        else:
            with args.output.open("x", encoding="utf-8") as stream:
                stream.write(payload)
    except EvaluationError as exc:
        print(f"notification evaluation failed: {exc}", file=sys.stderr)
        return 2
    except OSError:
        print("notification evaluation failed: evaluation_file_unavailable", file=sys.stderr)
        return 2
    return int(args.check and bool(result["summary"]["engineering_mismatch_cases"]))


if __name__ == "__main__":
    raise SystemExit(main())
