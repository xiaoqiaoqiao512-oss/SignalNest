"""Frozen reported phrases replayed without replacing 接受 or adding 完成.

These local decision inputs have explicit clocks and fictional Profiles. They
are not downloaded pages, human gold, or a claim to measure site-wide recall.
"""

import hashlib
import json
import sqlite3
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import httpx
import pytest

from signalnest.cli import main
from signalnest.contracts import NoticeContent
from signalnest.notifications.contracts import Action, EventContext, Profile
from signalnest.notifications.decision import decide
from signalnest.notifications.facts import extract_facts

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "docs/validation/notification-original-misses-cases.json"
CASES = json.loads(MANIFEST.read_text())
NOW = datetime.fromisoformat("2026-10-08T09:00:00+08:00")
CONTEXT = EventContext(next_digest_at=datetime.fromisoformat("2026-10-09T08:00:00+08:00"))
DEADLINE = "截止2026年10月8日23:59。"
EXPRESSIONS = {"O01": "即日起接受报名", "O02": "请登录学校系统报名辅修专业"}
CONTENT_HASHES = {
    "O01": "f557dd80370761fcba561ab5d8fa3cb0fe13695bfa5c22bb2e5bdb907df3944b",
    "O02": "ffc69d7db2fac041fccfa361bf164897b195eb90e95d8e01c7b8755e191db171",
}


def evaluate(row, *, body=None, title=None, profile=None):
    values = dict(row["synthetic_notice_content"])
    if body is not None:
        values.update(body_text=body, body_html=f"<p>{body}</p>")
    if title is not None:
        values["title"] = title
    content = NoticeContent.model_validate(values)
    profile = profile or Profile.model_validate(CASES["profiles"][row["profile"]])
    facts = extract_facts(content)
    return content, facts, decide(profile, facts, CONTEXT, now=NOW)


def test_complete_manifest_is_frozen_separately_from_the_previous_regressions():
    assert hashlib.sha256(MANIFEST.read_bytes()).hexdigest() == (
        "b3e89dd69fc9dddcc4459ed0ebee08666f42b50a7c10847b270363fcb748e91b"
    )
    assert {row["case_id"] for row in CASES["cases"]} == {"O01", "O02"}
    for row in CASES["cases"]:
        expression = EXPRESSIONS[row["case_id"]]
        assert row["synthetic_notice_content"]["body_text"] == (
            "面向武汉大学本科生。" + expression + "，" + DEADLINE
        )
        assert "完成" not in row["synthetic_notice_content"]["body_text"]
        assert row["content_sha256"] == CONTENT_HASHES[row["case_id"]]


@pytest.mark.parametrize("row", CASES["cases"], ids=lambda row: row["case_id"])
def test_original_phrases_reach_same_day_urgency_with_exact_positioned_evidence(row):
    content, facts, decision = evaluate(row)
    assert content.content_sha256() == CONTENT_HASHES[row["case_id"]]
    assert decision.action == Action.PUSH_NOW and decision.effective_route == "immediate"
    assert decision.time_status == "open" and decision.reason_codes == ("deadline_soon",)
    assert decision.eligibility == "eligible" and not decision.needs_review
    assert facts.opening_confirmed and facts.deadline_at.isoformat() == (
        "2026-10-08T23:59:00+08:00"
    )
    if row["case_id"] == "O01":
        assert facts.opens_at.isoformat() == "2026-10-08T00:00:00+08:00"
        assert any(
            proof.excerpt == EXPRESSIONS["O01"]
            for match in facts.topic_matches
            if match.topic == "research" and match.context == "opportunity"
            for proof in match.supporting_evidence
        )
    else:
        assert facts.opens_at is None
        assert any(proof.excerpt == "请登录学校系统报名" for proof in facts.time_evidence)
    for proof in facts.time_evidence + tuple(
        proof for match in facts.topic_matches for proof in match.supporting_evidence
    ):
        if proof.field == "body_text":
            assert content.body_text[proof.start : proof.end] == proof.excerpt
    again_content, again_facts, again_decision = evaluate(row)
    assert again_content == content and again_facts == facts and again_decision == decision
    assert decision.rules_version == "notification-rules-v10"


@pytest.mark.parametrize(
    "body",
    [
        "科研训练菜单：即日起接受报名→查看记录。",
        "点击科研训练菜单：即日起接受报名。",
        "科研训练报名历史记录写有“即日起接受报名”。",
        "去年科研训练曾接受报名。",
        "科研训练即日起接受报名缴费。",
        "科研训练即日起接受报名退课申请。",
        "科研训练即日起接受报名查询已有记录。",
        "请登录学校系统查看辅修专业报名记录。",
        "菜单：请登录学校系统报名辅修专业→查看。",
        "历史记录：去年请登录学校系统报名辅修专业。",
        "请登录学校系统报名辅修专业缴费。",
        "请登录学校系统报名辅修专业退课申请。",
        "请登录学校系统查询辅修专业报名情况。",
        "新生选课说明：辅修专业单独缴费。",
        "登录学校系统辅修专业栏目提供报名菜单。",
        "请登录学校系统报名查询已有辅修专业记录。",
        "请登录学校系统报名辅修专业的历史记录。",
    ],
)
def test_acceptance_and_login_do_not_turn_menu_history_or_payment_into_an_offer(body):
    profile = Profile(
        institution="whu",
        role="student",
        study_level="undergraduate",
        interest_topics=("research", "minor"),
    )
    _, facts, decision = evaluate(
        CASES["cases"][0],
        body="面向武汉大学本科生。" + body + DEADLINE,
        title="本科生查询说明",
        profile=profile,
    )
    assert decision.action == Action.IGNORE and decision.effective_route == "none"
    assert not any(
        match.context == "opportunity" and match.topic in profile.interest_topics
        for match in facts.topic_matches
    )


@pytest.mark.parametrize("row", CASES["cases"], ids=lambda row: row["case_id"])
def test_different_profile_does_not_inherit_the_original_interest(row):
    _, _, decision = evaluate(row, profile=Profile(interest_topics=("exchange",)))
    assert decision.action == Action.IGNORE and decision.effective_route == "none"


@pytest.mark.parametrize("row", CASES["cases"], ids=lambda row: row["case_id"])
def test_unknown_qualification_remains_unknown_even_when_the_opportunity_is_urgent(row):
    body = row["synthetic_notice_content"]["body_text"].replace(
        "本科生。", "本科生，绩点不低于3.5。"
    )
    _, facts, decision = evaluate(row, body=body)
    assert decision.action == Action.PUSH_NOW and decision.reason_codes == ("deadline_soon",)
    assert decision.eligibility == "unknown" and decision.needs_review
    assert any(item.field == "gpa" for item in facts.unknowns)


@pytest.mark.parametrize("row", CASES["cases"], ids=lambda row: row["case_id"])
def test_unrecognized_deadline_is_not_made_precise_by_the_new_expressions(row):
    body = row["synthetic_notice_content"]["body_text"].replace("23:59", "下午5点")
    _, facts, decision = evaluate(row, body=body)
    assert facts.deadline_at is None
    assert decision.action == Action.DIGEST and decision.time_status == "unknown"
    assert any(item.code == "invalid_time" for item in facts.unknowns)


@pytest.mark.parametrize("row", CASES["cases"], ids=lambda row: row["case_id"])
def test_recent_publication_and_open_application_without_deadline_are_not_urgency(row):
    body = row["synthetic_notice_content"]["body_text"].replace("，" + DEADLINE, "。")
    _, _, decision = evaluate(row, body=body)
    assert decision.action == Action.DIGEST and decision.time_status == "unknown"
    assert (
        decision.effective_route == "digest"
        and "decision.deadline_soon" not in decision.matched_rules
    )


def test_separate_payment_explanation_does_not_hide_a_real_minor_application():
    _, facts, decision = evaluate(
        CASES["cases"][1],
        body="面向武汉大学本科生。请登录学校系统报名辅修专业，缴费安排另见通知。" + DEADLINE,
        title="辅修专业选课通知",
        profile=Profile(
            institution="whu",
            role="student",
            study_level="undergraduate",
            interest_topics=("minor",),
            store_only_topics=("course_enrollment",),
        ),
    )
    assert facts.opening_confirmed and decision.action == Action.PUSH_NOW
    assert "interest.topic.minor" in decision.matched_rules
    assert "retention.topic.course_enrollment" in decision.matched_rules


def test_acceptance_keeps_the_cross_topic_priority_of_research_courses():
    _, _, decision = evaluate(
        CASES["cases"][0],
        title="科研训练课程选课通知",
        profile=Profile(
            institution="whu",
            role="student",
            study_level="undergraduate",
            interest_topics=("research",),
            store_only_topics=("course_enrollment",),
        ),
    )
    assert decision.action == Action.PUSH_NOW
    assert "interest.topic.research" in decision.matched_rules
    assert "retention.topic.course_enrollment" in decision.matched_rules


def test_direct_login_cannot_supply_another_title_topics_application():
    _, facts, decision = evaluate(
        CASES["cases"][1], title="科研训练项目通知", profile=Profile(interest_topics=("research",))
    )
    assert decision.action != Action.PUSH_NOW
    assert not any(
        match.topic == "research" and match.context == "opportunity"
        for match in facts.topic_matches
    )


def test_ambiguous_title_keeps_the_exact_acceptance_as_unknown_evidence():
    _, facts, decision = evaluate(CASES["cases"][0], title="科研训练与辅修专业安排通知")
    assert decision.action not in {Action.PUSH_NOW, Action.DIGEST}
    unknown = next(item for item in facts.unknowns if item.code == "topic_action_link_unknown")
    assert any(proof.excerpt == EXPRESSIONS["O01"] for proof in unknown.evidence)


def test_future_explicit_opening_still_takes_precedence_over_direct_login():
    _, facts, decision = evaluate(
        CASES["cases"][1],
        body="面向武汉大学本科生。报名时间：2026年10月9日至2026年10月10日。请登录学校系统报名辅修专业。",
    )
    assert facts.opening_confirmed and facts.opens_at > NOW
    assert decision.action == Action.DIGEST and decision.time_status == "not_started"


def preview_arguments(tmp_path, row):
    path = tmp_path / f"{row['case_id']}.json"
    path.write_text(json.dumps(row["synthetic_notice_content"], ensure_ascii=False))
    profile = tmp_path / f"{row['profile']}.toml"
    profile.write_text(
        'institution = "whu"\nrole = "student"\nstudy_level = "undergraduate"\n'
        f'interest_topics = ["{CASES["profiles"][row["profile"]]["interest_topics"][0]}"]\n'
    )
    return [
        "decision-preview",
        "--profile",
        str(profile),
        "--notice-json",
        str(path),
        "--at",
        row["evaluated_at"],
        "--next-digest-at",
        row["context"]["next_digest_at"],
    ]


@pytest.mark.parametrize("row", CASES["cases"], ids=lambda row: row["case_id"])
def test_real_local_preview_has_no_storage_lock_or_http_side_effects(
    tmp_path, row, monkeypatch, capsys
):
    from signalnest import instance_lock, storage

    def forbidden(*args, **kwargs):
        pytest.fail("a local rule preview must not access storage, a lock or HTTP")

    args = preview_arguments(tmp_path, row)
    before = {path.name: path.read_bytes() for path in tmp_path.iterdir()}
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sqlite3, "connect", forbidden)
    monkeypatch.setattr(httpx, "Client", forbidden)
    monkeypatch.setattr(storage, "open_initialized_engine", forbidden)
    monkeypatch.setattr(instance_lock, "writer_lock", forbidden)
    monkeypatch.setattr("signalnest.cli.load_config", forbidden)
    assert main(args) == 0
    captured = capsys.readouterr()
    result = json.loads(captured.out)
    assert captured.err == "" and result["preview_only"]
    assert (
        result["decision"]["action"] == "PUSH_NOW"
        and result["decision"]["effective_route"] == "immediate"
    )
    assert result["decision"]["content_sha256"] == CONTENT_HASHES[row["case_id"]]
    assert result["decision"]["rules_version"] == "notification-rules-v10"
    assert {path.name: path.read_bytes() for path in tmp_path.iterdir()} == before


@pytest.mark.parametrize("row", CASES["cases"], ids=lambda row: row["case_id"])
def test_cli_process_replays_the_frozen_input_from_another_directory(tmp_path, row):
    args = preview_arguments(tmp_path, row)
    child = subprocess.run(
        [sys.executable, "-m", "signalnest", *args], cwd=tmp_path, capture_output=True, text=True
    )
    assert child.returncode == 0 and child.stderr == ""
    result = json.loads(child.stdout)
    assert result["preview_only"] and result["decision"]["action"] == "PUSH_NOW"
    assert result["decision"]["content_sha256"] == CONTENT_HASHES[row["case_id"]]
    assert {path.suffix for path in tmp_path.iterdir()} == {".json", ".toml"}
