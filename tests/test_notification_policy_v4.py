"""N0 title/action linkage and real submission instructions, with local evidence.

These small contracts are engineering expectations, not human relevance labels.
They isolate the two v3 misses without weakening the witnessed incidental cases.
"""

import json
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from signalnest.contracts import NoticeContent, PageInput
from signalnest.notifications.contracts import (
    Action,
    Decision,
    EventContext,
    Evidence,
    NoticeFacts,
    Profile,
    TopicMatch,
    canonical_sha256,
)
from signalnest.notifications.decision import decide, policy_manifest
from signalnest.notifications.facts import extract_facts
from signalnest.parsing import parse_notice

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 10, 8, 9, tzinfo=ZoneInfo("Asia/Shanghai"))
DEADLINE = "截止2026年10月8日23:59。"


def notice(body, *, title="科研训练项目通知"):
    return NoticeContent(
        title=title,
        published_date=date(2026, 10, 8),
        body_text=body,
        body_html=f"<p>{body}</p>",
    )


def evaluate(content, *, interest_topics=("research",), **profile_changes):
    profile = Profile(
        institution="whu",
        study_level="undergraduate",
        role="student",
        interest_topics=interest_topics,
        **profile_changes,
    )
    facts = extract_facts(content)
    decision = decide(
        profile,
        facts,
        EventContext(next_digest_at=NOW + timedelta(hours=23)),
        now=NOW,
    )
    return facts, decision


def assert_evidence(content, items):
    for item in items:
        proofs = item.evidence if isinstance(item.evidence, tuple) else (item.evidence,)
        proofs += getattr(item, "supporting_evidence", ())
        for proof in proofs:
            assert proof.field in {"title", "body_text"}
            assert getattr(content, proof.field)[proof.start : proof.end] == proof.excerpt


def test_unique_title_subject_links_to_current_generic_body_registration():
    content = notice("面向武汉大学本科生。即日起报名，" + DEADLINE)
    facts, decision = evaluate(content)
    assert decision.action == Action.PUSH_NOW
    assert decision.reason_codes == ("deadline_soon",)
    assert decision.effective_route == "immediate" and decision.time_status == "open"
    assert any(
        match.topic == "research" and match.context == "opportunity"
        for match in facts.topic_matches
    )
    supporting = [
        proof
        for match in facts.topic_matches
        if match.topic == "research" and match.context == "opportunity"
        for proof in match.supporting_evidence
    ]
    assert supporting and all(proof.field == "body_text" for proof in supporting)
    assert any("报名" in proof.excerpt for proof in supporting)
    assert_evidence(content, facts.topic_matches)
    assert not any(item.code == "topic_action_link_unknown" for item in facts.unknowns)


def test_submission_instruction_is_not_discarded_as_system_navigation():
    content = notice(
        "面向武汉大学本科生。即日起，请登录系统完成科研训练报名，" + DEADLINE,
        title="本科生报名通知",
    )
    facts, decision = evaluate(content)
    assert decision.action == Action.PUSH_NOW and decision.effective_route == "immediate"
    assert decision.reason_codes == ("deadline_soon",)
    assert any(
        match.topic == "research" and match.context == "opportunity"
        for match in facts.topic_matches
    )
    assert_evidence(content, facts.topic_matches)


@pytest.mark.parametrize(
    "body",
    [
        "即日起，请登录系统完成科研训练报名。",
        "即日起，请登录系统提交科研训练申请。",
        "即日起报名，请登录系统完成科研训练报名。",
    ],
)
def test_live_submission_steps_remain_opportunities(body):
    facts, decision = evaluate(notice("面向武汉大学本科生。" + body + DEADLINE))
    assert decision.action == Action.PUSH_NOW and decision.time_status == "open"
    assert any(
        match.topic == "research" and match.context == "opportunity"
        for match in facts.topic_matches
    )


@pytest.mark.parametrize(
    "body",
    [
        "登录系统，菜单路径：科研训练→报名申请→查看。",
        "菜单路径：登录→科研训练报名→查看已有记录。",
        "登录系统查看已有科研训练报名记录。",
        "点击科研训练报名查看已有记录。",
        "登录系统查看去年科研训练报名记录。",
        "科研训练报名已结束，请登录系统查看记录。",
        "去年已完成科研训练报名，本次仅供查询。",
        "科研训练报名尚未开放，请查看历史记录。",
        "科研训练报名缴费说明：请登录系统完成缴费。",
        "科研训练退课申请请登录系统办理。",
    ],
)
def test_navigation_administration_and_history_do_not_become_urgent(body):
    content = notice(body + DEADLINE, title="本科生信息查询通知")
    facts, decision = evaluate(content)
    research = [match for match in facts.topic_matches if match.topic == "research"]
    assert research and all(match.context == "incidental" for match in research)
    assert decision.action not in {Action.PUSH_NOW, Action.DIGEST}
    assert decision.effective_route == "none"
    assert not any(item.code == "topic_action_link_unknown" for item in facts.unknowns)
    assert_evidence(content, research)


@pytest.mark.parametrize(
    "body",
    [
        "登录系统查看已有报名记录。",
        "菜单路径：登录→报名申请→查询。",
        "报名缴费说明：请登录系统完成缴费。",
        "报名已结束，现办理退课申请。",
        "去年已完成报名，本次供查询参考。",
    ],
)
def test_title_fallback_does_not_promote_clear_non_opportunity(body):
    facts, decision = evaluate(notice("面向武汉大学本科生。" + body + DEADLINE))
    assert not any(match.context == "opportunity" for match in facts.topic_matches)
    assert decision.action != Action.PUSH_NOW
    assert not any(item.code == "topic_action_link_unknown" for item in facts.unknowns)


@pytest.mark.parametrize("other", ["辅修专业", "选课"])
def test_title_research_cannot_borrow_another_topics_registration_or_deadline(other):
    content = notice("面向武汉大学本科生。即日起开放" + other + "报名，" + DEADLINE)
    facts, decision = evaluate(content)
    assert not any(
        match.topic == "research" and match.context == "opportunity"
        for match in facts.topic_matches
    )
    assert decision.action != Action.PUSH_NOW and decision.effective_route != "immediate"
    assert not any(item.code == "topic_action_link_unknown" for item in facts.unknowns)


def test_separate_generic_instruction_does_not_hide_a_competing_named_opportunity():
    content = notice("面向武汉大学本科生。现开放辅修专业报名。即日起报名，" + DEADLINE)
    facts, decision = evaluate(content)
    unknowns = [item for item in facts.unknowns if item.code == "topic_action_link_unknown"]
    assert unknowns and decision.needs_review
    assert decision.action == Action.STORE_ONLY and decision.relevance == "unknown"
    assert decision.effective_route == "none"
    assert any(
        match.topic == "research" and match.context == "uncertain" for match in facts.topic_matches
    )
    assert any(
        "辅修" in proof.excerpt and "报名" in proof.excerpt
        for item in unknowns
        for proof in item.evidence
    )
    assert_evidence(content, unknowns)


def test_incidental_minor_fee_does_not_compete_with_unique_research_title_subject():
    content = notice("面向武汉大学本科生。辅修专业单独缴费。即日起报名，" + DEADLINE)
    facts, decision = evaluate(content)
    assert decision.action == Action.PUSH_NOW and decision.reason_codes == ("deadline_soon",)
    assert all(
        match.context == "incidental" for match in facts.topic_matches if match.topic == "minor"
    )
    assert any(
        match.topic == "research" and match.context == "opportunity"
        for match in facts.topic_matches
    )
    assert not any(item.code == "topic_action_link_unknown" for item in facts.unknowns)


def test_unambiguous_research_course_still_beats_another_topics_retention():
    content = notice(
        "面向武汉大学本科生。即日起，请登录系统完成科研训练课程报名，" + DEADLINE,
        title="科研训练课程选课通知",
    )
    facts, decision = evaluate(content, store_only_topics=("course_enrollment",))
    assert any(
        match.topic == "research" and match.context == "opportunity"
        for match in facts.topic_matches
    )
    assert decision.action == Action.PUSH_NOW
    assert "interest.topic.research" in decision.matched_rules
    assert "retention.topic.course_enrollment" in decision.matched_rules


def test_generic_action_with_multiple_title_topics_preserves_uncertain_link_evidence():
    content = notice(
        "面向武汉大学本科生。即日起报名，" + DEADLINE,
        title="科研训练与辅修专业安排通知",
    )
    facts, decision = evaluate(content)
    unknowns = [item for item in facts.unknowns if item.code == "topic_action_link_unknown"]
    assert unknowns and all(item.evidence for item in unknowns)
    proofs = [proof for item in unknowns for proof in item.evidence]
    assert {proof.field for proof in proofs} == {"title", "body_text"}
    assert any(match.context == "uncertain" for match in facts.topic_matches)
    assert decision.action == Action.STORE_ONLY and decision.relevance == "unknown"
    assert decision.effective_route == "none" and decision.needs_review
    assert any(item.code == "topic_action_link_unknown" for item in decision.unknowns)
    assert_evidence(content, unknowns)


def test_uncertain_body_topic_is_retained_when_it_is_the_users_only_interest():
    content = notice(
        "面向武汉大学本科生。科研训练相关安排见下文。即日起报名，" + DEADLINE,
        title="本科生报名安排通知",
    )
    facts, decision = evaluate(content)
    unknowns = [item for item in facts.unknowns if item.code == "topic_action_link_unknown"]
    assert unknowns and decision.needs_review
    assert any(
        match.topic == "research" and match.context == "uncertain" for match in facts.topic_matches
    )
    assert decision.action == Action.STORE_ONLY and decision.relevance == "unknown"
    assert decision.effective_route == "none"
    assert_evidence(content, unknowns)


@pytest.mark.parametrize(
    "previous_route,comparison_known,expected_action,reason",
    [
        ("none", True, Action.STORE_ONLY, "topic_relation_unknown"),
        ("immediate", True, Action.DIGEST, "conditions_changed"),
        ("immediate", False, Action.DIGEST, "update_comparison_unknown"),
    ],
)
def test_uncertain_link_preserves_followed_opportunity_updates_without_borrowing_urgency(
    previous_route, comparison_known, expected_action, reason
):
    previous = extract_facts(
        notice(
            "面向武汉大学本科生。现开放科研训练报名。报名截止2026年10月10日。",
            title="科研训练项目报名通知",
        )
    )
    changed = extract_facts(
        notice(
            "面向武汉大学本科生。即日起报名，" + DEADLINE,
            title="科研训练与辅修专业安排通知",
        )
    )
    decision = decide(
        Profile(
            institution="whu",
            study_level="undergraduate",
            role="student",
            interest_topics=("research",),
        ),
        changed,
        EventContext(
            kind="update",
            previous_facts=previous,
            previous_action=Action.PUSH_NOW,
            previous_effective_route=previous_route,
            comparison_known=comparison_known,
            next_digest_at=NOW + timedelta(hours=23),
        ),
        now=NOW,
    )
    assert decision.action == expected_action
    assert decision.reason_codes == (reason,)
    assert decision.relevance == "unknown" and decision.needs_review
    assert decision.effective_route == ("digest" if previous_route != "none" else "none")
    assert decision.time_status == "open" and changed.deadline_at.date() == NOW.date()


def test_informational_title_without_action_does_not_invent_unknown_relationship():
    facts, decision = evaluate(notice("介绍科研训练项目的背景与目标。"))
    assert decision.action == Action.DIGEST
    assert not any(item.code == "topic_action_link_unknown" for item in facts.unknowns)
    assert all(match.context != "opportunity" for match in facts.topic_matches)


def test_separate_unrelated_deadline_does_not_upgrade_uncertain_research_relation():
    content = notice(
        "科研训练相关资料见附件。辅修专业即日起报名，" + DEADLINE,
        title="本科生事项安排通知",
    )
    _, decision = evaluate(content)
    assert decision.action != Action.PUSH_NOW and decision.effective_route != "immediate"


def test_uncertain_context_is_explicit_and_changes_fact_hash():
    proof = Evidence(field="body_text", start=0, end=4, excerpt="科研训练")
    subject = TopicMatch(topic="research", evidence=proof, context="subject")
    uncertain = TopicMatch(topic="research", evidence=proof, context="uncertain")
    assert uncertain.model_dump(mode="json")["context"] == "uncertain"
    assert canonical_sha256(subject.model_dump(mode="json")) != canonical_sha256(
        uncertain.model_dump(mode="json")
    )


def test_fixed_explicit_inputs_are_deterministic_for_linked_and_uncertain_notices():
    for title in ("科研训练项目通知", "科研训练与辅修安排通知"):
        content = notice("面向武汉大学本科生。即日起报名，" + DEADLINE, title=title)
        facts, decision = evaluate(content)
        again_facts, again_decision = evaluate(content)
        assert again_facts == facts and again_facts.sha256() == facts.sha256()
        assert again_decision == decision and again_decision.input_sha256 == decision.input_sha256


def test_current_versions_are_explicit_without_changing_content_or_routing_contract():
    content = notice("面向武汉大学本科生。即日起报名，" + DEADLINE)
    facts, decision = evaluate(content)
    assert facts.content_sha256 == content.content_sha256()
    assert facts.extractor_version == "whu-notice-facts-v10"
    assert decision.rules_version == "notification-rules-v10"
    assert decision.decision_engine_version == "notification-decision-v10"
    assert decision.routing_version == "notification-routing-v1"
    assert policy_manifest(Profile(interest_topics=("research",)))["versions"] == {
        "facts_extractor": "whu-notice-facts-v10",
        "rules": "notification-rules-v10",
        "decision_engine": "notification-decision-v10",
        "routing": "notification-routing-v1",
    }


@pytest.mark.parametrize("version", ["v3", "v4", "v5", "v7"])
def test_saved_facts_and_decisions_remain_readable_without_current_re_evaluation(version):
    snapshot = json.loads(
        (ROOT / f"docs/validation/notification-production-{version}.json").read_text()
    )
    assert snapshot["versions"]["rules"] == f"notification-rules-{version}"
    for saved in snapshot["results"]:
        body = parse_notice(
            PageInput(
                content=(ROOT / saved["fixture"]).read_bytes(),
                page_url=saved["source_url"],
            )
        ).content.body_text
        facts = NoticeFacts.model_validate(saved["facts"] | {"body_text": body})
        decision = Decision.model_validate(saved["decision"])
        full = facts.model_dump(mode="json")
        full.pop("body_text")
        assert full == saved["facts"]
        assert facts.sha256() == decision.facts_sha256
        assert decision.model_dump(mode="json") == saved["decision"]
        with pytest.raises(ValueError, match="re-extract"):
            decide(
                Profile.model_validate(saved["profile"]),
                facts,
                EventContext.model_validate(saved["context"]),
                now=datetime.fromisoformat(saved["evaluated_at"]),
            )


@pytest.mark.parametrize("version", ["v3", "v4", "v5", "v7"])
def test_full_sealed_snapshot_retains_exact_original_hashes_and_versions(version):
    snapshot = json.loads((ROOT / f"tests/fixtures/notification-policy-{version}.json").read_text())
    profile = Profile.model_validate(snapshot["profile"])
    facts = NoticeFacts.model_validate(snapshot["facts"])
    saved = Decision.model_validate(snapshot["decision"])
    assert profile.model_dump(mode="json") == snapshot["profile"]
    assert profile.sha256() == snapshot["profile_sha256"] == saved.profile_sha256
    assert facts.model_dump(mode="json") == snapshot["facts"]
    assert facts.sha256() == snapshot["facts_sha256"] == saved.facts_sha256
    assert saved.model_dump(mode="json") == snapshot["decision"]
    assert canonical_sha256(snapshot["manifest"]) == snapshot["manifest_sha256"]
    assert snapshot["manifest_sha256"] == saved.policy_sha256
    assert saved.rules_version == f"notification-rules-{version}"
    assert all("supporting_evidence" not in match.model_dump() for match in facts.topic_matches)
