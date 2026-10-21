"""Actual SQLite/Parser/index readiness, with model and credential access forbidden."""

import hashlib
import json
import os
import runpy
import sqlite3
import subprocess
import sys
from pathlib import Path

import httpx
import pytest
import sqlalchemy as sa
from alembic import command
from test_query_agent import campus as campus
from test_query_snapshots import snapshots as snapshots

from signalnest.instance_lock import writer_lock
from signalnest.model_client import OpenAIChatClient
from signalnest.schema import search_documents
from signalnest.storage import make_engine, migration_config

ROOT = Path(__file__).resolve().parents[1]
MODULE = runpy.run_path(str(ROOT / "deploy/query_readiness.py"))
inspect_query_readiness = MODULE["inspect_query_readiness"]
main = MODULE["main"]


@pytest.fixture(autouse=True)
def forbid_network_and_credentials(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("readiness must not access a network or credential")

    monkeypatch.setattr(httpx.Client, "send", forbidden)
    monkeypatch.setattr(httpx.Client, "stream", forbidden)
    monkeypatch.setattr(OpenAIChatClient, "__init__", forbidden)
    original = type(os.environ).__getitem__

    def environment_item(environment, key):
        if key in {"SIGNALNEST_READINESS_TEST_KEY", "OPENAI_API_KEY", "SIGNALNEST_PROXY_API_KEY"}:
            forbidden()
        return original(environment, key)

    monkeypatch.setattr(type(os.environ), "__getitem__", environment_item)


def file_state(directory):
    return {
        str(path.relative_to(directory)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in directory.rglob("*")
        if path.is_file()
    }


def test_real_two_source_readiness_under_writer_locks_is_readonly(campus):
    with (
        writer_lock(campus.instances["uc"].storage.database),
        writer_lock(campus.instances["cs"].storage.database),
    ):
        before = file_state(campus.query_file.parent)
        result = inspect_query_readiness(campus.query_file)
        assert file_state(campus.query_file.parent) == before
    assert result["local_ready"] and result["ready"]
    assert result["model_configured"] is False
    assert result["diagnostic_codes"] == ["model_not_configured"]
    assert {item["instance_id"] for item in result["instances"]} == {"uc", "cs"}
    assert all(item["schema_ready"] and item["index_ready"] for item in result["instances"])
    assert all(item["storage_readable"] for item in result["instances"])
    assert not any(
        result[key] for key in ("credentials_checked", "network_checked", "model_runtime_verified")
    )


def test_model_required_is_distinct_from_local_readiness(campus, capsys):
    assert main(["--config", str(campus.query_file), "--require-model"]) == 1
    result = json.loads(capsys.readouterr().out)
    assert result["local_ready"] is True and result["ready"] is False
    assert result["diagnostic_codes"] == ["model_not_configured"]


def test_configured_model_does_not_claim_credentials_or_runtime_verified(campus, capsys):
    with campus.query_file.open("a") as stream:
        stream.write(
            '\n[model]\nprovider="openai-compatible-chat-completions"\n'
            'base_url="http://127.0.0.1:8001/v1"\nmodel="operator-selected-model"\n'
            'api_key_env="SIGNALNEST_READINESS_TEST_KEY"\npricing_mode="unpriced_proxy"\n'
        )
    before = file_state(campus.query_file.parent)
    assert main(["--config", str(campus.query_file), "--require-model"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["model_configured"] and result["local_ready"] and result["ready"]
    assert not result["model_runtime_verified"] and not result["credentials_checked"]
    assert not result["network_checked"]
    assert file_state(campus.query_file.parent) == before


def test_missing_database_does_not_create_files(campus):
    settings = campus.instances["uc"]
    settings.storage.database.unlink()
    before = file_state(campus.query_file.parent)
    result = inspect_query_readiness(campus.query_file)
    assert not result["local_ready"]
    assert result["instances"][0]["error_code"] == "query_storage_unavailable"
    assert result["instances"][1]["ready"]
    assert file_state(campus.query_file.parent) == before


def test_real_schema_eight_is_explicitly_blocked_without_migration(campus):
    engine = make_engine(campus.instances["uc"].storage.database, must_exist=True)
    try:
        with engine.begin() as connection:
            config = migration_config()
            config.attributes["connection"] = connection
            command.downgrade(config, "0008_list_references")
    finally:
        engine.dispose()
    before = file_state(campus.query_file.parent)
    result = inspect_query_readiness(campus.query_file)
    assert not result["local_ready"]
    assert result["instances"][0]["error_code"] == "query_schema_upgrade_required"
    assert result["instances"][0]["schema_ready"] is False
    assert file_state(campus.query_file.parent) == before


def test_stale_index_is_not_repaired(campus):
    engine = make_engine(campus.instances["uc"].storage.database, must_exist=True)
    try:
        with engine.begin() as connection:
            connection.execute(sa.update(search_documents).values(index_version="old-index"))
    finally:
        engine.dispose()
    before = file_state(campus.query_file.parent)
    result = inspect_query_readiness(campus.query_file)
    assert not result["local_ready"]
    item = result["instances"][0]
    assert item["schema_ready"] and not item["index_ready"]
    assert item["error_code"] == "search_index_stale"
    assert file_state(campus.query_file.parent) == before


def test_missing_index_is_not_rebuilt(campus):
    with sqlite3.connect(campus.instances["uc"].storage.database) as connection:
        connection.execute("DROP TABLE search_fts")
    before = file_state(campus.query_file.parent)
    result = inspect_query_readiness(campus.query_file)
    assert result["instances"][0]["error_code"] == "search_index_missing"
    assert not result["local_ready"]
    assert file_state(campus.query_file.parent) == before


@pytest.mark.parametrize(
    ("contents", "error"),
    [(b"", "query_storage_uninitialized"), (b"not sqlite", "query_storage_unavailable")],
)
def test_uninitialized_and_corrupted_storage_are_finite(campus, contents, error):
    campus.instances["uc"].storage.database.write_bytes(contents)
    before = file_state(campus.query_file.parent)
    result = inspect_query_readiness(campus.query_file)
    assert result["instances"][0]["error_code"] == error
    assert not result["local_ready"]
    assert file_state(campus.query_file.parent) == before


def test_valid_snapshot_manifest_is_checked(snapshots):
    directory, *_ = snapshots
    before = file_state(directory)
    result = inspect_query_readiness(directory / "ask.local.toml")
    assert result["local_ready"]
    assert all(item["snapshot_verified"] for item in result["instances"])
    assert file_state(directory) == before


def test_damaged_snapshot_manifest_fails_without_exposing_input(snapshots):
    directory, *_ = snapshots
    (directory / "uc/snapshot.json").write_text('{"private":"do-not-echo"}')
    before = file_state(directory)
    result = inspect_query_readiness(directory / "ask.local.toml")
    assert not result["local_ready"]
    assert result["instances"][0]["error_code"] == "snapshot_manifest_invalid"
    assert "do-not-echo" not in json.dumps(result)
    assert file_state(directory) == before


@pytest.mark.parametrize("kind", ["absent", "invalid-toml", "unknown-secret-field"])
def test_configuration_errors_are_finite_and_side_effect_free(tmp_path, capsys, kind):
    config = tmp_path / "query.toml"
    if kind == "invalid-toml":
        config.write_text('secret="do-not-echo')
    elif kind == "unknown-secret-field":
        config.write_text('api_key="do-not-echo"\n')
    before = file_state(tmp_path)
    assert main(["--config", str(config)]) == 2
    captured = capsys.readouterr()
    result = json.loads(captured.out)
    assert result["diagnostic_codes"] == ["query_configuration_invalid"]
    assert "do-not-echo" not in captured.out + captured.err
    assert file_state(tmp_path) == before


def test_help_never_loads_inputs_or_writes_files(tmp_path, capsys, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("help must not inspect inputs")

    monkeypatch.setitem(main.__globals__, "inspect_query_readiness", forbidden)
    before = file_state(tmp_path)
    with pytest.raises(SystemExit) as exc:
        main(["--help"])
    assert exc.value.code == 0 and "--require-model" in capsys.readouterr().out
    assert file_state(tmp_path) == before


def test_actual_cli_missing_configuration_exits_clearly_without_files(tmp_path):
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "deploy/query_readiness.py"),
            "--config",
            str(tmp_path / "missing.toml"),
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    assert result.returncode == 2
    assert json.loads(result.stdout)["diagnostic_codes"] == ["query_configuration_invalid"]
    assert result.stderr == ""
    assert not list(tmp_path.iterdir())
