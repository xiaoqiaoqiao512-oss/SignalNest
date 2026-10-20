"""Preserve actual v8 serialized evidence and mail across an explicit v9 update.

The standalone seal was produced by executing archived HEAD v8 code in an
isolated PYTHONPATH. SQLite setup below instead converts an unchanged exchange
example into internally consistent v8 records: synthetic compatibility setup,
not an execution or snapshot of the old deployment. No network or mail is sent.
"""

import hashlib
import json
from copy import deepcopy
from datetime import datetime
from pathlib import Path

import pytest
import test_notification_policy_v1_compatibility as legacy
from test_notification_service import ACTIVATED_AT, SOURCE, USER_PROFILE, live, opportunity, rows
from test_notification_service import activated as activated

from signalnest.errors import IngestError
from signalnest.mail.contracts import PlanOptions
from signalnest.mail.planning import load_frozen_mail, plan_mail
from signalnest.notifications.contracts import (
    Decision,
    EventContext,
    NoticeFacts,
    Profile,
    canonical_sha256,
)
from signalnest.notifications.decision import decide, policy_manifest
from signalnest.notifications.maintenance import update_policy
from signalnest.schema import (
    email_outbox,
    mail_message_members,
    mail_messages,
    notification_decisions,
    notification_events,
)

FIXTURE = Path(__file__).parent / "fixtures/notification-policy-v8.json"
V8 = json.loads(FIXTURE.read_text())
V8_VERSIONS = dict(V8["manifest"]["versions"])


def v8_exchange_json(facts, context, decision, content, manifest, previous_content=None):
    """Synthetic version/hash conversion only for the unchanged exchange template."""
    full = deepcopy(facts) | dict(
        body_text=content.body_text, extractor_version=V8_VERSIONS["facts_extractor"]
    )
    full_context = deepcopy(context)
    previous = full_context.get("previous_facts")
    if previous is not None:
        assert previous_content is not None
        previous = previous | dict(
            body_text=previous_content.body_text,
            extractor_version=V8_VERSIONS["facts_extractor"],
        )
        full_context["previous_facts"] = previous
        context["previous_facts"] = {
            key: value for key, value in previous.items() if key != "body_text"
        }
    profile, policy_hash = manifest["profile"], canonical_sha256(manifest)
    decision = decision | dict(
        facts_extractor_version=V8_VERSIONS["facts_extractor"],
        rules_version=V8_VERSIONS["rules"],
        decision_engine_version=V8_VERSIONS["decision_engine"],
        routing_version=V8_VERSIONS["routing"],
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


def install_synthetic_v8_seals(env, monkeypatch):
    """Use actual v8 manifest plus explicit synthetic conversion in real SQLite."""
    monkeypatch.setattr(legacy, "V1", V8)
    monkeypatch.setattr(legacy, "V1_VERSIONS", V8_VERSIONS)
    monkeypatch.setattr(legacy, "old_json", v8_exchange_json)
    legacy.install_old_seals(env)
    assert env.engine.dialect.name == "sqlite"
    assert Path(env.engine.url.database).is_file()


def deny_current_rules(*_args, **_kwargs):
    raise AssertionError("reading frozen v8 mail must not evaluate current rules")


def test_actual_archived_v8_seal_retains_serialized_facts_decision_and_original_hashes():
    assert hashlib.sha256(FIXTURE.read_bytes()).hexdigest() == (
        "b4381a9dab36176c1a9d59eb1a7cdad940ab44c4c0847b30682f2bd2ea824586"
    )
    assert V8["provenance"]["git_head"] == "e90023b01e4d8396aa5c3a3682c6207553c5a7d3"
    assert V8["provenance"]["source_sha256"]["src/signalnest/notifications/facts.py"] == (
        "abfc463d4da5f9b81f7f3d0e52b7bbcfd045e3d5936300d3a0bd74a10c35638b"
    )
    profile = Profile.model_validate(V8["profile"])
    facts = NoticeFacts.model_validate(V8["facts"])
    decision = Decision.model_validate(V8["decision"])
    assert profile.model_dump(mode="json") == V8["profile"]
    assert facts.model_dump(mode="json") == V8["facts"]
    assert decision.model_dump(mode="json") == V8["decision"]
    assert profile.sha256() == V8["profile_sha256"] == decision.profile_sha256
    assert facts.sha256() == V8["facts_sha256"] == decision.facts_sha256
    assert canonical_sha256(V8["manifest"]) == V8["manifest_sha256"] == decision.policy_sha256
    assert (
        canonical_sha256(
            dict(
                profile=V8["profile"],
                facts=V8["facts"],
                context=V8["context"],
                evaluated_at=decision.evaluated_at.isoformat(),
                policy_sha256=decision.policy_sha256,
            )
        )
        == decision.input_sha256
    )
    assert decision.rules_version == "notification-rules-v8"
    assert policy_manifest(profile)["versions"]["rules"] == "notification-rules-v10"
    assert canonical_sha256(policy_manifest(profile)) != decision.policy_sha256
    with pytest.raises(ValueError, match="re-extract"):
        decide(
            profile,
            facts,
            EventContext.model_validate(V8["context"]),
            now=decision.evaluated_at,
        )


def test_real_sqlite_frozen_v8_mail_survives_explicit_v9_policy_update(activated, monkeypatch):
    env = activated
    live(env, opportunity())
    install_synthetic_v8_seals(env, monkeypatch)
    planned = plan_mail(env.engine, SOURCE, PlanOptions(), at=ACTIVATED_AT + 10)
    before = load_frozen_mail(env.engine, SOURCE, planned["mail_ids"][0])
    assert before.rendered.payload and before.rendered.message_id
    assert hashlib.sha256(before.rendered.payload).hexdigest() == before.rendered.payload_sha256
    members = rows(env, mail_message_members)
    assert members[0]["snapshot"]["decision"]["rules_version"] == "notification-rules-v8"
    decisions, events, outbox = (
        rows(env, notification_decisions),
        rows(env, notification_events),
        rows(env, email_outbox),
    )
    update_policy(env.engine, SOURCE, USER_PROFILE, "explicit-v9", at=ACTIVATED_AT + 20)
    monkeypatch.setattr("signalnest.notifications.decision.decide", deny_current_rules)
    monkeypatch.setattr("signalnest.notifications.facts.extract_facts", deny_current_rules)
    after = load_frozen_mail(env.engine, SOURCE, before.mail_id)
    assert after == before
    assert after.rendered.payload == before.rendered.payload
    assert after.rendered.message_id == before.rendered.message_id
    assert after.members_json == before.members_json
    assert rows(env, mail_message_members) == members
    assert rows(env, notification_decisions) == decisions
    assert rows(env, notification_events) == events
    assert rows(env, email_outbox) == outbox
    assert len(rows(env, mail_messages)) == 1


def test_real_sqlite_v8_live_policy_requires_explicit_update_without_rewriting_old_decisions(
    activated, monkeypatch
):
    env = activated
    body = opportunity()
    live(env, body)
    install_synthetic_v8_seals(env, monkeypatch)
    events, decisions = rows(env, notification_events), rows(env, notification_decisions)
    with pytest.raises(IngestError, match="notification_policy_outdated"):
        live(env, body, at=ACTIVATED_AT + 10)
    assert rows(env, notification_events) == events
    assert rows(env, notification_decisions) == decisions
    update_policy(env.engine, SOURCE, USER_PROFILE, "explicit-v9", at=ACTIVATED_AT + 20)
    assert rows(env, notification_events) == events
    assert rows(env, notification_decisions) == decisions
    live(env, body.replace("现启动".encode(), "现正式启动".encode()), at=ACTIVATED_AT + 30)
    after_events, after_decisions = (
        rows(env, notification_events),
        rows(env, notification_decisions),
    )
    assert len(after_events) == 2 and after_events[-1]["kind"] == "update"
    assert after_decisions[0] == decisions[0]
    assert after_decisions[0]["decision"]["rules_version"] == "notification-rules-v8"
    assert after_decisions[-1]["decision"]["rules_version"] == "notification-rules-v10"
