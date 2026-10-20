"""Real 0002 databases, retained evidence and transactional 0003 upgrades."""

import hashlib
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.runtime.migration import MigrationContext

from signalnest.cache import select_cache_candidate
from signalnest.config import StorageSettings
from signalnest.contracts import PageInput, RequestProfile
from signalnest.ingestion import process_response
from signalnest.parsing import parse_notice
from signalnest.rawstore import RawStore
from signalnest.schema import documents, http_resources, notice_versions, raw_responses
from signalnest.storage import initialize_storage, make_engine, migration_config

FIXTURES = Path(__file__).resolve().parents[1] / "research/fixtures"
URL = "https://uc.whu.edu.cn/info/1517/128231.htm"
SOURCE = "whu-undergrad-student"


@pytest.fixture
def legacy(tmp_path):
    settings = StorageSettings(
        data_dir=str(tmp_path / "data"), database=str(tmp_path / "db.sqlite")
    )
    (settings.data_dir / "raw").mkdir(parents=True)
    content = (FIXTURES / "current-notice-detail.html").read_bytes()
    blob = RawStore(settings.data_dir).archive(content)
    parsed = parse_notice(PageInput(content=content, page_url=URL))
    engine = make_engine(settings.database)
    with engine.begin() as connection:
        config = migration_config()
        config.attributes["connection"] = connection
        command.upgrade(config, "0002_response_target")
        document = connection.exec_driver_sql(
            "INSERT INTO documents(source_id,source_document_id,detail_url,"
            "discovered_title,discovered_at) VALUES (?,?,?,?,?)",
            (SOURCE, "1517:128231", URL, "通知", 100),
        ).lastrowid
        response = connection.exec_driver_sql(
            "INSERT INTO raw_responses(source_id,requested_url,final_url,fetched_at,status_code,"
            "etag,body_path,body_sha256,page_type,document_id,last_attempt_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                SOURCE,
                URL,
                URL,
                101,
                200,
                '"old-etag"',
                blob.path,
                blob.sha256,
                "notice",
                document,
                102,
            ),
        ).lastrowid
        version = connection.execute(
            notice_versions.insert().values(
                document_id=document,
                raw_response_id=response,
                content_sha256=parsed.content.content_sha256(),
                parser_version=parsed.parser_version,
                parsed_at=102,
                title=parsed.content.title,
                published_date=parsed.content.published_date,
                normalized_content=parsed.content.model_dump(mode="json"),
            )
        ).inserted_primary_key[0]
        connection.exec_driver_sql(
            "UPDATE documents SET status='processed',current_version_id=?,last_attempt_at=102,"
            "last_success_at=102,next_due_at=200 WHERE id=?",
            (version, document),
        )
    yield settings, engine, blob, response, version
    engine.dispose()


def test_upgrade_preserves_old_data_without_inventing_cache_qualification(legacy):
    settings, engine, blob, response, version = legacy
    assert initialize_storage(settings) == "0009_opportunity_review"
    assert initialize_storage(settings) == "0009_opportunity_review"
    with engine.connect() as connection:
        doc = connection.execute(sa.select(documents)).mappings().one()
        raw = connection.execute(sa.select(raw_responses)).mappings().one()
        assert doc["current_version_id"] == version and doc["next_due_at"] == 200
        assert doc["discovery_origin"] == "unknown" and doc["first_discovery_run_id"] is None
        assert (
            raw["body_state"] == "unknown"
            and raw["resource_id"] is None
            and raw["validated_response_id"] is None
        )
        assert raw["fetched_at"] == 101 and raw["etag"] == '"old-etag"'
        assert connection.execute(sa.select(http_resources)).all() == []
        assert connection.exec_driver_sql("PRAGMA foreign_key_check").all() == []
        assert connection.exec_driver_sql("PRAGMA foreign_keys").scalar_one() == 1
    assert hashlib.sha256((settings.data_dir / blob.path).read_bytes()).hexdigest() == blob.sha256
    profile = RequestProfile(user_agent="test", accept="text/html")
    assert select_cache_candidate(
        engine, RawStore(settings.data_dir), SOURCE, URL, profile
    ).requires_full_fetch
    result = process_response(engine, RawStore(settings.data_dir), response, 103)
    assert result.version_id == version
    with engine.connect() as connection:
        assert (
            connection.execute(sa.select(sa.func.count()).select_from(raw_responses)).scalar_one()
            == 1
        )


def test_upgrade_interruption_rolls_back_columns_tables_and_keeps_0002(legacy):
    # Ordinary in-process exception in DDL, not a killed process/power-loss test.
    settings, engine, blob, response, version = legacy

    def inject(connection, cursor, statement, parameters, context, executemany):
        if statement.startswith("ALTER TABLE raw_responses ADD COLUMN resource_id"):
            raise RuntimeError("injected migration interruption")

    # initialize_storage creates its own engine; class listener catches that connection.
    sa.event.listen(sa.engine.Engine, "after_cursor_execute", inject)
    try:
        with pytest.raises(RuntimeError, match="injected migration interruption"):
            initialize_storage(settings)
    finally:
        sa.event.remove(sa.engine.Engine, "after_cursor_execute", inject)
    with engine.connect() as connection:
        assert (
            MigrationContext.configure(connection).get_current_revision() == "0002_response_target"
        )
        assert "body_state" not in {
            col["name"] for col in sa.inspect(connection).get_columns("raw_responses")
        }
        assert "http_resources" not in sa.inspect(connection).get_table_names()
        assert (
            connection.exec_driver_sql("SELECT current_version_id FROM documents").scalar_one()
            == version
        )
        assert connection.exec_driver_sql("PRAGMA foreign_key_check").all() == []
    assert (settings.data_dir / blob.path).is_file()
    assert initialize_storage(settings) == "0009_opportunity_review"
