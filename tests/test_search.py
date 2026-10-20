"""Offline search uses real migrations, Parser, archives and SQLite FTS5."""

from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.migration import MigrationContext
from pydantic import ValidationError

from signalnest.config import StorageSettings
from signalnest.contracts import PageInput
from signalnest.ems_parsing import parse_ems_notice
from signalnest.ingestion import ResponseInput, import_page
from signalnest.parsing import parse_notice
from signalnest.rawstore import RawStore
from signalnest.schema import documents, notice_versions, search_documents
from signalnest.search import (
    INDEX_VERSION,
    QUERY_ALIASES,
    QUERY_VERSION,
    SearchError,
    SearchQuery,
    get_notice,
    rebuild_index,
    search_notices,
    sync_document_in_transaction,
)
from signalnest.storage import initialize_storage, make_engine, migration_config

FIXTURES = Path(__file__).resolve().parents[1] / "research/fixtures"
SOURCE = "whu-undergrad-student"
URL = "https://uc.whu.edu.cn/info/1517/128231.htm"


def raw():
    return (FIXTURES / "current-notice-detail.html").read_bytes()


def response(**overrides):
    return ResponseInput(
        **dict(
            page_type="notice",
            source_id=SOURCE,
            source_document_id="1517:128231",
            requested_url=URL,
            final_url=URL,
            fetched_at=100,
            status_code=200,
        )
        | overrides
    )


@pytest.fixture
def env(tmp_path):
    settings = StorageSettings(data_dir=str(tmp_path / "data"), database=str(tmp_path / "db"))
    initialize_storage(settings)
    engine = make_engine(settings.database)
    store = RawStore(settings.data_dir)
    import_page(
        engine,
        store,
        ResponseInput(
            page_type="list",
            source_id=SOURCE,
            requested_url="https://uc.whu.edu.cn/tzgg/xstz.htm",
            final_url="https://uc.whu.edu.cn/tzgg/xstz.htm",
            fetched_at=100,
            status_code=200,
        ),
        (FIXTURES / "student-notices-page1.html").read_bytes(),
        101,
    )
    result = import_page(engine, store, response(), raw(), 102)
    yield SimpleNamespace(settings=settings, engine=engine, store=store, result=result)
    engine.dispose()


def query(env, text="选课", **kwargs):
    return search_notices(env.engine, SearchQuery(query=text, **kwargs))


def test_real_fixture_search_title_body_ids_and_original_url(env):
    update = rebuild_index(env.engine)
    assert update.indexed_count == 1
    result = query(env)
    assert result.index_version == INDEX_VERSION
    assert result.total == 1
    item = result.items[0]
    assert item.document_id == env.result.document_id
    assert item.version_id == env.result.version_id
    assert item.source_id == SOURCE
    assert item.source_document_id == "1517:128231"
    parsed = parse_notice(PageInput(content=raw(), page_url=URL))
    assert item.title == parsed.content.title
    assert item.published_date == parsed.content.published_date
    assert item.original_url == URL
    assert "选课" in item.snippet
    assert "<p>" not in item.snippet


def test_long_chinese_and_multiword_query(env):
    rebuild_index(env.engine)
    assert query(env, "本科生选课").total == 1
    assert query(env, "本科生选课 选课").total == 1
    assert query(env, "本科生选课 不存在词").total == 0
    assert query(env, "本科生选课 本科生选课").total == 1


def test_explicit_alias_plan_is_visible_fixed_and_not_applied_to_bare_topic(env):
    rebuild_index(env.engine)
    result = query(env, "助教招聘 竞赛")
    assert result.query == "助教招聘 竞赛"
    assert result.query_version == QUERY_VERSION
    assert result.expanded_terms == (QUERY_ALIASES["助教招聘"], QUERY_ALIASES["竞赛"])
    assert query(env, "助教").expanded_terms == (("助教",),)
    assert query(env, "助教招聘会").expanded_terms == (("助教招聘会",),)
    with pytest.raises(TypeError):
        QUERY_ALIASES["anything"] = ("changed",)


def test_real_assistant_recruitment_alias_and_incidental_negative(env):
    url = "https://ems.whu.edu.cn/info/1588/250571.htm"
    with env.engine.begin() as connection:
        connection.execute(
            documents.insert().values(
                source_id="whu-ems-offline",
                source_document_id="1588:250571",
                detail_url=url,
                discovered_title="历史助教选聘通知",
                discovered_at=103,
            )
        )
    imported = import_page(
        env.engine,
        env.store,
        ResponseInput(
            page_type="notice",
            source_id="whu-ems-offline",
            source_document_id="1588:250571",
            requested_url=url,
            final_url=url,
            fetched_at=103,
            status_code=200,
        ),
        (
            FIXTURES / "teaching-assistant/ems-notice-250571-20261008T102823953399Z.html"
        ).read_bytes(),
        104,
        notice_parser=parse_ems_notice,
    )
    rebuild_index(env.engine)
    result = query(env, "助教招聘")
    assert result.total == 1
    assert result.items[0].document_id == imported.document_id
    assert result.items[0].source_id == "whu-ems-offline"
    assert "助教选聘" in result.items[0].title
    assert query(env, "助教招聘", date_from=date(2026, 1, 1)).total == 0
    assert query(env, "助教招聘", source_id=SOURCE).total == 0
    # The current WHU selection notice contains no recruitment phrase. Adding
    # bare 助教 teaching support still must not satisfy the recruitment query.
    incidental = raw().replace("根据".encode(), "课程配备助教，根据".encode())
    import_page(env.engine, env.store, response(fetched_at=105), incidental, 106)
    rebuild_index(env.engine)
    assert query(env, "助教招聘").total == 1
    assert query(env, "助教").total == 2


def test_short_alias_group_and_literal_group_are_anded(env):
    body = raw().replace("根据".encode(), "举办大赛，根据".encode())
    import_page(env.engine, env.store, response(fetched_at=103), body, 104)
    rebuild_index(env.engine)
    assert query(env, "竞赛").total == 1
    assert query(env, "竞赛 选课").total == 1
    assert query(env, "竞赛 不存在词").total == 0


@pytest.mark.parametrize("text", ["课", "选课"])
def test_short_chinese_terms_use_literal_fallback(env, text):
    rebuild_index(env.engine)
    assert query(env, text).total == 1


def test_inclusive_publication_date_and_source_filters(env):
    rebuild_index(env.engine)
    published = get_notice(env.engine, env.result.document_id).content.published_date
    assert query(env, date_from=published, date_to=published, source_id=SOURCE).total == 1
    assert query(env, date_from=date(2099, 1, 1)).total == 0
    assert query(env, date_to=date(1900, 1, 1)).total == 0
    assert query(env, source_id="different-source").total == 0


@pytest.mark.parametrize("text", ["选课 OR 不存在", "选课*", '"选课"', "100%", "_"])
def test_fts_operators_quotes_and_sql_wildcards_are_literal(env, text):
    rebuild_index(env.engine)
    assert query(env, text).total == 0


def test_query_cannot_inject_sql(env):
    rebuild_index(env.engine)
    assert query(env, "'; DROP TABLE documents; --").total == 0
    with env.engine.connect() as connection:
        assert connection.execute(sa.select(sa.func.count()).select_from(documents)).scalar() == 25


def test_real_rebuild_deterministic_and_reopen(env):
    first = rebuild_index(env.engine)
    before = query(env, "本科生选课")
    second = rebuild_index(env.engine)
    assert first.indexed_count == second.indexed_count == second.deleted_count == 1
    assert query(env, "本科生选课") == before
    env.engine.dispose()
    engine = make_engine(env.settings.database)
    try:
        assert search_notices(engine, SearchQuery(query="本科生选课")) == before
    finally:
        engine.dispose()


def test_reading_never_repairs_stale_index(env):
    with env.engine.begin() as connection:
        connection.execute(search_documents.delete())
    with pytest.raises(SearchError, match="search_index_stale"):
        query(env)
    with env.engine.connect() as connection:
        assert (
            connection.execute(sa.select(sa.func.count()).select_from(search_documents)).scalar()
            == 0
        )
    assert get_notice(env.engine, env.result.document_id).version_id == env.result.version_id


def test_unknown_index_version_requires_explicit_rebuild(env):
    rebuild_index(env.engine)
    with env.engine.begin() as connection:
        connection.execute(search_documents.update().values(index_version="future-unknown"))
    with pytest.raises(SearchError, match="search_index_stale"):
        query(env)
    rebuild_index(env.engine)
    assert query(env).total == 1


def test_missing_fts_table_fails_clearly_without_creating_it(env):
    with env.engine.begin() as connection:
        connection.exec_driver_sql("DROP TABLE search_fts")
    with pytest.raises(SearchError, match="search_index_missing"):
        query(env)
    with env.engine.connect() as connection:
        assert "search_fts" not in sa.inspect(connection).get_table_names()


def test_failure_recheck_keeps_last_success_searchable(env):
    rebuild_index(env.engine)
    with env.engine.begin() as connection:
        connection.execute(
            documents.update()
            .where(documents.c.id == env.result.document_id)
            .values(
                status="failed",
                last_attempt_at=103,
                last_error_code="parse_missing_structure",
            )
        )
    result = query(env)
    assert result.items[0].version_id == env.result.version_id
    detail = get_notice(env.engine, env.result.document_id)
    assert detail.status == "failed"
    assert detail.last_error_code == "parse_missing_structure"


def test_current_a_b_a_uses_pointer_not_largest_version(env):
    a = env.result
    b_body = raw().replace("本科生选课".encode(), "未来学习".encode())
    b = import_page(env.engine, env.store, response(fetched_at=103), b_body, 104)
    with env.engine.begin() as connection:
        sync_document_in_transaction(connection, a.document_id)
    assert b.version_id > a.version_id
    assert query(env, "未来学习").total == 1
    again = import_page(env.engine, env.store, response(fetched_at=105), raw(), 106)
    with env.engine.begin() as connection:
        sync_document_in_transaction(connection, a.document_id)
    assert again.version_id == a.version_id
    assert query(env, "未来学习").total == 0
    assert query(env, "本科生选课").items[0].version_id == a.version_id


def test_sync_rollback_restores_success_and_index_together(env):
    rebuild_index(env.engine)
    before = query(env)
    with pytest.raises(RuntimeError, match="injected"), env.engine.begin() as connection:
        connection.execute(search_documents.update().values(title_text="故障期间临时内容"))
        sync_document_in_transaction(connection, env.result.document_id)
        raise RuntimeError("injected")
    assert query(env) == before


def test_explicit_sync_requires_transaction(env):
    with env.engine.connect() as connection:
        with pytest.raises(SearchError, match="transaction_required"):
            sync_document_in_transaction(connection, env.result.document_id)


def test_corrupt_normalized_content_fails_rebuild_and_preserves_previous_index(env):
    rebuild_index(env.engine)
    with env.engine.connect() as connection:
        before = connection.execute(sa.select(search_documents)).mappings().all()
    with env.engine.begin() as connection:
        connection.execute(
            notice_versions.update()
            .where(notice_versions.c.id == env.result.version_id)
            .values(
                normalized_content={"body_text": "incomplete"},
            )
        )
    with pytest.raises(SearchError, match="notice_content_invalid"):
        rebuild_index(env.engine)
    with env.engine.connect() as connection:
        assert connection.execute(sa.select(search_documents)).mappings().all() == before


def test_query_and_detail_work_on_sqlite_query_only_connection(env):
    rebuild_index(env.engine)

    @sa.event.listens_for(env.engine, "connect")
    def query_only(dbapi_connection, record):
        dbapi_connection.execute("PRAGMA query_only=ON")

    env.engine.dispose()
    assert query(env).total == 1
    assert get_notice(env.engine, env.result.document_id).version_id == env.result.version_id
    with pytest.raises(SearchError, match="search_index_write_failed"):
        rebuild_index(env.engine)


def test_pagination_is_stable_and_total_independent_of_page(env):
    rebuild_index(env.engine)
    first = query(env, limit=1, offset=0)
    second = query(env, limit=1, offset=1)
    assert first.total == second.total == 1
    assert len(first.items) == 1
    assert second.items == ()


def test_literal_matching_normalizes_case_and_unicode_without_changing_content(env):
    body = raw().replace("根据".encode(), "根据 SIGNALNEST Caf\u00e9 ".encode())
    import_page(env.engine, env.store, response(fetched_at=103), body, 104)
    rebuild_index(env.engine)
    assert query(env, "signalnest").total == 1
    assert query(env, "CAFE\u0301").total == 1
    assert (
        "SIGNALNEST Caf\u00e9" in get_notice(env.engine, env.result.document_id).content.body_text
    )


def test_source_filter_is_literal_and_cannot_inject_sql(env):
    rebuild_index(env.engine)
    assert query(env, source_id="' OR 1=1 --").total == 0


def test_fts_postings_loss_is_explicitly_rebuildable(env):
    rebuild_index(env.engine)
    with env.engine.begin() as connection:
        connection.exec_driver_sql("INSERT INTO search_fts(search_fts) VALUES('delete-all')")
    # Reader cannot mutate to repair or run FTS's write-style integrity command.
    # Explicit maintenance reconstructs even postings that are entirely absent.
    rebuild_index(env.engine)
    assert query(env, "本科生选课").total == 1


def test_rebuild_fts_write_fault_rolls_back_existing_index(env):
    rebuild_index(env.engine)
    before = query(env)
    with env.engine.begin() as connection:
        connection.exec_driver_sql(
            "CREATE TRIGGER reject_search BEFORE INSERT ON search_documents "
            "BEGIN SELECT RAISE(ABORT, 'injected'); END"
        )
    with pytest.raises(SearchError, match="search_index_write_failed"):
        rebuild_index(env.engine)
    assert query(env) == before


@pytest.mark.parametrize("failure", ["search_documents", "search_fts", "search_documents_update"])
def test_migration_ddl_fault_rolls_back_and_preserves_previous_head(tmp_path, failure):
    settings = StorageSettings(data_dir=str(tmp_path / "data"), database=str(tmp_path / "db"))
    engine = make_engine(settings.database)
    with engine.begin() as connection:
        config = migration_config()
        config.attributes["connection"] = connection
        command.upgrade(config, "0006_mail_sending")
        connection.execute(
            documents.insert().values(
                source_id=SOURCE,
                source_document_id="1517:1",
                detail_url=URL,
                discovered_title="迁移前事实",
                discovered_at=1,
            )
        )

    def fail_ddl(connection, cursor, statement, parameters, context, executemany):
        if failure in statement and statement.lstrip().upper().startswith("CREATE"):
            raise RuntimeError("injected migration DDL fault")

    sa.event.listen(sa.engine.Engine, "before_cursor_execute", fail_ddl)
    try:
        with pytest.raises(RuntimeError, match="injected migration DDL fault"):
            initialize_storage(settings)
    finally:
        sa.event.remove(sa.engine.Engine, "before_cursor_execute", fail_ddl)
    try:
        with engine.connect() as connection:
            assert (
                MigrationContext.configure(connection).get_current_revision() == "0006_mail_sending"
            )
            assert "search_documents" not in sa.inspect(connection).get_table_names()
            assert "search_fts" not in sa.inspect(connection).get_table_names()
            assert (
                connection.execute(sa.select(documents.c.discovered_title)).scalar_one()
                == "迁移前事实"
            )
        assert initialize_storage(settings) == "0009_opportunity_review"
    finally:
        engine.dispose()


@pytest.mark.parametrize("document_id", [0, -1, True, "1"])
def test_invalid_document_identity(env, document_id):
    with pytest.raises(SearchError, match="invalid_document_id"):
        get_notice(env.engine, document_id)


def test_missing_or_unprocessed_notice_details(env):
    with pytest.raises(SearchError, match="document_not_found"):
        get_notice(env.engine, 999999)
    with env.engine.connect() as connection:
        pending = connection.execute(
            sa.select(documents.c.id).where(documents.c.current_version_id.is_(None)).limit(1)
        ).scalar_one()
    with pytest.raises(SearchError, match="notice_not_processed"):
        get_notice(env.engine, pending)


@pytest.mark.parametrize(
    "values",
    [
        {"query": ""},
        {"query": " "},
        {"query": "x" * 201},
        {"query": "a\x00b"},
        {"query": "1 2 3 4 5 6 7 8 9"},
        {"query": "a", "limit": 0},
        {"query": "a", "limit": 101},
        {"query": "a", "offset": -1},
        {"query": "a", "limit": True},
        {"query": "a", "date_from": "2026-10-08", "date_to": "2026-10-07"},
    ],
)
def test_invalid_query_contract(values):
    with pytest.raises(ValidationError):
        SearchQuery(**values)


def test_empty_database_query_is_valid_and_does_not_insert(tmp_path):
    settings = StorageSettings(data_dir=str(tmp_path / "data"), database=str(tmp_path / "db"))
    initialize_storage(settings)
    engine = make_engine(settings.database)
    try:
        result = search_notices(engine, SearchQuery(query="助教招聘"))
        assert result.total == 0
        assert result.items == ()
        with engine.connect() as connection:
            assert (
                connection.execute(
                    sa.select(sa.func.count()).select_from(search_documents)
                ).scalar()
                == 0
            )
    finally:
        engine.dispose()


def test_0006_upgrade_preserves_business_rows_and_requires_explicit_index(tmp_path):
    settings = StorageSettings(data_dir=str(tmp_path / "data"), database=str(tmp_path / "db"))
    engine = make_engine(settings.database)
    try:
        with engine.begin() as connection:
            config = migration_config()
            config.attributes["connection"] = connection
            command.upgrade(config, "0006_mail_sending")
            connection.execute(
                documents.insert().values(
                    source_id=SOURCE,
                    source_document_id="1517:1",
                    detail_url=URL,
                    discovered_title="旧通知",
                    discovered_at=1,
                )
            )
        assert initialize_storage(settings) == "0009_opportunity_review"
        assert initialize_storage(settings) == "0009_opportunity_review"
        with engine.connect() as connection:
            assert (
                connection.execute(sa.select(documents.c.discovered_title)).scalar_one() == "旧通知"
            )
            assert (
                connection.execute(
                    sa.select(sa.func.count()).select_from(search_documents)
                ).scalar()
                == 0
            )
            assert (
                MigrationContext.configure(connection).get_current_revision()
                == "0009_opportunity_review"
            )
            assert connection.exec_driver_sql("PRAGMA foreign_key_check").all() == []
    finally:
        engine.dispose()
