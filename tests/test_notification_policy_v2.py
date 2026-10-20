"""Real immutable samples and conservative v2 rules, including v1 snapshot compatibility."""

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
    NoticeFacts,
    Profile,
    canonical_sha256,
)
from signalnest.notifications.decision import decide, policy_manifest
from signalnest.notifications.facts import extract_facts
from signalnest.parsing import parse_notice

FIXTURES = Path(__file__).resolve().parents[1] / "research/fixtures/notifications"
SHANGHAI = ZoneInfo("Asia/Shanghai")


def sample(identity):
    path = next(FIXTURES.glob(f"notice-{identity}-*.html"))
    metadata = json.loads(path.with_suffix(".json").read_text())
    return parse_notice(
        PageInput(content=path.read_bytes(), page_url=metadata["final_url"])
    ).content


def evaluate(content, at, **profile_values):
    profile = Profile(
        institution="whu",
        study_level="undergraduate",
        interest_topics=("research", "minor", "competition"),
        **profile_values,
    )
    now = datetime.fromisoformat(at)
    facts = extract_facts(content)
    return facts, decide(
        profile, facts, EventContext(next_digest_at=now + timedelta(hours=20)), now=now
    )


def notice(body):
    return NoticeContent(
        title="国际交流项目报名通知",
        published_date=date(2026, 10, 1),
        body_text=body,
        body_html=f"<p>{body}</p>",
    )


def codes(facts):
    return {item.code for item in facts.unknowns}


def test_research_interest_wins_over_course_store_only_in_real_sample():
    content = sample("128291")
    facts, active = evaluate(
        content, "2026-09-28T09:00:00+08:00", store_only_topics=("course_enrollment",)
    )
    assert active.action == Action.PUSH_NOW
    assert active.reason_codes == ("deadline_soon",)
    assert active.eligibility == "unknown" and active.needs_review
    assert facts.deadline_at == datetime(2026, 9, 28, 23, 59, tzinfo=SHANGHAI)
    retained = decide(
        Profile(interest_topics=("course_enrollment",), store_only_topics=("course_enrollment",)),
        facts,
        EventContext(next_digest_at=active.evaluated_at + timedelta(hours=20)),
        now=active.evaluated_at,
    )
    assert retained.action == Action.STORE_ONLY and retained.effective_route == "none"
    no_store = decide(
        Profile(interest_topics=("research",)),
        facts,
        EventContext(next_digest_at=active.evaluated_at + timedelta(hours=20)),
        now=active.evaluated_at,
    )
    assert no_store.action == active.action


def test_explicit_literal_interest_is_not_suppressed_by_a_store_topic():
    content = sample("128291")
    facts = extract_facts(content)
    now = datetime(2026, 9, 28, 9, tzinfo=SHANGHAI)
    result = decide(
        Profile(include_phrases=("科研训练",), store_only_topics=("course_enrollment",)),
        facts,
        EventContext(next_digest_at=now + timedelta(days=1)),
        now=now,
    )
    assert result.action == Action.PUSH_NOW
    assert "interest.literal_phrase" in result.matched_rules


def test_real_first_minor_round_has_inherited_year_and_exact_next_day_midnight():
    facts, result = evaluate(sample("117011"), "2024-12-09T09:00:00+08:00", role="student")
    assert facts.opens_at == datetime(2024, 12, 2, 9, tzinfo=SHANGHAI)
    assert facts.deadline_at == datetime(2024, 12, 10, tzinfo=SHANGHAI)
    assert facts.deadline_lower_at is None
    assert "secondary_time_unknown" in codes(facts)
    assert "year_missing" not in codes(facts)
    assert result.action == Action.PUSH_NOW and result.time_status == "open"
    assert result.eligibility == "unknown" and result.needs_review


def test_real_project_team_deadline_is_not_replaced_by_advisor_or_college_deadline():
    facts, result = evaluate(sample("17361"), "2024-02-24T09:00:00+08:00", role="student")
    assert facts.deadline_lower_at == datetime(2024, 2, 25, tzinfo=SHANGHAI)
    assert facts.deadline_at == datetime(2024, 2, 25, 23, 59, 59, tzinfo=SHANGHAI)
    assert facts.opens_at is None and facts.opening_confirmed
    assert "imprecise_deadline" in codes(facts)
    assert "project_membership" in {item.field for item in facts.unknowns}
    assert result.action == Action.PUSH_NOW and result.needs_review
    assert result.eligibility == "unknown"


def test_imprecise_before_date_does_not_pretend_the_deadline_has_passed_at_day_start():
    facts, result = evaluate(sample("17361"), "2024-02-25T12:00:00+08:00")
    assert result.time_status == "open" and result.action == Action.PUSH_NOW
    assert "imprecise_deadline" in {item.code for item in result.unknowns}
    _, after = evaluate(sample("17361"), "2024-02-26T00:00:00+08:00")
    assert after.time_status == "closed" and after.action == Action.IGNORE
    assert facts.deadline_lower_at < result.evaluated_at < facts.deadline_at


@pytest.mark.parametrize(
    "now,next_digest",
    [
        ("2024-02-24T09:00:00+08:00", "2024-02-25T09:00:00+08:00"),
        ("2024-02-25T12:00:00+08:00", "2024-02-26T09:00:00+08:00"),
    ],
)
def test_activation_recent_imprecise_cutoff_does_not_wait_past_possible_deadline(now, next_digest):
    facts = extract_facts(sample("17361"))
    profile = Profile(interest_topics=("research",), role="student")
    result = decide(
        profile,
        facts,
        EventContext(kind="activation_recent", next_digest_at=datetime.fromisoformat(next_digest)),
        now=datetime.fromisoformat(now),
    )
    assert result.action == Action.PUSH_NOW and result.effective_route == "immediate"
    assert result.routing_reason_codes == ("activation_urgency_exception",)
    assert result.time_status == "open" and result.needs_review
    assert "imprecise_deadline" in {item.code for item in result.unknowns}


def test_lower_boundary_within_72_hours_is_urgent_even_when_upper_boundary_is_later():
    facts, result = evaluate(sample("17361"), "2024-02-22T12:00:00+08:00")
    assert facts.deadline_lower_at - result.evaluated_at < timedelta(hours=72)
    assert facts.deadline_at - result.evaluated_at > timedelta(hours=72)
    assert result.action == Action.PUSH_NOW and result.reason_codes == ("deadline_soon",)
    assert result.needs_review
    precise = facts.model_copy(update={"deadline_lower_at": None})
    ordinary = decide(
        Profile(interest_topics=("research",)),
        precise,
        EventContext(next_digest_at=result.evaluated_at + timedelta(days=1)),
        now=result.evaluated_at,
    )
    assert ordinary.action == Action.DIGEST


def test_imprecise_activation_after_upper_boundary_has_no_urgency_exception():
    now = datetime(2024, 2, 26, tzinfo=SHANGHAI)
    result = decide(
        Profile(interest_topics=("research",)),
        extract_facts(sample("17361")),
        EventContext(kind="activation_recent", next_digest_at=now + timedelta(hours=9)),
        now=now,
    )
    assert result.time_status == "closed"
    assert result.action == Action.STORE_ONLY and result.effective_route == "none"


def test_an_uncertain_deadline_contract_always_requires_review_even_without_an_unknown_item():
    facts = extract_facts(
        notice("面向武汉大学本科生。报名时间：2026年10月1日至2026年10月6日。")
    ).model_copy(update={"deadline_lower_at": datetime(2026, 10, 6, tzinfo=SHANGHAI)})
    assert not facts.unknowns and facts.eligibility_complete
    now = datetime(2026, 10, 5, 12, tzinfo=SHANGHAI)
    result = decide(
        Profile(institution="whu", study_level="undergraduate", interest_topics=("exchange",)),
        facts,
        EventContext(next_digest_at=now + timedelta(days=1)),
        now=now,
    )
    assert result.action == Action.PUSH_NOW and result.eligibility == "eligible"
    assert result.needs_review
    assert "imprecise_deadline" in {item.code for item in result.unknowns}


@pytest.mark.parametrize(
    "role,eligibility", [("student", "ineligible"), ("faculty", "unknown"), (None, "unknown")]
)
def test_current_faculty_applicant_is_distinct_from_beneficiary_students(role, eligibility):
    facts, result = evaluate(sample("14147"), "2022-05-14T09:00:00+08:00", role=role)
    roles = [item for item in facts.constraints if item.field == "role"]
    assert len(roles) == 1 and roles[0].values == ("faculty",)
    assert "面向全校教师征集" in roles[0].evidence.excerpt
    assert not any(item.field == "study_level" for item in facts.constraints)
    assert result.eligibility == eligibility
    if role == "student":
        assert result.action == Action.IGNORE and result.effective_route == "none"
    else:
        assert result.needs_review
    assert facts.deadline_at is None and "year_missing" in codes(facts)


def test_study_level_does_not_silently_supply_an_unknown_application_role():
    facts = extract_facts(sample("14147"))
    now = datetime(2022, 5, 14, 9, tzinfo=SHANGHAI)
    result = decide(
        Profile(study_level="undergraduate", interest_topics=("research",)),
        facts,
        EventContext(next_digest_at=now + timedelta(days=1)),
        now=now,
    )
    constraint = next(item for item in result.constraint_results if item.constraint.field == "role")
    assert constraint.actual is None and constraint.result == "unknown"


def test_a_teacher_mentioned_as_mentor_is_not_an_application_role():
    facts = extract_facts(
        notice(
            "面向武汉大学本科生。每个学生可以联系教师作为指导老师。即日起报名，截止2026年10月6日。"
        )
    )
    assert facts.eligibility_complete
    assert not any(item.field == "role" for item in facts.constraints)
    assert any(
        item.field == "study_level" and item.values == ("undergraduate",)
        for item in facts.constraints
    )


def test_competition_deadline_remains_unknown_and_fake_cancellation_is_not_extracted():
    facts, result = evaluate(sample("18135"), "2024-06-28T09:00:00+08:00", role="student")
    assert facts.category == "opportunity" and not facts.cancelled
    assert facts.deadline_at is None and "time_unrecognized" in codes(facts)
    assert (
        result.action == Action.DIGEST and result.time_status == "unknown" and result.needs_review
    )
    assert result.eligibility == "unknown"
    assert facts.media and all(item.url for item in facts.media)


def test_real_research_result_is_reference_not_a_new_opportunity():
    facts, result = evaluate(
        sample("127511"), "2026-06-26T09:00:00+08:00", high_value_topics=("research",)
    )
    assert facts.category == "reference"
    assert result.action == Action.STORE_ONLY and result.effective_route == "none"


@pytest.mark.parametrize(
    "body,start,end",
    [
        ("报名时间：2026年10月1日至10月6日。", (2026, 10, 1, 0, 0, 0), (2026, 10, 6, 23, 59, 59)),
        (
            "报名时间：2026年12月1日9:00至12月31日24:00。",
            (2026, 12, 1, 9, 0, 0),
            (2027, 1, 1, 0, 0, 0),
        ),
        ("报名时间：2024年2月28日至2月29日24:00。", (2024, 2, 28, 0, 0, 0), (2024, 3, 1, 0, 0, 0)),
    ],
)
def test_supported_same_clause_ranges_have_explainable_boundaries(body, start, end):
    facts = extract_facts(notice(body))
    assert facts.opens_at == datetime(*start, tzinfo=SHANGHAI)
    assert facts.deadline_at == datetime(*end, tzinfo=SHANGHAI)
    assert not codes(facts) & {"year_missing", "invalid_time"}


@pytest.mark.parametrize(
    "body,code",
    [
        ("报名截止：10月6日。", "year_missing"),
        ("报名时间：12月2日9:00至12月9日24:00。", "year_missing"),
        ("报名时间：2026年12月20日至1月6日。", "invalid_time"),
        ("报名时间：2026年10月1日。报名截止：10月6日。", "year_missing"),
        ("报名时间：2026年10月1日，2027年安排截止10月6日。", "year_missing"),
        ("报名时间：2026年10月1日至10月6日24:01。", "invalid_time"),
        ("报名时间：2026年10月1日至10月6日24:00:01。", "invalid_time"),
        ("报名时间：2026年10月1日至10月6日下午5点。", "invalid_time"),
        ("报名时间：2026年10月1日至10月6日24:00韩国时间。", "unsupported_timezone"),
        ("报名时间：2026年10月1日至10月6日。报名截止另行通知。", "time_unrecognized"),
        (
            "报名时间：2026年10月1日至10月6日。第二轮报名时间将另行通知，同时本轮截止也另行通知。",
            "time_unrecognized",
        ),
    ],
)
def test_v2_does_not_guess_unsupported_time_expressions(body, code):
    facts = extract_facts(notice(body))
    assert facts.deadline_at is None and code in codes(facts)


def test_unknown_later_round_without_a_first_round_does_not_invent_a_deadline():
    facts = extract_facts(notice("第二轮报名时间将另行通知。"))
    assert facts.deadline_at is None and facts.opens_at is None
    assert {"secondary_time_unknown", "deadline_unknown"} <= codes(facts)


def test_team_materials_mentioning_a_historical_date_are_not_a_submission_deadline():
    facts = extract_facts(notice("项目团队需提交2024年2月25日的研究材料。"))
    assert facts.deadline_at is None
    assert "deadline_unknown" in codes(facts)


@pytest.mark.parametrize("role", ["graduate", "staff", "Student", "", 1, ["student"]])
def test_application_role_validation_is_strict(role):
    with pytest.raises(ValidationError):
        Profile(interest_topics=("research",), role=role)


def test_optional_role_changes_profile_hash_only_when_explicit():
    omitted = Profile(interest_topics=("research",))
    unknown = Profile(interest_topics=("research",), role=None)
    student = Profile(interest_topics=("research",), role="student")
    assert omitted.model_dump(mode="json") == unknown.model_dump(mode="json")
    assert "role" not in omitted.model_dump(mode="json")
    assert omitted.sha256() == unknown.sha256() != student.sha256()


def test_persisted_v1_profile_facts_and_decision_keep_their_original_snapshots_and_hashes():
    path = Path(__file__).parent / "fixtures/notification-policy-v1.json"
    stored = json.loads(path.read_text())
    profile = Profile.model_validate(stored["profile"])
    facts = NoticeFacts.model_validate(stored["facts"])
    decision = Decision.model_validate(stored["decision"])
    assert profile.model_dump(mode="json") == stored["profile"]
    assert facts.model_dump(mode="json") == stored["facts"]
    assert decision.model_dump(mode="json") == stored["decision"]
    assert profile.sha256() == stored["profile_sha256"] == decision.profile_sha256
    assert facts.sha256() == stored["facts_sha256"] == decision.facts_sha256
    assert (
        canonical_sha256(stored["manifest"]) == stored["manifest_sha256"] == decision.policy_sha256
    )
    assert policy_manifest(profile)["versions"] == {
        "facts_extractor": "whu-notice-facts-v10",
        "rules": "notification-rules-v10",
        "decision_engine": "notification-decision-v10",
        "routing": "notification-routing-v1",
    }
    assert canonical_sha256(policy_manifest(profile)) != stored["manifest_sha256"]
    with pytest.raises(ValueError, match="re-extract"):
        decide(
            profile,
            facts,
            EventContext(next_digest_at=decision.evaluated_at + timedelta(days=1)),
            now=decision.evaluated_at,
        )
