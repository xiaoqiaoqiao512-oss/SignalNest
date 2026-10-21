"""Only explicitly requested, unpublished query copies may migrate from 0008."""

import json
import sqlite3
import subprocess
import sys
from contextlib import closing

import pytest
from alembic import command
from test_mail_backup_verification import mail_backup as mail_backup
from test_notification_service import activated as activated
from test_query_agent import campus as campus
from test_query_snapshots import (
    AT,
    CAPTURED_AT,
    PREPARED_AT,
    ROOT,
    BackupInstance,
    SnapshotSpec,
    prepare_query_snapshot,
    query,
)
from test_query_snapshots import snapshots as snapshots

from signalnest.querying.contracts import QueryError
from signalnest.querying.snapshots import database_sha256
from signalnest.sources import UC
from signalnest.storage import make_engine, migration_config


def downgrade_backup(database):
    engine = make_engine(database, must_exist=True)
    try:
        with engine.begin() as connection:
            config = migration_config()
            config.attributes["connection"] = connection
            command.downgrade(config, "0008_list_references")
    finally:
        engine.dispose()
    return database_sha256(database)


def revision(database):
    with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as conn:
        conn.execute("PRAGMA query_only=ON")
        return conn.execute("SELECT version_num FROM alembic_version").fetchone()[0]


def test_old_backup_is_refused_without_explicit_upgrade_and_not_modified(snapshots, tmp_path):
    _, spec, _, parents = snapshots
    digests = {name: downgrade_backup(path) for name, (path, _) in parents.items()}
    output = tmp_path / "never-published"
    with pytest.raises(QueryError, match="^snapshot_schema_upgrade_required$"):
        prepare_query_snapshot(spec, output, prepared_at=PREPARED_AT)
    assert not output.exists() and not list(tmp_path.glob(".query-snapshot-*"))
    for name, (database, _) in parents.items():
        assert database_sha256(database) == digests[name]
        assert revision(database) == "0008_list_references"


def test_actual_migrations_of_two_copies_preserve_originals_and_allow_read_only_query(
    snapshots, tmp_path
):
    _, spec, _, parents = snapshots
    digests = {name: downgrade_backup(path) for name, (path, _) in parents.items()}
    output = tmp_path / "upgraded"
    result = prepare_query_snapshot(spec, output, prepared_at=PREPARED_AT, upgrade_copy=True)
    for item in result["instances"]:
        name = item["instance_id"]
        original = parents[name][0]
        database = output / name / "signalnest.sqlite3"
        assert database_sha256(original) == digests[name]
        assert revision(original) == "0008_list_references"
        assert revision(database) == item["verified_revision"] == "0009_opportunity_review"
        assert item["parent_revision"] == "0008_list_references"
        assert item["schema_upgraded_in_copy"] is True
        assert item["parent_database_sha256"] == digests[name]
        manifest = json.loads((output / name / "snapshot.json").read_text())
        assert manifest["transformation"] == "pause_and_upgrade_0008_to_0009_v1"
        assert manifest["snapshot_at"] == CAPTURED_AT
        assert not (output / name / "raw").exists()
    frozen = {name: database_sha256(output / name / "signalnest.sqlite3") for name in parents}
    report = query(output)
    assert report.status == "local" and report.error_code is None
    assert any(hit["instance_id"] == "cs" for hit in report.search_results)
    assert {source["instance_id"] for source in report.source_status} == {"uc", "cs"}
    assert frozen == {
        name: database_sha256(output / name / "signalnest.sqlite3") for name in parents
    }
    assert report.at == AT


def test_upgrade_preserves_frozen_mail_bytes_members_delivery_and_attempts(mail_backup, tmp_path):
    backup = mail_backup
    (backup.data_dir / "backup.complete").write_text("verified\n")
    original_sha = downgrade_backup(backup.database)
    spec = SnapshotSpec(
        instances=(
            BackupInstance(
                id="uc",
                source_id=UC.source_id,
                parser=UC.name,
                backup_dir=backup.data_dir,
                snapshot_at=CAPTURED_AT,
            ),
        )
    )
    output = tmp_path / "mail-query-copy"
    prepare_query_snapshot(spec, output, prepared_at=PREPARED_AT, upgrade_copy=True)
    with (
        closing(sqlite3.connect(backup.database.as_uri() + "?mode=ro", uri=True)) as original,
        closing(
            sqlite3.connect((output / "uc/signalnest.sqlite3").as_uri() + "?mode=ro", uri=True)
        ) as derivative,
    ):
        for table in (
            "mail_messages",
            "mail_message_members",
            "mail_delivery",
            "mail_attempts",
            "notification_events",
            "notification_decisions",
            "notification_policy_revisions",
            "documents",
            "notice_versions",
        ):
            assert (
                derivative.execute(f"SELECT * FROM {table}").fetchall()
                == original.execute(f"SELECT * FROM {table}").fetchall()
            )
        assert derivative.execute("SELECT paused FROM notification_channel_state").fetchall() == [
            (1,)
        ]
    assert database_sha256(backup.database) == original_sha


def test_failed_copy_upgrade_cleans_stage_and_keeps_original(snapshots, tmp_path, monkeypatch):
    _, spec, _, parents = snapshots
    digests = {name: downgrade_backup(path) for name, (path, _) in parents.items()}

    def fail(database):
        # The real database is private stage output, never an input backup.
        assert database.parent.parent.name.startswith(".query-snapshot-")
        raise QueryError("snapshot_copy_upgrade_failed")

    monkeypatch.setitem(prepare_query_snapshot.__globals__, "_upgrade_query_copy", fail)
    with pytest.raises(QueryError, match="snapshot_copy_upgrade_failed"):
        prepare_query_snapshot(
            spec, tmp_path / "not-published", prepared_at=PREPARED_AT, upgrade_copy=True
        )
    assert not (tmp_path / "not-published").exists()
    assert not list(tmp_path.glob(".query-snapshot-*"))
    assert all(database_sha256(path) == digests[name] for name, (path, _) in parents.items())


def test_unknown_revision_is_not_guessed_or_upgraded(snapshots, tmp_path):
    _, spec, _, parents = snapshots
    original = parents["uc"][0]
    with closing(sqlite3.connect(original)) as conn, conn:
        conn.execute("UPDATE alembic_version SET version_num='unknown_revision'")
    before = database_sha256(original)
    with pytest.raises(QueryError, match="snapshot_revision_unsupported"):
        prepare_query_snapshot(
            spec, tmp_path / "unknown", prepared_at=PREPARED_AT, upgrade_copy=True
        )
    assert database_sha256(original) == before
    assert not (tmp_path / "unknown").exists()


def test_cli_upgrade_is_explicit_and_same_revision_does_not_run_migration(
    snapshots, tmp_path, monkeypatch
):
    _, spec, _, parents = snapshots

    def forbidden(database):
        raise AssertionError("head copies need no migration")

    monkeypatch.setitem(prepare_query_snapshot.__globals__, "_upgrade_query_copy", forbidden)
    prepared = prepare_query_snapshot(
        spec, tmp_path / "same-head", prepared_at=PREPARED_AT, upgrade_copy=True
    )
    assert all(not item["schema_upgraded_in_copy"] for item in prepared["instances"])
    for database, _ in parents.values():
        downgrade_backup(database)
    spec_path = tmp_path / "spec.json"
    spec_path.write_text(spec.model_dump_json())
    command = [
        sys.executable,
        str(ROOT / "deploy/prepare_query_snapshot.py"),
        "--spec",
        str(spec_path),
    ]
    refused = subprocess.run(
        [*command, "--output", str(tmp_path / "no-flag")], capture_output=True, text=True
    )
    assert refused.returncode == 1 and "snapshot_schema_upgrade_required" in refused.stderr
    upgraded = subprocess.run(
        [*command, "--output", str(tmp_path / "with-flag"), "--upgrade-copy"],
        capture_output=True,
        text=True,
    )
    assert upgraded.returncode == 0, upgraded.stderr
    assert all(item["schema_upgraded_in_copy"] for item in json.loads(upgraded.stdout)["instances"])


def test_transformation_is_checked_against_actual_schema(snapshots):
    output, _, _, _ = snapshots
    database = output / "uc/signalnest.sqlite3"
    database.chmod(0o600)
    downgrade_backup(database)
    manifest_path = output / "uc/snapshot.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["database_sha256"] = database_sha256(database)
    manifest["transformation"] = "pause_and_upgrade_0008_to_0009_v1"
    manifest_path.write_text(json.dumps(manifest))
    report = query(output)
    assert report.source_status[0]["error_code"] == "snapshot_binding_mismatch"
