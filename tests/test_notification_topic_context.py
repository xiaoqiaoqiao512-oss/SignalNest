"""Opportunity evidence must belong to the matched topic, rather than a nearby action word.

The immutable WHU pages provide the positive and negative acceptance cases; the
small synthetic notices guard specific ways incidental mentions can look active.
No network, storage or mutable wall clock is involved.
"""

import json
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError

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
FIXTURES = ROOT / "research/fixtures"
SHANGHAI = ZoneInfo("Asia/Shanghai")
NOW = datetime(2026, 10, 5, 12, tzinfo=SHANGHAI)


def profile(**changes):
    values = dict(
        institution="whu",
        study_level="undergraduate",
        role="student",
        interest_topics=("minor", "research"),
        high_value_topics=("minor", "research"),
        store_only_topics=("course_enrollment",),
    )
    return Profile(**(values | changes))


def content(body, *, title="关于本科生选课安排的通知"):
    return NoticeContent(
        title=title,
        published_date=date(2026, 10, 1),
        body_html=f"<p>{body}</p>",
        body_text=body,
    )


def fixture(name, url):
    return parse_notice(PageInput(content=(FIXTURES / name).read_bytes(), page_url=url)).content


def evaluate(notice, *, user=None, at=NOW, context=None):
    facts = extract_facts(notice)
    result = decide(
        user or profile(),
        facts,
        context or EventContext(next_digest_at=at + timedelta(hours=20)),
        now=at,
    )
    return facts, result


def test_real_freshman_course_fee_mention_is_not_a_minor_opportunity():
    notice = fixture("current-notice-detail.html", "https://uc.whu.edu.cn/info/1517/128231.htm")
    at = datetime(2026, 9, 28, 9, tzinfo=SHANGHAI)
    facts, result = evaluate(notice, at=at)
    assert "辅修专业单独缴费的课程，不允许退课" in notice.body_text
    minor_mentions = [match for match in facts.topic_matches if match.topic == "minor"]
    assert minor_mentions and all(match.context == "incidental" for match in minor_mentions)
    assert result.action == Action.STORE_ONLY and result.effective_route == "none"
    assert "interest.topic.minor" not in result.matched_rules
    assert "retention.topic.course_enrollment" in result.matched_rules
    # The rejected text remains inspectable evidence, rather than disappearing.
    for match in minor_mentions:
        proof = match.evidence
        assert getattr(notice, proof.field)[proof.start : proof.end] == proof.excerpt


def test_real_minor_registration_remains_an_urgent_opportunity():
    notice = fixture(
        "notifications/notice-117011-20261005T133549Z.html",
        "https://uc.whu.edu.cn/info/1517/117011.htm",
    )
    facts, result = evaluate(notice, at=datetime(2024, 12, 9, 9, tzinfo=SHANGHAI))
    assert any(
        match.topic == "minor" and match.context == "opportunity" for match in facts.topic_matches
    )
    assert result.action == Action.PUSH_NOW and result.effective_route == "immediate"
    assert result.reason_codes == ("deadline_soon",)
    assert result.eligibility == "unknown" and result.needs_review


def test_real_research_course_still_overrides_a_different_store_only_topic():
    notice = fixture(
        "notifications/notice-128291-20261005T133533Z.html",
        "https://uc.whu.edu.cn/info/1517/128291.htm",
    )
    at = datetime(2026, 9, 28, 9, tzinfo=SHANGHAI)
    facts, result = evaluate(notice, at=at)
    assert any(
        match.topic == "research" and match.context == "opportunity"
        for match in facts.topic_matches
    )
    assert result.action == Action.PUSH_NOW and result.reason_codes == ("deadline_soon",)
    assert "interest.topic.research" in result.matched_rules
    assert "retention.topic.course_enrollment" in result.matched_rules
    _, retained = evaluate(
        notice,
        at=at,
        user=profile(
            interest_topics=("course_enrollment",),
            high_value_topics=(),
        ),
    )
    assert retained.action == Action.STORE_ONLY and retained.effective_route == "none"


@pytest.mark.parametrize(
    "body",
    [
        "选课报名现已开放，辅修专业单独缴费。",
        "辅修专业单独缴费，不允许退课；本科生选课报名现已开放。",
        "选课报名结束后，可申请辅修退课。",
        "学生可申请退出辅修专业学习。",
        "辅修专业缴费申请现已开放。",
        "辅修专业学生可申请补缴学费。",
        "登录→报名申请→辅修报名，查看个人已有报名记录。",
        "通过教学科研菜单进入报名申请，选择辅修报名查看缴费记录。",
        "本次不开展辅修专业报名，现发布选课安排。",
        "本次未开放辅修专业申请。",
        "辅修专业报名不开放，现发布选课安排。",
        "辅修专业报名尚未开放，选课报名现已开放。",
        "去年已开展辅修专业报名，现发布本学期选课安排。",
        "回顾去年辅修专业报名工作，现发布选课安排。",
        "辅修专业报名已结束，本次仅说明课程缴费。",
        "辅修专业申请已经截止，请查看已有记录。",
        "选课报名即日起开放，辅修专业有关报名事项另行通知。",
        "辅修专业收费标准见附件；科研训练现开放报名。",
        "辅修专业单独缴费，科研训练现开放报名。",
    ],
)
def test_incidental_minor_mentions_do_not_enter_digest_even_beside_other_actions(body):
    notice = content(body)
    facts, result = evaluate(
        notice,
        user=profile(interest_topics=("minor",), high_value_topics=("minor",)),
    )
    matches = [match for match in facts.topic_matches if match.topic == "minor"]
    assert matches and all(match.context == "incidental" for match in matches)
    assert result.action == Action.STORE_ONLY and result.effective_route == "none"
    assert "interest.topic.minor" not in result.matched_rules


@pytest.mark.parametrize(
    "title,body",
    [
        ("关于辅修专业缴费的通知", "学生须完成缴费申请。"),
        ("关于辅修专业退出申请的通知", "学生可申请退出辅修学习。"),
        ("辅修专业退课办理说明", "选课报名结束后集中办理退课。"),
    ],
)
def test_minor_administration_title_is_reference_not_a_new_registration(title, body):
    facts, result = evaluate(content(body, title=title))
    assert facts.category == "reference"
    assert result.action == Action.STORE_ONLY and result.effective_route == "none"


@pytest.mark.parametrize(
    "body",
    [
        "现启动辅修专业报名。面向武汉大学本科生。报名时间：2026年10月1日至10月6日。",
        "面向武汉大学本科生，现开放辅修专业申请。报名时间：2026年10月1日至10月6日。",
        "辅修专业单独缴费。现启动辅修专业报名。报名时间：2026年10月1日至10月6日。",
        "本学期辅修专业报名开放。报名时间：2026年10月1日至10月6日。",
        "现启动辅修专业报名，请登录系统完成操作。报名时间：2026年10月1日至10月6日。",
        "现启动辅修专业报名，缴费标准为每学分100元。报名时间：2026年10月1日至10月6日。",
    ],
)
def test_body_only_real_registration_is_active_even_after_an_earlier_fee_mention(body):
    facts, result = evaluate(content(body, title="本科生报名安排通知"))
    assert any(
        match.topic == "minor" and match.context == "opportunity" for match in facts.topic_matches
    )
    assert result.action == Action.PUSH_NOW and result.reason_codes == ("deadline_soon",)
    assert result.effective_route == "immediate"


def test_same_clause_research_action_does_not_activate_minor_but_still_beats_course_retention():
    notice = content(
        "辅修专业单独缴费，科研训练现开放报名。"
        "面向武汉大学本科生。报名时间：2026年10月1日至10月6日。"
    )
    facts, result = evaluate(notice)
    assert all(
        match.context == "incidental" for match in facts.topic_matches if match.topic == "minor"
    )
    assert any(
        match.context == "opportunity" for match in facts.topic_matches if match.topic == "research"
    )
    assert result.action == Action.PUSH_NOW
    assert "interest.topic.research" in result.matched_rules
    assert "interest.topic.minor" not in result.matched_rules


def test_explicit_literal_interest_retains_its_deliberate_broad_matching_semantics():
    notice = content("辅修专业单独缴费。选课报名即日起开放，截止2026年10月6日。")
    _, result = evaluate(
        notice,
        user=profile(interest_topics=(), high_value_topics=(), include_phrases=("辅修",)),
    )
    assert result.action in {Action.PUSH_NOW, Action.DIGEST}
    assert result.effective_route != "none"
    assert "interest.literal_phrase" in result.matched_rules
    assert "interest.topic.minor" not in result.matched_rules


@pytest.mark.parametrize(
    "changed_body,eligibility",
    [
        (
            "面向武汉大学本科生。辅修专业报名已结束。报名截止时间：2026年10月4日。",
            "eligible",
        ),
        (
            "仅限研究生报名。辅修专业申请已关闭。报名截止时间：2026年10月4日。",
            "ineligible",
        ),
    ],
)
def test_followed_opportunity_still_reports_closure_or_tightened_eligibility(
    changed_body, eligibility
):
    previous = extract_facts(
        content(
            "面向武汉大学本科生。现启动辅修专业报名。报名时间：2026年10月1日至10月6日。",
            title="关于辅修专业报名的通知",
        )
    )
    changed = content(changed_body, title="辅修专业报名后续事项通知")
    facts, result = evaluate(
        changed,
        context=EventContext(
            kind="update",
            previous_facts=previous,
            previous_action=Action.PUSH_NOW,
            previous_effective_route="immediate",
            next_digest_at=NOW + timedelta(hours=20),
        ),
    )
    assert facts.deadline_at < NOW
    assert result.eligibility == eligibility
    assert result.action == Action.DIGEST and result.effective_route == "digest"
    assert result.reason_codes == ("conditions_changed",)


def test_followed_conditions_change_is_not_suppressed_by_another_store_only_title():
    previous = extract_facts(
        content(
            "面向武汉大学本科生。现启动辅修专业报名。报名时间：2026年10月1日至10月6日。",
            title="辅修专业报名通知",
        )
    )
    changed = content(
        "辅修专业报名已结束。仅限研究生报名。报名截止时间：2026年10月4日。",
        title="选课安排更新通知",
    )
    _, result = evaluate(
        changed,
        context=EventContext(
            kind="update",
            previous_facts=previous,
            previous_action=Action.PUSH_NOW,
            previous_effective_route="immediate",
            next_digest_at=NOW + timedelta(hours=20),
        ),
    )
    assert result.eligibility == "ineligible"
    assert "retention.topic.course_enrollment" in result.matched_rules
    assert result.action == Action.DIGEST and result.effective_route == "digest"
    assert result.reason_codes == ("conditions_changed",)


@pytest.mark.parametrize("revision", ["v1", "v2"])
def test_old_topic_context_is_unknown_without_rewriting_persisted_snapshots_or_hashes(revision):
    snapshot = json.loads(
        (ROOT / f"tests/fixtures/notification-policy-{revision}.json").read_text()
    )
    facts = NoticeFacts.model_validate(snapshot["facts"])
    stored = Decision.model_validate(snapshot["decision"])
    assert facts.model_dump(mode="json") == snapshot["facts"]
    assert all(match.context is None for match in facts.topic_matches)
    assert all("context" not in match.model_dump(mode="json") for match in facts.topic_matches)
    assert facts.sha256() == snapshot["facts_sha256"] == stored.facts_sha256
    assert stored.model_dump(mode="json") == snapshot["decision"]
    assert canonical_sha256(snapshot["manifest"]) == stored.policy_sha256
    with pytest.raises(ValueError, match="re-extract"):
        decide(
            Profile.model_validate(snapshot["profile"]),
            facts,
            EventContext(next_digest_at=stored.evaluated_at + timedelta(days=1)),
            now=stored.evaluated_at,
        )


def test_current_policy_versions_change_without_changing_content_hash_contract():
    notice = content("辅修专业单独缴费。")
    facts = extract_facts(notice)
    assert facts.content_sha256 == notice.content_sha256()
    assert policy_manifest(profile())["versions"] == {
        "facts_extractor": "whu-notice-facts-v10",
        "rules": "notification-rules-v10",
        "decision_engine": "notification-decision-v10",
        "routing": "notification-routing-v1",
    }


@pytest.mark.parametrize("context", ["subject", "opportunity", "incidental"])
def test_explicit_topic_context_is_serialized_and_part_of_fact_hash(context):
    proof = Evidence(field="body_text", start=0, end=2, excerpt="辅修")
    old = TopicMatch(topic="minor", evidence=proof)
    explicit = TopicMatch(topic="minor", evidence=proof, context=context)
    assert "context" not in old.model_dump(mode="json")
    assert explicit.model_dump(mode="json")["context"] == context
    assert canonical_sha256(old.model_dump(mode="json")) != canonical_sha256(
        explicit.model_dump(mode="json")
    )


@pytest.mark.parametrize("context", ["active", "keyword", "", True, 1])
def test_invalid_topic_context_is_rejected(context):
    with pytest.raises(ValidationError):
        TopicMatch(
            topic="minor",
            evidence=Evidence(field="body_text", start=0, end=2, excerpt="辅修"),
            context=context,
        )
