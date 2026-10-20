"""Real CS recruitment and bounded policy-v7 counterexamples, not human gold."""

import hashlib
import json
import runpy
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from test_cs_parsing import NOTICE, capture, changed

from signalnest.contracts import NoticeContent
from signalnest.cs_parsing import parse_cs_notice
from signalnest.notifications.contracts import (
    FACTS_EXTRACTOR_VERSION,
    Action,
    Decision,
    EventContext,
    Profile,
)
from signalnest.notifications.decision import decide, policy_manifest
from signalnest.notifications.facts import extract_facts
from signalnest.notifications.profile import load_profile

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime.fromisoformat("2026-10-10T09:00:00+08:00")


def profile(*, interest=("teaching_assistant",), level="undergraduate", high_value=()):
    # Explicit engineering hypothesis, not verification of the user's status.
    return Profile(
        institution="whu",
        study_level=level,
        role="student",
        college="计算机学院",
        major="人工智能",
        entry_year=2025,
        interest_topics=interest,
        high_value_topics=high_value,
        store_only_topics=("course_enrollment",),
    )


def evaluate(content, chosen=None, now=NOW):
    facts = extract_facts(content)
    result = decide(
        chosen or profile(),
        facts,
        EventContext(next_digest_at=now + timedelta(days=1)),
        now=now,
    )
    return facts, result


def content(title, body):
    return NoticeContent(
        title=title,
        published_date=NOW.date(),
        body_html=f"<p>{body}</p>",
        body_text=body,
    )


@pytest.mark.parametrize("level", ["undergraduate", "master", "doctoral"])
@pytest.mark.parametrize(
    "clock", ["2026-07-14T09:00:00+08:00", "2026-09-19T09:00:00+08:00", "2026-10-10T09:00:00+08:00"]
)
def test_real_recruitment_is_relevant_but_no_branch_or_current_opening_is_confirmed(level, clock):
    notice = parse_cs_notice(capture(NOTICE))
    facts, result = evaluate(notice.content, profile(level=level), datetime.fromisoformat(clock))
    assert facts.category == "opportunity"
    assert result.action == Action.DIGEST and result.effective_route == "digest"
    assert result.relevance == "matched"
    assert result.eligibility == result.time_status == "unknown"
    assert result.needs_review and not facts.eligibility_complete
    assert facts.deadline_at is None and facts.deadline_lower_at is None
    assert facts.opens_at is None and not facts.opening_confirmed
    assert "interest.topic.teaching_assistant" in result.matched_rules
    assert "decision.deadline_soon" not in result.matched_rules
    assert "audience_mismatch" not in result.reason_codes
    assert not any(
        item.field == "study_level" and item.operator != "unsupported" for item in facts.constraints
    )
    assert not any(item.field == "entry_year" for item in facts.constraints)
    # 2025 entry does not imply the official interpretation of 高年级, grades
    # above 85, a completed course or a teacher's permission.
    unknown_proofs = [proof for item in facts.unknowns for proof in item.evidence]
    for phrase in (
        "高年级本科生",
        "85分",
        "考查同意",
        "全日制",
        "9月20日前",
        "2025年7月10日",
        "2026-2027学年",
        "docs.qq.com",
        "QQ群文件",
    ):
        assert any(phrase in proof.excerpt for proof in unknown_proofs)
    assert any(item.code == "recruitment_year_conflict" for item in facts.unknowns)
    assert any(item.code == "year_missing" for item in facts.unknowns)
    assert any(
        item.code == "conflicting_evidence" and item.field == "eligibility"
        for item in facts.unknowns
    )
    assert all(
        getattr(notice.content, proof.field)[proof.start : proof.end] == proof.excerpt
        for proof in unknown_proofs
    )
    title_match = next(
        item for item in facts.topic_matches if item.topic == "teaching_assistant" and item.primary
    )
    assert any("招募本科课程助教" in proof.excerpt for proof in title_match.supporting_evidence)


def test_profile_interest_difference_and_incidental_minor_are_visible():
    notice = parse_cs_notice(capture(NOTICE))
    facts, interested = evaluate(notice.content)
    _, unrelated = evaluate(notice.content, profile(interest=("minor",)))
    assert interested.action == Action.DIGEST
    assert unrelated.action == Action.IGNORE and unrelated.effective_route == "none"
    assert "interest.topic.minor" not in unrelated.matched_rules
    assert any(
        item.topic == "minor" and item.context == "incidental" for item in facts.topic_matches
    )


def test_consistent_signature_still_does_not_supply_missing_deadline_year():
    def edit(tree):
        paragraph = next(
            node
            for node in tree.select(".v_news_content p")
            if node.get_text().strip() == "2025年7月10日"
        )
        paragraph.string = "2026年7月13日"

    amended = parse_cs_notice(changed(capture(NOTICE), edit)).content
    # Explicitly transformed engineering input, not a corrected real capture.
    facts, result = evaluate(amended)
    assert not any(item.code == "recruitment_year_conflict" for item in facts.unknowns)
    assert any(item.code == "year_missing" for item in facts.unknowns)
    assert facts.deadline_at is None and result.action == Action.DIGEST
    assert facts.opening_confirmed


def test_year_conflict_blocks_even_a_supported_clock_and_present_tense_instruction():
    body = (
        "学院现在全院高年级本科生中招募本科课程助教。请登录系统完成报名。"
        "申请截止2026年10月10日16:00。\n计算机学院\n2025年7月10日"
    )
    sample = content("2026-2027学年第一学期助教招募通知", body)
    facts, result = evaluate(sample)
    assert facts.deadline_at is None and not facts.opening_confirmed
    assert result.time_status == "unknown" and result.action == Action.DIGEST
    assert any(item.code == "recruitment_year_conflict" for item in facts.unknowns)
    # Earlier dates in prose are historical references, not a stand-alone signature.
    sample = sample.model_copy(
        update={"body_text": body.replace("\n2025年7月10日", "\n往年通知发表于2025年7月10日。")}
    )
    facts, result = evaluate(sample)
    assert facts.deadline_at == datetime.fromisoformat("2026-10-10T16:00:00+08:00")
    assert facts.opening_confirmed and result.action == Action.PUSH_NOW


def test_new_pair_retains_timely_current_instruction_without_inventing_eligibility():
    sample = content(
        "本科课程助教招募通知",
        "学院现在全院高年级本科生中招募本科课程助教。"
        "请登录系统完成报名，报名截止2026年10月10日16:00。",
    )
    facts, result = evaluate(sample)
    assert result.action == Action.PUSH_NOW and result.effective_route == "immediate"
    assert result.eligibility == "unknown" and result.needs_review
    assert facts.deadline_at == datetime.fromisoformat("2026-10-10T16:00:00+08:00")
    assert "interest.topic.teaching_assistant" in result.matched_rules
    assert "decision.deadline_soon" in result.matched_rules


@pytest.mark.parametrize(
    "title,body",
    [
        (
            "本科课程助教招募通知",
            "登录菜单→本科课程助教报名→查看报名记录。报名截止2026年10月10日16:00。",
        ),
        (
            "本科课程助教招募通知",
            "去年学院现在全院高年级本科生中招募本科课程助教，已于去年完成报名。",
        ),
        ("本科课程助教招募通知", "学院现在不招募本科课程助教。请提交选课缴费申请。"),
        ("本科课程助教津贴发放通知", "请现有助教完成缴费和津贴确认。"),
        ("本科课程优秀助教公告", "公布已担任助教的优秀名单。请现有助教完成考核。"),
        ("新生选课通知", "课程配有助教，辅修专业单独缴费，请登录系统完成选课报名。"),
    ],
)
def test_new_course_pair_cannot_promote_menus_past_administration_or_employee_profiles(title, body):
    facts, result = evaluate(content(title, body))
    assert result.action not in {Action.PUSH_NOW, Action.DIGEST}
    assert result.effective_route == "none"
    assert "interest.topic.teaching_assistant" not in result.matched_rules
    assert not any(
        item.topic == "teaching_assistant" and item.context == "opportunity"
        for item in facts.topic_matches
    )


def test_explicit_hard_restriction_without_conflicting_branch_stays_ineligible():
    sample = content(
        "本科课程助教招募通知",
        "现面向全院硕士研究生招募本科课程助教。请登录系统完成报名。申请截止2026年10月10日16:00。",
    )
    facts, result = evaluate(sample)
    assert result.eligibility == "ineligible" and result.effective_route == "none"
    assert any(
        item.field == "study_level" and item.operator == "one_of" for item in facts.constraints
    )


def test_example_profile_keeps_hypothetical_qualifications_and_current_manifest():
    chosen = load_profile(ROOT / "profile.cs-undergrad.example.toml")
    assert chosen.entry_year is None and chosen.college is None
    assert chosen.institution is None and chosen.major is None
    assert chosen.study_level == "undergraduate"
    assert "teaching_assistant" in chosen.interest_topics
    manifest = policy_manifest(chosen)
    assert manifest["versions"] == {
        "facts_extractor": "whu-notice-facts-v10",
        "rules": "notification-rules-v10",
        "decision_engine": "notification-decision-v10",
        "routing": "notification-routing-v1",
    }
    assert manifest["facts_rules"]["teaching_assistant"]["applicant_object"]
    assert manifest["facts_rules"]["teaching_assistant"]["year_conflict"]


def test_deterministic_decisions_and_compact_v6_compatibility_sample():
    original = parse_cs_notice(capture(NOTICE)).content
    first = evaluate(original)
    assert evaluate(original) == first
    compact = json.loads((ROOT / "tests/fixtures/notification-policy-v6-sample.json").read_bytes())
    decision = Decision.model_validate(compact["decision"])
    assert decision.rules_version == "notification-rules-v6"
    assert decision.model_dump(mode="json") == compact["decision"]
    assert FACTS_EXTRACTOR_VERSION == "whu-notice-facts-v10"


def test_cs_production_evaluation_matches_selected_sanitized_inputs_and_summary():
    script = runpy.run_path(str(ROOT / "deploy/evaluate_notifications.py"))
    manifest = ROOT / "docs/validation/cs-offline-cases.json"
    report = script["evaluate_cases"](manifest)
    current = json.loads((ROOT / "docs/validation/cs-offline-current.json").read_bytes())
    assert report["summary"]["engineering_mismatch_cases"] == []
    assert report["summary"]["human_gold"] is False
    assert report["summary"]["fixture_cases"] == 6
    assert report["summary"]["parse_success"] == 6
    assert report["summary"]["unique_pages"] == 2
    assert (
        report["source_sha256"]["src/signalnest/cs_parsing.py"]
        == hashlib.sha256((ROOT / "src/signalnest/cs_parsing.py").read_bytes()).hexdigest()
    )
    for field in ("source_sha256", "manifest_sha256", "versions", "summary"):
        assert report[field] == current[field]
    assert "results" not in current
