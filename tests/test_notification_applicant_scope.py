"""Synthetic applicant-scope regressions derived from a reviewed failure mode.

These are engineering expectations, not human labels or a claim that all
project qualifications or monthly periods are known. Raw source notices remain
outside the repository.
"""

from datetime import date, datetime, timedelta

import pytest

from signalnest.contracts import NoticeContent
from signalnest.notifications.contracts import Action, EventContext, Profile
from signalnest.notifications.decision import decide
from signalnest.notifications.facts import extract_facts


def evaluate(content, *, clock="2026-10-18T09:00:00+08:00", role="student"):
    now = datetime.fromisoformat(clock)
    facts = extract_facts(content)
    decision = decide(
        Profile(
            institution="whu",
            study_level="undergraduate",
            role=role,
            entry_year=2025,
            interest_topics=("research",),
        ),
        facts,
        EventContext(next_digest_at=now + timedelta(days=1)),
        now=now,
    )
    return facts, decision


def synthetic(body):
    return NoticeContent(
        title="大学生科研训练项目申报通知",
        published_date=date(2026, 10, 18),
        body_text=body,
        body_html=f"<p>{body}</p>",
    )


def assert_no_hard_faculty_constraint(facts):
    assert not any(
        constraint.field in {"role", "study_level"}
        and "faculty" in constraint.values
        and constraint.operator != "unsupported"
        for constraint in facts.constraints
    )


def assert_undergraduate_applicant(facts):
    assert any(
        constraint.field == "study_level"
        and constraint.values == ("undergraduate",)
        and constraint.operator != "unsupported"
        and "项目负责人必须为本科生" in constraint.evidence.excerpt
        for constraint in facts.constraints
    )


def assert_positioned_evidence(content, facts):
    proofs = [constraint.evidence for constraint in facts.constraints]
    proofs.extend(facts.opportunity_evidence)
    proofs.extend(facts.time_evidence)
    for match in facts.topic_matches:
        proofs.append(match.evidence)
        proofs.extend(match.supporting_evidence)
    for unknown in facts.unknowns:
        proofs.extend(unknown.evidence)
    for proof in proofs:
        if proof.field in {"title", "body_text"}:
            text = getattr(content, proof.field)
            assert text[proof.start : proof.end] == proof.excerpt


@pytest.mark.parametrize(
    "body",
    [
        "学校面向全校教师共征集了1000余个在研科研项目选题。"
        "项目负责人必须为本科生。现启动科研训练项目申报。",
        "项目负责人必须为本科生。现启动科研训练项目申报。"
        "学校面向全校教师共征集了1000余个在研科研项目选题。",
        "学校面向全校教师\n共征集了1000余个在研科研项目选题。"
        "项目负责人必须为本科生。现启动科研训练项目申报。",
        "今年已完成面向全校教师的选题征集。项目负责人必须为本科生。现启动科研训练项目申报。",
    ],
    ids=["historical-first", "student-first", "wrapped-history", "completed-this-year"],
)
def test_completed_faculty_stage_is_not_current_student_qualification(body):
    facts, decision = evaluate(synthetic(body))
    assert_no_hard_faculty_constraint(facts)
    assert_undergraduate_applicant(facts)
    assert decision.eligibility != "ineligible" and decision.action == Action.DIGEST


def test_mentor_in_student_audience_sentence_is_not_a_faculty_applicant():
    content = synthetic(
        "科研训练项目面向本科生的、在指导教师指导下独立完成的科研训练活动。"
        "项目负责人必须为本科生。现接受科研训练项目申报。"
        "指导教师应具有副高以上职称或博士学位。"
    )
    facts, decision = evaluate(content)
    assert_no_hard_faculty_constraint(facts)
    assert_undergraduate_applicant(facts)
    assert decision.action == Action.DIGEST and decision.eligibility == "unknown"
    assert decision.needs_review


def test_current_faculty_solicitation_is_not_overridden_by_future_student_beneficiaries():
    content = synthetic(
        "现面向全校教师征集2027年大创项目选题。提供的选题应适合本科生实施开展。"
        "本科生院汇总选题后，集中向全校本科生发布。"
        "学生与教师实行双向选择，确定指导关系后，学生即可在指导教师的指导下申报。"
    )
    facts, decision = evaluate(content)
    assert any(
        constraint.field == "role"
        and constraint.values == ("faculty",)
        and constraint.operator != "unsupported"
        for constraint in facts.constraints
    )
    assert decision.action == Action.IGNORE and decision.eligibility == "ineligible"


def test_two_explicit_current_applicant_roles_remain_unknown_without_reliable_scope():
    content = synthetic(
        "现面向全校教师征集科研训练项目选题。现面向全校本科生开展科研训练项目申报。"
    )
    facts, decision = evaluate(content)
    assert decision.eligibility == "unknown" and decision.needs_review
    assert decision.action == Action.DIGEST
    assert_no_hard_faculty_constraint(facts)
    assert any(unknown.field == "eligibility" and unknown.evidence for unknown in facts.unknowns)


@pytest.mark.parametrize(
    "condition",
    [
        "项目负责人并非本科生。",
        "项目负责人必须为非本科生。",
        "项目负责人必须为本科生或硕士研究生。",
        "项目负责人不必为本科生。",
    ],
)
def test_negated_or_alternative_student_subject_does_not_become_a_hard_match(condition):
    facts, decision = evaluate(synthetic("现启动科研训练项目申报。" + condition))
    assert not any(
        constraint.field == "study_level"
        and constraint.values == ("undergraduate",)
        and constraint.operator != "unsupported"
        for constraint in facts.constraints
    )
    assert decision.eligibility == "unknown" and decision.needs_review


@pytest.mark.parametrize(
    "student_declaration",
    ["报名对象：本科生。", "本科生可申请科研训练项目。"],
    ids=["explicit-applicant-object", "explicit-student-can-apply"],
)
def test_current_student_applicant_declaration_conflicts_with_current_faculty_solicitation(
    student_declaration,
):
    facts, decision = evaluate(
        synthetic("现面向全校教师征集科研训练项目选题。" + student_declaration)
    )
    assert decision.action == Action.DIGEST and decision.eligibility == "unknown"
    assert decision.needs_review
    assert_no_hard_faculty_constraint(facts)
    assert "conflicting_evidence" in {unknown.code for unknown in facts.unknowns}


def test_nearer_current_teacher_solicitation_is_not_erased_by_previous_historical_stage():
    content = synthetic("去年已征集部分选题，现面向全校教师征集科研训练项目选题。")
    facts, decision = evaluate(content)
    assert any(
        constraint.field == "role"
        and constraint.values == ("faculty",)
        and constraint.operator != "unsupported"
        for constraint in facts.constraints
    )
    assert decision.action == Action.IGNORE and decision.eligibility == "ineligible"
    assert_positioned_evidence(content, facts)


def test_project_leader_level_does_not_inherit_team_member_eligibility():
    content = synthetic("现接受科研训练项目申报。项目负责人必须为本科生，团队可以有研究生参与。")
    facts, decision = evaluate(content)
    assert_undergraduate_applicant(facts)
    assert not any(
        constraint.field == "study_level"
        and constraint.operator != "unsupported"
        and set(constraint.values) & {"master", "doctoral"}
        for constraint in facts.constraints
    )
    assert any(
        "团队可以有研究生参与" in proof.excerpt
        for unknown in facts.unknowns
        for proof in unknown.evidence
    )
    assert decision.eligibility == "unknown" and decision.needs_review
    now = datetime.fromisoformat("2026-10-18T09:00:00+08:00")
    postgraduate = decide(
        Profile(
            institution="whu",
            role="student",
            study_level="master",
            interest_topics=("research",),
        ),
        facts,
        EventContext(next_digest_at=now + timedelta(days=1)),
        now=now,
    )
    assert postgraduate.eligibility == "ineligible" and postgraduate.action == Action.IGNORE
    assert_positioned_evidence(content, facts)


@pytest.mark.parametrize(
    "body",
    [
        "去年项目负责人必须为本科生。现面向全校教师征集科研训练项目选题。",
        "现面向全校教师征集科研训练项目选题。选题发布后，项目负责人必须为本科生。",
    ],
    ids=["past-project-leader", "later-project-leader"],
)
def test_noncurrent_project_leader_does_not_override_current_faculty_applicant(body):
    content = synthetic(body)
    facts, decision = evaluate(content)
    assert any(
        constraint.field == "role"
        and constraint.values == ("faculty",)
        and constraint.operator != "unsupported"
        for constraint in facts.constraints
    )
    assert not any(
        constraint.field == "study_level"
        and constraint.values == ("undergraduate",)
        and constraint.operator != "unsupported"
        for constraint in facts.constraints
    )
    assert decision.action == Action.IGNORE and decision.eligibility == "ineligible"
    assert any(
        "项目负责人必须为本科生" in proof.excerpt
        for unknown in facts.unknowns
        for proof in unknown.evidence
    )
    assert_positioned_evidence(content, facts)
