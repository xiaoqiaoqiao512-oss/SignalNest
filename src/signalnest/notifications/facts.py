"""Conservative literal WHU text facts; no network, storage, OCR or attachment parsing.

This is a fixed, deliberately small rule set. A phrase match is evidence for that
phrase, not a claim that all conditions in an arbitrary notice were understood.
"""

import re
from datetime import datetime, time, timedelta
from types import MappingProxyType
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup

from signalnest.contracts import NoticeContent
from signalnest.notifications.contracts import (
    FACTS_EXTRACTOR_VERSION,
    RULES_VERSION,
    Evidence,
    NoticeFacts,
    QualificationConstraint,
    ScopedDeadline,
    TopicMatch,
    UnknownItem,
)

TOPIC_PHRASES = MappingProxyType(
    {
        "exchange": ("国际交流", "交换生", "交流项目", "海外交流", "出国交流", "海外研修"),
        "scholarship": ("奖学金", "助学金"),
        "research": ("创新研究", "科研训练", "创新创业训练", "大创项目", "本科生科研"),
        "competition": ("竞赛", "大赛"),
        "course_enrollment": ("选课", "课程报名"),
        "minor": ("辅修", "双学士学位"),
        "recommendation": ("推免", "推荐免试"),
        "teaching_assistant": ("助教", "教学助理"),
    }
)

SHANGHAI = ZoneInfo("Asia/Shanghai")
_DATE = re.compile(
    r"(?<!\d)(?P<year>20\d{2})(?:年|[-/])\s*(?P<month>\d{1,2})(?:月|[-/])"
    r"\s*(?P<day>\d{1,2})(?!\d)(?:日)?"
    r"(?:\s*(?P<hour>\d{1,2})[:：](?P<minute>\d{2})(?!\d)"
    r"(?::(?P<second>\d{2})(?!\d))?)?"
)
_TIMELESS_DATE = re.compile(
    r"(?<![\d年/-])(?P<month>\d{1,2})月\s*(?P<day>\d{1,2})日"
    r"(?:\s*(?P<hour>\d{1,2})[:：](?P<minute>\d{2})(?!\d)"
    r"(?::(?P<second>\d{2})(?!\d))?)?"
)
_UNSUPPORTED_TIMEZONE = re.compile(
    r"\b(?:UTC|GMT|EST|EDT|PST|PDT|CST|CDT|MST|MDT|CET|CEST)\b"
    r"|纽约时间|伦敦时间|东京时间|当地时间|美国时间|英国时间"
    r"|(?:美国(?:东部|中部|西部|山地|太平洋)|美东|美中|美西|欧洲中部|中欧|东部|中部|太平洋)时间",
    re.IGNORECASE,
)
_UNSUPPORTED_TIME_SUFFIX = re.compile(
    r"\s*(?:[:：.]|[~～—–-]\s*\d{1,2}\s*[:：]|(?:至|到)\s*\d{1,2}\s*(?:[:：]|日)"
    r"|[+-]\s*\d{2}:|\b(?:AM|PM|Z)\b|时|点|秒)",
    re.IGNORECASE,
)
_UNSUPPORTED_DATE_CLOCK = re.compile(
    r"\s*(?:[，,（(]\s*)?(?:清晨|凌晨|早上|上午|中午|下午|傍晚|晚上|夜间|午夜"
    r"|[晚早]\s*\d|T\s*\d{1,2}[:：]|\d{1,2}\s*[:：时点])"
)
_LOCAL_TIMEZONES = ("北京时间", "上海时间", "中国标准时间")
_BRACKETED_TIMEZONE = re.compile(r"[（(]\s*([\u4e00-\u9fff]{2,16}(?:时间|时区))\s*[)）]")
_SUFFIX_TIMEZONE = re.compile(r"\s*([\u4e00-\u9fff]{2,16}(?:时间|时区))")
_DEADLINE = re.compile(
    r"(?:报名|申请|申报|选课)?(?:(?:截止|截至)(?:时间|日期)?(?!后)|最迟)"
    r"|(?:报名|申请|申报|选课)时间"
)
_PERIOD = re.compile(r"(?:报名|申请|申报|选课)(?:时间|期间)")
_AUDIENCE = re.compile(
    r"(?:仅面向|只面向|面向|仅限|只限|(?:报名|申请|申报|选课)对象(?:与要求)?\s*[:：]|"
    r"报名条件\s*[:：\n]\s*(?:\d+[.．]\s*)?)(?P<value>[^。；;\n]+)"
)
_CAN_PARTICIPATE = re.compile(
    r"(?:武汉大学|我校|全校)?(?:20\d{2}级)?(?:全日制)?(?:在校)?"
    r"(?:专科生|本科生|硕士研究生|博士研究生|研究生|在华留学生)"
    r"(?:[、和及](?:专科生|本科生|硕士研究生|博士研究生|研究生|在华留学生))*"
    r"(?:均可|可|可以)(?:报名|申请|参加)"
)
_TIME_HEADING = re.compile(
    r"(?:[（(]?\d+[.．、)）]\s*)?(?:报名|申请|申报|选课)"
    r"(?:(?:截止|截至)(?:时间|日期)?|时间|日期|期间)\s*[:：]?\s*$"
)
_TABLE_APPLICATION_LABEL = re.compile(r"(?:报名|申请|申报)(?:及(?:作品|材料)提交)?$")
_SCOPED_EXTENSION = re.compile(
    r"(?:注[:：]\s*)?(?P<groups>研究生组(?:[和及、]在华留学生组)?)"
    r"[，,]\s*学生参赛报名截止日期可延到(?P<date>[^。；;\n]+)$"
)
_UNSUPPORTED_QUALIFICATIONS = (
    ("gpa", re.compile(r"GPA|绩点|平均成绩", re.IGNORECASE)),
    (
        "language",
        re.compile(r"(?:英语|语言)[^。；\n]{0,16}(?:要求|达到|不低于)|CET-[46]|雅思|托福"),
    ),
    ("grade", re.compile(r"[一二三四五六]年级|大[一二三四五六]")),
    (
        "project_membership",
        re.compile(r"(?:面向|仅限)[^。；\n]{0,20}(?:预立项项目|项目成员|项目团队)"),
    ),
    ("financial_condition", re.compile(r"(?:家庭)?经济困难|贫困(?:生|家庭)")),
    ("age", re.compile(r"年龄[^。；\n]{0,18}(?:周岁|不超过|不低于)|年满\s*\d+|\d+\s*周岁")),
    ("disciplinary_record", re.compile(r"无处分记录|无纪律处分|未受[^。；\n]{0,8}处分|无违纪记录")),
)
_QUALIFICATION_SECTION = re.compile(
    r"(?m)^\s*(?:[一二三四五六七八九十]+[、.．]\s*)?"
    r"(?:(?:报名|申请|申报|选课)(?:条件|要求)|资格(?:条件|要求))\s*[:：]?\s*"
)
_NEXT_SECTION = re.compile(
    r"(?m)^\s*[一二三四五六七八九十]+[、.．]\s*"
    r"(?:(?:报名|申请|申报|选课|提交)(?:材料|流程|方式|时间|安排|费用)|"
    r"日程|咨询|联系|附件)"
)
_NUMBERED_CONDITION = re.compile(
    r"(?:^|[。\n；;])\s*(?:\d+[.．、)）]|[（(]\s*(?:\d+|[一二三四五六七八九十]+)"
    r"\s*[)）]|[一二三四五六七八九十]+[、.．])\s*(?P<value>[^。；;\n]+)"
)
_SECTION_OR = re.compile(r"满足(?:其中)?(?:一项|任一项)|符合(?:其中)?任一|任一条件|条件之一|任选")
_UNSCOPED_OR = re.compile(
    r"满足(?:其中)?(?:一项|任一项)|符合(?:其中)?任一(?:项|条件)|任一条件|条件之一"
)
_OR = re.compile(
    r"或者|或|(?:本科生|研究生|教师)[、和及](?:本科生|研究生|教师)|含20\d{2}级"
    r"|不含|除外|除.*以外|20\d{2}(?:至|到|[-—])20\d{2}级"
)
_CRITICAL_MEDIA = re.compile(
    r"(?:报名|申请|资格|条件|对象|截止|时间|安排)[^。；\n]{0,25}"
    r"(?:见|详见|参见|如下)[^。；\n]{0,15}(?:附件|图片|图中|下图|图表)"
    r"|(?:条件|对象|资格|截止|报名时间)[^。；\n]{0,25}[（(]附件"
)
_RESULTS = re.compile(r"结题|验收结果|评审结果|获奖名单|录取名单|结果公示|公布.*结果")
_FACULTY_APPLICANT = re.compile(r"现面向(?:全校|我校|武汉大学)教师征集[^。；;\n]{0,24}选题")
_COMPLETED_FACULTY_COLLECTION = re.compile(
    r"面向(?:全校|我校|武汉大学)教师\s*(?:共|累计)?征集了"
    r"|已完成面向(?:全校|我校|武汉大学)教师的(?:选题|课题)征集"
)
_HISTORICAL_APPLICANT_PREFIX = re.compile(r"去年|往年|此前|曾经|曾于|历史|上(?:一)?轮|当时")
_CURRENT_APPLICANT_PREFIX = re.compile(r"(?:现(?:已)?|本次|本轮|即日起)\s*$")
_APPLICANT_PHASE_RESET = re.compile(r"(?:^|[，,])\s*(?:现(?:已)?|本次|本轮|即日起)")
_LATER_APPLICANT_PREFIX = re.compile(r"选题发布后|汇总选题后|确定指导关系后|下一阶段|下一轮|今后")
_BENEFICIARY_AUDIENCE = re.compile(r"本科生的[、，,\s]*在(?:指导教师|教师|导师)指导下")
_PROJECT_APPLICANT = re.compile(
    r"项目负责人(?:必须|须|应当|应)(?:为|是)(?P<value>本科生[^，,。；;\n]*)"
)
_PROJECT_CONDITIONS = (
    re.compile(r"毕业年级学生不能作为项目负责人申报"),
    re.compile(r"(?:项目的团队成员|项目团队成员)[^。；;\n]{0,70}不超过\d+人"),
    re.compile(r"每位学生只能参与一个[^。；;\n]+"),
    re.compile(r"指导教师(?:应|须|必须)具有[^。；;\n]+"),
    re.compile(r"团队(?:成员)?(?:可以|可|允许)有研究生参与"),
)
_TEAM_SUBMISSION = re.compile(r"项目团队(?:完成|须|需)[^。；;\n]{0,24}(?:提交|材料)")
_ADMIN_TIME = re.compile(
    r"(?:指导教师|各项目单位|各学院)[^。；;\n]{0,32}(?:审核|评审|汇总|提交|发送)"
)
_CURRENT_APPLICATION = re.compile(r"可提出立项补报申请")
_LATER_ROUND_TIME = re.compile(
    r"^(?:辅修第一轮录取结果和)?第二轮报名时间将另行通知(?:[，,]请及时关注)?\s*$"
)
_DEFERRED_DEADLINE = re.compile(
    r"截止(?:时间|日期)?\s*(?:另行通知|(?:见|详见|以)[^，,。；;\n]{0,20}(?:通知|为准))"
)
_ENTRY_ACTION = re.compile(r"报名|申请|申报|征集|招募|招生|招收|选拔|选派|选课")
_CONTEXT_BREAK = re.compile(r"[。；;！!？?\n]")
_STEP_BREAK = re.compile(r"[，,]")
_ADMIN_ACTION = re.compile(r"缴费|收费|补缴|退费|退课|退出|成绩转换|学分转换|证书申请")
_NAVIGATION = re.compile(r"登录|菜单|点击|智慧珞珈|报名申请\s*[-→>]")
_PAST_CONTEXT = re.compile(r"去年|往年|上学期|上一年度|曾经|曾于|已于|此前|历史")
_CURRENT_CONTEXT = re.compile(r"即日起|现(?:启动|开放|开展|接受)|本次|本轮|今年")
_NEGATED_ENTRY = re.compile(r"(?:不|未|暂停|停止|取消)[^，,。；;\n]{0,10}$")
_CLOSED_ENTRY = re.compile(r"\s*(?:已(?:经)?(?:结束|截止|完成|关闭)|结束|停止|关闭)")
_UNAVAILABLE_ENTRY = re.compile(
    r"\s*(?:尚未|暂未|未|不|暂停|停止|取消)(?:正式)?(?:开放|启动|开始|开展|接受|进行)"
)
_MINOR_AFTER = re.compile(
    r"\s*(?:专业|项目|学习|课程)?\s*(?:的)?\s*"
    r"(?:(?:现|即日起)?(?:开始|启动|开放|接受))?\s*"
    r"(?P<action>报名|招生|招收|招募|申请(?:修读|攻读)?)"
)
_MINOR_BEFORE = re.compile(r"(?:报名(?:修读|参加)?|申请(?:修读|攻读)?)\s*$")
_DIRECTIVE_ENTRY = re.compile(
    r"(?:请|须|需|应)(?:[^。；;\n，,→>]{0,40}(?:完成|进行|提交)"
    r"[^。；;\n，,→>]{0,12}|登录(?:学校)?系统\s*)"
    r"(?P<action>报名|申请|申报|选课)"
)
_PRESENT_ENTRY = re.compile(
    r"(?:即日起(?:\s*接受)?|现(?:已)?(?:启动|开放|接受|开始|开展)|正在接受)\s*"
    r"(?P<action>报名|申请|申报|征集|招募|招生|招收|选拔|选派|选课)"
)
_COMPETITION_CURRENT = re.compile(
    r"组织活动已经开始|选拔优秀团队参加[^。；;\n]{1,90}(?:大赛|竞赛)[，,]"
    r"现将有关事项通知如下"
)
_COMPETITION_SCHEDULE = re.compile(r"报名及(?:比赛安排|作品提交)|报名截止时间")
_COMPETITION_DEFERRED = re.compile(r"报名(?:尚未|暂未|暂不|将另行)|暂不接受报名|报名时间另行通知")
_ENTRY_RECORD = re.compile(r"查看|查询|已有|历史记录|报名记录|(?:报名|申请)情况")
_MENU_PATH = re.compile(r"菜单|点击|[→>]|报名申请\s*[-→>]")
_UNBOUND_SUBJECT = re.compile(r"(?:相关|有关)(?:安排|事宜|事项)|(?:安排|事项)(?:如下|见下文)")


# Recruitment is a separate action, not a synonym for every mention of 助教.
_TA_PAIR = re.compile(
    r"(?P<after>助教|教学助理)(?:岗位|人员|的)?(?P<after_action>招聘|招募|选聘)"
    r"|(?P<before_action>招聘|招募|选聘)(?:本科教学课程|本科课程|课程|学生|若干名)?"
    r"(?P<before>助教|教学助理)"
)
_TA_REFERENCE_TITLE = re.compile(
    r"津贴|薪酬|工资|报酬|退出|退聘|考核|任职介绍|回顾|经验|使用说明|栏目|"
    r"管理办法|制度|实施细则|结果|名单|怎样当"
)
_TA_PRESENT = re.compile(
    r"即日起|现(?:已)?(?:面向|招聘|招募|选聘)|本(?:次|学期|轮)[^。；;\n]{0,24}(?:招聘|招募|选聘)"
    r"|现在[^。；;\n]{0,24}(?:招聘|招募|选聘)"
)
_TA_FORM_SUBMISSION = re.compile(
    r"请[^。；;\n]{0,16}(?:助教|申请人|应聘者)[^。；;\n]{0,100}"
    r"(?:申请审核表|岗位申请表)[^。；;\n]{0,36}提交"
)
_TA_UNAVAILABLE = re.compile(
    r"(?:不|未|暂停|停止|取消)(?:招聘|招募|选聘)(?:课程)?(?:助教|教学助理)"
    r"|(?:助教|教学助理)(?:岗位)?(?:招聘|招募|选聘)[^。；;\n]{0,8}"
    r"(?:尚未|暂未|未|暂停|停止|取消)(?:正式)?(?:开放|启动|开始)"
)
_TA_ADMIN_BODY = re.compile(r"津贴|薪酬|工资|退出申请|退聘|考核表")
_TA_AUDIENCE = re.compile(r"(?P<soft>原则上)?从(?P<value>[^。；;\n，,（）()]{1,40})中选聘")
_TA_APPLICANT_OBJECT = re.compile(r"助教岗位申请对象\s*(?:包括|[:：])(?P<value>[^。\n]+)")
_TA_APPROVAL = re.compile(r"(?:主讲|任课)教师[^。；;\n]{0,90}(?:签署聘用意见|推荐|考查同意)")
_TA_CONDITIONS = (
    ("grade", re.compile(r"高年级本科生|高年级任低年级")),
    ("course_performance", re.compile(r"系统学习过申请课程并获得[0-9]{1,3}分以上成绩")),
    ("student_status", re.compile(r"全日制脱产学习|全日制在校在籍")),
    ("other", re.compile(r"不得同时承担两门课程及以上的助教岗位")),
    (
        "application_channel",
        re.compile(r"https://docs\.qq\.com/[^\s，,。；;]+|两个表都在QQ群文件里"),
    ),
)
_TA_MONTH_DAY_SUBMISSION = re.compile(
    r"(?<![0-9年])(?P<date>[0-9]{1,2}月[0-9]{1,2}日)前"
    r"(?:填写在线文档报名|交老师签字的《助教申请表》)"
)
_TA_ACADEMIC_YEAR = re.compile(r"(?P<first>20[0-9]{2})[—–-](?P<last>20[0-9]{2})学年")
_TA_SIGNATURE_DATE = re.compile(r"(?m)^[ \t]*(?P<date>20[0-9]{2}年[0-9]{1,2}月[0-9]{1,2}日)[ \t]*$")
_TA_CLOCK = re.compile(
    r"(?<!\d)(?P<year>20\d{2})年(?P<month>\d{1,2})月(?P<day>\d{1,2})日"
    r"(?:[（(](?:周|星期)(?P<weekday>[一二三四五六日天])[）)])?"
    r"下午(?P<hour>1[3-9]|2[0-3])点前"
)


def _sentences(text: str):
    """Yield nonempty clauses and original Unicode offsets without rewriting text."""
    for match in re.finditer(r"[^。；;\n]+", text):
        if match.group().strip():
            yield match.group(), match.start(), match.end()


def _date_value(
    match: re.Match[str], *, deadline: bool, inherited_year: int | None = None
) -> datetime | None:
    """An inherited year is supplied only for an explicit same-clause range end."""
    year = int(match["year"]) if "year" in match.groupdict() else inherited_year
    if year is None:
        return None
    month, day = (int(match[name]) for name in ("month", "day"))
    hour, minute = match["hour"], match["minute"]
    second = match.groupdict().get("second")
    suffix = match.string[match.end() :]
    if _UNSUPPORTED_TIME_SUFFIX.match(suffix):
        # Never truncate an unsupported suffix, clock range, fraction or explicit offset.
        return None
    if hour is None:
        if _UNSUPPORTED_DATE_CLOCK.match(suffix):
            return None
        clock = time(23, 59, 59) if deadline else time.min
    else:
        if int(hour) == 24 and int(minute) == 0 and int(second or 0) == 0:
            try:
                return datetime(year, month, day, tzinfo=SHANGHAI) + timedelta(days=1)
            except ValueError:
                return None
        try:
            clock = time(int(hour), int(minute), int(second or 0))
        except ValueError:
            return None
    try:
        return datetime(year, month, day, clock.hour, clock.minute, clock.second, tzinfo=SHANGHAI)
    except ValueError:
        return None


def _source_fields(content: NoticeContent):
    return (("title", content.title), ("body_text", content.body_text))


def _qualification_sections(text):
    """Explicit headings; unrecognized numbered conditions cannot end the section."""
    for header in _QUALIFICATION_SECTION.finditer(text):
        following = _NEXT_SECTION.search(text[header.end() :])
        end = header.end() + following.start() if following else len(text)
        yield header.start(), header.end(), end


def _evidence(field: str, text: str, start: int, end: int) -> Evidence:
    end = min(end, start + 120)
    return Evidence(field=field, start=start, end=end, excerpt=text[start:end])


def _unknown(code, field, message, *evidence):
    return UnknownItem(code=code, field=field, message=message, evidence=tuple(evidence))


def rule_manifest() -> dict:
    """Serializable immutable-policy input; never read mutable configuration at runtime."""
    return {
        "facts_extractor_version": FACTS_EXTRACTOR_VERSION,
        "rules_version": RULES_VERSION,
        "topic_phrases": {key: list(values) for key, values in TOPIC_PHRASES.items()},
        "primary_topic": "title subject; body keyword alone is incidental, not active interest",
        "topic_context": {
            "values": ["subject", "opportunity", "incidental", "uncertain"],
            "entry_actions": _ENTRY_ACTION.pattern,
            "clause_break": _CONTEXT_BREAK.pattern,
            "administration_step_break": _STEP_BREAK.pattern,
            "maximum_topic_action_gap": 24,
            "minor_after_pattern": _MINOR_AFTER.pattern,
            "minor_before_pattern": _MINOR_BEFORE.pattern,
            "administration_pattern": _ADMIN_ACTION.pattern,
            "navigation_pattern": _NAVIGATION.pattern,
            "past_pattern": _PAST_CONTEXT.pattern,
            "current_pattern": _CURRENT_CONTEXT.pattern,
            "negated_entry_pattern": _NEGATED_ENTRY.pattern,
            "closed_entry_pattern": _CLOSED_ENTRY.pattern,
            "unavailable_entry_pattern": _UNAVAILABLE_ENTRY.pattern,
            "login_completion_directive": _DIRECTIVE_ENTRY.pattern,
            "direct_login_scope": (
                "imperative 登录(学校)系统 immediately followed by the exact entry action; "
                "same topic binding and menu/record/history/administration exclusions"
            ),
            "present_entry_pattern": _PRESENT_ENTRY.pattern,
            "current_competition_call": _COMPETITION_CURRENT.pattern,
            "current_competition_schedule": _COMPETITION_SCHEDULE.pattern,
            "deferred_competition_entry": _COMPETITION_DEFERRED.pattern,
            "record_pattern": _ENTRY_RECORD.pattern,
            "menu_path_pattern": _MENU_PATH.pattern,
            "unbound_subject_pattern": _UNBOUND_SUBJECT.pattern,
            "title_body_link": (
                "one title topic, explicit current body instruction, no competing body "
                "opportunity or reference/administration title; retain separate body evidence"
            ),
            "ambiguous_link": "uncertain topic and topic_action_link_unknown, never urgency",
            "other_topic": (
                "another named topic in the gap rejects binding; research course is shared"
            ),
            "occurrences": (
                "classify every occurrence; incidental mentions cannot mask later offers"
            ),
        },
        "category": {
            "reference_title": _RESULTS.pattern,
            "administration_title": _ADMIN_ACTION.pattern,
            "administration_reference": "only when no title topic has a linked entry action",
            "opportunity_phrase": _ENTRY_ACTION.pattern,
            "opportunity_exclusions": "same entry exclusions as topic binding",
        },
        "audience_patterns": [_AUDIENCE.pattern, _CAN_PARTICIPATE.pattern],
        "supported_qualification_fields": [
            "role: explicit current faculty topic-solicitation applicant, not student beneficiary",
            "institution: explicit Wuhan University or 全校/我校 scope",
            "study_level: undergraduate/master/doctoral/faculty",
            "college: literal 学院名 in audience clause",
            "major: literal 专业为/专业： or X专业学生",
            "entry_year: explicit 20XX级",
        ],
        "unsupported_qualification_patterns": {
            field: pattern.pattern for field, pattern in _UNSUPPORTED_QUALIFICATIONS
        },
        "boolean_logic": (
            "explicit AND; "
            "full coordinated 均可 applicant list is inclusive; unsupported OR/conflict unknown"
        ),
        "qualification_sections": {
            "heading_pattern": _QUALIFICATION_SECTION.pattern,
            "next_section_pattern": _NEXT_SECTION.pattern,
            "numbered_condition_pattern": _NUMBERED_CONDITION.pattern,
            "unparsed_numbered_conditions": (
                "unsupported; recognizing the first item is insufficient"
            ),
            "or_pattern": _SECTION_OR.pattern,
            "unscoped_or_pattern": _UNSCOPED_OR.pattern,
        },
        "time": {
            "complete_date_pattern": _DATE.pattern,
            "deadline_context_pattern": _DEADLINE.pattern,
            "timezone": "Asia/Shanghai",
            "date_only_deadline": "23:59:59 on stated date",
            "date_only_start": "00:00:00 on stated date",
            "relative_opening": "即日起/自通知发布之日起 use explicit site publication date",
            "unsupported_time_suffix_pattern": _UNSUPPORTED_TIME_SUFFIX.pattern,
            "unsupported_date_clock_pattern": _UNSUPPORTED_DATE_CLOCK.pattern,
            "unsupported_timezone_pattern": _UNSUPPORTED_TIMEZONE.pattern,
            "local_timezone_labels": list(_LOCAL_TIMEZONES),
            "bracketed_timezone_pattern": _BRACKETED_TIMEZONE.pattern,
            "suffix_timezone_pattern": _SUFFIX_TIMEZONE.pattern,
            "year_inheritance": "only YYYY date 至/到 M月D日 in one explicit application interval",
            "seconds": "optional ASCII :SS; 00..59; no truncated suffix, fraction or offset",
            "midnight": "24:00 or 24:00:00 exactly means next calendar day 00:00",
            "standalone_heading": _TIME_HEADING.pattern,
            "heading_scope": (
                "bare heading plus immediately adjacent date only; no global date borrowing"
            ),
            "application_table_row": _TABLE_APPLICATION_LABEL.pattern,
            "table_backend": (
                "html.parser; same-row first two cells; unique exact body-text mapping"
            ),
            "scoped_extension": _SCOPED_EXTENSION.pattern,
            "scope_selection": (
                "preserve witnessed applicant branches; no global earliest/latest deadline"
            ),
            "before_date": "日前 retains [day start, day end] boundary and imprecise_deadline",
            "team_submission_pattern": _TEAM_SUBMISSION.pattern,
            "administrative_deadline_pattern": _ADMIN_TIME.pattern,
            "current_application_pattern": _CURRENT_APPLICATION.pattern,
            "linked_application_directive": {
                "pattern": _DIRECTIVE_ENTRY.pattern,
                "scope": (
                    "body only; exact action passes entry exclusions and is covered by a "
                    "linked opportunity's body evidence or title supporting evidence"
                ),
                "effect": "confirms opening without inventing an opens_at timestamp",
            },
            "separate_later_round_pattern": _LATER_ROUND_TIME.pattern,
            "deferred_deadline_pattern": _DEFERRED_DEADLINE.pattern,
            "unsupported": (
                "standalone missing year, cross-year guessing, multiple applicant deadlines, "
                "seconds, clock ranges, other timezones"
            ),
        },
        "faculty_applicant_pattern": _FACULTY_APPLICANT.pattern,
        "applicant_scope": {
            "completed_faculty_collection": _COMPLETED_FACULTY_COLLECTION.pattern,
            "historical_clause_prefix": _HISTORICAL_APPLICANT_PREFIX.pattern,
            "current_applicant_prefix": _CURRENT_APPLICANT_PREFIX.pattern,
            "current_phase_reset": _APPLICANT_PHASE_RESET.pattern,
            "later_phase_prefix": _LATER_APPLICANT_PREFIX.pattern,
            "beneficiary_description": _BENEFICIARY_AUDIENCE.pattern,
            "student_project_applicant": _PROJECT_APPLICANT.pattern,
            "unverified_project_conditions": [p.pattern for p in _PROJECT_CONDITIONS],
            "teacher_role": (
                "only the current faculty topic-solicitation declaration; teacher words "
                "elsewhere never become an applicant study level"
            ),
            "scope": (
                "each declaration separately; completed collection and beneficiary/mentor "
                "descriptions retain evidence without hard applicant constraints"
            ),
            "conflict": (
                "unresolved current faculty solicitation and explicit current student "
                "application make their applicant constraints unsupported"
            ),
        },
        "teaching_assistant": {
            "recruitment_pair": _TA_PAIR.pattern,
            "reference_title": _TA_REFERENCE_TITLE.pattern,
            "present_recruitment": _TA_PRESENT.pattern,
            "application_form_submission": _TA_FORM_SUBMISSION.pattern,
            "unavailable": _TA_UNAVAILABLE.pattern,
            "administrative_body": _TA_ADMIN_BODY.pattern,
            "applicant_selection": _TA_AUDIENCE.pattern,
            "applicant_object": _TA_APPLICANT_OBJECT.pattern,
            "applicant_branches": (
                "explicit applicant-object clause can cross semicolons; differing level sets "
                "make level constraints unsupported, never discard the undergraduate branch"
            ),
            "teacher_approval": _TA_APPROVAL.pattern,
            "unverified_conditions": {field: pattern.pattern for field, pattern in _TA_CONDITIONS},
            "month_day_submission": _TA_MONTH_DAY_SUBMISSION.pattern,
            "academic_year": _TA_ACADEMIC_YEAR.pattern,
            "standalone_signature_date": _TA_SIGNATURE_DATE.pattern,
            "year_conflict": (
                "signature year outside title academic-year range invalidates time claims; "
                "retain exact evidence, never correct or infer a deadline year"
            ),
            "application_clock": _TA_CLOCK.pattern,
            "clock_scope": (
                "initial application form only; explicit year, optional consistent weekday, "
                "afternoon 13-23 点前; never result/assessment dates or guessed year"
            ),
            "bare_mentions": "incidental; never promoted by generic registration fallback",
            "title_requirement": (
                "recruitment pair plus linked current body recruitment/application instruction; "
                "otherwise uncertain, or incidental for explicit historical/administrative closure"
            ),
            "generic_instruction": (
                "one title subject, no competing named body opportunity "
                "or unavailable recruitment; "
                "explicit form submission cannot be a historical/menu step"
            ),
            "conflicting_availability": "uncertain; never borrow urgency from generic action",
            "soft_conditions": "原则上 selection remains unsupported, not a hard exclusion",
        },
        "media": "references remain unparsed; image-only or explicit critical dependency unknown",
        "cancellation": "disabled pending trustworthy positive sample",
        "limits": "literal small rule set; no OCR, attachment parsing, semantic completeness claim",
    }


def _topics(content):
    matches = []
    for field, text in _source_fields(content):
        for topic, phrases in TOPIC_PHRASES.items():
            if topic == "teaching_assistant":
                continue
            for phrase in phrases:
                for match in re.finditer(re.escape(phrase), text):
                    context, start, end = _topic_context(topic, field, text, match)
                    matches.append(
                        TopicMatch(
                            topic=topic,
                            evidence=_evidence(field, text, start, end),
                            primary=field == "title",
                            context=context,
                        )
                    )
    return tuple(matches) + _teaching_assistant_topics(content)


def _teaching_assistant_topics(content):
    """Require a recruitment pair and present application evidence for body offers.

    A title may publish recruitment; a biography or an existing employee's form
    cannot. Generic registration fallback deliberately never promotes a bare TA.
    """
    title_allowed = not _TA_REFERENCE_TITLE.search(content.title)
    title_pairs = [
        match
        for match in _TA_PAIR.finditer(content.title)
        if title_allowed and not _entry_blocked(content.title, *match.span())
    ]
    submission = _TA_FORM_SUBMISSION.search(content.body_text) if title_pairs else None
    if submission is not None and _entry_blocked(content.body_text, *submission.span()):
        submission = None
    body_pairs = []
    rejected_body_pair = False
    for match in _TA_PAIR.finditer(content.body_text):
        left, right = _clause_bounds(content.body_text, *match.span())
        clause = content.body_text[left:right]
        if _entry_blocked(content.body_text, *match.span()):
            rejected_body_pair = True
            continue
        if _TA_PRESENT.search(clause) or (title_pairs and submission is not None):
            body_pairs.append(match)
    proofs = tuple(_evidence("body_text", content.body_text, *match.span()) for match in body_pairs)
    if submission is not None:
        proofs += (_evidence("body_text", content.body_text, *submission.span()),)
    # N0's explicit login/application instruction remains valid for a genuine
    # recruitment title. A named different body opportunity owns its action.
    competing_subject = any(
        phrase in content.title
        for topic, phrases in TOPIC_PHRASES.items()
        if topic != "teaching_assistant"
        for phrase in phrases
    ) or any(
        _topic_context(topic, "body_text", content.body_text, match)[0] == "opportunity"
        for topic, phrases in TOPIC_PHRASES.items()
        if topic != "teaching_assistant"
        for phrase in phrases
        for match in re.finditer(re.escape(phrase), content.body_text)
    )
    explicitly_unavailable = _TA_UNAVAILABLE.search(content.body_text)
    if title_pairs and not competing_subject and not explicitly_unavailable:
        for action in _ENTRY_ACTION.finditer(content.body_text):
            if _entry_blocked(content.body_text, *action.span()):
                continue
            instruction = _instruction(content.body_text, *action.span(), _DIRECTIVE_ENTRY)
            if instruction is None:
                instruction = _instruction(content.body_text, *action.span(), _PRESENT_ENTRY)
            if instruction is None:
                continue
            left, right = _clause_bounds(content.body_text, *action.span())
            if any(
                phrase in content.body_text[left:right]
                for topic, phrases in TOPIC_PHRASES.items()
                if topic != "teaching_assistant"
                for phrase in phrases
            ):
                continue
            proofs += (_evidence("body_text", content.body_text, *instruction.span()),)
    unavailable = not proofs and (
        rejected_body_pair
        or _TA_UNAVAILABLE.search(content.body_text)
        or _TA_ADMIN_BODY.search(content.body_text)
    )
    conflicting = bool(proofs and explicitly_unavailable)
    matches = []
    for field, text in _source_fields(content):
        pairs = title_pairs if field == "title" else body_pairs
        for phrase in TOPIC_PHRASES["teaching_assistant"]:
            for match in re.finditer(re.escape(phrase), text):
                pair = next(
                    (pair for pair in pairs if pair.start() <= match.start() < pair.end()), None
                )
                context = "incidental"
                if pair is not None and not unavailable:
                    context = "opportunity" if field == "body_text" or proofs else "uncertain"
                    if conflicting:
                        context = "uncertain"
                matches.append(
                    TopicMatch(
                        topic="teaching_assistant",
                        primary=field == "title" and context == "opportunity",
                        context=context,
                        evidence=_evidence(
                            field, text, *(pair.span() if pair is not None else match.span())
                        ),
                        supporting_evidence=proofs if field == "title" and pair is not None else (),
                    )
                )
    return tuple(matches)


def _clause_bounds(text, start, end):
    before = list(_CONTEXT_BREAK.finditer(text, 0, start))
    after = _CONTEXT_BREAK.search(text, end)
    return before[-1].end() if before else 0, after.start() if after else len(text)


def _instruction(text, start, end, pattern):
    left, right = _clause_bounds(text, start, end)
    return next(
        (
            match
            for match in pattern.finditer(text, left, right)
            if match.span("action") == (start, end)
        ),
        None,
    )


def _entry_blocked(text, start, end, *, subject_start=None, subject_end=None):
    """Finite exclusions for witnessed administration, not general language understanding."""
    left, right = _clause_bounds(text, start, end)
    beginning = min(start, subject_start if subject_start is not None else start)
    stop = max(end, subject_end if subject_end is not None else end)
    prefix = text[max(left, beginning - 32) : beginning]
    after = text[end : min(right, end + 16)]
    if (
        _NEGATED_ENTRY.search(prefix)
        or _CLOSED_ENTRY.match(after)
        or _UNAVAILABLE_ENTRY.match(after)
    ):
        return True
    prior_steps = list(_STEP_BREAK.finditer(text, left, beginning))
    step_left = prior_steps[-1].end() if prior_steps else left
    next_step = _STEP_BREAK.search(text, stop, right)
    step_right = next_step.start() if next_step else right
    local = text[step_left : min(step_right, stop + 8)]
    if _ENTRY_RECORD.search(local):
        return True
    if _NAVIGATION.search(local):
        # A witnessed imperative to submit an application is different from
        # a menu path. It must name this exact action, not another nearby word.
        if (
            _instruction(text, start, end, _DIRECTIVE_ENTRY) is None
            or _MENU_PATH.search(local)
            or "登录" not in local
        ):
            return True
    past = list(_PAST_CONTEXT.finditer(prefix))
    current = list(_CURRENT_CONTEXT.finditer(prefix))
    if past and (not current or past[-1].start() > current[-1].start()):
        return True
    # Do not veto an entire notice for mentioning fees. Only the proposed
    # topic/action relation and the action's immediate object are considered.
    if _ADMIN_ACTION.search(text[beginning : min(step_right, stop + 8)]):
        return True
    if _ADMIN_ACTION.search(text[max(step_left, start - 4) : start]):
        return True
    return False


def _topic_context(topic, field, text, match):
    start, end = match.span()
    left, right = _clause_bounds(text, start, end)
    for action in _ENTRY_ACTION.finditer(text, max(left, start - 32), min(right, end + 40)):
        if action.end() <= start:
            gap = text[action.end() : start]
        elif end <= action.start():
            gap = text[end : action.start()]
        else:
            gap = ""
        if len(gap) > 24 or _entry_blocked(
            text, action.start(), action.end(), subject_start=start, subject_end=end
        ):
            continue
        if topic == "minor":
            after = _MINOR_AFTER.match(text[end:right])
            before = _MINOR_BEFORE.search(text[left:start])
            if not (
                (after is not None and end + after.start("action") == action.start())
                or (before is not None and left + before.start() == action.start())
            ):
                continue
        if any(
            phrase in gap
            for other, phrases in TOPIC_PHRASES.items()
            if other != topic and not (topic == "research" and other == "course_enrollment")
            for phrase in phrases
        ):
            continue
        return "opportunity", min(start, action.start()), max(end, action.end())
    return ("subject" if field == "title" else "incidental"), start, end


def _opportunities(content):
    return tuple(
        _evidence(field, text, match.start(), match.end())
        for field, text in _source_fields(content)
        for match in _ENTRY_ACTION.finditer(text)
        if not _entry_blocked(text, match.start(), match.end())
    )


def _linked_application_directive(content, topics):
    """A present application instruction must belong to a validated opportunity.

    A title alone, an uncertain relation, or an instruction about another action
    cannot establish that the opportunity is open. Existing entry exclusions
    still reject administration, menu paths and historical instructions.
    """
    proofs = tuple(
        proof
        for item in topics
        if item.context == "opportunity"
        for proof in (item.evidence, *item.supporting_evidence)
        if proof.field == "body_text"
    )
    for action in _ENTRY_ACTION.finditer(content.body_text):
        if not any(proof.start <= action.start() and action.end() <= proof.end for proof in proofs):
            continue
        if _entry_blocked(content.body_text, *action.span()):
            continue
        instruction = _instruction(content.body_text, *action.span(), _DIRECTIVE_ENTRY)
        if instruction is not None:
            return instruction
    return None


def _link_document_actions(content, topics):
    """Resolve one title subject, retaining limited unresolved relations as unknown."""
    unbound = [item for item in topics if item.primary and item.context == "subject"]
    if _RESULTS.search(content.title) or _ADMIN_ACTION.search(content.title):
        return topics, ()
    title_topics = {item.topic for item in topics if item.primary and item.context != "incidental"}
    if title_topics and not unbound:
        return topics, ()
    instructions = []
    for action in _ENTRY_ACTION.finditer(content.body_text):
        if _entry_blocked(content.body_text, action.start(), action.end()):
            continue
        instruction = _instruction(content.body_text, *action.span(), _PRESENT_ENTRY)
        if instruction is None:
            instruction = _instruction(content.body_text, *action.span(), _DIRECTIVE_ENTRY)
        if instruction is None:
            continue
        left, right = _clause_bounds(content.body_text, *action.span())
        clause_topics = {
            topic
            for topic, phrases in TOPIC_PHRASES.items()
            if any(phrase in content.body_text[left:right] for phrase in phrases)
        }
        # A named body subject owns its action. Local linking already handles
        # that subject; a different title cannot borrow it via this fallback.
        if clause_topics:
            continue
        instructions.append(
            _evidence("body_text", content.body_text, instruction.start(), instruction.end())
        )
    if not instructions:
        return topics, ()
    if not title_topics:
        # A neutral body heading plus a separate generic instruction is only
        # a possible relation. It cannot borrow urgency or become a Digest.
        for item in topics:
            if item.context != "incidental":
                continue
            left, right = _clause_bounds(content.body_text, item.evidence.start, item.evidence.end)
            clause = content.body_text[left:right]
            if _UNBOUND_SUBJECT.search(clause) and not any(
                pattern.search(clause)
                for pattern in (
                    _ADMIN_ACTION,
                    _NAVIGATION,
                    _ENTRY_RECORD,
                    _PAST_CONTEXT,
                    _ENTRY_ACTION,
                )
            ):
                unbound.append(item)
        if not unbound:
            return topics, ()
    competitors = [
        item
        for item in topics
        if not item.primary and item.context == "opportunity" and item.topic not in title_topics
    ]
    unique_subject = len(title_topics) == 1 and not competitors
    linked = tuple(
        item.model_copy(
            update={
                "context": "opportunity" if unique_subject else "uncertain",
                "supporting_evidence": tuple(instructions),
            }
        )
        if item in unbound
        else item
        for item in topics
    )
    if unique_subject:
        return linked, ()
    unknown = _unknown(
        "topic_action_link_unknown",
        "interest",
        "可见主题与正文报名行动缺少可靠绑定，无法确定该行动属于哪项机会。",
        *(item.evidence for item in unbound),
        *(item.evidence for item in competitors),
        *instructions,
    )
    return linked, (unknown,)


def _noncurrent_applicant_stage(text, start, end):
    """Only witnessed local markers; a new current clause can reset past context."""
    left, _ = _clause_bounds(text, start, end)
    prefix = text[left:start]
    markers = [
        (match.end(), stage)
        for pattern, stage in (
            (_HISTORICAL_APPLICANT_PREFIX, "historical"),
            (_LATER_APPLICANT_PREFIX, "later"),
            (_APPLICANT_PHASE_RESET, None),
        )
        for match in pattern.finditer(prefix)
    ]
    return max(markers, key=lambda item: item[0])[1] if markers else None


def _qualifications(content):
    constraints, unknowns = [], []
    current_student_proofs = []
    audience_declared = False
    incomplete = False
    for field, text in _source_fields(content):
        faculty_applicants = tuple(
            match
            for match in _FACULTY_APPLICANT.finditer(text)
            if _noncurrent_applicant_stage(text, match.start() + 1, match.end()) is None
        )
        audience_matches = sorted(
            (
                *_AUDIENCE.finditer(text),
                *_CAN_PARTICIPATE.finditer(text),
                *_PROJECT_APPLICANT.finditer(text),
            ),
            key=lambda match: match.start(),
        )
        for match in audience_matches:
            clause = match.groupdict().get("value") or match.group()
            evidence = _evidence(field, text, match.start(), match.end())
            left, _ = _clause_bounds(text, match.start(), match.end())
            prefix = text[left : match.start()]
            scope_end = match.end()
            # Only bridge a wrapped, otherwise bare teacher subject into this
            # witnessed completed-collection expression, not arbitrary paragraphs.
            if clause.strip() in {"全校教师", "我校教师", "武汉大学教师"}:
                continuation = re.match(r"\s+(?:共|累计)?征集了[^。；;\n]+", text[scope_end:])
                if continuation is not None:
                    scope_end += continuation.end()
            scoped_text = text[left:scope_end]
            stage = _noncurrent_applicant_stage(text, match.start(), match.end())
            completed = "教师" in clause and _COMPLETED_FACULTY_COLLECTION.search(scoped_text)
            if stage is not None or completed:
                incomplete = True
                unknowns.append(
                    _unknown(
                        "later_audience" if stage == "later" else "historical_audience",
                        "audience",
                        "历史或后续阶段的对象不作为本次申请资格；保留原句供核对。",
                        _evidence(field, text, left, scope_end),
                    )
                )
                continue
            if _BENEFICIARY_AUDIENCE.match(clause):
                incomplete = True
                unknowns.append(
                    _unknown(
                        "beneficiary_audience",
                        "audience",
                        "项目受益学生和导师指导描述不等于当前申请主体。",
                        evidence,
                    )
                )
                continue
            faculty_applicant = next(
                (
                    applicant
                    for applicant in faculty_applicants
                    if match.start() < applicant.end() and applicant.start() < match.end()
                ),
                None,
            )
            audience_declared = True
            negated = re.search(
                r"不|非", text[max(0, match.start() - 3) : match.start()]
            ) or re.search(r"非(?:本科生|研究生|教师)|不包括|并非", clause)
            soft = re.search(
                r"原则上|优先|通常|一般", text[max(0, match.start() - 3) : match.end()]
            ) is not None and (
                match.re is _PROJECT_APPLICANT
                or any(
                    item.context == "opportunity" for item in _teaching_assistant_topics(content)
                )
            )
            # A witnessed coordinated list followed by 均可 is one inclusive
            # applicant set. Never run OR detection on only the final suffix.
            coordinated = match.re is _CAN_PARTICIPATE and "或" not in clause
            ambiguous = (
                (not coordinated and _OR.search(clause) is not None) or negated is not None or soft
            )
            if ambiguous:
                incomplete = True
                unknowns.append(
                    _unknown(
                        "soft_qualification"
                        if soft
                        else "unsupported_negation"
                        if negated
                        else "unsupported_or",
                        "eligibility",
                        "资格含软限定、未支持的否定、OR 或例外条件。",
                        evidence,
                    )
                )
            extracted = []
            if any(scope in clause for scope in ("武汉大学", "全校", "我校")):
                extracted.append(("institution", ("whu",)))
            levels = []
            if "本科生" in clause:
                levels.append("undergraduate")
            if "硕士研究生" in clause:
                levels.append("master")
            if "博士研究生" in clause:
                levels.append("doctoral")
            if "研究生" in clause and not any(word in clause for word in ("硕士", "博士")):
                levels.extend(("master", "doctoral"))
            if faculty_applicant:
                extracted.append(("role", ("faculty",)))
            # A teacher mentioned as mentor, past contributor or future party
            # cannot supply a hard applicant role or study-level requirement.
            elif "教师" in clause and not levels:
                incomplete = True
                unknowns.append(
                    _unknown(
                        "applicant_scope_unknown",
                        "eligibility",
                        "教师称谓与本次申请人的关系未可靠识别，不据此排除。",
                        evidence,
                    )
                )
            if levels:
                extracted.append(("study_level", tuple(levels)))
                if "undergraduate" in levels and (
                    match.re is _PROJECT_APPLICANT
                    or match.re is _CAN_PARTICIPATE
                    or match.group().startswith(("报名对象", "申请对象", "申报对象", "选课对象"))
                    or (_CURRENT_APPLICANT_PREFIX.search(prefix) and _ENTRY_ACTION.search(clause))
                ):
                    current_student_proofs.append(evidence)
            years = tuple(int(value) for value in re.findall(r"(20\d{2})级", clause))
            if years:
                extracted.append(("entry_year", years))
            # Narrow separators keep 全校各学院 and 辅修专业 from becoming personal requirements.
            local_clause = clause
            for scope in ("武汉大学", "全校", "我校"):
                local_clause = local_clause.removeprefix(scope)
            college_match = re.search(
                r"(?:^|[:：，,\s]|大学)([\u4e00-\u9fff]{2,15}学院)(?=学生|本科生|研究生|20\d{2}级|[，,\s]|$)",
                local_clause,
            )
            if college_match and college_match[1] not in {"各学院", "全校各学院"}:
                extracted.append(("college", (college_match[1],)))
            major_match = re.search(r"专业(?:为|[:：])\s*([^，,。；;\s]+)", local_clause)
            if major_match is None:
                major_match = re.search(
                    r"(?:^|[:：，,\s]|大学)([\u4e00-\u9fff]{2,20})专业(?:的)?(?:学生|本科生|研究生)",
                    local_clause,
                )
            if major_match:
                extracted.append(("major", (major_match[1],)))
            if "全日制" in clause or "在校" in clause:
                incomplete = True
                status = "全日制" if "全日制" in clause else "在校"
                constraints.append(
                    QualificationConstraint(
                        field="student_status",
                        values=(status,),
                        operator="unsupported",
                        evidence=evidence,
                    )
                )
                unknowns.append(
                    _unknown(
                        "unsupported_condition",
                        "student_status",
                        "Profile 未保存学籍状态。",
                        evidence,
                    )
                )
            for constraint_field, values in extracted:
                proof = (
                    _evidence(field, text, *faculty_applicant.span())
                    if constraint_field == "role" and faculty_applicant is not None
                    else evidence
                )
                constraints.append(
                    QualificationConstraint(
                        field=constraint_field,
                        values=values,
                        operator="unsupported" if ambiguous else "one_of",
                        evidence=proof,
                    )
                )
            # Completeness is restricted to fully consumed small audience expressions.
            # Recognizing 本科生 alone must not erase a comma followed by extra requirements.
            residue = clause
            for constraint_field, values in extracted:
                if constraint_field in {"college", "major"}:
                    for value in values:
                        residue = residue.replace(str(value), "")
                elif constraint_field == "entry_year":
                    for value in values:
                        residue = residue.replace(f"{value}级", "")
            residue = re.sub(
                r"武汉大学|全校|我校|全体|本科生|硕士研究生|博士研究生|研究生|教师|"
                r"专业为|专业[:：]|学生|均可|可以|报名|申请|参加|征集|可|且|为|的|[、和及，,\s]",
                "",
                residue,
            )
            if residue:
                incomplete = True
                constraints.append(
                    QualificationConstraint(
                        field="other", operator="unsupported", evidence=evidence
                    )
                )
                unknowns.append(
                    _unknown(
                        "unrecognized_condition",
                        "eligibility",
                        "对象声明含未完整理解的附加内容。",
                        evidence,
                    )
                )
            if not extracted:
                incomplete = True
                unknowns.append(
                    _unknown(
                        "unrecognized_audience",
                        "eligibility",
                        "对象声明不属于支持的资格表达式。",
                        evidence,
                    )
                )
                constraints.append(
                    QualificationConstraint(
                        field="other", operator="unsupported", evidence=evidence
                    )
                )
        for constraint_field, pattern in _UNSUPPORTED_QUALIFICATIONS:
            for match in pattern.finditer(text):
                incomplete = True
                evidence = _evidence(field, text, match.start(), match.end())
                stored_field = (
                    constraint_field
                    if constraint_field in {"gpa", "language", "grade"}
                    else "other"
                )
                constraints.append(
                    QualificationConstraint(
                        field=stored_field, operator="unsupported", evidence=evidence
                    )
                )
                unknowns.append(
                    _unknown(
                        "unsupported_condition",
                        constraint_field,
                        "正文有当前 Profile 无法核对的条件，不默认为符合。",
                        evidence,
                    )
                )
        for pattern in _PROJECT_CONDITIONS:
            for match in pattern.finditer(text):
                incomplete = True
                proof = _evidence(field, text, *match.span())
                constraints.append(
                    QualificationConstraint(field="other", operator="unsupported", evidence=proof)
                )
                unknowns.append(
                    _unknown(
                        "project_condition_unknown",
                        "eligibility",
                        "项目团队、毕业年级、参与限制或导师资格需另行核对，不默认为符合。",
                        proof,
                    )
                )
        # Unknown additional mandatory conditions cannot be erased by recognizing a student label.
        extra = re.search(
            r"(?:还须|还需|必须|应当|须具备|(?:报名|申请|选课|参与)[^。；;\n]{0,8}"
            r"(?:须|需|应|要求))[^。；;\n]+",
            text,
        )
        if extra:
            incomplete = True
            evidence = _evidence(field, text, extra.start(), extra.end())
            constraints.append(
                QualificationConstraint(field="other", operator="unsupported", evidence=evidence)
            )
            unknowns.append(
                _unknown(
                    "unsupported_condition", "other", "有额外强制条件，首版未完整理解。", evidence
                )
            )
        sections = tuple(_qualification_sections(text))
        if not sections and audience_matches and (alternative := _UNSCOPED_OR.search(text)):
            incomplete = True
            constraints = [
                item.model_copy(update={"operator": "unsupported"})
                if item.evidence.field == field
                else item
                for item in constraints
            ]
            proof = _evidence(field, text, alternative.start(), alternative.end())
            unknowns.append(
                _unknown("unsupported_or", "eligibility", "跨句的资格 OR 无法可靠分组。", proof)
            )
        for section_start, body_start, section_end in sections:
            section = text[body_start:section_end]
            for item in _NUMBERED_CONDITION.finditer(section):
                start = body_start + item.start("value")
                end = body_start + item.end("value")
                if any(match.start() <= start and end <= match.end() for match in audience_matches):
                    continue
                incomplete = True
                evidence = _evidence(field, text, start, end)
                constraints.append(
                    QualificationConstraint(
                        field="other", operator="unsupported", evidence=evidence
                    )
                )
                unknowns.append(
                    _unknown(
                        "unparsed_condition", "eligibility", "编号资格条件尚未完整理解。", evidence
                    )
                )
            alternative = _SECTION_OR.search(section)
            if alternative:
                incomplete = True
                constraints = [
                    item.model_copy(update={"operator": "unsupported"})
                    if item.evidence.field == field
                    and section_start <= item.evidence.start < section_end
                    else item
                    for item in constraints
                ]
                proof = _evidence(
                    field, text, body_start + alternative.start(), body_start + alternative.end()
                )
                unknowns.append(
                    _unknown(
                        "unsupported_or",
                        "eligibility",
                        "资格章节有跨条目的 OR，不能按全部条件合取。",
                        proof,
                    )
                )
    assistant = any(item.context == "opportunity" for item in _teaching_assistant_topics(content))
    if assistant:
        for match in _TA_APPLICANT_OBJECT.finditer(content.body_text):
            audience_declared, incomplete = True, True
            clause = match["value"]
            proof = _evidence("body_text", content.body_text, *match.span())
            levels = tuple(
                level
                for level, word in (
                    ("undergraduate", "本科生"),
                    ("master", "硕士研究生"),
                    ("doctoral", "博士研究生"),
                    ("faculty", "教师"),
                )
                if word in clause
            )
            conflicting = [
                item
                for item in constraints
                if item.field == "study_level"
                and item.operator != "unsupported"
                and levels
                and set(item.values) != set(levels)
            ]
            if conflicting:
                unknowns.append(
                    _unknown(
                        "conflicting_evidence",
                        "eligibility",
                        "招聘对象与后文申请分支不一致；不能只选研究生声明而排除本科分支。",
                        *(item.evidence for item in conflicting),
                        proof,
                    )
                )
                constraints = [
                    item.model_copy(update={"operator": "unsupported"})
                    if item in conflicting
                    else item
                    for item in constraints
                ]
            constraints.append(
                QualificationConstraint(
                    field="study_level" if levels else "other",
                    values=levels,
                    operator="unsupported",
                    evidence=proof,
                )
            )
            unknowns.append(
                _unknown(
                    "recruitment_condition_unknown",
                    "eligibility",
                    "申请对象包含不同层次分支及附加条件，当前无法可靠逐分支核验。",
                    proof,
                )
            )
        for constraint_field, pattern in _TA_CONDITIONS:
            for match in pattern.finditer(content.body_text):
                incomplete = True
                proof = _evidence("body_text", content.body_text, *match.span())
                constraints.append(
                    QualificationConstraint(
                        field=constraint_field
                        if constraint_field in {"grade", "student_status"}
                        else "other",
                        operator="unsupported",
                        evidence=proof,
                    )
                )
                unknowns.append(
                    _unknown(
                        "application_channel_unverified"
                        if constraint_field == "application_channel"
                        else "unsupported_condition",
                        constraint_field,
                        "外部申请载体及群文件未访问，需核对申请表和实际岗位。"
                        if constraint_field == "application_channel"
                        else "助教申请的附加条件无法从当前 Profile 核验，不能默认符合。",
                        proof,
                    )
                )
        for match in _TA_AUDIENCE.finditer(content.body_text):
            audience_declared, incomplete = True, True
            proof = _evidence("body_text", content.body_text, *match.span())
            clause = match["value"]
            levels = (
                ("master",)
                if "硕士研究生" in clause
                else ("doctoral",)
                if "博士研究生" in clause
                else ("master", "doctoral")
                if "研究生" in clause
                else ("undergraduate",)
                if "本科生" in clause
                else ()
            )
            if levels:
                constraints.append(
                    QualificationConstraint(
                        field="study_level",
                        values=levels,
                        operator="unsupported" if match["soft"] else "one_of",
                        evidence=proof,
                    )
                )
            unknowns.append(
                _unknown(
                    "recruitment_condition_unknown",
                    "eligibility",
                    "聘任对象含院属、学籍或软限定；不把原则上当作硬排除，也不能确认全部资格。",
                    proof,
                )
            )
        for match in _TA_APPROVAL.finditer(content.body_text):
            incomplete = True
            unknowns.append(
                _unknown(
                    "teacher_approval_required",
                    "eligibility",
                    "主讲教师的推荐或聘用审核需人工核对。",
                    _evidence("body_text", content.body_text, *match.span()),
                )
            )
    faculty_constraints = [
        item for item in constraints if item.field == "role" and item.values == ("faculty",)
    ]
    if faculty_constraints and current_student_proofs:
        incomplete = True
        proofs = tuple(item.evidence for item in faculty_constraints) + tuple(
            current_student_proofs
        )
        unknowns.append(
            _unknown(
                "conflicting_evidence",
                "eligibility",
                "同时存在当前教师征题和学生申报声明，未可靠分清各自范围。",
                *proofs,
            )
        )
        constraints = [
            item.model_copy(update={"operator": "unsupported"})
            if item in faculty_constraints or item.evidence in current_student_proofs
            else item
            for item in constraints
        ]
    # Multiple declarations on one field can be contradictory, not just an AND mismatch.
    for constraint_field in sorted(
        {item.field for item in constraints if item.operator != "unsupported"}
    ):
        related = [
            item
            for item in constraints
            if item.field == constraint_field and item.operator != "unsupported"
        ]
        if len(related) > 1 and not set.intersection(*(set(item.values) for item in related)):
            incomplete = True
            unknowns.append(
                _unknown(
                    "conflicting_evidence",
                    "eligibility",
                    "同一资格字段有互相冲突的声明。",
                    *(item.evidence for item in related),
                )
            )
            constraints = [
                item.model_copy(update={"operator": "unsupported"}) if item in related else item
                for item in constraints
            ]
    return tuple(constraints), audience_declared, audience_declared and not incomplete, unknowns


def _table_application_time_spans(content):
    """Bind only witnessed application/date cells in one HTML row to exact text."""
    soup = BeautifulSoup(content.body_html, "html.parser")
    spans = []
    for row in soup.select("table tr"):
        cells = row.find_all(("td", "th"), recursive=False)
        if not 2 <= len(cells) <= 4:
            continue
        label, date_cell = (" ".join(cell.get_text().split()) for cell in cells[:2])
        if not _TABLE_APPLICATION_LABEL.fullmatch(label) or not re.match(
            r"(?:截止|截至)(?:时间|日期)?\s*[:：]?\s*", date_cell
        ):
            continue
        cell_patterns = [
            r"\s*".join(re.escape(part) for part in value.split()) for value in (label, date_cell)
        ]
        matches = tuple(re.finditer(r"\s+".join(cell_patterns), content.body_text))
        if len(matches) == 1:
            spans.append(matches[0].span())
    return tuple(sorted(set(spans)))


def _time_clauses(content):
    """Keep source offsets; associate a bare heading only with its adjacent value."""
    table_spans = _table_application_time_spans(content)
    for field, text in _source_fields(content):
        sentences = tuple(_sentences(text))
        for index, (clause, start, end) in enumerate(sentences):
            if field == "body_text":
                containing = next(
                    (span for span in table_spans if span[0] <= start < span[1]), None
                )
                if containing is not None:
                    if start == containing[0]:
                        yield field, text, text[containing[0] : containing[1]], *containing
                    continue
            if _TIME_HEADING.fullmatch(clause.strip()):
                # A heading carries no date itself. Missing value stays unknown;
                # it must not invalidate another complete applicant deadline.
                continue
            if index:
                prior, prior_start, prior_end = sentences[index - 1]
                if (
                    _TIME_HEADING.fullmatch(prior.strip())
                    and re.fullmatch(r"\s*", text[prior_end:start])
                    and (_DATE.search(clause) or _TIMELESS_DATE.search(clause))
                    and len(clause) <= 240
                ):
                    yield field, text, text[prior_start:end], prior_start, end
                    continue
            yield field, text, clause, start, end


def _times(content, category):
    openings, deadlines, evidence, unknowns, lower_bounds = [], [], [], [], []
    deadline_proofs, extensions = [], []
    assistant = any(item.context == "opportunity" for item in _teaching_assistant_topics(content))
    if assistant:
        for match in _TA_MONTH_DAY_SUBMISSION.finditer(content.body_text):
            proof = _evidence("body_text", content.body_text, *match.span())
            evidence.append(proof)
            unknowns.append(
                _unknown(
                    "year_missing", "time", "申请月日缺少年份，不从学年或发布日期补全。", proof
                )
            )
        academic = _TA_ACADEMIC_YEAR.search(content.title)
        if academic is not None:
            for signature in _TA_SIGNATURE_DATE.finditer(content.body_text):
                year = int(signature["date"][:4])
                if not int(academic["first"]) <= year <= int(academic["last"]):
                    proofs = (
                        _evidence("title", content.title, *academic.span()),
                        _evidence("body_text", content.body_text, *signature.span("date")),
                    )
                    evidence.extend(proofs)
                    unknowns.append(
                        _unknown(
                            "recruitment_year_conflict",
                            "time",
                            "标题学年与独立落款年份冲突，不能确认当前开放或补成绝对截止。",
                            *proofs,
                        )
                    )
    for field, text, clause, start, end in _time_clauses(content):
        extension = _SCOPED_EXTENSION.fullmatch(clause.strip())
        if extension is not None:
            proof = _evidence(field, text, start, end)
            evidence.append(proof)
            dates = tuple(_DATE.finditer(extension["date"]))
            if len(dates) != 1 or _TIMELESS_DATE.search(extension["date"]):
                unknowns.append(
                    _unknown("year_missing", "time", "分组延期缺少唯一完整年份日期。", proof)
                )
            else:
                value = _date_value(dates[0], deadline=True)
                if value is None:
                    unknowns.append(
                        _unknown("invalid_time", "time", "分组延期日期或时刻无效。", proof)
                    )
                else:
                    groups = (
                        ("graduate", "international")
                        if "在华留学生组" in extension["groups"]
                        else ("graduate",)
                    )
                    extensions.extend((group, value, proof) for group in groups)
            continue
        prior_deadline_count = len(deadlines)
        if assistant and _TA_FORM_SUBMISSION.search(clause):
            proof = _evidence(field, text, start, end)
            evidence.append(proof)
            clocks = list(_TA_CLOCK.finditer(clause))
            if len(clocks) == 1:
                clock = clocks[0]
                try:
                    deadline = datetime(
                        *(int(clock[name]) for name in ("year", "month", "day", "hour")),
                        tzinfo=SHANGHAI,
                    )
                    if clock["weekday"] is not None and "一二三四五六日"[
                        deadline.weekday()
                    ] != clock["weekday"].replace("天", "日"):
                        raise ValueError("weekday mismatch")
                except ValueError:
                    unknowns.append(_unknown("invalid_time", "time", "申请表提交日期无效。", proof))
                else:
                    deadlines.append(deadline)
            else:
                unknowns.append(
                    _unknown(
                        "time_unrecognized",
                        "time",
                        "申请表提交时间不属于已验证的表达式。",
                        proof,
                    )
                )
            continue
        team_deadline = bool(
            _TEAM_SUBMISSION.search(clause)
            and any(re.match(r"\s*前", clause[match.end() :]) for match in _DATE.finditer(clause))
        )
        time_context = (
            _DEADLINE.search(clause)
            or team_deadline
            or re.search(r"报名开始|开放报名|开始报名|即日起|自通知发布之日起", clause)
        )
        if not time_context:
            continue
        proof = _evidence(field, text, start, end)
        evidence.append(proof)
        if _ADMIN_TIME.search(clause) and not team_deadline:
            # These are review/submission steps for teachers and units in
            # the witnessed project template, not the applicant's deadline.
            continue
        if _LATER_ROUND_TIME.search(clause):
            unknowns.append(
                _unknown(
                    "secondary_time_unknown",
                    "time",
                    "另行通知的第二轮报名不是已声明的首轮区间，仍保留未知。",
                    proof,
                )
            )
            continue
        if _DEFERRED_DEADLINE.search(clause):
            unknowns.append(
                _unknown(
                    "time_unrecognized",
                    "time",
                    "截止指向后续通知，不能把同句开始日期当截止。",
                    proof,
                )
            )
            continue
        dates = list(_DATE.finditer(clause))
        timeless_dates = list(_TIMELESS_DATE.finditer(clause))
        inherited_range = (
            bool(_PERIOD.search(clause))
            and len(dates) == 1
            and len(timeless_dates) == 1
            and dates[0].end() <= timeless_dates[0].start()
            and re.fullmatch(r"\s*(?:至|到)\s*", clause[dates[0].end() : timeless_dates[0].start()])
            is not None
        )
        declared_zones = _BRACKETED_TIMEZONE.findall(clause)
        declared_zones.extend(
            zone[1]
            for date in dates + timeless_dates
            if (zone := _SUFFIX_TIMEZONE.match(clause[date.end() :])) is not None
        )
        if _UNSUPPORTED_TIMEZONE.search(clause) or any(
            zone not in _LOCAL_TIMEZONES for zone in declared_zones
        ):
            unknowns.append(
                _unknown("unsupported_timezone", "time", "时间不属于支持的上海时间表达式。", proof)
            )
            continue
        if timeless_dates and not inherited_range:
            unknowns.append(
                _unknown("year_missing", "time", "月日缺少年份，不从发布日期推测。", proof)
            )
            continue
        relative_start = re.search(r"自通知发布之日起|即日起|现(?:开始|开放)报名", clause)
        if relative_start:
            openings.append(datetime.combine(content.published_date, time.min, SHANGHAI))
        if inherited_range:
            opens = _date_value(dates[0], deadline=False)
            deadline = _date_value(
                timeless_dates[0], deadline=True, inherited_year=int(dates[0]["year"])
            )
            if opens is not None and deadline is not None and opens <= deadline:
                openings.append(opens)
                deadlines.append(deadline)
            else:
                unknowns.append(_unknown("invalid_time", "time", "报名区间日期或顺序无效。", proof))
        elif _PERIOD.search(clause) and len(dates) == 2:
            opens = _date_value(dates[0], deadline=False)
            deadline = _date_value(dates[1], deadline=True)
            if opens is not None and deadline is not None and opens <= deadline:
                openings.append(opens)
                deadlines.append(deadline)
            else:
                unknowns.append(_unknown("invalid_time", "time", "报名区间日期或顺序无效。", proof))
        elif len(dates) == 1:
            opening_only = re.search(
                r"报名开始|开放报名|开始报名", clause
            ) and not _DEADLINE.search(clause)
            value = _date_value(dates[0], deadline=not opening_only)
            if value is None:
                unknowns.append(_unknown("invalid_time", "time", "日期或时刻无效。", proof))
            elif opening_only:
                openings.append(value)
            elif (_DEADLINE.search(clause) or relative_start or team_deadline) and (
                not _PERIOD.search(clause) or re.search(r"至|前|截止", clause)
            ):
                deadlines.append(value)
                if dates[0]["hour"] is None and re.match(r"\s*前", clause[dates[0].end() :]):
                    lower_bounds.append(datetime.combine(value.date(), time.min, SHANGHAI))
                    unknowns.append(
                        _unknown(
                            "imprecise_deadline",
                            "deadline",
                            "“日前”未声明精确时刻；保留当日日初至日末边界，请提前核对。",
                            proof,
                        )
                    )
            else:
                unknowns.append(
                    _unknown("ambiguous_time", "time", "日期用途不明确，不能猜测截止时间。", proof)
                )
        elif len(dates) > 2:
            unknowns.append(
                _unknown("ambiguous_time", "time", "同句有多个日期，首版不猜测报名区间。", proof)
            )
        elif not relative_start:
            unknowns.append(
                _unknown("time_unrecognized", "time", "时间声明没有支持的完整年份日期。", proof)
            )
        if len(deadlines) > prior_deadline_count:
            deadline_proofs.append(proof)
    opens_at = next(iter(set(openings))) if len(set(openings)) == 1 else None
    deadline_at = next(iter(set(deadlines))) if len(set(deadlines)) == 1 else None
    if len(set(openings)) > 1 or len(set(deadlines)) > 1:
        unknowns.append(
            _unknown(
                "conflicting_evidence",
                "time",
                "存在不同的开始或截止日期，不能挑选其一。",
                *evidence,
            )
        )
    if any(
        item.code
        in {
            "unsupported_timezone",
            "year_missing",
            "invalid_time",
            "ambiguous_time",
            "time_unrecognized",
            "recruitment_year_conflict",
        }
        for item in unknowns
    ):
        # A recognized date must not mask a competing unsupported time declaration.
        opens_at, deadline_at = None, None
    if opens_at is not None and deadline_at is not None and opens_at > deadline_at:
        unknowns.append(_unknown("conflicting_evidence", "time", "开始晚于截止。", *evidence))
        opens_at, deadline_at = None, None
    if category == "opportunity":
        if opens_at is None:
            unknowns.append(
                _unknown("opening_unknown", "time", "报名是否开始尚不能从可见正文核实。")
            )
        if deadline_at is None:
            unknowns.append(
                _unknown("deadline_unknown", "time", "没有唯一可核对的完整年份截止时间。")
            )
    lower = next(iter(set(lower_bounds))) if len(set(lower_bounds)) == 1 else None
    if deadline_at is None:
        lower = None
    scoped = ()
    if extensions and deadline_at is not None:
        # Explicit general registration followed by named graduate/international
        # extension. Preserve each branch, never choose a global min or max.
        audience = next(
            (
                match
                for match in _CAN_PARTICIPATE.finditer(content.body_text)
                if "本科生" in match.group() and "研究生" in match.group()
            ),
            None,
        )
        if audience is not None and len({value for _, value, _ in extensions}) == 1:
            audience_proof = _evidence("body_text", content.body_text, *audience.span())
            scoped = (
                ScopedDeadline(
                    applicant_group="undergraduate",
                    deadline_at=deadline_at,
                    deadline_lower_at=lower,
                    evidence=tuple(deadline_proofs) + (audience_proof,),
                ),
            ) + tuple(
                ScopedDeadline(
                    applicant_group=group,
                    deadline_at=value,
                    evidence=(proof,),
                )
                for group, value, proof in extensions
            )
        else:
            unknowns.append(
                _unknown(
                    "deadline_scope_unknown",
                    "time",
                    "分组截止与完整报名对象或基础期限的关系未可靠确定。",
                    *evidence,
                )
            )
        deadline_at, lower = None, None
        unknowns.append(
            _unknown(
                "scoped_deadline",
                "time",
                "截止按报名组别区分，不能选全篇最早或最晚日期；国际生身份仍需核对。",
                *(proof for _, _, proof in extensions),
            )
        )
    return opens_at, deadline_at, lower, tuple(evidence), unknowns, scoped


def extract_facts(content: NoticeContent) -> NoticeFacts:
    """Extract deterministic visible evidence. Each invocation performs full extraction."""
    media = tuple(
        Evidence(field=field, index=index, url=reference.url)
        for field in ("images", "attachments")
        for index, reference in enumerate(getattr(content, field))
    )
    topics, link_unknowns = _link_document_actions(content, _topics(content))
    unresolved_recruitment = tuple(
        item.evidence
        for item in topics
        if item.topic == "teaching_assistant" and item.context == "uncertain"
    )
    if unresolved_recruitment:
        link_unknowns += (
            _unknown(
                "topic_action_link_unknown",
                "interest",
                "助教招聘标题尚缺可关联的当前申请行动；不能借用其他主题的截止时间。",
                *unresolved_recruitment,
            ),
        )
    opportunities = _opportunities(content)
    opportunities += tuple(
        item.evidence
        for item in topics
        if item.topic == "teaching_assistant" and item.context == "opportunity"
    )
    if _RESULTS.search(content.title) or (
        _ADMIN_ACTION.search(content.title)
        and not any(item.context == "opportunity" and item.primary for item in topics)
    ):
        category = "reference"
    elif opportunities:
        category = "opportunity"
    elif content.body_text.strip():
        category = "information"
    else:
        category = "unknown"
    if category != "opportunity":
        opportunities = ()
    constraints, audience, complete, unknowns = _qualifications(content)
    unknowns.extend(link_unknowns)
    opens_at, deadline_at, deadline_lower_at, time_evidence, time_unknowns, scoped_deadlines = (
        _times(content, category)
    )
    current_application = _CURRENT_APPLICATION.search(content.body_text)
    if current_application is None:
        current_application = _linked_application_directive(content, topics)
    if (
        current_application is None
        and category == "opportunity"
        and deadline_at is not None
        and any(item.topic == "competition" and item.context == "opportunity" for item in topics)
        and _COMPETITION_SCHEDULE.search(content.body_text)
        and not _COMPETITION_DEFERRED.search(content.body_text)
        and not _RESULTS.search(content.title)
    ):
        # Two witnessed current-call templates plus their explicit application
        # schedule. A deadline, a recent date or a competition title alone is
        # insufficient; do not invent an exact start timestamp.
        current_application = _COMPETITION_CURRENT.search(content.body_text)
        if (
            current_application is not None
            and _noncurrent_applicant_stage(
                content.body_text, current_application.start(), current_application.end()
            )
            is not None
        ):
            current_application = None
    if current_application is None and any(
        item.topic == "teaching_assistant" and item.context == "opportunity" for item in topics
    ):
        current_application = _TA_FORM_SUBMISSION.search(content.body_text)
        if current_application is None:
            # The recorded current application instruction confirms opening;
            # an isolated future date or the recruitment title does not.
            for item in topics:
                if item.topic != "teaching_assistant" or item.context != "opportunity":
                    continue
                for proof in item.supporting_evidence:
                    current_application = _DIRECTIVE_ENTRY.search(
                        content.body_text, proof.start, proof.end
                    )
                    if current_application is None:
                        current_application = _PRESENT_ENTRY.search(
                            content.body_text, proof.start, proof.end
                        )
                    if current_application is not None:
                        break
                if current_application is not None:
                    break
                if item.evidence.field == "body_text":
                    left, right = _clause_bounds(
                        content.body_text, item.evidence.start, item.evidence.end
                    )
                    current_application = _TA_PRESENT.search(content.body_text, left, right)
                    if current_application is not None:
                        break
    if any(item.code == "recruitment_year_conflict" for item in time_unknowns):
        current_application = None
    if current_application is not None and category == "opportunity":
        # This explicit present-tense permission confirms the witnessed team's
        # application is open; it does not supply an invented start timestamp.
        time_evidence += (
            _evidence(
                "body_text",
                content.body_text,
                current_application.start(),
                current_application.end(),
            ),
        )
        time_unknowns = [item for item in time_unknowns if item.code != "opening_unknown"]
    unknowns.extend(time_unknowns)
    incomplete = bool(media and not content.body_text.strip())
    critical = _CRITICAL_MEDIA.search(content.body_text)
    if critical is not None:
        incomplete = True
        unknowns.append(
            _unknown(
                "media_required",
                "media",
                "关键条件或时间指向未解析的媒体。",
                _evidence("body_text", content.body_text, critical.start(), critical.end()),
            )
        )
    if content.images and re.search(
        r"(?:选课|报名|时间|资格)[^。；\n]{0,35}安排如下|(?:时间|资格|条件)[^。；\n]{0,12}如下",
        content.body_text,
    ):
        incomplete = True
        unknowns.append(
            _unknown("media_required", "media", "安排指向图片，首版不读取图片内容。", *media)
        )
    if incomplete:
        complete = False
        if not any(item.code == "media_required" for item in unknowns):
            unknowns.append(
                _unknown("media_required", "media", "有意义的正文仅在未解析媒体中。", *media)
            )
    if category == "opportunity" and not audience:
        unknowns.append(
            _unknown(
                "audience_unknown", "eligibility", "没有明确支持的报名对象，不能视为面向全体。"
            )
        )
    return NoticeFacts(
        content_sha256=content.content_sha256(),
        title=content.title,
        published_date=content.published_date,
        body_text=content.body_text,
        topic_matches=topics,
        category=category,
        opportunity_evidence=opportunities,
        constraints=constraints,
        audience_declared=audience,
        eligibility_complete=complete,
        opens_at=opens_at,
        deadline_at=deadline_at,
        deadline_lower_at=deadline_lower_at,
        scoped_deadlines=scoped_deadlines,
        opening_confirmed=opens_at is not None or current_application is not None,
        time_evidence=time_evidence,
        information_incomplete=incomplete,
        media=media,
        unknowns=tuple(unknowns),
    )
