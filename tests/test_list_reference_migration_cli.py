"""Upgrade, read-only CLI and backup checks for the unadapted-reference ledger."""

import hashlib
import json
import shutil
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.runtime.migration import MigrationContext
from test_backup_verification import BackupVerificationError, verify_backup
from test_crawling import SOURCE, html, rows, run
from test_list_references import PAGE6, evidence, mixed
from test_offline_cli import manifest
from test_offline_cli import run as cli_run

from signalnest.cli import main
from signalnest.config import StorageSettings, load_config
from signalnest.ingestion import import_page
from signalnest.instance_lock import writer_lock
from signalnest.rawstore import RawStore
from signalnest.schema import discovered_references
from signalnest.storage import initialize_storage, make_engine, migration_config

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def old_database(tmp_path):
    settings = StorageSettings(data_dir=str(tmp_path / "data"), database=str(tmp_path / "db"))
    (settings.data_dir / "raw").mkdir(parents=True)
    store = RawStore(settings.data_dir)
    engine = make_engine(settings.database)
    with engine.begin() as connection:
        config = migration_config()
        config.attributes["connection"] = connection
        command.upgrade(config, "0007_history_search")
        connection.exec_driver_sql(
            "INSERT INTO source_ingestion_state(source_id) VALUES (?)", (SOURCE,)
        )
        connection.exec_driver_sql(
            "INSERT INTO ingestion_runs(id,source_id,origin,parser_version,started_at,"
            "finished_at,result,coverage,coverage_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (
                "old-complete",
                SOURCE,
                "bootstrap",
                "whu-student-notices-v3",
                1,
                10,
                "succeeded",
                "complete",
                9,
            ),
        )
    content = (ROOT / "research/fixtures/student-notices-page1.html").read_bytes()
    import_page(engine, store, evidence(), content, 101)
    tables = ("documents", "raw_responses", "source_ingestion_state", "ingestion_runs")
    with engine.connect() as connection:
        before = {
            table: connection.exec_driver_sql(f"SELECT * FROM {table}").all() for table in tables
        }
    yield settings, engine, before
    engine.dispose()


def test_0007_upgrade_and_repeat_keep_existing_evidence_unknown(old_database):
    settings, engine, before = old_database
    original = {path.name: path.read_bytes() for path in (settings.data_dir / "raw").iterdir()}
    assert initialize_storage(settings) == initialize_storage(settings) == "0009_opportunity_review"
    with engine.connect() as connection:
        assert connection.execute(sa.select(discovered_references)).all() == []
        for table, values in before.items():
            after = connection.exec_driver_sql(f"SELECT * FROM {table}").all()
            if table == "ingestion_runs":
                assert [tuple(row[:-1]) for row in after] == [tuple(row) for row in values]
                assert all(row[-1] is None for row in after)
            else:
                assert after == values
        assert connection.exec_driver_sql("PRAGMA foreign_keys").scalar_one() == 1
        assert connection.exec_driver_sql("PRAGMA foreign_key_check").all() == []
    assert {
        path.name: path.read_bytes() for path in (settings.data_dir / "raw").iterdir()
    } == original


@pytest.mark.parametrize("boundary", ["table", "column"])
def test_failed_upgrade_is_atomic_without_rebuilding_old_tables(old_database, boundary):
    settings, engine, before = old_database

    def inject(connection, cursor, statement, parameters, context, executemany):
        if (
            boundary == "table"
            and statement.lstrip().startswith("CREATE TABLE discovered_references")
        ) or (
            boundary == "column"
            and statement.startswith("ALTER TABLE ingestion_runs ADD COLUMN coverage_evidence")
        ):
            raise RuntimeError("injected DDL interruption")

    sa.event.listen(sa.engine.Engine, "after_cursor_execute", inject)
    try:
        with pytest.raises(RuntimeError, match="injected DDL interruption"):
            initialize_storage(settings)
    finally:
        sa.event.remove(sa.engine.Engine, "after_cursor_execute", inject)
    with engine.connect() as connection:
        assert (
            MigrationContext.configure(connection).get_current_revision() == "0007_history_search"
        )
        assert "discovered_references" not in sa.inspect(connection).get_table_names()
        assert "coverage_evidence" not in {
            c["name"] for c in sa.inspect(connection).get_columns("ingestion_runs")
        }
        for table, values in before.items():
            assert connection.exec_driver_sql(f"SELECT * FROM {table}").all() == values
    assert initialize_storage(settings) == "0009_opportunity_review"


def test_database_constraints_protect_reference_identity_and_provenance(state_env):
    env = state_env
    import_page(env.engine, env.store, evidence(), mixed(), 101)
    row = dict(rows(env, discovered_references)[0])
    row.pop("id")
    with pytest.raises(sa.exc.IntegrityError), env.engine.begin() as connection:
        connection.execute(discovered_references.insert().values(**row))
    for updates in (
        {"status": "processed"},
        {"first_seen_at": 102},
        {"first_body_response_id": 999},
    ):
        with pytest.raises(sa.exc.IntegrityError), env.engine.begin() as connection:
            connection.execute(discovered_references.update().values(**updates))


def test_real_cli_import_reparse_and_read_only_references_from_other_cwd(tmp_path):
    config = tmp_path / "signalnest.toml"
    config.write_bytes((ROOT / "config.example.toml").read_bytes())
    assert cli_run(tmp_path, "storage-init", "--config", str(config)).returncode == 0
    metadata = manifest(tmp_path, requested_url=PAGE6, final_url=PAGE6)
    html_file = next((ROOT / "research/fixtures/list-links-20261009").glob("uc19-*.html"))
    imported = cli_run(
        tmp_path.parent,
        "import-page",
        "--config",
        str(config),
        "--metadata",
        str(metadata),
        "--file",
        str(html_file),
        "--processed-at",
        "101",
    )
    assert imported.returncode == 0, imported.stderr
    result = json.loads(imported.stdout)
    assert result["discovered_count"] == 24 and result["reference_count"] == 1
    assert result["registered_row_count"] == 25 and result["pagination"]["current_page"] == 6
    replay = cli_run(
        tmp_path.parent,
        "reparse",
        "--config",
        str(config),
        "--response-id",
        str(result["response_id"]),
        "--processed-at",
        "102",
    )
    assert replay.returncode == 0, replay.stderr
    settings = load_config(config)
    before = settings.storage.database.read_bytes()
    selected = cli_run(tmp_path.parent, "references-list", "--config", str(config))
    assert selected.returncode == 0, selected.stderr
    report = json.loads(selected.stdout)
    assert report["total"] == 1 and report["references"][0]["first_seen_at"] == 101
    assert report["references"][0]["last_seen_at"] == 102
    assert report["references"][0]["status"] == "pending_adapter"
    assert settings.storage.database.read_bytes() == before
    assert "sim.whu.edu.cn" not in selected.stderr  # Full URI appears only in requested output.
    status = cli_run(tmp_path.parent, "status", "--config", str(config))
    assert status.returncode == 0 and json.loads(status.stdout)["unadapted_references"] == 1


def test_references_read_during_writer_lock_without_http_or_side_effect(
    tmp_path, monkeypatch, capsys
):
    config = tmp_path / "signalnest.toml"
    config.write_bytes((ROOT / "config.example.toml").read_bytes())
    settings = load_config(config)
    initialize_storage(settings.storage)

    def forbidden(*args, **kwargs):
        raise AssertionError("read-only reference inspection accessed HTTP or writer lock")

    with writer_lock(settings.storage.database):
        before = settings.storage.database.read_bytes()
        monkeypatch.setattr("httpx.Client", forbidden)
        monkeypatch.setattr("signalnest.instance_lock.writer_lock", forbidden)
        assert main(["references-list", "--config", str(config)]) == 0
        assert json.loads(capsys.readouterr().out)["total"] == 0
        assert settings.storage.database.read_bytes() == before


@pytest.mark.parametrize(
    "args,code", [((), 1), (("--limit", "0"), 2), (("--offset", "-1"), 2), (("--limit", "101"), 2)]
)
def test_reference_cli_errors_do_not_create_storage(tmp_path, args, code):
    config = tmp_path / "signalnest.toml"
    config.write_bytes((ROOT / "config.example.toml").read_bytes())
    result = cli_run(tmp_path, "references-list", "--config", str(config), *args)
    assert result.returncode == code and "Traceback" not in result.stderr
    assert set(tmp_path.iterdir()) == {config}


def test_reference_help_does_not_open_database_http_or_lock(tmp_path, monkeypatch, capsys):
    def forbidden(*args, **kwargs):
        raise AssertionError("help attempted I/O")

    monkeypatch.setattr("sqlite3.dbapi2.connect", forbidden)
    monkeypatch.setattr("httpx.Client", forbidden)
    monkeypatch.setattr("signalnest.instance_lock.writer_lock", forbidden)
    with pytest.raises(SystemExit) as exited:
        main(["references-list", "--help"])
    assert exited.value.code == 0
    assert "--limit" in capsys.readouterr().out
    assert not list(tmp_path.iterdir())


@pytest.fixture
def reference_backup(state_env, tmp_path):
    env = state_env
    result = run(env, lambda request: html(mixed(1, 1)))
    assert result.coverage == "complete"
    clone = tmp_path / "snapshot"
    clone.mkdir()
    database = clone / "signalnest.sqlite3"
    with writer_lock(env.settings.database):
        with (
            closing(sqlite3.connect(env.settings.database)) as source,
            closing(sqlite3.connect(database)) as destination,
        ):
            source.backup(destination)
        shutil.copytree(env.settings.data_dir / "raw", clone / "raw", symlinks=True)
    return database, clone


def test_backup_verifies_reference_provenance_and_complete_ledger_read_only(reference_backup):
    database, directory = reference_backup
    before = hashlib.sha256(database.read_bytes()).hexdigest()
    report = verify_backup(database, directory)
    assert report["discovered_references"] == report["complete_scan_ledgers"] == 1
    assert hashlib.sha256(database.read_bytes()).hexdigest() == before


@pytest.mark.parametrize("damage", ["key", "uri", "source", "body", "ledger_member", "ledger_home"])
def test_backup_rejects_invalid_reference_or_missing_coverage_member(reference_backup, damage):
    database, directory = reference_backup
    with sqlite3.connect(database) as connection:
        if damage == "key":
            connection.execute(
                "UPDATE discovered_references SET candidate_key=?", ("link:v1:" + "0" * 64,)
            )
        elif damage == "uri":
            connection.execute(
                "UPDATE discovered_references SET resolved_url='https://different.example/a'"
            )
        elif damage == "source":
            connection.execute("UPDATE discovered_references SET source_id='wrong-source'")
        elif damage == "body":
            connection.execute("UPDATE discovered_references SET first_body_response_id=999")
        else:
            run_id, raw = connection.execute(
                "SELECT id,coverage_evidence FROM ingestion_runs"
            ).fetchone()
            ledger = json.loads(raw)
            if damage == "ledger_member":
                ledger["registrations"][0]["rows"][0][1] = "1517:999"
                ledger["home_recheck"]["rows"][0][1] = "1517:999"
            else:
                ledger["home_recheck"]["rows"] = [["notice", "1517:128231"]]
            connection.execute(
                "UPDATE ingestion_runs SET coverage_evidence=? WHERE id=?",
                (json.dumps(ledger), run_id),
            )
    with pytest.raises(BackupVerificationError):
        verify_backup(database, directory)
