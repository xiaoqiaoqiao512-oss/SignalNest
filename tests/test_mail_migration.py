"""Upgrade genuine populated N1 storage; faults are injected DDL exceptions."""

import shutil
import sqlite3
from contextlib import closing
from types import SimpleNamespace

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.runtime.migration import MigrationContext
from test_notification_service import (
    ACTIVATED_AT,
    OPTIONS,
    SOURCE,
    USER_PROFILE,
    complete_scan_fact,
    discover_historical,
    live,
    opportunity,
    start_live,
)

from signalnest.config import StorageSettings
from signalnest.mail.contracts import PlanOptions
from signalnest.mail.planning import plan_mail
from signalnest.notifications.state import activate_notifications
from signalnest.rawstore import RawStore
from signalnest.schema import (
    email_outbox,
    mail_message_members,
    mail_messages,
    mail_plan_errors,
    notification_events,
)
from signalnest.storage import initialize_storage, make_engine, migration_config

NEW_TABLES = {"mail_messages", "mail_message_members", "mail_plan_errors"}


N4_TABLES = {"mail_delivery", "mail_attempts", "notification_operations"}


def snapshot(engine):
    with engine.connect() as connection:
        return {
            name: [
                {
                    k: v
                    for k, v in dict(row).items()
                    if k not in {"pause_reason", "paused_at", "coverage_evidence"}
                }
                for row in connection.execute(
                    sa.select(sa.Table(name, sa.MetaData(), autoload_with=connection))
                ).mappings()
            ]
            for name in sa.inspect(connection).get_table_names()
            if name != "alembic_version"
            and name not in NEW_TABLES | N4_TABLES
            and name != "search_documents"
            and name != "discovered_references"
            and name != "notification_opportunity_review_operations"
            and not name.startswith("search_fts")
        }


def seed_compatible_0004(settings, engine, tmp_path):
    """Copy valid business facts into the historical schema's exact columns.

    Current services only write current storage. This is historical-schema data
    seeding, not execution of an old rule engine or a production index bypass.
    """
    current_settings = StorageSettings(
        data_dir=str(tmp_path / "current-data"), database=str(tmp_path / "current.sqlite")
    )
    initialize_storage(current_settings)
    current_engine = make_engine(current_settings.database)
    current = SimpleNamespace(
        settings=current_settings, engine=current_engine, store=RawStore(current_settings.data_dir)
    )
    try:
        discover_historical(current)
        complete_scan_fact(current)
        activate_notifications(current_engine, SOURCE, USER_PROFILE, OPTIONS, at=ACTIVATED_AT)
        start_live(current)
        live(current)
        live(current, opportunity(), at=ACTIVATED_AT + 5)
        with engine.begin() as connection:
            config = migration_config()
            config.attributes["connection"] = connection
            command.upgrade(config, "0004_notification_state")
        with (
            closing(sqlite3.connect(current_settings.database)) as source,
            closing(sqlite3.connect(settings.database)) as destination,
        ):
            names = [
                name
                for (name,) in destination.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' "
                    "AND name NOT LIKE 'sqlite_%' AND name != 'alembic_version'"
                )
            ]
            destination.execute("PRAGMA foreign_keys=OFF")
            destination.execute("BEGIN")
            for name in names:
                columns = [row[1] for row in destination.execute(f'PRAGMA table_info("{name}")')]
                selected = ",".join(f'"{column}"' for column in columns)
                placeholders = ",".join("?" for _ in columns)
                destination.executemany(
                    f'INSERT INTO "{name}" ({selected}) VALUES ({placeholders})',
                    source.execute(f'SELECT {selected} FROM "{name}"').fetchall(),
                )
            destination.commit()
            destination.execute("PRAGMA foreign_keys=ON")
            assert destination.execute("PRAGMA foreign_key_check").fetchall() == []
        shutil.copytree(current_settings.data_dir / "raw", settings.data_dir / "raw")
    finally:
        current_engine.dispose()


@pytest.mark.parametrize("failure_table", [None, *sorted(NEW_TABLES)])
def test_0004_upgrade_preserves_pending_events_and_intents(tmp_path, failure_table):
    settings = StorageSettings(
        data_dir=str(tmp_path / "data"), database=str(tmp_path / "db.sqlite")
    )
    engine = make_engine(settings.database)
    try:
        seed_compatible_0004(settings, engine, tmp_path)
        original = snapshot(engine)
        assert len(original[notification_events.name]) == 2
        assert len(original[email_outbox.name]) == 1
        raw_files = {path.name: path.read_bytes() for path in (settings.data_dir / "raw").iterdir()}

        def inject(connection, cursor, statement, parameters, context, executemany):
            if failure_table and statement.lstrip().startswith(f"CREATE TABLE {failure_table} "):
                raise RuntimeError("injected N2 migration failure")

        if failure_table:
            sa.event.listen(sa.engine.Engine, "after_cursor_execute", inject)
            try:
                with pytest.raises(RuntimeError, match="injected N2 migration failure"):
                    initialize_storage(settings)
            finally:
                sa.event.remove(sa.engine.Engine, "after_cursor_execute", inject)
            assert snapshot(engine) == original
            with engine.connect() as connection:
                assert (
                    MigrationContext.configure(connection).get_current_revision()
                    == "0004_notification_state"
                )
                assert not NEW_TABLES.intersection(sa.inspect(connection).get_table_names())

        assert initialize_storage(settings) == "0009_opportunity_review"
        assert initialize_storage(settings) == "0009_opportunity_review"
        assert snapshot(engine) == original
        with engine.connect() as connection:
            for table in (mail_messages, mail_message_members, mail_plan_errors):
                assert connection.execute(sa.select(table)).all() == []
            assert connection.exec_driver_sql("PRAGMA foreign_key_check").all() == []
        assert {
            path.name: path.read_bytes() for path in (settings.data_dir / "raw").iterdir()
        } == raw_files
        # Migration itself never allocates historical mail. An explicit plan can
        # consume the already eligible N1 intent after upgrade.
        result = plan_mail(engine, SOURCE, PlanOptions(), at=ACTIVATED_AT + 20)
        assert result["planned"] == 1
        assert result["deferred_digest"] == 1
    finally:
        engine.dispose()
