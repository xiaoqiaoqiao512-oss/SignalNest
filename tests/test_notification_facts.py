"""Offline evidence extraction: limited literal expressions, never speculative eligibility."""

import json
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from signalnest.contracts import (
    AttachmentReference,
    ImageReference,
    NoticeContent,
    PageInput,
)
from signalnest.notifications.contracts import (
    FACTS_EXTRACTOR_VERSION,
    RULES_VERSION,
    Action,
    EventContext,
    Profile,
)
from signalnest.notifications.decision import decide
from signalnest.notifications.facts import TOPIC_PHRASES, extract_facts, rule_manifest
from signalnest.parsing import parse_notice

FIXTURES = Path(__file__).resolve().parents[1] / "research/fixtures"
SHANGHAI = ZoneInfo("Asia/Shanghai")


def content(body="武汉大学本科生可报名。报名时间：2026年10月1日至2026年10月6日。", **values):
    defaults = {
        "title": "国际交流项目报名通知",
        "published_date": date(2026, 10, 1),
        "body_html": f"<p>{body}</p>",
        "body_text": body,
    }
    defaults.update(values)
    return NoticeContent(**defaults)


def unknown_codes(facts):
    return {item.code for item in facts.unknowns}


def test_supported_and_audience_and_period_have_exact_source_evidence():
    notice = content(
        "面向武汉大学计算机学院2024级本科生，专业为计算机科学与技术。"
        "报名时间：2026年10月1日9:00至2026年10月6日18:30。"
    )
    facts = extract_facts(notice)
    actual = {item.field: item.values for item in facts.constraints}
    assert actual == {
        "institution": ("whu",),
        "college": ("计算机学院",),
        "entry_year": (2024,),
        "study_level": ("undergraduate",),
        "major": ("计算机科学与技术",),
    }
    assert facts.audience_declared and facts.eligibility_complete
    assert facts.opens_at == datetime(2026, 10, 1, 9, tzinfo=SHANGHAI)
    assert facts.deadline_at == datetime(2026, 10, 6, 18, 30, tzinfo=SHANGHAI)
    assert facts.opening_confirmed
    assert not facts.unknowns
    assert facts.content_sha256 == notice.content_sha256()
    for evidence in (
        *(item.evidence for item in facts.topic_matches),
        *(item.evidence for item in facts.constraints),
        *facts.time_evidence,
    ):
        text = getattr(notice, evidence.field)
        assert text[evidence.start : evidence.end] == evidence.excerpt
        assert len(evidence.excerpt) <= 120


def test_absent_audience_is_unknown_not_all_students():
    facts = extract_facts(content("报名时间：2026年10月1日至2026年10月6日。"))
    assert not facts.audience_declared
    assert not facts.eligibility_complete
    assert not facts.constraints
    assert "audience_unknown" in unknown_codes(facts)


def test_background_having_qualities_is_not_an_audience_declaration():
    facts = extract_facts(
        content("为培养具有国际视野、跨学科能力的人才，满足我校学生学习需求，现启动交流报名。")
    )
    assert not facts.audience_declared
    assert not facts.eligibility_complete
    assert not facts.constraints
    assert "audience_unknown" in unknown_codes(facts)


def test_bare_research_graduate_word_does_not_exclude_undergraduate_recommendation():
    facts = extract_facts(content("推荐免试研究生申请事宜。", title="本科生推免通知"))
    assert "recommendation" in {match.topic for match in facts.topic_matches}
    assert not facts.constraints
    declared = extract_facts(content("仅限研究生报名。"))
    assert declared.audience_declared and declared.eligibility_complete
    assert declared.constraints[0].values == ("master", "doctoral")


@pytest.mark.parametrize(
    "body,field",
    [
        ("面向本科生或研究生报名。", "eligibility"),
        ("面向2022至2024级本科生报名。", "eligibility"),
        ("面向本科生（不含毕业生）报名。", "eligibility"),
        ("面向本科生，GPA达到3.0。", "gpa"),
        ("面向二年级本科生报名。", "grade"),
        ("面向本科生，雅思不低于6分。", "language"),
        ("面向经济困难本科生报名。", "financial_condition"),
        ("仅限预立项项目成员申请。", "project_membership"),
        ("面向全日制在校本科生报名。", "student_status"),
        ("面向本科生，必须获得导师同意。", "other"),
    ],
)
def test_unsupported_conditions_are_explicit_unknown(body, field):
    facts = extract_facts(content(body))
    assert facts.audience_declared
    assert not facts.eligibility_complete
    assert field in {item.field for item in facts.unknowns}
    assert any(item.operator == "unsupported" for item in facts.constraints)


def test_conflicting_audiences_are_unknown_not_false_reliable_mismatch():
    facts = extract_facts(content("报名对象：本科生。仅限研究生报名。"))
    assert not facts.eligibility_complete
    assert "conflicting_evidence" in unknown_codes(facts)
    assert all(item.operator == "unsupported" for item in facts.constraints)


@pytest.mark.parametrize(
    "body",
    [
        "面向全校本科生，且获得导师同意。",
        "面向全校本科生，成绩优秀者优先。",
        "面向全校本科生。申请人须获得导师同意。",
        "面向全校本科生，专业不限但需校内推荐。",
    ],
)
def test_recognizing_student_label_does_not_hide_additional_conditions(body):
    facts = extract_facts(content(body))
    assert facts.audience_declared and not facts.eligibility_complete
    assert any(item.operator == "unsupported" for item in facts.constraints)
    assert {"unrecognized_condition", "unsupported_condition"} & unknown_codes(facts)


@pytest.mark.parametrize(
    "body", ["不面向本科生报名。", "仅限非本科生报名。", "并非仅面向研究生报名。"]
)
def test_negative_audience_is_not_misread_as_positive_supported_constraint(body):
    facts = extract_facts(content(body))
    assert not facts.eligibility_complete
    assert "unsupported_negation" in unknown_codes(facts)
    assert all(item.operator == "unsupported" for item in facts.constraints)


def evaluate_facts(facts, *, study_level="undergraduate"):
    return decide(
        Profile(interest_topics=("exchange",), study_level=study_level),
        facts,
        EventContext(next_digest_at=datetime(2026, 10, 6, 9, tzinfo=SHANGHAI)),
        now=datetime(2026, 10, 5, 12, tzinfo=SHANGHAI),
    )


@pytest.mark.parametrize(
    "requirements,code",
    [
        ("报名条件：本科生。\n2. 无处分记录。", "unsupported_condition"),
        ("报名条件：\n1. 本科生。\n2. 取得导师书面同意。", "unparsed_condition"),
        ("报名条件：本科生。\n（2）取得导师书面同意。", "unparsed_condition"),
        ("报名条件：本科生。\n（二）取得导师书面同意。", "unparsed_condition"),
        ("报名条件：本科生。\n二、取得导师书面同意。", "unparsed_condition"),
        ("面向本科生。申请者年龄不超过22周岁。", "unsupported_condition"),
        ("面向本科生。申请者需年满18周岁。", "unsupported_condition"),
        ("面向本科生。未受纪律处分。", "unsupported_condition"),
    ],
)
def test_additional_separate_qualification_never_becomes_eligible(requirements, code):
    facts = extract_facts(content(requirements + "即日起报名，截止2026年10月6日。"))
    decision = evaluate_facts(facts)
    assert facts.audience_declared and not facts.eligibility_complete
    assert code in unknown_codes(facts)
    assert decision.eligibility == "unknown" and decision.needs_review
    # Unknown qualifications do not hide the credible short deadline.
    assert decision.action == Action.PUSH_NOW
    assert any("资格待核实" in reason for reason in decision.reasons)


@pytest.mark.parametrize("heading", ["报名条件：", ""])
def test_cross_clause_or_is_not_a_reliable_and_mismatch(heading):
    facts = extract_facts(
        content(
            heading + "面向本科生。\n2. 研究生也可申请。满足其中一项即可。"
            "\n即日起报名，截止2026年10月6日。"
        )
    )
    decision = evaluate_facts(facts, study_level="master")
    assert "unsupported_or" in unknown_codes(facts)
    assert decision.eligibility == "unknown"
    assert all(result.result != "mismatch" for result in decision.constraint_results)


def test_numbered_qualification_section_stops_at_next_main_heading():
    facts = extract_facts(
        content(
            "一、报名条件：本科生。\n二、提交材料：\n1.填写申请表。\n"
            "即日起报名，截止2026年10月6日。"
        )
    )
    assert facts.eligibility_complete
    assert "unparsed_condition" not in unknown_codes(facts)
    assert evaluate_facts(facts).eligibility == "eligible"


@pytest.mark.parametrize(
    "body,deadline,opens",
    [
        ("报名截止：2026年10月6日。", datetime(2026, 10, 6, 23, 59, 59, tzinfo=SHANGHAI), None),
        ("报名截止：2026-10-06 18:30。", datetime(2026, 10, 6, 18, 30, tzinfo=SHANGHAI), None),
        ("报名开始：2026年10月2日。", None, datetime(2026, 10, 2, tzinfo=SHANGHAI)),
        (
            "自通知发布之日起至2026年10月6日。",
            datetime(2026, 10, 6, 23, 59, 59, tzinfo=SHANGHAI),
            datetime(2026, 10, 1, tzinfo=SHANGHAI),
        ),
        (
            "即日起报名。报名截止：2026年10月6日。",
            datetime(2026, 10, 6, 23, 59, 59, tzinfo=SHANGHAI),
            datetime(2026, 10, 1, tzinfo=SHANGHAI),
        ),
    ],
)
def test_supported_time_forms_use_explicit_shanghai_semantics(body, deadline, opens):
    facts = extract_facts(content(body))
    assert facts.deadline_at == deadline
    assert facts.opens_at == opens


@pytest.mark.parametrize(
    "body,code",
    [
        ("报名截止：10月6日。", "year_missing"),
        ("报名截止：2026年2月30日。", "invalid_time"),
        ("报名截止：2026年10月6日18时。", "invalid_time"),
        ("报名截止：2026年10月6日下午5点。", "invalid_time"),
        ("报名截止：2026年10月6日中午12点。", "invalid_time"),
        ("报名截止：2026年10月6日凌晨1点。", "invalid_time"),
        ("报名截止：2026年10月6日晚8点。", "invalid_time"),
        ("报名截止：2026年10月6日（中午12点）。", "invalid_time"),
        ("报名截止：2026-10-06T18:30。", "invalid_time"),
        ("报名截止：2026年10月6日18:30:60。", "invalid_time"),
        ("报名截止：2026年10月6日18:30：45。", "invalid_time"),
        ("报名截止：2026年10月6日18:30.123。", "invalid_time"),
        ("报名截止：2026年10月6日18:30-20:00。", "invalid_time"),
        ("报名截止：2026年10月6日18:30 ～ 20:00。", "invalid_time"),
        ("报名截止：2026年10月6日18:30至20:00。", "invalid_time"),
        ("报名截止：2026年10月6日18:30 到 20:00。", "invalid_time"),
        ("报名截止：2026年10月6日至8日。", "invalid_time"),
        ("报名截止：2026年10月6日18:30+08:00。", "invalid_time"),
        ("报名截止：2026年10月6日18:30 PM。", "invalid_time"),
        ("报名截止：2026年10月6日18:30纽约时间。", "unsupported_timezone"),
        ("报名截止：2026年10月6日18:30伦敦时间。", "unsupported_timezone"),
        ("报名截止：2026年10月6日18:30东京时间。", "unsupported_timezone"),
        ("报名截止：2026年10月6日18:30 GMT。", "unsupported_timezone"),
        ("报名截止：2026年10月6日（美国东部时间）。", "unsupported_timezone"),
        ("报名截止：2026年10月6日18:30美西时间。", "unsupported_timezone"),
        ("报名截止：2026年10月6日18:30欧洲中部时间。", "unsupported_timezone"),
        ("报名截止：2026年10月6日18:30（韩国时间）。", "unsupported_timezone"),
        ("报名截止：2026年10月6日18:30新加坡时间。", "unsupported_timezone"),
        ("报名截止：2026年10月6日。申请截止：2026年10月8日。", "conflicting_evidence"),
        ("报名时间：2026年10月8日至2026年10月1日。", "invalid_time"),
    ],
)
def test_unsupported_or_conflicting_times_do_not_guess_deadline(body, code):
    facts = extract_facts(content(body))
    assert facts.deadline_at is None
    assert code in unknown_codes(facts)


@pytest.mark.parametrize(
    "competing",
    ["报名截止另行通知。", "报名开始另行通知。", "申请时间请等候后续说明。"],
)
def test_unresolved_competing_time_cannot_keep_a_trusted_urgent_deadline(competing):
    facts = extract_facts(content("面向本科生。即日起报名，报名截止2026年10月6日。" + competing))
    decision = evaluate_facts(facts)
    assert facts.deadline_at is None and not facts.opening_confirmed
    assert "time_unrecognized" in unknown_codes(facts)
    assert decision.time_status == "unknown" and decision.needs_review
    assert decision.action == Action.DIGEST


@pytest.mark.parametrize("zone", ["北京时间", "上海时间", "中国标准时间"])
def test_known_shanghai_timezone_and_opening_only_cue_remain_supported(zone):
    facts = extract_facts(
        content(f"面向本科生。即日起报名。报名截止2026年10月6日18:30（{zone}）。")
    )
    assert facts.opens_at == datetime(2026, 10, 1, tzinfo=SHANGHAI)
    assert facts.deadline_at == datetime(2026, 10, 6, 18, 30, tzinfo=SHANGHAI)
    assert "time_unrecognized" not in unknown_codes(facts)
    assert evaluate_facts(facts).action == Action.PUSH_NOW


def test_publication_date_does_not_supply_missing_deadline_or_opening():
    facts = extract_facts(content("面向本科生报名。"))
    assert facts.deadline_at is None and facts.opens_at is None
    assert not facts.opening_confirmed
    assert {"opening_unknown", "deadline_unknown"} <= unknown_codes(facts)


def test_unchanged_deadline_repetition_is_not_a_conflict():
    facts = extract_facts(content("报名截止：2026年10月6日。申请截止：2026年10月6日。"))
    assert facts.deadline_at == datetime(2026, 10, 6, 23, 59, 59, tzinfo=SHANGHAI)
    assert "conflicting_evidence" not in unknown_codes(facts)


def test_body_topic_evidence_is_interest_only_not_reliable_exclusion():
    facts = extract_facts(content("参加项目后有机会获得奖学金。"))
    assert any(item.topic == "scholarship" and not item.primary for item in facts.topic_matches)
    assert any(item.topic == "exchange" and item.primary for item in facts.topic_matches)


def test_media_only_does_not_read_alt_or_attachment_names_as_facts():
    notice = content(
        "",
        title="学生通知",
        body_html='<p><img src="https://example.com/poster.png"></p>',
        images=(
            ImageReference(url="https://example.com/poster.png", alt_text="国际交流本科生报名"),
        ),
        attachments=(
            AttachmentReference(url="https://example.com/file.pdf", name="奖学金资格.pdf"),
        ),
    )
    facts = extract_facts(notice)
    assert facts.information_incomplete and facts.category == "unknown"
    assert not facts.topic_matches and not facts.constraints
    assert len(facts.media) == 2
    assert [(item.field, item.index) for item in facts.media] == [("images", 0), ("attachments", 0)]
    assert all(not item.excerpt for item in facts.media)
    assert "media_required" in unknown_codes(facts)


def test_critical_attachment_differs_from_supplementary_attachment():
    attachment = AttachmentReference(url="https://example.com/file.pdf", name="操作指南")
    complete = extract_facts(content(attachments=(attachment,)))
    assert complete.eligibility_complete and not complete.information_incomplete
    missing = extract_facts(
        content("面向本科生报名。报名条件详见附件。", attachments=(attachment,))
    )
    assert missing.information_incomplete and not missing.eligibility_complete
    assert "media_required" in unknown_codes(missing)


def test_deterministic_facts_and_manifest_do_not_mutate_content_or_rule_table():
    notice = content()
    before = notice.canonical_json()
    first, second = extract_facts(notice), extract_facts(notice)
    assert first == second and first.sha256() == second.sha256()
    assert first.extractor_version == FACTS_EXTRACTOR_VERSION
    assert notice.canonical_json() == before
    manifest = rule_manifest()
    assert manifest["rules_version"] == RULES_VERSION
    json.dumps(manifest, ensure_ascii=False, sort_keys=True)
    manifest["topic_phrases"]["exchange"].append("伪造词组")
    assert "伪造词组" not in TOPIC_PHRASES["exchange"]


@pytest.mark.parametrize(
    "fixture,category,important_unknowns",
    [
        (
            "notice-117011-20261005T133549Z",
            "opportunity",
            {"unsupported_or", "unsupported_condition", "media_required", "secondary_time_unknown"},
        ),
        ("notice-127511-20261005T133829869212Z", "reference", set()),
        ("notice-128291-20261005T133533Z", "opportunity", {"unsupported_condition"}),
        ("notice-14147-20261005T133543Z", "opportunity", {"year_missing"}),
        (
            "notice-17361-20261005T133538Z",
            "opportunity",
            {"unsupported_condition", "media_required"},
        ),
    ],
)
def test_real_research_samples_are_conservative_and_originals_unchanged(
    fixture, category, important_unknowns
):
    path = FIXTURES / "notifications" / f"{fixture}.html"
    original = path.read_bytes()
    metadata = json.loads(path.with_suffix(".json").read_text())
    parsed = parse_notice(PageInput(content=original, page_url=metadata["final_url"]))
    facts = extract_facts(parsed.content)
    assert facts.category == category
    assert important_unknowns <= unknown_codes(facts)
    assert facts.content_sha256 == parsed.content.content_sha256()
    assert path.read_bytes() == original
    if fixture.startswith("notice-127511"):
        assert not facts.information_incomplete  # Result spreadsheet is supplemental reference.
    if fixture.startswith("notice-128291"):
        assert facts.deadline_at == datetime(2026, 9, 28, 23, 59, tzinfo=SHANGHAI)
        assert facts.opens_at == datetime(2026, 9, 7, tzinfo=SHANGHAI)
    if fixture.startswith("notice-14147"):
        assert any(
            item.field == "role" and item.values == ("faculty",) for item in facts.constraints
        )


def test_real_competition_template_keeps_unknown_deadline_and_individual_disqualification():
    path = FIXTURES / "notifications" / "notice-18135-20261005T133554Z.html"
    original = path.read_bytes()
    metadata = json.loads(path.with_suffix(".json").read_text())
    parsed = parse_notice(PageInput(content=original, page_url=metadata["final_url"]))
    facts = extract_facts(parsed.content)
    assert facts.category == "opportunity"
    assert "competition" in {match.topic for match in facts.topic_matches}
    assert facts.deadline_at is None
    assert "time_unrecognized" in unknown_codes(facts)
    assert "取消参赛资格" in facts.body_text
    assert not facts.cancelled
    assert path.read_bytes() == original


def test_existing_legacy_fixture_exposes_image_schedule_as_unparsed():
    parsed = parse_notice(
        PageInput(
            content=(FIXTURES / "legacy-notice-detail.html").read_bytes(),
            page_url="https://uc.whu.edu.cn/2022/show.jsp?wbtreeid=1517&wbnewsid=127581",
        )
    )
    facts = extract_facts(parsed.content)
    assert facts.information_incomplete
    assert "media_required" in unknown_codes(facts)
    assert facts.media and all(item.url for item in facts.media)


def test_cancellation_extraction_disabled_without_positive_site_sample():
    facts = extract_facts(content("取消违规学生参赛资格。", title="竞赛规则说明"))
    assert not facts.cancelled
