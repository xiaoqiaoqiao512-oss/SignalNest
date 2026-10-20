"""Simulated model HTTP uses real UC/CS Parser, archive, index and read-only databases."""

import hashlib
import json
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
import sqlalchemy as sa
from bs4 import BeautifulSoup
from pydantic import ValidationError

from signalnest.config import load_config
from signalnest.ingestion import ResponseInput, import_page
from signalnest.instance_lock import writer_lock
from signalnest.model_client import ModelBudget, ModelConfig
from signalnest.querying.agent import ask, render_report
from signalnest.querying.config import QueryLimits, load_query_config, load_query_inputs
from signalnest.querying.contracts import QueryError, Question
from signalnest.querying.dates import publication_window, seed_keywords
from signalnest.querying.tools import QueryTools, tool_definitions
from signalnest.rawstore import RawStore
from signalnest.schema import documents
from signalnest.search import get_notice
from signalnest.sources import CS, UC
from signalnest.storage import initialize_storage, open_initialized_engine

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "research/fixtures"
AT = datetime.fromisoformat("2026-10-12T14:00:00+08:00")
QUESTION = Question(text="有没有选课或助教招聘？列出条件和截止时间。", at=AT)


def completion(name, args, identifier="call_1", **kwargs):
    return {
        "choices": [
            {
                "finish_reason": "tool_calls",
                "message": {
                    "role": "assistant",
                    "tool_calls": [
                        {
                            "id": identifier,
                            "type": "function",
                            "function": {
                                "name": name,
                                "arguments": json.dumps(args, ensure_ascii=False),
                            },
                        }
                    ],
                },
            }
        ],
        "usage": {"prompt_tokens": 100, "completion_tokens": 100},
    } | kwargs


def model_settings(**overrides):
    return ModelConfig(
        model="test-model",
        input_usd_per_million="1",
        output_usd_per_million="4",
        max_cost_usd="1",
        **overrides,
    )


def import_capture(settings, binding, name, url, identity=None, **options):
    engine = open_initialized_engine(settings.storage.database)
    list_parser, notice_parser, parser_version = binding.parsers()
    try:
        return import_page(
            engine,
            RawStore(settings.storage.data_dir),
            ResponseInput(
                page_type="notice" if identity else "list",
                source_id=settings.source.id,
                source_document_id=identity,
                requested_url=url,
                final_url=url,
                fetched_at=100,
                status_code=200,
            ),
            (FIXTURES / name).read_bytes(),
            102,
            notice_parser=notice_parser,
            list_parser=list_parser,
            list_parser_version=parser_version,
            **options,
        )
    finally:
        engine.dispose()


def import_text_notice(campus, body, title="本科生科研训练项目报名通知"):
    # Change an in-memory copy; real Parser, archive and version transaction still run.
    tree = BeautifulSoup((FIXTURES / "current-notice-detail.html").read_bytes(), "html.parser")
    tree.select_one(".title_nei > b").string = title
    region = tree.select_one(".v_news_content")
    region.clear()
    paragraph = tree.new_tag("p")
    paragraph.string = body
    region.append(paragraph)
    engine = open_initialized_engine(campus.instances["uc"].storage.database)
    try:
        imported = import_page(
            engine,
            RawStore(campus.instances["uc"].storage.data_dir),
            ResponseInput(
                page_type="notice",
                source_id=UC.source_id,
                source_document_id="1517:128231",
                requested_url="https://uc.whu.edu.cn/info/1517/128231.htm",
                final_url="https://uc.whu.edu.cn/info/1517/128231.htm",
                fetched_at=100,
                status_code=200,
            ),
            tree.encode("utf-8"),
            104,
        )
        assert imported.outcome == "processed"
        return imported
    finally:
        engine.dispose()


@pytest.fixture
def campus(tmp_path):
    instances = {}
    for identifier, example, binding, listing in (
        ("uc", "config.example.toml", UC, "student-notices-page1.html"),
        ("cs", "config.cs.example.toml", CS, "ta-entrypoints-20261009/cs-undergrad-list-home.html"),
    ):
        directory = tmp_path / identifier
        directory.mkdir()
        path = directory / "instance.toml"
        path.write_bytes((ROOT / example).read_bytes())
        settings = load_config(path)
        initialize_storage(settings.storage)
        import_capture(settings, binding, listing, str(settings.source.list_url))
        instances[identifier] = settings
    uc = import_capture(
        instances["uc"],
        UC,
        "current-notice-detail.html",
        "https://uc.whu.edu.cn/info/1517/128231.htm",
        "1517:128231",
    )
    cs = import_capture(
        instances["cs"],
        CS,
        "ta-entrypoints-20261009/cs-ta-recruit-64481.html",
        "https://cs.whu.edu.cn/info/1074/64481.htm",
        "1074:64481",
    )
    (tmp_path / "profile.toml").write_bytes(
        (ROOT / "profile.cs-undergrad.example.toml").read_bytes()
    )
    query_file = tmp_path / "ask.toml"
    query_file.write_text(
        'profile = "profile.toml"\n[[instances]]\nid="uc"\nconfig="uc/instance.toml"\n'
        '[[instances]]\nid="cs"\nconfig="cs/instance.toml"\n',
        encoding="utf-8",
    )
    yield SimpleNamespace(
        instances=instances,
        settings=load_query_config(query_file),
        query_file=query_file,
        uc=uc,
        cs=cs,
    )


@pytest.fixture
def tools(campus):
    instances, profile = load_query_inputs(campus.settings)
    budget = ModelBudget(max_calls=6, max_requests=8, run_seconds=90)
    tools = QueryTools(campus.settings, instances, profile, QUESTION, budget)
    yield tools
    tools.close()


def call(tools, name, **args):
    return tools.call(name, json.dumps(args, ensure_ascii=False))


def read(tools, identifier, document_id, **args):
    result = call(tools, "read_notice", instance_id=identifier, document_id=document_id, **args)
    assert result["ok"], result
    return result["data"]


def selection(notice, **overrides):
    return {
        "reference": notice["reference"],
        "relevance": [{"field": "title", "text": notice["title"]}],
        "conditions": [],
        "deadline": [],
    } | overrides


def digests(campus):
    return {
        i: hashlib.sha256(s.storage.database.read_bytes()).hexdigest()
        for i, s in campus.instances.items()
    }


def test_cross_instance_search_detail_profile_and_status_remain_readonly(campus, tools):
    before = digests(campus)
    with (
        writer_lock(campus.instances["uc"].storage.database),
        writer_lock(campus.instances["cs"].storage.database),
    ):
        result = call(tools, "search", query="助教招聘")["data"]
        assert {r["instance_id"] for r in result["sources"]} == {"uc", "cs"}
        assert result["sources"][0]["total"] == 0
        assert result["sources"][1]["items"][0]["document_id"] == campus.cs.document_id
        assert call(tools, "search", query="选课")["data"]["sources"][0]["total"] == 1
        uc = read(tools, "uc", campus.uc.document_id)
        cs = read(tools, "cs", campus.cs.document_id)
        assert uc["source_document_id"] == "1517:128231"
        assert cs["source_document_id"] == "1074:64481"
        assert cs["reference"] != uc["reference"]
        assert uc["attachments"] or uc["images"]
        assert cs["media_contents_read"] is False
        assert "body_html" not in cs
        assert cs["facts"]["official_eligibility_verified"] is False
        assert call(tools, "read_profile")["data"]["profile"]["study_level"] == "undergraduate"
        status = call(tools, "source_status")["data"]
        assert len(status) == 2 and status[0]["first_processing"]["total"] > 0
        assert status[0]["source"]["last_complete_scan_at"] is None
        with tools._engine("uc").connect() as connection:
            assert connection.exec_driver_sql("PRAGMA query_only").scalar_one() == 1
            with pytest.raises(sa.exc.SQLAlchemyError):
                connection.exec_driver_sql("DELETE FROM documents")
    assert digests(campus) == before


def test_same_local_id_is_resolved_using_instance(campus, tools):
    # This same integer exists in both real lists; the CS row is a different notice.
    identifier = campus.uc.document_id
    assert call(tools, "read_notice", instance_id="uc", document_id=identifier)["ok"]
    other = call(tools, "read_notice", instance_id="cs", document_id=identifier)
    if campus.cs.document_id == identifier:
        assert other["data"]["source_document_id"] == "1074:64481"
    else:
        assert other["error_code"] == "notice_not_processed"


@pytest.mark.parametrize(
    "name,args,code",
    [
        ("shell", {"command": "touch forbidden"}, "unknown_tool"),
        ("crawl_once", {}, "unknown_tool"),
        (
            "read_notice",
            {"instance_id": "../../private", "document_id": 1},
            "invalid_tool_arguments",
        ),
        ("read_notice", {"instance_id": "missing", "document_id": 1}, "unknown_instance"),
        ("read_notice", {"instance_id": "uc", "document_id": 99999}, "document_not_found"),
        ("read_notice", {"instance_id": "uc", "document_id": True}, "invalid_tool_arguments"),
        ("read_notice", {"instance_id": "uc", "document_id": 2**63}, "invalid_tool_arguments"),
        (
            "read_notice",
            {"instance_id": "uc", "document_id": 1, "url": "https://evil.test"},
            "invalid_tool_arguments",
        ),
        ("search", {"query": "选课", "date_from": "2020-01-01"}, "invalid_tool_arguments"),
        ("search", {"query": "a b c d e f g h i"}, "invalid_tool_arguments"),
        ("read_profile", {"path": "/etc/signalnest/smtp.env"}, "invalid_tool_arguments"),
    ],
)
def test_fixed_tools_reject_unavailable_capabilities(tools, name, args, code):
    assert call(tools, name, **args) == {"ok": False, "error_code": code}


def test_only_delivered_quotes_can_be_cited(campus, tools):
    guessed = {
        "reference": f"uc:{campus.uc.document_id}:v{campus.uc.version_id}",
        "relevance": [{"field": "title", "text": "本科生选课"}],
    }
    assert call(tools, "answer", notices=[guessed])["error_code"] == "citation_not_read"
    notice = read(tools, "uc", campus.uc.document_id)
    bad = selection(notice, conditions=[{"field": "body_text", "text": "所有本科生都完全符合资格"}])
    assert call(tools, "answer", notices=[bad])["error_code"] == "citation_quote_invalid"
    supplied_url = selection(notice, original_url="https://evil.test")
    assert call(tools, "answer", notices=[supplied_url])["error_code"] == "invalid_tool_arguments"
    assert (
        call(tools, "answer", notices=[selection(notice), selection(notice)])["error_code"]
        == "duplicate_answer_reference"
    )
    result = call(tools, "answer", notices=[selection(notice)])["data"][0]
    assert result["original_url"] == notice["original_url"]
    assert "official_eligibility_not_verified" in result["unknown_codes"]
    assert result["conditions"] == [] and result["deadline"] == []
    with pytest.raises(QueryError, match="query_already_finished"):
        call(tools, "source_status")


def test_chunking_never_authenticates_unread_suffix(campus, tools):
    tools.settings = tools.settings.model_copy(
        update={"limits": QueryLimits(notice_chunk_chars=500)}
    )
    notice = read(tools, "uc", campus.uc.document_id)
    assert len(notice["body_text"]) == 500 and notice["next_offset"] == 500
    snapshot = tools._snapshots[("uc", campus.uc.document_id)]
    remaining = snapshot["detail"].content.body_text
    hidden = remaining[-35:]
    assert not any(hidden in text for _, text in tools._visible[notice["reference"]])
    proposed = selection(notice, conditions=[{"field": "body_text", "text": hidden}])
    assert call(tools, "answer", notices=[proposed])["error_code"] == "citation_quote_invalid"
    end = read(tools, "uc", campus.uc.document_id, offset=len(remaining) - 100)
    assert end["next_offset"] is None
    assert call(tools, "answer", notices=[proposed])["ok"]


def test_read_snapshot_is_pinned_across_new_commit(campus, tools):
    old = read(tools, "uc", campus.uc.document_id)
    engine = open_initialized_engine(campus.instances["uc"].storage.database)
    try:
        with engine.begin() as connection:
            # A real writer changes the latest processing diagnostic while no
            # query transaction is held across calls.
            connection.execute(
                documents.update()
                .where(documents.c.id == campus.uc.document_id)
                .values(
                    status="failed",
                    last_attempt_at=103,
                    last_error_code="missing_structure",
                )
            )
        raw = (
            (FIXTURES / "current-notice-detail.html")
            .read_bytes()
            .replace("选课".encode(), "选课（更新）".encode())
        )
        new = import_page(
            engine,
            RawStore(campus.instances["uc"].storage.data_dir),
            ResponseInput(
                page_type="notice",
                source_id=UC.source_id,
                source_document_id="1517:128231",
                requested_url="https://uc.whu.edu.cn/info/1517/128231.htm",
                final_url="https://uc.whu.edu.cn/info/1517/128231.htm",
                fetched_at=100,
                status_code=200,
            ),
            raw,
            104,
        )
        assert new.version_id != old["version_id"]
        assert get_notice(engine, campus.uc.document_id).version_id == new.version_id
    finally:
        engine.dispose()
    again = read(tools, "uc", campus.uc.document_id)
    assert again["processing_status"] == old["processing_status"] == "processed"
    assert again["content_sha256"] == old["content_sha256"]
    assert again["version_id"] == old["version_id"]


def test_full_model_loop_refines_reads_and_answers_from_real_data(campus, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-secret-not-a-live-key")
    settings = campus.settings.model_copy(update={"model": model_settings()})
    steps = []

    def handler(request):
        assert str(request.url) == "https://api.openai.com/v1/chat/completions"
        payload = json.loads(request.content)
        steps.append(payload)
        if len(steps) == 1:
            assert payload["store"] is False and payload["parallel_tool_calls"] is False
            return httpx.Response(
                200, json=completion("search", {"query": "助教招聘", "instance_id": "cs"}, "s")
            )
        if len(steps) == 2:
            data = json.loads(payload["messages"][-1]["content"])["data"]
            identifier = data["sources"][0]["items"][0]["document_id"]
            return httpx.Response(
                200,
                json=completion(
                    "read_notice", {"instance_id": "cs", "document_id": identifier}, "r"
                ),
            )
        body = json.loads(
            next(m["content"] for m in reversed(payload["messages"]) if m["role"] == "tool")
        )["data"]
        return httpx.Response(200, json=completion("answer", {"notices": [selection(body)]}, "a"))

    before = digests(campus)
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        report = ask(settings, QUESTION, client=client)
    assert report.status == "answered" and len(report.notices) == 1
    assert report.notices[0]["source_document_id"] == "1074:64481"
    assert report.notices[0]["original_url"] == "https://cs.whu.edu.cn/info/1074/64481.htm"
    assert len(steps) == report.model_usage["http_requests"] == 3
    assert digests(campus) == before
    text = render_report(report)
    assert "条件原文：未知" in text and "截止原文：未知" in text
    assert "official_eligibility_not_verified" in text
    assert "test-secret" not in report.model_dump_json()


def test_historical_teacher_scope_does_not_exclude_student_or_claim_verified_eligibility(
    campus, monkeypatch
):
    teacher_sentence = "面向全校教师共征集了1100余个在研科研项目选题"
    student_sentence = "项目负责人必须为本科生"
    body_text = f"{teacher_sentence}。{student_sentence}。请本科生报名科研训练项目。"
    imported = import_text_notice(campus, body_text)
    monkeypatch.setenv("OPENAI_API_KEY", "offline-placeholder")
    settings = campus.settings.model_copy(
        update={
            "model": model_settings(),
            "limits": QueryLimits(max_model_calls=2, max_requests=2),
        }
    )
    responses = []

    def handler(request):
        payload = json.loads(request.content)
        if not responses:
            responses.append("read")
            return httpx.Response(
                200,
                json=completion(
                    "read_notice", {"instance_id": "uc", "document_id": imported.document_id}
                ),
            )
        notice = json.loads(
            next(m["content"] for m in reversed(payload["messages"]) if m["role"] == "tool")
        )["data"]
        responses.append(notice)
        assert notice["facts"]["eligibility"] == "unknown"
        return httpx.Response(
            200,
            json=completion(
                "answer",
                {
                    "notices": [
                        selection(
                            notice,
                            conditions=[{"field": "body_text", "text": student_sentence}],
                        )
                    ]
                },
                "answer",
            ),
        )

    before = digests(campus)
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        report = ask(settings, Question(text="科研训练", at=AT), client=client)
    assert report.status == "answered" and report.error_code is None
    result = report.notices[0]
    assert result["official_eligibility_verified"] is False
    assert result["rule_eligibility"] == "unknown"
    assert result["rule_mismatch_evidence"] == []
    text = render_report(report)
    assert "本人资格：未知（尚未核对完整申请主体与条件）" in text
    assert "规则不匹配依据" not in text and student_sentence in text
    assert digests(campus) == before


def test_real_current_teacher_mismatch_keeps_bounded_original_evidence(campus, tools):
    sentence = "现面向全校教师征集2027年科研训练项目选题"
    body = sentence + "。这些项目由教师指导学生开展科研训练。"
    imported = import_text_notice(campus, body)
    before = digests(campus)
    data = read(tools, "uc", imported.document_id)
    assert data["facts"]["eligibility"] == "ineligible"
    assert data["facts"]["official_eligibility_verified"] is False
    # Read through the actual tools then compose a locally validated answer;
    # qualification evidence stays auditable without claiming official eligibility.
    outcome = call(tools, "answer", notices=[selection(data)])
    assert outcome["ok"], outcome
    mismatches = outcome["data"][0]["rule_mismatch_evidence"]
    assert 1 <= len(mismatches) <= 4
    role = next(item for item in mismatches if item["field"] == "role")
    proof = role["evidence"]
    assert sentence in proof["excerpt"]
    assert body[proof["start"] : proof["end"]] == proof["excerpt"]
    assert all(set(item) == {"field", "evidence"} for item in mismatches)
    assert digests(campus) == before


def test_model_failure_after_tool_read_returns_actual_candidates(campus, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "fake")
    settings = campus.settings.model_copy(update={"model": model_settings()})
    count = 0

    def handler(request):
        nonlocal count
        count += 1
        if count == 1:
            return httpx.Response(
                200,
                json=completion(
                    "read_notice", {"instance_id": "cs", "document_id": campus.cs.document_id}
                ),
            )
        return httpx.Response(401, text="secret upstream payload")

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        report = ask(settings, Question(text="选课", at=AT), client=client)
    assert report.status == "fallback" and report.error_code == "model_authentication_failed"
    assert {r["instance_id"] for r in report.search_results} == {"uc", "cs"}
    assert report.notices == ()
    assert "secret upstream" not in report.model_dump_json()


def test_call_tool_and_character_budgets_are_real_limits(campus, monkeypatch, tools):
    tools.settings = tools.settings.model_copy(update={"limits": QueryLimits(max_tool_calls=4)})
    for _ in range(4):
        assert call(tools, "read_profile")["ok"]
    with pytest.raises(QueryError, match="tool_call_budget"):
        call(tools, "read_profile")
    tools.trace.clear()
    tools.chars = tools.settings.limits.max_tool_chars
    assert (
        call(tools, "read_notice", instance_id="uc", document_id=campus.uc.document_id)[
            "error_code"
        ]
        == "tool_character_budget"
    )
    assert tools._visible == {}
    monkeypatch.setenv("OPENAI_API_KEY", "fake")
    settings = campus.settings.model_copy(
        update={"model": model_settings(), "limits": QueryLimits(max_model_calls=2)}
    )
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json=completion("read_profile", {}, f"call_{len(requests)}"))

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        report = ask(settings, QUESTION, client=client)
    # v3 reserves the two remaining calls for read+answer; a provider ignoring
    # required read_notice is rejected before any arbitrary tool executes.
    assert report.error_code == "model_required_tool_ignored" and len(requests) == 1
    assert report.search_results


def test_missing_credentials_and_stale_index_return_finite_fallback(campus, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    settings = campus.settings.model_copy(update={"model": model_settings()})
    report = ask(settings, QUESTION)
    assert report.error_code == "model_credentials_missing"
    assert report.model_usage["http_requests"] == 0
    engine = open_initialized_engine(campus.instances["uc"].storage.database)
    with engine.begin() as connection:
        connection.exec_driver_sql("DELETE FROM search_documents")
    engine.dispose()
    report = ask(campus.settings, QUESTION, local=True)
    assert "search_index_stale" in report.warnings
    # Queries never fix the stale index.
    engine = open_initialized_engine(campus.instances["uc"].storage.database)
    with engine.connect() as connection:
        assert connection.exec_driver_sql("SELECT count(*) FROM search_documents").scalar_one() == 0
    engine.dispose()


def test_programming_errors_are_not_disguised_as_model_failures(campus, monkeypatch):
    def defect(*args, **kwargs):
        raise AssertionError("implementation defect")

    monkeypatch.setattr("signalnest.querying.tools.search_notices", defect)
    with pytest.raises(AssertionError, match="implementation defect"):
        ask(campus.settings, QUESTION, local=True)


@pytest.mark.parametrize(
    "question,start,end",
    [
        ("最近一个月科研机会", "2026-09-12", "2026-10-12"),
        ("近30天科研机会", "2026-09-13", "2026-10-12"),
        ("最近一周科研机会", "2026-10-06", "2026-10-12"),
        ("本月竞赛", "2026-10-01", "2026-10-12"),
        ("上个月助教招聘", "2026-09-01", "2026-09-30"),
        ("今年科研", "2026-01-01", "2026-10-12"),
        ("今天通知", "2026-10-12", "2026-10-12"),
        ("2024-02-28至2024-03-01发布的通知", "2024-02-28", "2024-03-01"),
    ],
)
def test_program_resolves_publication_dates(question, start, end):
    window = publication_window(question, AT, "Asia/Shanghai")
    assert (window["date_from"], window["date_to"]) == (start, end)


@pytest.mark.parametrize(
    "question,warning",
    [
        ("近期通知", "date_expression_not_resolved"),
        ("最近一个月和今年的科研", "ambiguous_date_expression"),
        ("本月截止的报名", "deadline_window_not_supported"),
        ("报名截止于本月的机会", "deadline_window_not_supported"),
        ("2024-02-30到2024-03-01通知", "invalid_date_expression"),
        ("2026-10-01至2026-09-01通知", "invalid_date_expression"),
    ],
)
def test_unknown_date_never_becomes_publication_filter(question, warning):
    result = publication_window(question, AT, "Asia/Shanghai")
    assert result["warning"] == warning and result["date_from"] is None


def test_month_clamp_timezone_clock_and_seeds():
    day = datetime.fromisoformat("2024-03-31T16:30:00+00:00")
    window = publication_window("最近一个月科研", day, "UTC")
    assert window["date_from"] == "2024-02-29"
    assert publication_window("今天通知", day, "Asia/Shanghai")["date_to"] == "2024-04-01"
    assert seed_keywords("本科生科研机会") == ("科研", "创新训练")
    assert seed_keywords("助教招聘") == ("助教招聘",)
    with pytest.raises(ValidationError):
        Question(text="科研", at=datetime(2026, 1, 1))
    assert {t["function"]["name"] for t in tool_definitions()} == {
        "search",
        "read_notice",
        "read_profile",
        "source_status",
        "answer",
    }


def test_relative_date_filters_real_search_not_deadlines(campus):
    report = ask(campus.settings, Question(text="最近一个月选课通知", at=AT), local=True)
    assert report.status == "local" and report.search_results == ()
    assert report.publication_window["date_from"] == "2026-09-12"
    assert ask(campus.settings, Question(text="选课通知", at=AT), local=True).search_results


def test_real_research_notice_answers_conditions_and_deadline_with_publication_filter(
    campus, monkeypatch
):
    # Original research HTML, not a newly invented opportunity or a live request.
    imported = import_capture(
        campus.instances["uc"],
        UC,
        "notifications/notice-128291-20261005T133533Z.html",
        "https://uc.whu.edu.cn/info/1517/128291.htm",
        "1517:128291",
    )
    monkeypatch.setenv("OPENAI_API_KEY", "fake")
    at = datetime.fromisoformat("2026-09-10T12:00:00+08:00")
    settings = campus.settings.model_copy(update={"model": model_settings()})
    seen = []

    def handler(request):
        payload = json.loads(request.content)
        seen.append(payload)
        if len(seen) == 1:
            first = json.loads(payload["messages"][1]["content"])
            assert first["publication_window"]["date_from"] == "2026-08-10"
            hits = first["initial_retrieval"][0]["data"]["sources"][0]["items"]
            assert any(h["document_id"] == imported.document_id for h in hits)
            return httpx.Response(
                200,
                json=completion(
                    "read_notice",
                    {"instance_id": "uc", "document_id": imported.document_id},
                    "read",
                ),
            )
        notice = json.loads(payload["messages"][-1]["content"])["data"]
        evidence = [r["constraint"]["evidence"] for r in notice["facts"]["constraints"]]
        conditions = [{"field": e["field"], "text": e["excerpt"]} for e in evidence]
        times = [
            {"field": e["field"], "text": e["excerpt"]}
            for e in notice["facts"]["deadline_evidence"]
        ]
        assert times and conditions
        selected = selection(notice, conditions=conditions, deadline=times)
        return httpx.Response(200, json=completion("answer", {"notices": [selected]}, "answer"))

    before = digests(campus)
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        report = ask(
            settings,
            Question(
                text="最近一个月有没有本科生可以参加的科研机会？列出报名条件和截止时间。", at=at
            ),
            client=client,
        )
    assert report.status == "answered"
    assert report.query_version == "campus-query-v4"
    result = report.notices[0]
    assert result["published_date"] == "2026-09-07"
    assert result["extracted_deadline_at"] == "2026-09-28T23:59:00+08:00"
    assert result["deadline_evidence_status"] == "rule_supported"
    assert result["conditions"] and result["deadline"]
    assert result["published_date"] not in result["extracted_deadline_at"]
    assert digests(campus) == before


def test_title_date_cannot_be_presented_as_verified_application_deadline(campus, tools):
    notice = read(tools, "uc", campus.uc.document_id)
    result = call(
        tools,
        "answer",
        notices=[selection(notice, deadline=[{"field": "title", "text": notice["title"]}])],
    )["data"][0]
    assert result["deadline_evidence_status"] == "unknown"
    assert "deadline_quote_scope_unknown" in result["unknown_codes"]


@pytest.mark.parametrize(
    "body,quote",
    [
        (
            "报名开始时间为2026年9月1日。报名截止时间为2026年9月28日23:59。",
            "报名开始时间为2026年9月1日",
        ),
        (
            "报名时间为2026年9月1日至2026年9月28日23:59。",
            "报名时间为2026年9月1日至",
        ),
        (
            "报名截止时间为2026年9月28日23:59，请同学们报名。",
            "请同学们报名",
        ),
        (
            "报名截止时间为2026年9月28日23:59。各学院提交截止时间为2026年9月28日23:59。",
            "各学院提交截止时间为2026年9月28日23:59",
        ),
        (
            "报名截止时间为2026年9月28日23:59。各学院提交截止时间为2026年9月28日23:59。",
            "提交截止时间为2026年9月28日23:59",
        ),
        (
            "报名截止时间为2026年9月28日23:59。",
            "2026年9月28日23:59",
        ),
        (
            "学生提交截止时间为2026年9月28日23:59。各学院提交截止时间为2026年9月28日23:59。",
            "提交截止时间为2026年9月28日23:59",
        ),
    ],
    ids=[
        "opening",
        "range-start",
        "generic-action",
        "admin",
        "admin-substring",
        "bare-date",
        "ambiguous-occurrences",
    ],
)
def test_deadline_quotes_cannot_borrow_another_statement(campus, tools, body, quote):
    imported = import_text_notice(campus, body)
    before = digests(campus)
    notice = read(tools, "uc", imported.document_id)
    assert notice["facts"]["deadline_at"] == "2026-09-28T23:59:00+08:00"
    response = call(
        tools,
        "answer",
        notices=[selection(notice, deadline=[{"field": "body_text", "text": quote}])],
    )
    assert response["ok"]
    result = response["data"][0]
    assert result["extracted_deadline_at"] == "2026-09-28T23:59:00+08:00"
    assert result["deadline_evidence_status"] == "unknown"
    assert "deadline_quote_scope_unknown" in result["unknown_codes"]
    assert digests(campus) == before


@pytest.mark.parametrize(
    "body,quote",
    [
        (
            "报名开始时间为2026年9月1日。报名截止时间为2026年9月28日23:59。",
            "报名截止时间为2026年9月28日23:59",
        ),
        (
            "报名时间为2026年9月1日至2026年9月28日23:59。",
            "报名时间为2026年9月1日至2026年9月28日23:59",
        ),
        (
            "报名时间为2026年9月1日至9月28日23:59。",
            "报名时间为2026年9月1日至9月28日23:59",
        ),
        (
            "自通知发布之日起至2026年9月28日23:59报名。",
            "自通知发布之日起至2026年9月28日23:59报名",
        ),
    ],
    ids=["deadline", "range", "inherited-range", "relative-start"],
)
def test_selected_statement_itself_supports_the_same_deadline(campus, tools, body, quote):
    imported = import_text_notice(campus, body)
    before = digests(campus)
    notice = read(tools, "uc", imported.document_id)
    proofs = notice["facts"]["deadline_evidence"]
    assert len(proofs) == 1 and quote in proofs[0]["excerpt"]
    response = call(
        tools,
        "answer",
        notices=[selection(notice, deadline=[{"field": "body_text", "text": quote}])],
    )
    assert response["ok"]
    result = response["data"][0]
    assert result["deadline_evidence_status"] == "rule_supported"
    assert result["extracted_deadline_at"] == "2026-09-28T23:59:00+08:00"
    assert "deadline_quote_scope_unknown" not in result["unknown_codes"]
    assert "official_eligibility_not_verified" in result["unknown_codes"]
    assert digests(campus) == before


@pytest.mark.parametrize("keep_before", [True, False])
def test_deadline_quote_preserves_before_date_precision(campus, tools, keep_before):
    imported = import_text_notice(campus, "报名截止2026年9月28日前。")
    notice = read(tools, "uc", imported.document_id)
    quote = "报名截止2026年9月28日" + ("前" if keep_before else "")
    result = call(
        tools,
        "answer",
        notices=[selection(notice, deadline=[{"field": "body_text", "text": quote}])],
    )["data"][0]
    assert result["deadline_evidence_status"] == ("rule_supported" if keep_before else "unknown")
    assert result["extracted_deadline_at"] == "2026-09-28T23:59:59+08:00"
    assert result["deadline_lower_at"] == "2026-09-28T00:00:00+08:00"
    assert "imprecise_deadline" in result["unknown_codes"]


@pytest.mark.parametrize(
    "article,fixture,deadline,lower",
    [
        (
            "117011",
            "notifications/notice-117011-20261005T133549Z.html",
            "2024-12-10T00:00:00+08:00",
            None,
        ),
        (
            "17361",
            "notifications/notice-17361-20261005T133538Z.html",
            "2024-02-25T23:59:59+08:00",
            "2024-02-25T00:00:00+08:00",
        ),
    ],
)
def test_real_minor_and_team_submission_deadline_proofs(
    campus, tools, article, fixture, deadline, lower
):
    # These historical fixture targets are not in today's list. Register them
    # explicitly as offline/unknown discovery, as the fixed-corpus runner does.
    engine = open_initialized_engine(campus.instances["uc"].storage.database)
    try:
        with engine.begin() as connection:
            connection.execute(
                documents.insert().values(
                    source_id=UC.source_id,
                    source_document_id=f"1517:{article}",
                    discovered_title="离线固定样本目标",
                    detail_url=f"https://uc.whu.edu.cn/info/1517/{article}.htm",
                    discovered_at=100,
                    discovery_origin="unknown",
                )
            )
    finally:
        engine.dispose()
    imported = import_capture(
        campus.instances["uc"],
        UC,
        fixture,
        f"https://uc.whu.edu.cn/info/1517/{article}.htm",
        f"1517:{article}",
    )
    before = digests(campus)
    notice = read(tools, "uc", imported.document_id)
    proof = notice["facts"]["deadline_evidence"][0]
    result = call(
        tools,
        "answer",
        notices=[selection(notice, deadline=[{"field": proof["field"], "text": proof["excerpt"]}])],
    )["data"][0]
    assert result["deadline_evidence_status"] == "rule_supported"
    assert result["extracted_deadline_at"] == deadline
    assert result["deadline_lower_at"] == lower
    assert digests(campus) == before


@pytest.mark.parametrize("field", ["title", "body_text"])
def test_title_deadline_support_is_confined_to_its_own_statement(campus, tools, field):
    imported = import_text_notice(
        campus,
        "报名开始时间为2026年9月1日。",
        title="科研训练项目报名截止2026年9月28日23:59",
    )
    notice = read(tools, "uc", imported.document_id)
    assert notice["facts"]["deadline_at"] == "2026-09-28T23:59:00+08:00"
    assert {e["field"] for e in notice["facts"]["deadline_evidence"]} == {"title"}
    result = call(
        tools,
        "answer",
        notices=[
            selection(
                notice,
                deadline=[
                    {
                        "field": field,
                        "text": notice["title"]
                        if field == "title"
                        else "报名开始时间为2026年9月1日",
                    }
                ],
            )
        ],
    )["data"][0]
    assert result["deadline_evidence_status"] == (
        "rule_supported" if field == "title" else "unknown"
    )


@pytest.mark.parametrize("include_deadline", [False, True])
def test_model_opening_quote_is_reported_as_unknown_in_the_real_loop(
    campus, monkeypatch, include_deadline
):
    imported = import_text_notice(
        campus, "报名开始时间为2026年9月1日。报名截止时间为2026年9月28日23:59。"
    )
    monkeypatch.setenv("OPENAI_API_KEY", "fake-offline-key")
    settings = campus.settings.model_copy(update={"model": model_settings()})
    seen = []

    def handler(request):
        payload = json.loads(request.content)
        seen.append(payload)
        if len(seen) == 1:
            return httpx.Response(
                200,
                json=completion(
                    "read_notice", {"instance_id": "uc", "document_id": imported.document_id}
                ),
            )
        notice = json.loads(payload["messages"][-1]["content"])["data"]
        assert notice["facts"]["deadline_evidence"][0]["excerpt"].startswith("报名截止")
        quotes = [{"field": "body_text", "text": "报名开始时间为2026年9月1日"}]
        if include_deadline:
            proof = notice["facts"]["deadline_evidence"][0]
            quotes.append({"field": proof["field"], "text": proof["excerpt"]})
        selected = selection(notice, deadline=quotes)
        return httpx.Response(200, json=completion("answer", {"notices": [selected]}, "end"))

    before = digests(campus)
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        report = ask(settings, Question(text="科研训练报名截止时间", at=AT), client=client)
    assert report.status == "answered" and report.model_usage["http_requests"] == 2
    assert report.query_version == "campus-query-v4"
    assert report.notices[0]["deadline_evidence_status"] == "unknown"
    assert "截止原文：未知；时间原文待核对：报名开始时间为2026年9月1日" in render_report(report)
    assert digests(campus) == before


def test_failed_recheck_still_reads_old_success_and_exposes_diagnostic(campus, tools):
    engine = open_initialized_engine(campus.instances["uc"].storage.database)
    with engine.begin() as connection:
        connection.execute(
            documents.update()
            .where(documents.c.id == campus.uc.document_id)
            .values(
                status="failed",
                last_attempt_at=103,
                last_error_code="missing_structure",
            )
        )
    engine.dispose()
    notice = read(tools, "uc", campus.uc.document_id)
    assert notice["version_id"] == campus.uc.version_id
    assert (
        notice["processing_status"] == "failed" and notice["last_error_code"] == "missing_structure"
    )
    report = ask(campus.settings, QUESTION, local=True)
    assert "uc:failed_recheck_using_saved_version" in report.warnings


def test_one_unavailable_source_does_not_erase_other_results(campus):
    campus.instances["cs"].storage.database.unlink()
    report = ask(campus.settings, Question(text="选课", at=AT), local=True)
    assert report.search_results and {h["instance_id"] for h in report.search_results} == {"uc"}
    assert (
        "cs:storage_unavailable" in report.warnings and "tool_results_incomplete" in report.warnings
    )


def test_invalid_tool_json_fails_without_echoing_it(tools):
    result = tools.call("read_profile", '{"credential": private-raw-data}')
    assert result == {"ok": False, "error_code": "invalid_tool_arguments"}


def test_direct_id_read_cannot_bypass_resolved_publication_window(campus):
    instances, profile = load_query_inputs(campus.settings)
    tools = QueryTools(
        campus.settings,
        instances,
        profile,
        Question(text="最近一个月选课", at=AT),
        ModelBudget(max_calls=2, max_requests=2, run_seconds=30),
    )
    try:
        result = call(tools, "read_notice", instance_id="uc", document_id=campus.uc.document_id)
        assert result["error_code"] == "notice_outside_publication_window"
        assert tools._visible == {} and not tools.search_results
        assert tools.trace[-1].instance_id == "uc"
        assert tools.trace[-1].document_id == campus.uc.document_id
    finally:
        tools.close()


def test_unknown_citations_never_become_successful_final_answer(campus, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "fake")
    settings = campus.settings.model_copy(
        update={
            "model": model_settings(),
            "limits": QueryLimits(max_model_calls=2),
        }
    )
    invented = {"reference": "uc:999:v1", "relevance": [{"field": "title", "text": "虚构科研机会"}]}

    def handler(request):
        payload = json.loads(request.content)
        if payload["tool_choice"]["function"]["name"] == "read_notice":
            return httpx.Response(
                200,
                json=completion(
                    "read_notice",
                    {"instance_id": "uc", "document_id": campus.uc.document_id},
                    "read-valid",
                ),
            )
        return httpx.Response(200, json=completion("answer", {"notices": [invented]}, "invented"))

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        report = ask(settings, Question(text="选课", at=AT), client=client)
    assert report.status == "fallback" and report.notices == ()
    assert report.search_results and report.error_code == "citation_not_read"
    assert report.tool_trace[-1].error_code == "citation_not_read"


def test_model_requested_mutation_is_rejected_in_full_loop(campus, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "fake")
    settings = campus.settings.model_copy(update={"model": model_settings()})
    before = digests(campus)
    count = 0

    def handler(request):
        nonlocal count
        count += 1
        if count == 1:
            return httpx.Response(200, json=completion("mail_drain", {"send": True}, "mutate"))
        payload = json.loads(request.content)
        assert json.loads(payload["messages"][-1]["content"])["error_code"] == "unknown_tool"
        return httpx.Response(200, json=completion("answer", {"notices": []}, "finish"))

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        report = ask(settings, QUESTION, client=client)
    assert report.tool_trace[-2].error_code == "unknown_tool"
    assert digests(campus) == before
