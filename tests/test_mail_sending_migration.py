"""Populated N2 migration preserves frozen evidence; injected failures roll DDL back.

These are transaction fault-injection tests, not process termination or power-loss
experiments. The legacy database is seeded with actual successful N1/N2 records.
"""

import shutil
import sqlite3
from contextlib import closing

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.runtime.migration import MigrationContext
from test_notification_service import ACTIVATED_AT, SOURCE, live, opportunity
from test_notification_service import activated as activated

from signalnest.config import StorageSettings
from signalnest.mail.contracts import PlanOptions
from signalnest.mail.planning import plan_mail
from signalnest.schema import mail_attempts, mail_delivery, notification_operations
from signalnest.storage import initialize_storage, make_engine, migration_config

NEW_TABLES = {"mail_delivery", "mail_attempts", "notification_operations"}
NEW_COLUMNS = {"pause_reason", "paused_at", "coverage_evidence"}


def historical_snapshot(database):
    """Read exact DBAPI values, including JSON strings and frozen wire bytes."""
    with closing(sqlite3.connect(database)) as connection:
        tables = sorted(
            name
            for (name,) in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
            if name != "alembic_version"
            and name not in NEW_TABLES
            and name != "search_documents"
            and name != "discovered_references"
            and name != "notification_opportunity_review_operations"
            and not name.startswith("search_fts")
        )
        result = {}
        for name in tables:
            columns = [
                row[1]
                for row in connection.execute(f'PRAGMA table_info("{name}")')
                if row[1] not in NEW_COLUMNS
            ]
            selected = ",".join(f'"{column}"' for column in columns)
            result[name] = (
                columns,
                connection.execute(f'SELECT {selected} FROM "{name}"').fetchall(),
            )
        return result


def legacy_copy(env, tmp_path):
    settings = StorageSettings(
        data_dir=str(tmp_path / "legacy-data"), database=str(tmp_path / "legacy.sqlite")
    )
    engine = make_engine(settings.database)
    with engine.begin() as connection:
        config = migration_config()
        config.attributes["connection"] = connection
        command.upgrade(config, "0005_mail_planning")
    snapshot = historical_snapshot(env.settings.database)
    # N1 has cyclic event/decision/outbox references. Seed the historical schema
    # using SQLite's explicit FK-off mode outside a transaction, then validate it.
    with closing(sqlite3.connect(settings.database)) as connection:
        connection.execute("PRAGMA foreign_keys = OFF")
        connection.execute("BEGIN")
        for name, (columns, rows) in snapshot.items():
            selected = ",".join(f'"{column}"' for column in columns)
            placeholders = ",".join("?" for _ in columns)
            connection.executemany(
                f'INSERT INTO "{name}" ({selected}) VALUES ({placeholders})', rows
            )
        connection.commit()
        connection.execute("PRAGMA foreign_keys = ON")
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    shutil.copytree(env.settings.data_dir / "raw", settings.data_dir / "raw")
    assert historical_snapshot(settings.database) == snapshot
    return settings, engine, snapshot


@pytest.mark.parametrize("failure_point", [None, "pause_column", *sorted(NEW_TABLES), "backfill"])
def test_populated_0005_upgrade_and_failures_preserve_all_evidence(
    activated, tmp_path, failure_point
):
    env = activated
    live(env, opportunity())
    assert plan_mail(env.engine, SOURCE, PlanOptions(), at=ACTIVATED_AT + 10)["planned"] == 1
    settings, engine, original = legacy_copy(env, tmp_path)
    original_files = {
        path.name: path.read_bytes() for path in (settings.data_dir / "raw").iterdir()
    }
    assert len(original["mail_messages"][1]) == 1
    assert len(original["mail_message_members"][1]) == 1

    def inject(connection, cursor, statement, parameters, context, executemany):
        statement = statement.lstrip()
        hit = (
            (
                failure_point == "pause_column"
                and statement.startswith(
                    "ALTER TABLE notification_channel_state ADD COLUMN pause_reason"
                )
            )
            or (
                failure_point in NEW_TABLES
                and statement.startswith(f"CREATE TABLE {failure_point} ")
            )
            or (failure_point == "backfill" and statement.startswith("INSERT INTO mail_delivery "))
        )
        if hit:
            raise RuntimeError("injected N4 migration failure")

    try:
        if failure_point:
            sa.event.listen(sa.engine.Engine, "after_cursor_execute", inject)
            try:
                with pytest.raises(RuntimeError, match="injected N4 migration failure"):
                    initialize_storage(settings)
            finally:
                sa.event.remove(sa.engine.Engine, "after_cursor_execute", inject)
            assert historical_snapshot(settings.database) == original
            with engine.connect() as connection:
                assert (
                    MigrationContext.configure(connection).get_current_revision()
                    == "0005_mail_planning"
                )
                assert not NEW_TABLES.intersection(sa.inspect(connection).get_table_names())
                columns = {
                    row["name"]
                    for row in sa.inspect(connection).get_columns("notification_channel_state")
                }
                assert not NEW_COLUMNS.intersection(columns)
                assert connection.exec_driver_sql("PRAGMA foreign_key_check").all() == []

        assert initialize_storage(settings) == "0009_opportunity_review"
        assert historical_snapshot(settings.database) == original
        with engine.connect() as connection:
            deliveries = connection.execute(sa.select(mail_delivery)).mappings().all()
            assert len(deliveries) == 1
            delivery = deliveries[0]
            assert delivery["state"] == "pending" and delivery["attempt_count"] == 0
            assert not delivery["manual_retry_pending"] and delivery["accepted_at"] is None
            mail_columns, mail_rows = original["mail_messages"]
            frozen_at = dict(zip(mail_columns, mail_rows[0], strict=True))["frozen_at"]
            assert delivery["next_attempt_at"] == delivery["updated_at"] == frozen_at
            assert connection.execute(sa.select(mail_attempts)).all() == []
            assert connection.execute(sa.select(notification_operations)).all() == []
            assert connection.exec_driver_sql("PRAGMA foreign_key_check").all() == []
        before = settings.database.read_bytes()
        assert initialize_storage(settings) == "0009_opportunity_review"
        assert settings.database.read_bytes() == before
        assert {
            path.name: path.read_bytes() for path in (settings.data_dir / "raw").iterdir()
        } == original_files
    finally:
        engine.dispose()
