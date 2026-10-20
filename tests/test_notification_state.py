"""Activation identity/date evidence and additive upgrades using real SQLite/files.

Migration faults are injected exceptions, not process termination or power loss.
"""

from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.runtime.migration import MigrationContext

from signalnest.config import StorageSettings
from signalnest.contracts import PageInput, PaginationEvidence
from signalnest.errors import IngestError
from signalnest.ingestion import ResponseInput, discover_page, import_page, record_response
from signalnest.ingestion_state import (
    ScanCompletion,
    finish_run_in_transaction,
    record_coverage_in_transaction,
    start_run_in_transaction,
)
from signalnest.notifications.contracts import Profile
from signalnest.notifications.state import (
    ActivationOptions,
    activate_notifications,
    candidate_values,
    next_digest_at,
    notification_time,
    preview_activation,
    register_listing_evidence_in_transaction,
)
from signalnest.parsing import PARSER_VERSION, parse_list
from signalnest.rawstore import RawStore
from signalnest.schema import (
    documents,
    notification_channel_state,
)
from signalnest.schema import (
    notification_activation_members as members,
)
from signalnest.schema import (
    notification_listing_evidence as listings,
)
from signalnest.storage import initialize_storage, make_engine, migration_config

ROOT = Path(__file__).resolve().parents[1]
SOURCE = "whu-undergrad-student"
HOME = "https://uc.whu.edu.cn/tzgg/xstz.htm"
BODY = (ROOT / "research/fixtures/student-notices-page1.html").read_bytes()
DAY = int(datetime(2026, 9, 4, 12, tzinfo=ZoneInfo("Asia/Shanghai")).timestamp())
PROFILE = Profile(interest_topics=("exchange",))
OPTIONS = ActivationOptions(
    activation_id="activation-state", sender="sender@example.org", recipient="self@example.org"
)


def list_response(at, **overrides):
    return ResponseInput(
        **(
            dict(
                source_id=SOURCE,
                page_type="list",
                requested_url=HOME,
                final_url=HOME,
                fetched_at=at,
                status_code=200,
            )
            | overrides
        )
    )


def scan(engine, *, coverage="complete", origin="bootstrap", result="partial_failure", at=DAY - 20):
    with engine.begin() as connection:
        start_run_in_transaction(connection, SOURCE, f"scan-{at}", at, origin=origin)
        record_coverage_in_transaction(
            connection,
            f"scan-{at}",
            coverage,
            at + 1,
            completion=ScanCompletion(
                pages=(
                    PaginationEvidence(
                        current_page=1,
                        total_pages=1,
                        is_last_page=True,
                        terminal_evidence="disabled_next_and_last",
                    ),
                ),
                home_recheck_unchanged=True,
            )
            if coverage == "complete"
            else None,
        )
        finish_run_in_transaction(connection, f"scan-{at}", result, at + 2)


def test_activation_requires_complete_production_coverage_not_detail_success(state_env):
    env = state_env
    assert (
        preview_activation(env.engine, SOURCE, PROFILE, OPTIONS, at=DAY)["blocker"]
        == "complete_scan_required"
    )
    scan(env.engine, coverage="limited", at=DAY - 50)
    with pytest.raises(IngestError, match="notification_complete_scan_required"):
        activate_notifications(env.engine, SOURCE, PROFILE, OPTIONS, at=DAY)
    scan(env.engine, origin="historical", at=DAY - 40)
    assert not preview_activation(env.engine, SOURCE, PROFILE, OPTIONS, at=DAY)["ready"]
    scan(env.engine, result="partial_failure")
    assert preview_activation(env.engine, SOURCE, PROFILE, OPTIONS, at=DAY)["ready"]
    first = activate_notifications(env.engine, SOURCE, PROFILE, OPTIONS, at=DAY)
    assert first["member_count"] == 0
    replay = activate_notifications(env.engine, SOURCE, PROFILE, OPTIONS, at=DAY + 86400)
    assert replay["reused"] and replay["activation_at"] == DAY
    assert first["installation_id"] == replay["installation_id"]
    with pytest.raises(IngestError, match="notification_activation_conflict"):
        activate_notifications(
            env.engine,
            SOURCE,
            PROFILE,
            OPTIONS.model_copy(update={"notification_mode": "digest_only"}),
            at=DAY,
        )


def test_response_less_discovery_does_not_invent_usable_list_evidence(state_env):
    env = state_env
    discover_page(env.engine, SOURCE, parse_list(PageInput(content=BODY, page_url=HOME)), DAY - 100)
    scan(env.engine)
    preview = preview_activation(env.engine, SOURCE, PROFILE, OPTIONS, at=DAY)
    assert preview["candidate_counts"]["unknown"] == 25
    with env.engine.connect() as connection:
        assert connection.execute(sa.select(listings)).all() == []


def test_activation_uses_stable_identity_even_if_sqlite_reuses_document_id(state_env):
    env = state_env
    scan(env.engine)
    with env.engine.begin() as connection:
        old_id = connection.execute(
            documents.insert().values(
                source_id=SOURCE,
                source_document_id="1517:old",
                detail_url=HOME,
                discovered_title="old",
                discovered_at=DAY - 100,
            )
        ).inserted_primary_key[0]
    activate_notifications(env.engine, SOURCE, PROFILE, OPTIONS, at=DAY)
    with env.engine.begin() as connection:
        connection.execute(documents.delete().where(documents.c.id == old_id))
        new_id = connection.execute(
            documents.insert().values(
                source_id=SOURCE,
                source_document_id="1517:new",
                detail_url=HOME,
                discovered_title="new",
                discovered_at=DAY + 1,
            )
        ).inserted_primary_key[0]
        assert new_id == old_id
        identities = connection.execute(sa.select(members.c.source_document_id)).scalars().all()
        assert identities == ["1517:old"]


def test_stale_list_replay_cannot_promote_activation_member(state_env):
    env = state_env
    old_body = BODY.replace(b"2026-09-04", b"2026-08-01")
    import_page(env.engine, env.store, list_response(DAY - 100), old_body, DAY - 90)
    scan(env.engine)
    activate_notifications(env.engine, SOURCE, PROFILE, OPTIONS, at=DAY)
    older = record_response(env.engine, env.store, list_response(DAY - 200), BODY)
    discover_page(
        env.engine,
        SOURCE,
        parse_list(PageInput(content=BODY, page_url=HOME)),
        DAY - 180,
        response_id=older,
    )
    with env.engine.connect() as connection:
        doc_id = connection.execute(
            sa.select(documents.c.id).where(documents.c.source_document_id == "1517:128231")
        ).scalar_one()
        entry = (
            connection.execute(sa.select(listings).where(listings.c.document_id == doc_id))
            .mappings()
            .one()
        )
        member = (
            connection.execute(
                sa.select(members).where(members.c.source_document_id == "1517:128231")
            )
            .mappings()
            .one()
        )
        assert entry["published_date"].isoformat() == "2026-08-01"
        assert member["candidate_state"] == "not_recent"
        assert member["list_evidence"]["published_date"] == "2026-08-01"


@pytest.mark.parametrize("failure", ["source", "status", "page_type", "missing"])
def test_listing_registry_validates_provenance_on_direct_connection(state_env, failure):
    env = state_env
    page = parse_list(PageInput(content=BODY, page_url=HOME))
    discover_page(env.engine, SOURCE, page, DAY - 100)
    override = {
        "source": {"source_id": "other-source"},
        "status": {"status_code": 500},
        "page_type": {},
        "missing": {},
    }[failure]
    response_id = record_response(env.engine, env.store, list_response(DAY - 100, **override), BODY)
    if failure == "page_type":
        with env.engine.begin() as connection:
            connection.execute(
                sa.text("UPDATE raw_responses SET page_type=NULL WHERE id=:id"), {"id": response_id}
            )
    if failure == "missing":
        response_id += 1000
    with (
        pytest.raises(IngestError, match="notification_list_evidence_invalid"),
        env.engine.begin() as connection,
    ):
        register_listing_evidence_in_transaction(
            connection,
            source_id=SOURCE,
            page=page,
            body_response_id=response_id,
            observed_response_id=response_id,
            processed_at=DAY,
            parser_version=PARSER_VERSION,
            processing_origin="offline",
            ingestion_run_id=None,
            new_document_ids=set(),
        )


def test_date_selection_and_first_conflict_evidence_are_bounded_and_sticky():
    state = dict(activation_at=DAY, initial_recent_review=True)
    recent = dict(kind="list_entry", published_date="2026-09-04", body_response_id=1)
    old = dict(kind="notice_version", published_date="2026-08-01", body_response_id=2)
    selected = candidate_values(state, None, recent, None)
    conflict = candidate_values(state, selected, recent, old)
    revised = candidate_values(
        state, conflict, old | {"kind": "list_entry", "body_response_id": 3}, old
    )
    assert revised["candidate_state"] == "selected" and revised["date_conflict"]
    assert revised["selection_evidence"] == recent
    assert revised["conflict_evidence"] == [recent, old]
    assert len(revised["conflict_evidence"]) == 2


@pytest.mark.parametrize("at", [2**63 - 1, -1, True])
def test_notification_clock_errors_are_finite(at):
    with pytest.raises(IngestError):
        notification_time(at)


def test_daily_digest_calendar_is_strictly_future_at_exact_boundary():
    boundary = int(datetime(2026, 9, 4, 9, tzinfo=ZoneInfo("Asia/Shanghai")).timestamp())
    assert (
        next_digest_at(dict(digest_hour=9, digest_minute=0), boundary).timestamp()
        == boundary + 86400
    )
    assert (
        next_digest_at(dict(digest_hour=9, digest_minute=0), boundary - 1).timestamp() == boundary
    )


@pytest.mark.parametrize("fail_table", [None, "notification_events", "email_outbox"])
def test_0003_upgrade_preserves_data_and_never_activates_notifications(tmp_path, fail_table):
    settings = StorageSettings(
        data_dir=str(tmp_path / "data"), database=str(tmp_path / "db.sqlite")
    )
    settings.data_dir.mkdir()
    (settings.data_dir / "raw").mkdir()
    store = RawStore(settings.data_dir)
    blob = store.archive(BODY)
    engine = make_engine(settings.database)
    try:
        with engine.begin() as connection:
            config = migration_config()
            config.attributes["connection"] = connection
            command.upgrade(config, "0003_ingestion_state")
            connection.execute(
                documents.insert().values(
                    source_id=SOURCE,
                    source_document_id="1517:old",
                    detail_url=HOME,
                    discovered_title="preserved",
                    discovered_at=1,
                )
            )

        def inject(connection, cursor, statement, parameters, context, executemany):
            if fail_table and statement.lstrip().startswith(f"CREATE TABLE {fail_table} "):
                raise RuntimeError("injected notification migration failure")

        if fail_table:
            sa.event.listen(sa.engine.Engine, "after_cursor_execute", inject)
            try:
                with pytest.raises(RuntimeError, match="injected notification migration failure"):
                    initialize_storage(settings)
            finally:
                sa.event.remove(sa.engine.Engine, "after_cursor_execute", inject)
            with engine.connect() as connection:
                assert (
                    MigrationContext.configure(connection).get_current_revision()
                    == "0003_ingestion_state"
                )
                assert "notification_channel_state" not in sa.inspect(connection).get_table_names()
                assert (
                    connection.execute(sa.select(documents.c.discovered_title)).scalar_one()
                    == "preserved"
                )
        assert initialize_storage(settings) == "0009_opportunity_review"
        assert initialize_storage(settings) == "0009_opportunity_review"
        with engine.connect() as connection:
            assert (
                connection.execute(sa.select(documents.c.discovered_title)).scalar_one()
                == "preserved"
            )
            assert connection.execute(sa.select(notification_channel_state)).all() == []
            assert connection.execute(sa.select(members)).all() == []
            assert connection.exec_driver_sql("PRAGMA foreign_key_check").all() == []
        assert store.read(blob.path, blob.sha256) == BODY
    finally:
        engine.dispose()
