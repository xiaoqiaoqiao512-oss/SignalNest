"""Real SQLite services with short synthetic content as an explicit parser harness.

The synthetic raw carrier is not a website response. Parser extraction is
covered separately; this suite exercises activation, live baselines and mail.
"""

import json
from datetime import datetime
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from test_notification_service import (
    OPTIONS,
    SOURCE,
    complete_scan_fact,
    live,
    rows,
    start_live,
)

from signalnest.contracts import (
    ListEntry,
    ListPage,
    NoticeContent,
    PaginationEvidence,
    ParsedNotice,
)
from signalnest.errors import IngestError
from signalnest.ingestion import discover_page
from signalnest.mail.contracts import PlanOptions
from signalnest.mail.planning import load_frozen_mail, plan_mail
from signalnest.notifications.contracts import Profile
from signalnest.notifications.opportunity_review import (
    prepare_opportunity_review,
    preview_open_opportunities,
    register_opportunity_review,
    review_open_opportunities,
)
from signalnest.notifications.state import activate_notifications
from signalnest.schema import (
    documents,
    email_outbox,
    mail_message_members,
    notice_versions,
    notification_activation_members,
    notification_events,
    notification_observations,
    notification_opportunity_review_operations,
)
from signalnest.storage import initialize_storage, migration_config

ACT = int(datetime.fromisoformat("2026-10-11T12:34:04+08:00").timestamp())
AT = int(datetime.fromisoformat("2026-10-19T12:00:00+08:00").timestamp())
CASES = json.loads(
    (Path(__file__).parent / "fixtures/notification-open-opportunities-synthetic.json").read_text()
)["cases"]
PROFILE = Profile(
    institution="whu", role="student", study_level="undergraduate", interest_topics=("competition",)
)
IDS = tuple(x["source_document_id"] for x in CASES[:3])


@pytest.fixture
def review_env(state_env):
    env = state_env
    page = ListPage(
        entries=tuple(
            ListEntry(
                source_document_id=c["source_document_id"],
                detail_url=c["source_url"],
                title=c["notice_content"]["title"],
                published_date=c["notice_content"]["published_date"],
            )
            for c in CASES
        ),
        pagination=PaginationEvidence(
            current_page=1,
            total_pages=1,
            is_last_page=True,
            terminal_evidence="disabled_next_and_last",
        ),
    )
    discover_page(env.engine, SOURCE, page, ACT - 20)
    complete_scan_fact(env)  # Explicit synthetic bootstrap fact, not a website scan.
    activate_notifications(env.engine, SOURCE, PROFILE, OPTIONS, at=ACT)
    start_live(env, at=ACT + 1)
    for i, case in enumerate(CASES):
        notice = ParsedNotice(
            source_document_id=case["source_document_id"],
            page_url=case["source_url"],
            parser_version="synthetic-notice-v1",
            content=NoticeContent.model_validate(case["notice_content"]),
        )
        live(
            env,
            ("synthetic raw carrier " + case["case_id"]).encode(),
            at=ACT + 2 + i * 2,
            url=case["source_url"],
            identity=case["source_document_id"],
            parser=lambda _page, result=notice: result,
        )
    return env


def test_history_gate_has_no_event_but_document_review_finds_three_open_candidates(review_env):
    env = review_env
    assert rows(env, notification_events) == []
    before = rows(env, notification_activation_members), rows(env, notification_observations)
    preview = preview_open_opportunities(env.engine, SOURCE, IDS, at=AT)
    assert preview["open_count"] == 3 and preview["creates_delivery"] is False
    assert all(item["decision"]["needs_review"] for item in preview["items"])
    assert before == (
        rows(env, notification_activation_members),
        rows(env, notification_observations),
    )
    assert rows(env, notification_events) == []


def test_review_registers_once_without_rewriting_live_activation_or_versions(review_env):
    env = review_env
    before = [
        rows(env, t)
        for t in (
            notification_activation_members,
            notification_observations,
            documents,
            notice_versions,
        )
    ]
    result = review_open_opportunities(
        env.engine, SOURCE, "three", IDS, at=AT, clock=lambda: AT + 1
    )
    assert result["complete"] and len(result["results"]) == 3
    events = rows(env, notification_events)
    assert len(events) == 3
    assert all(e["kind"] == "opportunity_review" and e["event_seq"] == 0 for e in events)
    assert all(e["effective_route"] == "digest" for e in events)
    assert before == [
        rows(env, t)
        for t in (
            notification_activation_members,
            notification_observations,
            documents,
            notice_versions,
        )
    ]
    again = review_open_opportunities(env.engine, SOURCE, "three", clock=lambda: AT + 50)
    assert again["reused"] and rows(env, notification_events) == events
    other = review_open_opportunities(
        env.engine, SOURCE, "other", IDS, at=AT + 60, clock=lambda: AT + 61
    )
    assert all(x["status"] == "already_notified" for x in other["results"])
    assert rows(env, notification_events) == events
    planned = plan_mail(env.engine, SOURCE, PlanOptions(), at=AT + 86400)
    assert planned["mail_ids"]
    frozen = load_frozen_mail(env.engine, SOURCE, planned["mail_ids"][0])
    assert "仍开放机会回顾" in frozen.rendered.body_text
    assert "待核对信息" in frozen.rendered.body_text
    assert len(rows(env, mail_message_members)) == 3
    assert plan_mail(env.engine, SOURCE, PlanOptions(), at=AT + 86401)["mail_ids"] == []


def test_expired_and_uncertain_time_are_not_registered(review_env):
    env = review_env
    ids = tuple(x["source_document_id"] for x in CASES[3:])
    result = review_open_opportunities(env.engine, SOURCE, "negative", ids, at=AT, clock=lambda: AT)
    assert result["complete"] and rows(env, notification_events) == []
    assert all(x["status"] != "applied" for x in result["results"])


def test_uncertain_interest_is_retained_as_unknown_instead_of_irrelevant(review_env):
    env = review_env
    case = CASES[0]
    item = NoticeContent.model_validate(case["notice_content"]).model_copy(
        update={
            "title": "通知",
            "body_text": "报名条件详见附件。",
            "body_html": "<p>报名条件详见附件。</p>",
        }
    )
    parsed = ParsedNotice(
        source_document_id=IDS[0],
        page_url=case["source_url"],
        parser_version="synthetic-notice-v1",
        content=item,
    )
    live(
        env,
        b"synthetic unknown-interest carrier",
        at=ACT + 200,
        url=case["source_url"],
        identity=IDS[0],
        parser=lambda _page: parsed,
    )
    result = preview_open_opportunities(env.engine, SOURCE, (IDS[0],), at=AT)
    assert result["items"][0]["status"] == "unknown"
    assert result["items"][0]["reason"] == "opportunity_relation_unknown"
    assert result["open_count"] == 0


def test_deadline_before_next_digest_uses_the_existing_immediate_mail_path(review_env):
    env = review_env
    at = int(datetime.fromisoformat("2026-10-23T12:00:00+08:00").timestamp())
    result = review_open_opportunities(
        env.engine, SOURCE, "urgent", (IDS[0],), at=at, clock=lambda: at
    )
    assert result["complete"]
    event = rows(env, notification_events)[0]
    assert event["effective_route"] == "immediate"
    assert event["outbox_id"] == rows(env, email_outbox)[0]["id"]
    assert plan_mail(env.engine, SOURCE, PlanOptions(), at=at)["mail_ids"]


def test_review_mail_is_included_in_read_only_backup_validation(review_env):
    from test_backup_verification import verify_backup

    env = review_env
    review_open_opportunities(env.engine, SOURCE, "backup", IDS, at=AT, clock=lambda: AT)
    plan_mail(env.engine, SOURCE, PlanOptions(), at=AT + 86400)
    result = verify_backup(env.settings.database, env.settings.data_dir)
    assert result["mail_messages"] > 0


def test_group_transaction_failure_rolls_back_and_reopens_for_frozen_retry(review_env):
    env = review_env
    with env.engine.begin() as c:
        c.exec_driver_sql(
            "CREATE TRIGGER reject_second BEFORE INSERT ON notification_events "
            "WHEN (SELECT count(*) FROM notification_events) = 1 "
            "BEGIN SELECT RAISE(ABORT, 'injected'); END"
        )
    with pytest.raises(IngestError, match="database_write_failed"):
        review_open_opportunities(env.engine, SOURCE, "atomic", IDS, at=AT, clock=lambda: AT)
    assert rows(env, notification_events) == []
    assert rows(env, notification_opportunity_review_operations)[0]["finished_at"] is None
    with env.engine.begin() as c:
        c.exec_driver_sql("DROP TRIGGER reject_second")
    result = review_open_opportunities(env.engine, SOURCE, "atomic", clock=lambda: AT + 3)
    assert result["complete"] and len(rows(env, notification_events)) == 3


def test_prepared_review_refuses_changed_live_input(review_env):
    env = review_env
    prepared = prepare_opportunity_review(env.engine, SOURCE, "stale", IDS, at=AT)
    with env.engine.begin() as c:
        c.execute(notification_observations.update().values(observed_at=ACT + 200))
    with pytest.raises(IngestError, match="stale_input"):
        register_opportunity_review(env.engine, prepared, clock=lambda: AT)
    assert rows(env, notification_events) == []


def test_nonempty_cyclic_graph_upgrade_preserves_frozen_mail(review_env):
    env = review_env
    # A normal live event (seq>0) supplies non-empty decision/event/member cycles.
    from test_notification_service import NOTICE, USER_PROFILE, opportunity

    from signalnest.notifications.maintenance import update_policy

    update_policy(env.engine, SOURCE, USER_PROFILE, "exchange", at=AT)
    start_live(env, at=AT + 1)
    discover_page(
        env.engine,
        SOURCE,
        ListPage(
            entries=(
                ListEntry(
                    source_document_id="1517:128231",
                    detail_url=NOTICE,
                    title="交流",
                    published_date="2026-09-04",
                ),
            ),
            pagination=PaginationEvidence(
                current_page=1,
                total_pages=1,
                is_last_page=True,
                terminal_evidence="disabled_next_and_last",
            ),
        ),
        AT,
    )
    live(env, opportunity(), at=AT + 2)
    # This old example has expired, so construct a distinct future-dated version
    # through the same real parser rather than editing event rows.
    raw = (
        opportunity()
        .replace(b"2026-09-04", b"2026-10-19")
        .replace("2026年9月4日".encode(), "2026年10月19日".encode())
    )
    live(env, raw, at=AT + 4)
    # The real update can be ordinary Digest; plan at its next daily slot.
    planned = plan_mail(env.engine, SOURCE, PlanOptions(), at=AT + 86400)
    assert planned["mail_ids"]
    frozen = load_frozen_mail(env.engine, SOURCE, planned["mail_ids"][0])
    cfg = migration_config()
    with env.engine.begin() as c:
        cfg.attributes["connection"] = c
        command.downgrade(cfg, "0008_list_references")
    with env.engine.connect() as c:
        old_event_sql = c.exec_driver_sql(
            "SELECT sql FROM sqlite_master WHERE name='notification_events'"
        ).scalar_one()

    def abort_after_rebuild(connection, cursor, statement, parameters, context, many):
        if statement.lstrip().startswith("CREATE TABLE notification_opportunity_review_operations"):
            raise RuntimeError("injected after cyclic event rebuild")

    sa.event.listen(sa.engine.Engine, "before_cursor_execute", abort_after_rebuild)
    try:
        with pytest.raises(RuntimeError, match="injected after cyclic"):
            initialize_storage(env.settings)
    finally:
        sa.event.remove(sa.engine.Engine, "before_cursor_execute", abort_after_rebuild)
    with env.engine.connect() as c:
        assert c.exec_driver_sql("SELECT version_num FROM alembic_version").scalar_one() == (
            "0008_list_references"
        )
        assert (
            c.exec_driver_sql(
                "SELECT sql FROM sqlite_master WHERE name='notification_events'"
            ).scalar_one()
            == old_event_sql
        )
        assert c.exec_driver_sql("PRAGMA foreign_key_check").all() == []
    assert initialize_storage(env.settings) == "0009_opportunity_review"
    assert initialize_storage(env.settings) == "0009_opportunity_review"
    assert load_frozen_mail(env.engine, SOURCE, frozen.mail_id) == frozen
    with env.engine.connect() as c:
        assert c.exec_driver_sql("PRAGMA foreign_key_check").all() == []
        assert c.exec_driver_sql("PRAGMA foreign_keys").scalar_one() == 1
