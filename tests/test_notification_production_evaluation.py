"""The evaluation replays immutable evidence through the actual production code."""

import hashlib
import json
import runpy
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "deploy/evaluate_notifications.py"
EVALUATION = runpy.run_path(str(SCRIPT))
evaluate_cases = EVALUATION["evaluate_cases"]
EvaluationError = EVALUATION["EvaluationError"]
MANIFEST = ROOT / "docs/validation/notification-production-cases.json"
SYNTHETIC_MANIFEST = ROOT / "docs/validation/notification-production-synthetic-cases.json"
TEACHING_ASSISTANT_MANIFEST = ROOT / "docs/validation/teaching-assistant-cases.json"
CURRENT_INSTRUCTION_MANIFEST = ROOT / "docs/validation/notification-current-instruction-cases.json"
ORIGINAL_MISSES_MANIFEST = ROOT / "docs/validation/notification-original-misses-cases.json"


def test_production_replay_is_deterministic_and_keeps_real_fixture_evidence():
    before = {
        path: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in (ROOT / "research/fixtures").rglob("*")
        if path.is_file()
    }
    first, second = evaluate_cases(), evaluate_cases()
    assert first["results"] == second["results"]
    assert first["summary"]["cases"] == first["summary"]["parse_success"] == 13
    assert first["summary"]["unique_pages"] == 8
    assert first["summary"]["human_gold"] is False
    assert first["summary"]["engineering_mismatch_cases"] == []
    assert first["manifest_sha256"] == hashlib.sha256(MANIFEST.read_bytes()).hexdigest()
    rows = {row["case_id"]: row for row in first["results"]}
    assert rows["P02"]["decision"]["action"] == "STORE_ONLY"
    assert rows["P12"]["decision"]["action"] == "IGNORE"
    assert rows["P13"]["decision"]["action"] == "PUSH_NOW"
    assert rows["P04"]["decision"]["action"] == "PUSH_NOW"
    assert rows["P11"]["decision"]["action"] == "DIGEST"
    for row in first["results"]:
        assert "body_text" not in row["facts"]
        assert row["decision"]["content_sha256"] == row["facts"]["content_sha256"]
        assert row["fixture_sha256"] == before[ROOT / row["fixture"]]
    assert before == {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in before}


@pytest.mark.parametrize("path", ["../outside.html", "/tmp/outside.html", "README.md"])
def test_fixture_paths_are_confined_to_existing_research_evidence(tmp_path, path):
    manifest = json.loads(MANIFEST.read_text())
    manifest["cases"][0]["fixture"] = path
    cases = tmp_path / "cases.json"
    cases.write_text(json.dumps(manifest))
    with pytest.raises(EvaluationError, match="evaluation_fixture_path_invalid"):
        evaluate_cases(cases)


def test_fixture_hash_mismatch_is_rejected_without_changing_original(tmp_path):
    manifest = json.loads(MANIFEST.read_text())
    manifest["cases"][0]["sha256"] = "0" * 64
    cases = tmp_path / "cases.json"
    cases.write_text(json.dumps(manifest))
    with pytest.raises(EvaluationError, match="evaluation_fixture_digest_mismatch"):
        evaluate_cases(cases)


@pytest.mark.parametrize("clock", ["2026-09-28T09:00:00", "tomorrow", "2026-10-01T09:00:00+08:00"])
def test_clocks_must_be_explicit_and_precede_the_next_digest(tmp_path, clock):
    manifest = json.loads(MANIFEST.read_text())
    manifest["cases"][0]["evaluated_at"] = clock
    cases = tmp_path / "cases.json"
    cases.write_text(json.dumps(manifest))
    with pytest.raises(EvaluationError, match="evaluation_clock_invalid"):
        evaluate_cases(cases)


def test_source_change_during_replay_is_rejected(monkeypatch):
    original = evaluate_cases.__globals__["source_hashes"]
    calls = 0

    def changed():
        nonlocal calls
        calls += 1
        return original() if calls < 3 else {"changed": "concurrent-edit"}

    monkeypatch.setitem(evaluate_cases.__globals__, "source_hashes", changed)
    with pytest.raises(EvaluationError, match="evaluation_source_changed"):
        evaluate_cases()


def test_cli_runs_from_other_directory_and_refuses_to_replace_snapshots(tmp_path):
    output = tmp_path / "report.json"
    command = [sys.executable, str(SCRIPT), "--check", "--output", str(output)]
    child = subprocess.run(command, cwd=tmp_path, capture_output=True, text=True)
    assert child.returncode == 0, child.stderr
    saved = output.read_bytes()
    assert not child.stdout
    assert json.loads(saved)["summary"]["engineering_mismatch_cases"] == []
    again = subprocess.run(command, cwd=tmp_path, capture_output=True, text=True)
    assert again.returncode == 2
    assert "evaluation_file_unavailable" in again.stderr
    assert output.read_bytes() == saved
    assert sorted(path.name for path in tmp_path.iterdir()) == ["report.json"]


def test_cli_check_reports_regression_mismatch_without_relabeling_result(tmp_path):
    manifest = json.loads(MANIFEST.read_text())
    manifest["cases"][0]["engineering_expectation"]["action"] = "PUSH_NOW"
    cases = tmp_path / "cases.json"
    cases.write_text(json.dumps(manifest))
    child = subprocess.run(
        [sys.executable, str(SCRIPT), "--cases", str(cases), "--check"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )
    assert child.returncode == 1
    result = json.loads(child.stdout)
    assert result["summary"]["engineering_mismatch_cases"] == ["P01"]
    assert result["results"][0]["decision"]["action"] == "STORE_ONLY"


def test_cli_help_is_independent_of_fixture_or_storage_access(tmp_path):
    child = subprocess.run(
        [sys.executable, str(SCRIPT), "--cases", "missing.json", "--help"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )
    assert child.returncode == 0
    assert "--check" in child.stdout
    assert list(tmp_path.iterdir()) == []


def test_explicit_synthetic_cases_use_production_rules_without_claiming_parser_success(monkeypatch):
    import signalnest.parsing

    def parser_must_not_run(page):
        pytest.fail("synthetic NoticeContent must not be described as parsed HTML")

    monkeypatch.setattr(signalnest.parsing, "parse_notice", parser_must_not_run)
    result = evaluate_cases(SYNTHETIC_MANIFEST)
    assert result["summary"]["cases"] == result["summary"]["decision_success"] == 2
    assert result["summary"]["synthetic_cases"] == 2
    assert result["summary"]["fixture_cases"] == result["summary"]["parse_success"] == 0
    assert result["summary"]["unique_pages"] == 0
    assert result["summary"]["human_gold"] is False
    assert result["summary"]["engineering_mismatch_cases"] == []
    for row in result["results"]:
        assert row["input_kind"] == "synthetic_notice_content"
        assert row["parser_applied"] is False
        assert "fixture_sha256" not in row
        assert "source_document_id" not in row
        assert "parser_version" not in row
        assert row["decision"]["action"] == "PUSH_NOW"
        assert "decision.deadline_soon" in row["decision"]["matched_rules"]


@pytest.mark.parametrize(
    ("change", "error"),
    [
        ({"content_sha256": "0" * 64}, "evaluation_content_digest_mismatch"),
        ({"fixture": "research/fixtures/current-notice-detail.html"}, "evaluation_case_invalid"),
        ({"input_kind": "guess"}, "evaluation_input_kind_invalid"),
        ({"synthetic_notice_content": {"title": ""}}, "evaluation_case_invalid"),
    ],
)
def test_synthetic_input_must_be_explicit_valid_and_bound_to_its_digest(tmp_path, change, error):
    manifest = json.loads(SYNTHETIC_MANIFEST.read_text())
    manifest["cases"][0].update(change)
    cases = tmp_path / "cases.json"
    cases.write_text(json.dumps(manifest))
    with pytest.raises(EvaluationError, match=error):
        evaluate_cases(cases)


def test_mapping_preserves_confirmed_research_actions_without_relabelling_adapted_inputs():
    review_path = ROOT / "research/experiments/notifications/human-review-20261008.json"
    review = json.loads(review_path.read_text())
    mapping = json.loads(
        (ROOT / "docs/validation/notification-production-mapping.json").read_text()
    )
    assert mapping["research_review_id"] == review["review_id"]
    assert mapping["research_review_sha256"] == hashlib.sha256(review_path.read_bytes()).hexdigest()
    confirmed = {row["case_id"]: row["expected_action"] for row in review["labels"]}
    assert len(confirmed) == 10
    rows = {row["production_case"]: row for row in mapping["cases"]}
    assert len(rows) == 15
    for row in rows.values():
        if row["research_case"] in confirmed:
            assert row["original_human_action"] == confirmed[row["research_case"]]
        assert row["relationship"] != "identical"
    for identifier in ("P05", "P07"):
        assert rows[identifier]["original_human_action"] == "STORE_ONLY"
        assert rows[identifier]["engineering_action"] == "IGNORE"
        assert rows[identifier]["review_status"] == "action_semantics_pending_reconfirmation"
    assert rows["P10"]["original_human_action"] is None
    for identifier in ("P12", "P13", "Q01", "Q02"):
        assert rows[identifier]["review_status"] == "new_case_not_human_labelled"


def test_v3_history_and_original_thirteen_inputs_remain_frozen():
    snapshot_path = ROOT / "docs/validation/notification-production-v3.json"
    snapshot_bytes = snapshot_path.read_bytes()
    assert hashlib.sha256(snapshot_bytes).hexdigest() == (
        "ee2ba7218fd7e082ea98daa5c22ededd783056ba1f6ca7c4cd9b14403d87feab"
    )
    snapshot = json.loads(snapshot_bytes)
    assert snapshot["versions"]["rules"] == "notification-rules-v3"
    assert snapshot["summary"]["cases"] == 13
    assert snapshot["manifest_sha256"] == hashlib.sha256(MANIFEST.read_bytes()).hexdigest()


def test_teaching_assistant_replay_selects_real_source_parsers_explicitly(monkeypatch):
    import signalnest.ems_parsing
    import signalnest.parsing

    ems_parser = signalnest.ems_parsing.parse_ems_notice
    whu_parser = signalnest.parsing.parse_notice
    calls = []

    def ems(page):
        calls.append("ems-notices")
        return ems_parser(page)

    def whu(page):
        calls.append("whu-student-notices")
        return whu_parser(page)

    monkeypatch.setattr(signalnest.ems_parsing, "parse_ems_notice", ems)
    monkeypatch.setattr(signalnest.parsing, "parse_notice", whu)
    result = evaluate_cases(TEACHING_ASSISTANT_MANIFEST)
    assert calls == ["ems-notices"] * 4 + ["whu-student-notices"] * 2
    assert result["summary"]["cases"] == result["summary"]["parse_success"] == 6
    assert result["summary"]["unique_pages"] == 2
    assert result["summary"]["human_gold"] is False
    assert result["summary"]["engineering_mismatch_cases"] == []
    assert (
        result["source_sha256"]["src/signalnest/ems_parsing.py"]
        == hashlib.sha256((ROOT / "src/signalnest/ems_parsing.py").read_bytes()).hexdigest()
    )
    rows = {row["case_id"]: row for row in result["results"]}
    assert rows["N1E01"]["decision"]["action"] == "DIGEST"
    assert rows["N1E04"]["decision"]["action"] == "IGNORE"
    assert rows["N1E04"]["decision"]["time_status"] == "closed"
    assert rows["N1E05"]["decision"]["action"] == "IGNORE"
    assert rows["N1E06"]["decision"]["action"] == "PUSH_NOW"
    for identifier in ("N1E02", "N1E03"):
        row = rows[identifier]
        assert row["parser"] == "ems-notices"
        assert row["parser_version"] == "ems-notices-v1"
        assert row["source_document_id"] == "1588:250571"
        assert row["facts"]["deadline_at"] == "2024-09-20T16:00:00+08:00"
        assert row["decision"]["action"] == "PUSH_NOW"
        assert row["decision"]["eligibility"] == "unknown"
        assert row["decision"]["needs_review"] is True
        assert not any(
            item["field"] == "study_level" and item["operator"] != "unsupported"
            for item in row["facts"]["constraints"]
        )


@pytest.mark.parametrize("parser", ["guess", "news-notices", None])
def test_unknown_fixture_parser_is_not_guessed(tmp_path, parser):
    manifest = json.loads(TEACHING_ASSISTANT_MANIFEST.read_text())
    manifest["cases"][0]["parser"] = parser
    cases = tmp_path / "cases.json"
    cases.write_text(json.dumps(manifest))
    with pytest.raises(EvaluationError, match="evaluation_parser_invalid"):
        evaluate_cases(cases)


def test_wrong_explicit_parser_fails_without_automatic_fallback(tmp_path):
    manifest = json.loads(TEACHING_ASSISTANT_MANIFEST.read_text())
    manifest["cases"][0]["parser"] = "whu-student-notices"
    cases = tmp_path / "cases.json"
    cases.write_text(json.dumps(manifest))
    result = evaluate_cases(cases)
    row = result["results"][0]
    assert row["parse_error"] == {"code": "unsupported_identity", "field": "identity"}
    assert row["engineering_mismatches"] == ["parse_success"]
    assert "decision" not in row


@pytest.mark.parametrize(
    ("expected", "mismatch"),
    [
        ({"deadline_at": "2024-09-27T23:59:59+08:00"}, "facts.deadline_at"),
        ({"unknown_evidence_contains": ["不存在的岗位数依据"]}, "facts.unknown_evidence_contains"),
    ],
)
def test_recruitment_fact_expectations_are_checked_in_report(tmp_path, expected, mismatch):
    manifest = json.loads(TEACHING_ASSISTANT_MANIFEST.read_text())
    manifest["cases"][0]["engineering_expectation"]["facts"].update(expected)
    cases = tmp_path / "cases.json"
    cases.write_text(json.dumps(manifest))
    result = evaluate_cases(cases)
    assert result["results"][0]["engineering_mismatches"] == [mismatch]
    assert result["summary"]["engineering_mismatch_cases"] == ["N1E01"]


@pytest.mark.parametrize(
    ("manifest", "snapshot"),
    [
        (MANIFEST, "notification-production-current.json"),
        (SYNTHETIC_MANIFEST, "notification-production-synthetic-current.json"),
        (TEACHING_ASSISTANT_MANIFEST, "teaching-assistant-current.json"),
        (CURRENT_INSTRUCTION_MANIFEST, "notification-current-instruction-current.json"),
        (ORIGINAL_MISSES_MANIFEST, "notification-original-misses-current.json"),
    ],
)
def test_current_snapshots_describe_actual_production_code_and_fixed_inputs(manifest, snapshot):
    current = evaluate_cases(manifest)
    saved = json.loads((ROOT / "docs/validation" / snapshot).read_text())
    for field in ("source_sha256", "manifest_sha256", "versions", "summary"):
        assert saved[field] == current[field]
    # Public snapshots are compact: full page content and row-level evidence
    # remain in the local research area, not version control.
    assert "results" not in saved


def test_current_instruction_regressions_keep_actual_v5_failure_and_explicit_engineering_inputs():
    previous = json.loads(
        (ROOT / "docs/validation/notification-current-instruction-v5.json").read_text()
    )
    current = evaluate_cases(CURRENT_INSTRUCTION_MANIFEST)
    assert previous["versions"]["rules"] == "notification-rules-v5"
    historic_bytes = (
        ROOT / "docs/validation/notification-current-instruction-cases-before-v8.json"
    ).read_bytes()
    assert previous["manifest_sha256"] == hashlib.sha256(historic_bytes).hexdigest()
    historic_input = json.loads(historic_bytes)
    current_input = json.loads(CURRENT_INSTRUCTION_MANIFEST.read_text())
    # Only the obsolete provenance note changes; actual regression inputs and
    # expectations remain exact, and the old manifest stays byte-for-byte auditable.
    for field in ("schema_version", "profiles", "cases"):
        assert historic_input[field] == current_input[field]
    assert previous["summary"]["engineering_mismatch_cases"] == ["R01", "R02"]
    assert current["summary"]["engineering_mismatch_cases"] == []
    assert current["summary"]["human_gold"] is False
    assert current["summary"]["synthetic_cases"] == 2
    assert current["summary"]["parse_success"] == 0
    for old, new in zip(previous["results"], current["results"], strict=True):
        for field in ("synthetic_notice_content", "profile", "context", "evaluated_at"):
            assert old[field] == new[field]
        assert old["decision"]["content_sha256"] == new["decision"]["content_sha256"]
        assert old["decision"]["action"] == "DIGEST"
        assert new["decision"]["action"] == "PUSH_NOW"
        assert old["facts"]["opens_at"] is new["facts"]["opens_at"] is None
        assert not old["facts"]["opening_confirmed"] and new["facts"]["opening_confirmed"]


def test_original_misses_keep_actual_v7_failures_and_the_same_complete_inputs():
    previous = json.loads(
        (ROOT / "docs/validation/notification-original-misses-v7.json").read_text()
    )
    current = evaluate_cases(ORIGINAL_MISSES_MANIFEST)
    assert previous["versions"]["rules"] == "notification-rules-v7"
    assert current["versions"]["rules"] == "notification-rules-v10"
    assert previous["manifest_sha256"] == current["manifest_sha256"]
    assert previous["summary"]["engineering_mismatch_cases"] == ["O01", "O02"]
    assert current["summary"]["engineering_mismatch_cases"] == []
    assert current["summary"]["human_gold"] is False
    assert current["summary"]["synthetic_cases"] == 2
    assert current["summary"]["parse_success"] == 0
    assert [row["decision"]["action"] for row in previous["results"]] == ["DIGEST", "IGNORE"]
    for old, new in zip(previous["results"], current["results"], strict=True):
        for field in (
            "synthetic_notice_content",
            "profile",
            "context",
            "evaluated_at",
            "content_sha256",
            "engineering_expectation",
        ):
            assert old[field] == new[field]
        assert new["decision"]["action"] == "PUSH_NOW"
        assert new["decision"]["effective_route"] == "immediate"
        assert new["decision"]["reason_codes"] == ["deadline_soon"]
        assert old["decision"]["content_sha256"] == new["decision"]["content_sha256"]



def _write_synthetic_archive(tmp_path):
    from signalnest.contracts import NoticeContent

    content = NoticeContent(
        title="科研训练报名通知",
        published_date="2026-10-19",
        body_html="<p>本科生可报名科研训练。</p>",
        body_text="本科生可报名科研训练。",
    )
    row = {
        "case_id": "SYNTHETIC-1",
        "notice_content": content.model_dump(mode="json"),
        "content_sha256": content.content_sha256(),
        "source_id": "synthetic-source",
        "source_document_id": "synthetic:notice-1",
        "source_url": "https://example.invalid/notices/1",
        "response_requested_url": "https://example.invalid/notices/1",
        "response_final_url": "https://example.invalid/notices/1",
        "raw_body_sha256": "a" * 64,
        "parser_version": "synthetic-parser-v1",
        "fetched_at": 1,
        "parsed_at": 2,
        "provenance": "explicit synthetic test data",
    }
    archive = {"format": "signalnest-notification-archive-v1", "cases": [row]}
    path = tmp_path / "tests" / "fixtures" / "archive.json"
    path.parent.mkdir(parents=True)
    payload = (json.dumps(archive, ensure_ascii=False) + "\n").encode()
    path.write_bytes(payload)
    case = {
        "archive": "tests/fixtures/archive.json",
        "archive_sha256": hashlib.sha256(payload).hexdigest(),
        "archive_case_id": "SYNTHETIC-1",
        "content_sha256": content.content_sha256(),
    }
    return content, archive, path, case


def test_synthetic_archived_content_requires_file_and_content_digests(tmp_path, monkeypatch):
    from signalnest.contracts import NoticeContent

    reader = EVALUATION["_archived_content"]
    monkeypatch.setitem(reader.__globals__, "ROOT", tmp_path)
    original, _, _, case = _write_synthetic_archive(tmp_path)
    loaded, provenance = reader(case, NoticeContent)
    assert loaded == original
    assert loaded.content_sha256() == case["content_sha256"]
    assert provenance["source_id"] == "synthetic-source"
    assert provenance["provenance"] == "explicit synthetic test data"


@pytest.mark.parametrize(
    "path",
    ["../archive.json", "/tmp/archive.json", "README.md", "research/fixtures/archive.json"],
)
def test_archived_content_paths_are_confined_to_json_test_fixtures(tmp_path, monkeypatch, path):
    from signalnest.contracts import NoticeContent

    reader = EVALUATION["_archived_content"]
    monkeypatch.setitem(reader.__globals__, "ROOT", tmp_path)
    _, _, _, case = _write_synthetic_archive(tmp_path)
    case["archive"] = path
    with pytest.raises(EvaluationError, match="evaluation_archive_path_invalid"):
        reader(case, NoticeContent)


def test_archived_content_rejects_digest_mismatch_and_ambiguous_case(tmp_path, monkeypatch):
    from signalnest.contracts import NoticeContent

    reader = EVALUATION["_archived_content"]
    monkeypatch.setitem(reader.__globals__, "ROOT", tmp_path)
    _, archive, path, case = _write_synthetic_archive(tmp_path)
    case["archive_sha256"] = "0" * 64
    with pytest.raises(EvaluationError, match="evaluation_archive_digest_mismatch"):
        reader(case, NoticeContent)

    archive["cases"].append(archive["cases"][0])
    payload = (json.dumps(archive, ensure_ascii=False) + "\n").encode()
    path.write_bytes(payload)
    case["archive_sha256"] = hashlib.sha256(payload).hexdigest()
    with pytest.raises(EvaluationError, match="evaluation_archive_case_invalid"):
        reader(case, NoticeContent)


def test_archived_content_symlink_cannot_escape_fixture_directory(tmp_path, monkeypatch):
    from signalnest.contracts import NoticeContent

    reader = EVALUATION["_archived_content"]
    monkeypatch.setitem(reader.__globals__, "ROOT", tmp_path)
    _, _, _, case = _write_synthetic_archive(tmp_path)
    outside = tmp_path / "outside.json"
    outside.write_bytes((tmp_path / case["archive"]).read_bytes())
    archive_path = tmp_path / case["archive"]
    archive_path.unlink()
    archive_path.symlink_to(outside)
    with pytest.raises(EvaluationError, match="evaluation_archive_path_invalid"):
        reader(case, NoticeContent)
