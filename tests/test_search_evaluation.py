"""Real public evidence and production SQLite search; no claimed human gold."""

import hashlib
import json
import runpy
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "deploy/evaluate_search.py"
MANIFEST = ROOT / "docs/validation/search-cases.json"
SNAPSHOT = ROOT / "docs/validation/search-current.json"
EVALUATION = runpy.run_path(str(SCRIPT))
evaluate_cases = EVALUATION["evaluate_cases"]
EvaluationError = EVALUATION["EvaluationError"]


@pytest.fixture(scope="module")
def report():
    return evaluate_cases()


def _modified_manifest(tmp_path, mutate):
    manifest = json.loads(MANIFEST.read_text())
    mutate(manifest)
    path = tmp_path / "cases.json"
    path.write_text(json.dumps(manifest))
    return path


def test_real_corpus_uses_original_archive_and_production_index(report):
    assert report["summary"]["corpus_count"] == report["summary"]["indexed_count"] == 9
    assert report["summary"]["query_count"] == 15
    assert report["storage_revision"] == "0009_opportunity_review"
    assert report["summary"]["engineering_mismatch_queries"] == []
    assert report["summary"]["rebuild_results_identical"] is True
    assert report["summary"]["production_matches_fixed_alias_candidate"] is True
    for row in report["corpus"]:
        assert hashlib.sha256((ROOT / row["fixture"]).read_bytes()).hexdigest() == row["sha256"]
        assert "body_text" not in row
    assert report["human_gold"] is False
    assert all(
        query["label_origin"] == "engineering_relevance_judgment_unconfirmed"
        for query in report["queries"]
    )


def test_fixed_alias_comparison_keeps_failures_and_precision_cost_visible(report):
    methods = report["methods"]
    literal = methods["sqlite_fts5_trigram_literal"]
    production = methods["production"]
    assert literal["query_results"]["S03"] == literal["query_results"]["S05"] == []
    assert production["query_results"]["S03"] == ["ems-250571"]
    assert production["query_results"]["S05"] == ["18135"]
    assert production["query_results"]["S15"] == ["18135", "128291", "14147"]
    assert literal["metrics"]["macro_recall_at_k"]["5"] == pytest.approx(10 / 13)
    assert production["metrics"]["macro_recall_at_k"] == {
        "1": 0.852564,
        "3": 0.980769,
        "5": 1.0,
    }
    assert production["metrics"]["macro_precision_among_returned_at_k"]["5"] < 1
    assert methods["sqlite_fts5_unicode61"]["metrics"]["macro_recall_at_k"]["5"] < 0.1
    assert methods["sqlite_literal_and"]["metrics"] == literal["metrics"]


def test_short_queries_filters_and_actual_publication_dates_are_preserved(report):
    results = report["methods"]["production"]["query_results"]
    assert results["S10"] == ["128291"]
    assert results["S11"] == results["S12"] == []
    assert results["S14"] == ["ems-250571"]
    assert report["methods"]["production"]["metrics"]["empty_query_correct"] == 2
    ems = next(row for row in report["corpus"] if row["corpus_id"] == "ems-250571")
    assert ems["published_date"] == "2024-09-09"
    assert ems["fetched_at"] > 1_790_000_000
    assert ems["source_id"] == "whu-ems-notices-offline"
    assert "手动" in ems["provenance"]
    pages = report["production_pages"]
    assert pages["S03"]["query"] == "助教招聘"
    assert pages["S03"]["query_version"] == "literal-alias-v1"
    assert "助教选聘" in pages["S03"]["expanded_terms"][0]
    assert all(hit["original_url"].startswith("https://") for hit in pages["S01"]["items"])


def test_replay_and_checked_snapshot_match_current_sources(report):
    fresh = evaluate_cases()
    snapshot = json.loads(SNAPSHOT.read_text())
    for key in (
        "source_sha256",
        "manifest_sha256",
        "corpus",
        "queries",
        "methods",
        "production_pages",
        "summary",
    ):
        assert fresh[key] == report[key] == snapshot[key], key
    assert report["source_sha256"] == EVALUATION["source_hashes"]()


@pytest.mark.parametrize("path", ["../outside.html", "/tmp/outside.html", "README.md"])
def test_paths_cannot_escape_existing_public_fixtures(tmp_path, path):
    cases = _modified_manifest(
        tmp_path, lambda manifest: manifest["corpus"][0].update(fixture=path)
    )
    with pytest.raises(EvaluationError, match="search_fixture_path_invalid"):
        evaluate_cases(cases)


def test_digest_mismatch_does_not_rewrite_evidence(tmp_path):
    cases = _modified_manifest(
        tmp_path, lambda manifest: manifest["corpus"][0].update(sha256="0" * 64)
    )
    with pytest.raises(EvaluationError, match="search_fixture_digest_mismatch"):
        evaluate_cases(cases)


@pytest.mark.parametrize(
    "change",
    [
        lambda manifest: manifest.update(human_gold=True),
        lambda manifest: manifest["corpus"].append(manifest["corpus"][0]),
        lambda manifest: manifest["queries"].append(manifest["queries"][0]),
        lambda manifest: manifest["queries"][0].update(relevant_corpus_ids=["imagined-page"]),
        lambda manifest: manifest["queries"][0].update(
            date_from="2026-10-08", date_to="2026-10-07"
        ),
        lambda manifest: manifest["queries"][0].update(label_origin="human_confirmed"),
    ],
)
def test_manifest_cannot_invent_gold_or_unknown_targets(tmp_path, change):
    cases = _modified_manifest(tmp_path, change)
    with pytest.raises(EvaluationError, match="search_manifest_invalid"):
        evaluate_cases(cases)


def test_mid_replay_source_change_is_explicit(monkeypatch):
    original = evaluate_cases.__globals__["source_hashes"]
    calls = 0

    def changed():
        nonlocal calls
        calls += 1
        return original() if calls < 3 else {"changed": "concurrent-edit"}

    monkeypatch.setitem(evaluate_cases.__globals__, "source_hashes", changed)
    with pytest.raises(EvaluationError, match="search_source_changed"):
        evaluate_cases()


def test_cli_is_cwd_independent_and_never_overwrites_snapshot(tmp_path):
    output = tmp_path / "report.json"
    command = [sys.executable, str(SCRIPT), "--check", "--output", str(output)]
    child = subprocess.run(command, cwd=tmp_path, capture_output=True, text=True)
    assert child.returncode == 0, child.stderr
    saved = output.read_bytes()
    assert not child.stdout
    assert json.loads(saved)["summary"]["engineering_mismatch_queries"] == []
    repeated = subprocess.run(command, cwd=tmp_path, capture_output=True, text=True)
    assert repeated.returncode == 2
    assert "search_evaluation_file_unavailable" in repeated.stderr
    assert output.read_bytes() == saved
    assert sorted(path.name for path in tmp_path.iterdir()) == ["report.json"]


def test_cli_does_not_hide_engineering_miss_by_relabelling_query(tmp_path):
    cases = _modified_manifest(
        tmp_path, lambda manifest: manifest["queries"][0].update(query="不存在的项目词组")
    )
    child = subprocess.run(
        [sys.executable, str(SCRIPT), "--cases", str(cases), "--check"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )
    assert child.returncode == 1, child.stderr
    result = json.loads(child.stdout)
    assert result["summary"]["engineering_mismatch_queries"] == ["S01"]
    assert result["queries"][0]["relevant_corpus_ids"] == ["128291", "14147", "17361"]


def test_help_does_not_open_instance_or_read_cases(tmp_path):
    child = subprocess.run(
        [sys.executable, str(SCRIPT), "--cases", "missing.json", "--help"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )
    assert child.returncode == 0
    assert "--check" in child.stdout
    assert list(tmp_path.iterdir()) == []
