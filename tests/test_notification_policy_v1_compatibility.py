"""v1 serialized records remain readable by v2 over real N1/N2 SQLite state.

The manifest comes from a pre-upgrade v1 fixture. Supported exchange notices
have the same facts and decisions in both versions; setup converts their sealed
JSON to the exact v1 shape and recomputes the documented audit hashes. These are
compatibility fixtures, not a claim to execute the historical v1 rule engine.
"""

import json
from copy import deepcopy
from datetime import datetime
from pathlib import Path

import pytest
from test_notification_maintenance import AT, SAVE_ONLY, none_event, register
from test_notification_service import (
    ACTIVATED_AT,
    SOURCE,
    USER_PROFILE,
    live,
    opportunity,
    rows,
)
from test_notification_service import activated as activated

from signalnest.contracts import NoticeContent
from signalnest.errors import IngestError
from signalnest.mail.contracts import PlanOptions
from signalnest.mail.planning import load_frozen_mail, plan_mail
from signalnest.notifications.contracts import NoticeFacts, canonical_sha256
from signalnest.notifications.maintenance import (
    _input,
    reevaluate_events,
    update_policy,
)
from signalnest.schema import (
    email_outbox,
    mail_message_members,
    mail_messages,
    notice_versions,
    notification_channel_state,
    notification_decisions,
    notification_events,
    notification_operations,
    notification_policy_revisions,
)

V1 = json.loads((Path(__file__).parent / "fixtures/notification-policy-v1.json").read_text())
V1_VERSIONS = dict(
    facts_extractor="whu-notice-facts-v1",
    rules="notification-rules-v1",
    decision_engine="notification-decision-v1",
    routing="notification-routing-v1",
)


def old_manifest(profile):
    manifest = deepcopy(V1["manifest"])
    manifest["profile"] = profile
    assert manifest["versions"] == V1_VERSIONS
    return manifest


def old_json(facts, context, decision, content, manifest, previous_content=None):
    """Produce the v1 on-disk shape for a supported unchanged exchange example."""
    full = facts | dict(body_text=content.body_text, extractor_version="whu-notice-facts-v1")
    full.pop("deadline_lower_at", None)
    previous = context.get("previous_facts")
    full_context = deepcopy(context)
    if previous is not None:
        assert previous_content is not None
        previous = previous | dict(
            body_text=previous_content.body_text, extractor_version="whu-notice-facts-v1"
        )
        previous.pop("deadline_lower_at", None)
        full_context["previous_facts"] = previous
        context["previous_facts"] = {
            key: value for key, value in previous.items() if key != "body_text"
        }
    profile, policy_hash = manifest["profile"], canonical_sha256(manifest)
    decision = decision | dict(
        facts_extractor_version="whu-notice-facts-v1",
        rules_version="notification-rules-v1",
        decision_engine_version="notification-decision-v1",
        routing_version="notification-routing-v1",
        profile_sha256=canonical_sha256(profile),
        facts_sha256=canonical_sha256(full),
        policy_sha256=policy_hash,
    )
    decision["input_sha256"] = canonical_sha256(
        dict(
            profile=profile,
            facts=full,
            context=full_context,
            evaluated_at=datetime.fromisoformat(decision["evaluated_at"]).isoformat(),
            policy_sha256=policy_hash,
        )
    )
    compact = dict(full)
    compact.pop("body_text")
    assert NoticeFacts.model_validate(full).sha256() == decision["facts_sha256"]
    return compact, decision


def install_old_seals(env):
    """Convert real sealed records to synthetic, internally consistent v1 state."""
    policies = rows(env, notification_policy_revisions)
    with env.engine.begin() as connection:
        for row in policies:
            manifest = old_manifest(row["manifest"]["profile"])
            connection.execute(
                notification_policy_revisions.update()
                .where(notification_policy_revisions.c.id == row["id"])
                .values(manifest=manifest, policy_sha256=canonical_sha256(manifest))
            )
        for row in rows(env, notification_decisions):
            event = next(
                item for item in rows(env, notification_events) if item["id"] == row["event_id"]
            )
            version = next(
                item for item in rows(env, notice_versions) if item["id"] == event["version_id"]
            )
            content = NoticeContent.model_validate(version["normalized_content"])
            previous = next(
                (
                    item
                    for item in rows(env, notice_versions)
                    if item["id"] == event["previous_version_id"]
                ),
                None,
            )
            manifest = old_manifest(
                next(item for item in policies if item["id"] == row["policy_revision_id"])[
                    "manifest"
                ]["profile"]
            )
            facts, decision = old_json(
                row["facts"],
                row["context"],
                row["decision"],
                content,
                manifest,
                NoticeContent.model_validate(previous["normalized_content"]) if previous else None,
            )
            connection.execute(
                notification_decisions.update()
                .where(notification_decisions.c.id == row["id"])
                .values(facts=facts, decision=decision, context=row["context"])
            )
        for operation in rows(env, notification_operations):
            if operation["kind"] != "policy_update":
                continue
            parameters = deepcopy(operation["parameters"])
            snapshot = deepcopy(operation["snapshot"])
            old_policy = next(
                item for item in policies if item["id"] == snapshot["payload"]["policy_revision_id"]
            )
            policy_hash = canonical_sha256(old_manifest(old_policy["manifest"]["profile"]))
            parameters["policy_sha256"] = policy_hash
            snapshot["payload"]["policy_sha256"] = policy_hash
            snapshot["sha256"] = canonical_sha256(snapshot["payload"])
            connection.execute(
                notification_operations.update()
                .where(notification_operations.c.id == operation["id"])
                .values(
                    parameters=parameters,
                    parameters_sha256=canonical_sha256(parameters),
                    snapshot=snapshot,
                )
            )


def deny_current_rules(*_args, **_kwargs):
    raise AssertionError("a saved v1 operation/mail must not execute current rules")


@pytest.mark.parametrize("kind", ["activation_recent", "update"])
def test_v1_pending_operation_resumes_original_hashes_and_decision_without_current_rules(
    activated, monkeypatch, kind
):
    env = activated
    if kind == "activation_recent":
        event_id = none_event(env)
    else:
        update_policy(env.engine, SOURCE, SAVE_ONLY, "save-only", at=ACTIVATED_AT + 1)
        live(env, opportunity())
        live(
            env,
            opportunity().replace("现启动".encode(), "现正式启动".encode()),
            at=ACTIVATED_AT + 5,
        )
        update_policy(env.engine, SOURCE, USER_PROFILE, "interested", at=AT)
        event_id = rows(env, notification_events)[-1]["id"]
    register(env, event_id, "old-operation")
    install_old_seals(env)
    operation = rows(env, notification_operations)[-1]
    assert operation["id"] == "old-operation" and operation["finished_at"] is None
    parameters = deepcopy(operation["parameters"])
    snapshot = deepcopy(operation["snapshot"])
    policy = next(
        item
        for item in rows(env, notification_policy_revisions)
        if item["id"] == parameters["policy_revision_id"]
    )
    parameters["policy_sha256"] = policy["policy_sha256"]
    snapshot["payload"]["policy_sha256"] = policy["policy_sha256"]
    item = snapshot["payload"]["items"][0]
    with env.engine.connect() as connection:
        state = rows(env, notification_channel_state)[0]
        inputs = _input(connection, state, event_id)
    content = NoticeContent.model_validate(inputs["version"]["normalized_content"])
    item["facts"], item["decision"] = old_json(
        item["facts"],
        item["context"],
        item["decision"],
        content,
        policy["manifest"],
        NoticeContent.model_validate(inputs["previous_version"]["normalized_content"])
        if inputs["previous_version"]
        else None,
    )
    item["input_token"] = canonical_sha256(inputs)
    snapshot["sha256"] = canonical_sha256(snapshot["payload"])
    with env.engine.begin() as connection:
        connection.execute(
            notification_operations.update()
            .where(notification_operations.c.id == "old-operation")
            .values(
                parameters=parameters,
                parameters_sha256=canonical_sha256(parameters),
                snapshot=snapshot,
            )
        )
    monkeypatch.setattr("signalnest.notifications.maintenance.extract_facts", deny_current_rules)
    monkeypatch.setattr("signalnest.notifications.maintenance.decide", deny_current_rules)
    result = reevaluate_events(env.engine, SOURCE, "old-operation", clock=lambda: AT + 30)
    assert result["complete"] and result["results"][0]["status"] == "applied"
    selected = rows(env, notification_decisions)[-1]
    assert selected["decision"] == item["decision"]
    assert selected["facts"] == item["facts"]
    assert selected["decision"]["facts_extractor_version"] == "whu-notice-facts-v1"
    assert len(rows(env, email_outbox)) == (1 if kind == "activation_recent" else 0)
    if kind == "update":
        assert selected["context"]["previous_facts"]["extractor_version"] == "whu-notice-facts-v1"
    repeated = reevaluate_events(env.engine, SOURCE, "old-operation", clock=deny_current_rules)
    assert repeated == result
    before_mail = rows(env, email_outbox)
    update_policy(env.engine, SOURCE, USER_PROFILE, "explicit-v2", at=AT + 40)
    assert rows(env, email_outbox) == before_mail
    assert (
        reevaluate_events(env.engine, SOURCE, "old-operation", clock=deny_current_rules) == result
    )


def test_v1_frozen_bytes_members_and_message_id_survive_explicit_policy_update(
    activated, monkeypatch
):
    env = activated
    live(env, opportunity())
    install_old_seals(env)
    planned = plan_mail(env.engine, SOURCE, PlanOptions(), at=ACTIVATED_AT + 10)
    mail_id = planned["mail_ids"][0]
    before = load_frozen_mail(env.engine, SOURCE, mail_id)
    members = rows(env, mail_message_members)
    assert (
        members[0]["snapshot"]["decision"]["decision_engine_version"] == "notification-decision-v1"
    )
    decisions = rows(env, notification_decisions)
    events = rows(env, notification_events)
    update_policy(env.engine, SOURCE, USER_PROFILE, "publish-v2", at=ACTIVATED_AT + 20)
    monkeypatch.setattr("signalnest.notifications.decision.decide", deny_current_rules)
    monkeypatch.setattr("signalnest.notifications.facts.extract_facts", deny_current_rules)
    after = load_frozen_mail(env.engine, SOURCE, mail_id)
    assert after == before
    assert rows(env, mail_message_members) == members
    assert rows(env, notification_decisions) == decisions
    assert rows(env, notification_events) == events
    assert len(rows(env, mail_messages)) == 1


def test_v1_live_policy_is_explicitly_outdated_then_new_update_reads_previous_v1_decision(
    activated,
):
    env = activated
    body = opportunity()
    live(env, body)
    install_old_seals(env)
    before = rows(env, notification_events)
    with pytest.raises(IngestError, match="notification_policy_outdated"):
        live(env, body, at=ACTIVATED_AT + 10)
    assert rows(env, notification_events) == before
    update_policy(env.engine, SOURCE, USER_PROFILE, "publish-v2", at=ACTIVATED_AT + 20)
    assert rows(env, notification_events) == before
    live(env, body.replace("现启动".encode(), "现正式启动".encode()), at=ACTIVATED_AT + 30)
    after = rows(env, notification_events)
    assert len(after) == 2 and after[-1]["kind"] == "update"
    assert (
        rows(env, notification_decisions)[0]["decision"]["decision_engine_version"]
        == "notification-decision-v1"
    )
    assert (
        rows(env, notification_decisions)[-1]["decision"]["decision_engine_version"]
        == "notification-decision-v10"
    )
