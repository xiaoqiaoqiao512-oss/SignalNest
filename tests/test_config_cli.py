import os
import subprocess
import sys
from pathlib import Path

import pytest

from signalnest.config import ConfigurationError, load_config

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = (ROOT / "config.example.toml").read_text()


def run_cli(cwd, *args):
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
    return subprocess.run(
        [sys.executable, "-m", "signalnest", *args],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def test_paths_and_validation_have_no_storage_side_effects(tmp_path, monkeypatch):
    config = tmp_path / "settings.toml"
    config.write_text(EXAMPLE)
    monkeypatch.chdir(tmp_path.parent)
    settings = load_config(config)
    assert settings.storage.data_dir == tmp_path / "data"
    assert settings.storage.database == tmp_path / "data/signalnest.sqlite3"
    result = run_cli(tmp_path.parent, "config-check", "--config", str(config))
    assert result.returncode == 0
    assert str(tmp_path / "data/signalnest.sqlite3") in result.stdout
    assert list(tmp_path.iterdir()) == [config]


@pytest.mark.parametrize(
    ("old", "new", "field"),
    [
        ("https://uc.whu.edu.cn/tzgg/xstz.htm", "ftp://example.org/a", "source.list_url"),
        (
            "https://uc.whu.edu.cn/tzgg/xstz.htm",
            "https://user:secret@example.org",
            "source.list_url",
        ),
        ("10.0", "0.0", "http.connect_timeout_seconds"),
        ("30.0", "-1.0", "http.read_timeout_seconds"),
        ("2.0", "inf", "http.request_interval_seconds"),
        ("2.0", "0.0", "http.request_interval_seconds"),
        ("2.0", "true", "http.request_interval_seconds"),
        ("data_dir", "data_dri", "storage.data_dir"),
        ('data_dir = "data"', 'data_dir = ""', "storage.data_dir"),
    ],
)
def test_invalid_config(tmp_path, old, new, field):
    config = tmp_path / "bad.toml"
    config.write_text(EXAMPLE.replace(old, new))
    with pytest.raises(ConfigurationError, match=field):
        load_config(config)
    result = run_cli(tmp_path, "config-check", "--config", str(config))
    assert result.returncode == 2
    assert field in result.stderr
    assert "secret" not in result.stderr
    assert not (tmp_path / "data").exists()


@pytest.mark.parametrize("content", ["[broken", "\xff"])
def test_bad_toml(tmp_path, content):
    config = tmp_path / "bad.toml"
    config.write_bytes(content.encode("latin1"))
    result = run_cli(tmp_path, "config-check", "--config", str(config))
    assert result.returncode == 2
    assert "invalid TOML" in result.stderr


def test_help_missing_file_and_unknown_command(tmp_path):
    assert run_cli(tmp_path, "--help").returncode == 0
    assert run_cli(tmp_path, "collect").returncode == 2
    result = run_cli(tmp_path, "config-check", "--config", "missing.toml")
    assert result.returncode == 2
    assert "cannot read" in result.stderr
    assert not list(tmp_path.iterdir())


def test_import_and_help_do_not_access_network_or_sqlite(tmp_path):
    script = """
import socket
import sqlite3
import logging
root_handlers = logging.getLogger().handlers[:]
app_handlers = logging.getLogger("signalnest").handlers[:]

def forbidden(*args, **kwargs):
    raise AssertionError("unexpected network or database operation")
socket.socket.connect = forbidden
socket.create_connection = forbidden
sqlite3.connect = forbidden
sqlite3.dbapi2.connect = forbidden
import signalnest
import signalnest.config
import signalnest.cli
import signalnest.__main__
import signalnest.schema
import signalnest.storage
import signalnest.contracts
import signalnest.fetching
import signalnest.parsing
import signalnest.eventlog
import signalnest.rawstore
import signalnest.ingestion
assert logging.getLogger().handlers == root_handlers
assert logging.getLogger("signalnest").handlers == app_handlers
signalnest.cli.main(["--help"])
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert not list(tmp_path.iterdir())


def test_absolute_paths(tmp_path):
    config = tmp_path / "settings.toml"
    config.write_text(EXAMPLE.replace('"data"', f'"{tmp_path}/elsewhere"'))
    assert load_config(config).storage.data_dir == tmp_path / "elsewhere"


def test_storage_init_cli_repeated(tmp_path):
    config = tmp_path / "settings.toml"
    config.write_text(EXAMPLE)
    for _ in range(2):
        result = run_cli(tmp_path.parent, "storage-init", "--config", str(config))
        assert result.returncode == 0, result.stderr
        assert "revision=0009_opportunity_review" in result.stdout
    assert (tmp_path / "data/signalnest.sqlite3").is_file()
    assert (tmp_path / "data/raw").is_dir()


def test_storage_init_errors(tmp_path):
    config = tmp_path / "settings.toml"
    config.write_text(EXAMPLE)
    (tmp_path / "data").write_text("existing file, not a directory")
    result = run_cli(tmp_path, "storage-init", "--config", str(config))
    assert result.returncode == 1
    assert "存储错误" in result.stderr
    assert "Traceback" not in result.stderr
    assert (tmp_path / "data").read_text() == "existing file, not a directory"
    config.write_text(EXAMPLE.replace("30.0", "0.0"))
    result = run_cli(tmp_path, "storage-init", "--config", str(config))
    assert result.returncode == 2
    assert "http.read_timeout_seconds" in result.stderr


def test_non_database_file_is_not_overwritten(tmp_path):
    config = tmp_path / "settings.toml"
    config.write_text(EXAMPLE)
    (tmp_path / "data").mkdir()
    database = tmp_path / "data/signalnest.sqlite3"
    database.write_bytes(b"this is not a database")
    result = run_cli(tmp_path, "storage-init", "--config", str(config))
    assert result.returncode == 1
    assert "存储错误" in result.stderr
    assert "Traceback" not in result.stderr
    assert database.read_bytes() == b"this is not a database"


def test_config_check_does_not_open_database_or_network(tmp_path):
    config = tmp_path / "settings.toml"
    config.write_text(EXAMPLE)
    script = """
import socket
import sqlite3
import sys

def forbidden(*args, **kwargs):
    raise AssertionError("unexpected network or database operation")
socket.socket.connect = forbidden
socket.create_connection = forbidden
sqlite3.connect = forbidden
sqlite3.dbapi2.connect = forbidden
from signalnest.cli import main
raise SystemExit(main(["config-check", "--config", sys.argv[1]]))
"""
    result = subprocess.run(
        [sys.executable, "-c", script, str(config)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert list(tmp_path.iterdir()) == [config]


def test_existing_config_database_name_is_preserved(tmp_path):
    import sqlite3

    config = tmp_path / "campus.toml"
    config.write_text(EXAMPLE.replace("signalnest.sqlite3", "campus.sqlite3"))
    result = run_cli(tmp_path, "storage-init", "--config", str(config))
    assert result.returncode == 0, result.stderr
    database = tmp_path / "data/campus.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO documents "
            "(source_id, source_document_id, detail_url, discovered_title, discovered_at) "
            "VALUES ('whu-undergrad-student', '1517:1', 'https://example.org/1', 'old notice', 1)"
        )
    result = run_cli(tmp_path.parent, "storage-init", "--config", str(config))
    assert result.returncode == 0, result.stderr
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT discovered_title FROM documents").fetchall() == [
            ("old notice",)
        ]
    assert not (tmp_path / "data/signalnest.sqlite3").exists()


def test_cli_events_are_structured_and_failures_are_not_successes(tmp_path):
    import json

    config = tmp_path / "signalnest.toml"
    config.write_text(EXAMPLE)
    result = run_cli(tmp_path, "config-check", "--config", str(config))
    event = json.loads(result.stderr)
    assert event["event"] == "config_validated"
    assert event["source_id"] == "whu-undergrad-student"
    assert event["run_id"]
    assert "config_validated" not in result.stdout
    (tmp_path / "data").write_text("blocked")
    result = run_cli(tmp_path, "storage-init", "--config", str(config))
    event = json.loads(result.stderr.splitlines()[0])
    assert result.returncode == 1
    assert event["event"] == "storage_init_failed"
    assert event["level"] == "ERROR"
    assert "storage_initialized" not in result.stderr
    assert not result.stdout
