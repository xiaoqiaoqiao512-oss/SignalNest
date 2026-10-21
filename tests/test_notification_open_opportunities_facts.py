"""Selected synthetic inputs and explicit boundary cases."""

import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from signalnest.contracts import NoticeContent
from signalnest.notifications.contracts import EventContext, Profile
from signalnest.notifications.decision import deadline_for_profile, decide
from signalnest.notifications.facts import extract_facts

DATA = json.loads(
    (Path(__file__).parent / "fixtures/notification-open-opportunities-synthetic.json").read_text()
)
CASES = {c["case_id"]: c for c in DATA["cases"]}
AT = datetime.fromisoformat("2026-10-19T12:00:00+08:00")
PROFILE = Profile(
    institution="whu", role="student", study_level="undergraduate", interest_topics=("competition",)
)


def content(case):
    return NoticeContent.model_validate(CASES[case]["notice_content"])


def evaluate(item, user=PROFILE, at=AT):
    facts = extract_facts(item)
    decision = decide(
        user,
        facts,
        EventContext(
            kind="opportunity_review",
            notification_mode="digest_only",
            next_digest_at=at + timedelta(days=1),
        ),
        now=at,
    )
    return facts, decision


@pytest.mark.parametrize("case", CASES)
def test_synthetic_content_identity_is_bound_to_extracted_facts(case):
    item = content(case)
    assert extract_facts(item).content_sha256 == item.content_sha256()


@pytest.mark.parametrize(
    "case,deadline,eligibility",
    [
        ("OPEN_PRIMARY", "2026-10-23T23:59:59+08:00", "eligible"),
        ("OPEN_GROUPED", "2026-10-31T23:59:59+08:00", "unknown"),
        ("OPEN_MIDNIGHT", "2026-11-01T00:00:00+08:00", "eligible"),
    ],
)
def test_three_old_but_open_synthetic_opportunities_are_digest_candidates(
    case, deadline, eligibility
):
    item = content(case)
    facts, result = evaluate(item)
    assert (AT.date() - item.published_date).days > 7
    assert result.effective_route == "digest" and result.time_status == "open"
    # The short PRIMARY/MIDNIGHT replacements state only known qualifications;
    # GROUPED retains the conservative mixed-audience unknown, with full-time /
    # attachment unknowns covered explicitly by the OPEN_UNCERTAIN case below.
    assert result.eligibility == eligibility
    assert result.needs_review is (eligibility == "unknown")
    assert deadline_for_profile(facts, PROFILE).deadline_at.isoformat() == deadline
    assert result.facts_sha256 == facts.sha256()
    for proof in result.evidence:
        if proof.field in {"title", "body_text"}:
            assert getattr(item, proof.field)[proof.start : proof.end] == proof.excerpt


def test_group_deadline_applies_to_profile_without_changing_the_extraction():
    facts, undergraduate = evaluate(content("OPEN_GROUPED"))
    before = facts.model_dump(mode="json")
    assert facts.deadline_at is None
    assert {s.applicant_group for s in facts.scoped_deadlines} == {
        "undergraduate",
        "graduate",
        "international",
    }
    graduate = PROFILE.model_copy(update={"study_level": "master"})
    assert deadline_for_profile(facts, graduate).deadline_at.day == 15
    assert deadline_for_profile(facts, graduate).deadline_at.month == 11
    assert deadline_for_profile(facts, PROFILE).deadline_at.month == 10
    assert (
        deadline_for_profile(facts, PROFILE.model_copy(update={"study_level": None})).deadline_at
        is None
    )
    assert facts.model_dump(mode="json") == before
    assert undergraduate.facts_sha256 == facts.sha256()


def test_coordinated_synthetic_audience_is_not_a_graduate_only_requirement():
    facts, result = evaluate(content("OPEN_UNCERTAIN"))
    study = [c for c in facts.constraints if c.field == "study_level"]
    assert any("undergraduate" in c.values and "master" in c.values for c in study)
    assert result.eligibility != "ineligible"
    assert result.needs_review  # Full-time status, attachments and missing year stay unknown.


def test_expired_competition_and_later_material_stage_do_not_become_open():
    _, expired = evaluate(content("CLOSED"))
    _, later_stage = evaluate(content("LATER_STAGE"))
    assert expired.time_status == "closed" and expired.effective_route == "none"
    assert later_stage.effective_route == "none"


@pytest.mark.parametrize(
    "clock,valid",
    [
        ("18:30:45", True),
        ("24:00:00", True),
        ("24:00:01", False),
        ("18:30:60", False),
        ("18:30:45.1", False),
        ("18:30:45Z", False),
    ],
)
def test_seconds_are_validated_instead_of_truncated(clock, valid):
    body = f"现启动竞赛报名。面向武汉大学本科生。报名截止时间：2026年10月23日{clock}。"
    item = NoticeContent(
        title="竞赛报名通知",
        published_date=AT.date(),
        body_text=body,
        body_html="<p>synthetic boundary</p>",
    )
    facts = extract_facts(item)
    assert (facts.deadline_at is not None) is valid


def test_date_header_does_not_invalidate_its_following_expired_application():
    body = "报名截止日期\n竞赛报名截止日期为2026年10月7日（周三）。"
    item = NoticeContent(
        title="竞赛报名通知",
        published_date="2026-09-20",
        body_text=body,
        body_html="<p>synthetic header boundary</p>",
    )
    _, result = evaluate(item)
    assert result.time_status == "closed" and result.effective_route == "none"


@pytest.mark.parametrize("replacement", ["去年组织活动已经开始", "报名尚未开放"])
def test_current_call_templates_do_not_accept_historical_or_deferred_calls(replacement):
    item = content("OPEN_PRIMARY")
    text = item.body_text.replace("组织活动已经开始", replacement)
    facts = extract_facts(item.model_copy(update={"body_text": text}))
    # The current-call template must not independently assert opening when its
    # same clause is historical or the application is explicitly deferred.
    assert not facts.opening_confirmed


def test_bare_future_deadline_cannot_establish_opening():
    item = NoticeContent(
        title="竞赛安排",
        published_date=AT.date(),
        body_text="报名截止时间：2026年10月23日。",
        body_html="<p>synthetic</p>",
    )
    assert not extract_facts(item).opening_confirmed
