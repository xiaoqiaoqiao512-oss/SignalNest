from datetime import date
from pathlib import Path
from shutil import copytree

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy.exc import IntegrityError

from signalnest import storage
from signalnest.config import StorageSettings
from signalnest.schema import documents, metadata, notice_versions, raw_responses
from signalnest.storage import StorageError, initialize_storage, make_engine


@pytest.fixture
def settings(tmp_path):
    return StorageSettings(
        data_dir=str(tmp_path / "data"), database=str(tmp_path / "db % name" / "signalnest.sqlite3")
    )


@pytest.fixture
def engine(settings):
    initialize_storage(settings)
    engine = make_engine(settings.database)
    yield engine
    engine.dispose()


def insert_document(connection, identity="1517:128231"):
    return connection.execute(
        documents.insert().values(
            source_id="whu-undergrad-student",
            source_document_id=identity,
            detail_url="https://uc.whu.edu.cn/info/1517/128231.htm",
            discovered_title="通知",
            discovered_at=100,
        )
    ).inserted_primary_key[0]


def insert_response(connection, **overrides):
    values = dict(
        source_id="whu-undergrad-student",
        requested_url="https://uc.whu.edu.cn/info/1517/128231.htm",
        final_url="https://uc.whu.edu.cn/info/1517/128231.htm",
        fetched_at=101,
        status_code=200,
        body_path="raw/" + "a" * 64 + ".bin",
        body_sha256="a" * 64,
    )
    return connection.execute(
        raw_responses.insert().values(**(values | overrides))
    ).inserted_primary_key[0]


def version_values(document_id, response_id):
    return dict(
        document_id=document_id,
        raw_response_id=response_id,
        content_sha256="b" * 64,
        parser_version="whu-v1",
        parsed_at=102,
        title="通知",
        published_date=date(2026, 9, 4),
        normalized_content={"text": "通知正文"},
    )


def test_initialization_and_schema_match(settings, engine):
    assert (settings.data_dir / "raw").is_dir()
    with engine.connect() as connection:
        assert set(sa.inspect(connection).get_table_names()) == {
            "alembic_version",
            "documents",
            "raw_responses",
            "notice_versions",
            "source_ingestion_state",
            "http_resources",
            "ingestion_runs",
            "notification_policy_revisions",
            "notification_channel_state",
            "notification_listing_evidence",
            "notification_activation_members",
            "notification_observations",
            "notification_events",
            "notification_decisions",
            "email_outbox",
            "mail_messages",
            "mail_message_members",
            "mail_plan_errors",
            "mail_delivery",
            "mail_attempts",
            "notification_operations",
            "notification_opportunity_review_operations",
            "search_documents",
            "discovered_references",
            "search_fts",
            "search_fts_config",
            "search_fts_data",
            "search_fts_docsize",
            "search_fts_idx",
        }
        context = MigrationContext.configure(
            connection,
            opts={
                "compare_server_default": True,
                # SQLite owns FTS's virtual/shadow table shape. The ordinary
                # search_documents projection still participates in comparison.
                "include_object": lambda obj, name, kind, reflected, compare_to: (
                    not (
                        kind == "table" and (name == "search_fts" or name.startswith("search_fts_"))
                    )
                ),
            },
        )
        assert context.get_current_revision() == "0009_opportunity_review"
        assert compare_metadata(context, metadata) == []
        assert connection.exec_driver_sql("PRAGMA foreign_keys").scalar_one() == 1


def test_repeat_initialization_preserves_data_and_raw_files(settings, engine):
    with engine.begin() as connection:
        document_id = insert_document(connection)
    raw = settings.data_dir / "raw" / "retained.bin"
    raw.write_bytes(b"unchanged")
    assert initialize_storage(settings) == "0009_opportunity_review"
    assert initialize_storage(settings) == "0009_opportunity_review"
    with engine.connect() as connection:
        assert connection.execute(sa.select(documents.c.id)).all() == [(document_id,)]
    assert raw.read_bytes() == b"unchanged"


def test_identity_uniqueness_is_not_url_or_title(engine):
    with engine.begin() as connection:
        insert_document(connection)
        insert_document(connection, "1517:128232")
    with pytest.raises(IntegrityError), engine.begin() as connection:
        insert_document(connection)


def test_version_uniqueness_and_reparse(engine):
    with engine.begin() as connection:
        document_id = insert_document(connection)
        response_id = insert_response(connection)
        values = version_values(document_id, response_id)
        connection.execute(notice_versions.insert().values(**values))
        connection.execute(
            notice_versions.insert().values(**(values | {"parser_version": "whu-v2"}))
        )
        connection.execute(
            notice_versions.insert().values(**(values | {"content_sha256": "c" * 64}))
        )
    with pytest.raises(IntegrityError), engine.begin() as connection:
        connection.execute(notice_versions.insert().values(**values))


def test_failure_preserves_success_and_survives_reopen(settings, engine):
    with engine.begin() as connection:
        document_id = insert_document(connection)
        discovered = connection.execute(sa.select(documents)).mappings().one()
        assert discovered["status"] == "discovered"
        assert discovered["last_success_at"] is None
        response_id = insert_response(connection)
        version_id = connection.execute(
            notice_versions.insert().values(**version_values(document_id, response_id))
        ).inserted_primary_key[0]
        connection.execute(
            documents.update().values(
                status="processed",
                current_version_id=version_id,
                last_attempt_at=102,
                last_success_at=102,
            )
        )
    with engine.begin() as connection:
        connection.execute(
            documents.update().values(
                status="failed",
                last_attempt_at=103,
                last_error_code="http_timeout",
                next_due_at=200,
            )
        )
    engine.dispose()
    reopened = make_engine(settings.database)
    try:
        with reopened.connect() as connection:
            row = connection.execute(sa.select(documents)).mappings().one()
            assert row["status"] == "failed"
            assert row["last_success_at"] == 102
            assert row["current_version_id"] == version_id
            assert row["next_due_at"] == 200
            assert (
                connection.execute(sa.select(sa.func.count()).select_from(notice_versions)).scalar()
                == 1
            )
    finally:
        reopened.dispose()


@pytest.mark.parametrize(
    "values",
    [
        {"status": "processed"},
        {"status": "processed", "last_attempt_at": 102, "last_success_at": 102},
        {"status": "failed", "last_attempt_at": 102},
        {"status": "unknown"},
    ],
)
def test_invalid_processing_states_rejected(engine, values):
    with engine.begin() as connection:
        insert_document(connection)
    with pytest.raises(IntegrityError), engine.begin() as connection:
        connection.execute(documents.update().values(**values))


def test_first_failure_is_not_success(engine):
    with engine.begin() as connection:
        insert_document(connection)
        connection.execute(
            documents.update().values(
                status="failed",
                last_attempt_at=102,
                last_error_code="parse_missing_body",
            )
        )
        row = connection.execute(sa.select(documents)).mappings().one()
        assert row["last_success_at"] is None
        assert row["current_version_id"] is None


def test_foreign_keys_and_cross_document_current_version(engine):
    with engine.begin() as connection:
        first = insert_document(connection)
        second = insert_document(connection, "1517:128232")
        response_id = insert_response(connection)
        version_id = connection.execute(
            notice_versions.insert().values(**version_values(first, response_id))
        ).inserted_primary_key[0]
    with pytest.raises(IntegrityError), engine.begin() as connection:
        connection.execute(notice_versions.insert().values(**version_values(999, response_id)))
    with pytest.raises(IntegrityError), engine.begin() as connection:
        connection.execute(notice_versions.insert().values(**version_values(first, 999)))
    with pytest.raises(IntegrityError), engine.begin() as connection:
        connection.execute(
            documents.update()
            .where(documents.c.id == second)
            .values(
                status="processed",
                current_version_id=version_id,
                last_attempt_at=102,
                last_success_at=102,
            )
        )


def test_304_has_no_body_and_body_reference_is_complete(engine):
    with engine.begin() as connection:
        insert_response(connection, status_code=304, body_path=None, body_sha256=None)
    with pytest.raises(IntegrityError), engine.begin() as connection:
        insert_response(connection, status_code=304)
    with pytest.raises(IntegrityError), engine.begin() as connection:
        insert_response(connection, body_sha256=None)


def test_engine_factory_does_not_create_database(settings):
    engine = make_engine(settings.database)
    engine.dispose()
    assert not settings.database.exists()
    assert not settings.data_dir.exists()


def test_unrecognized_revision_preserves_database(engine, settings):
    with engine.begin() as connection:
        insert_document(connection)
        connection.exec_driver_sql("UPDATE alembic_version SET version_num = 'future_revision'")
    with pytest.raises(StorageError):
        initialize_storage(settings)
    with engine.connect() as connection:
        assert (
            connection.exec_driver_sql("SELECT version_num FROM alembic_version").scalar_one()
            == "future_revision"
        )
        assert connection.execute(sa.select(documents)).first() is not None


@pytest.mark.parametrize("fail", [False, True])
def test_upgrade_existing_database_and_transactional_failure(
    settings, engine, tmp_path, monkeypatch, fail
):
    with engine.begin() as connection:
        insert_document(connection)
    migration_dir = tmp_path / "migration assets %"
    copytree(Path(storage.__file__).parent / "migrations", migration_dir)
    revision = migration_dir / "versions" / "0010_test.py"
    revision.write_text(
        "from alembic import op\nimport sqlalchemy as sa\n"
        "revision = '0010_test'\ndown_revision = '0009_opportunity_review'\n"
        "def upgrade():\n"
        "    op.add_column('documents', sa.Column('test_column', sa.Text()))\n"
        + ("    raise RuntimeError('simulated failure')\n" if fail else "")
    )
    original_config = storage.migration_config

    def test_config():
        config = original_config()
        config.set_main_option("script_location", str(migration_dir).replace("%", "%%"))
        return config

    monkeypatch.setattr(storage, "migration_config", test_config)
    if fail:
        with pytest.raises(RuntimeError, match="simulated failure"):
            initialize_storage(settings)
    else:
        assert initialize_storage(settings) == "0010_test"
    with engine.connect() as connection:
        columns = {column["name"] for column in sa.inspect(connection).get_columns("documents")}
        assert ("test_column" in columns) is not fail
        expected = "0009_opportunity_review" if fail else "0010_test"
        assert MigrationContext.configure(connection).get_current_revision() == expected
        assert connection.execute(sa.select(documents)).first() is not None


def test_version_and_state_update_rollback_together(engine):
    with engine.begin() as connection:
        document_id = insert_document(connection)
        response_id = insert_response(connection)
    with pytest.raises(IntegrityError), engine.begin() as connection:
        connection.execute(
            notice_versions.insert().values(**version_values(document_id, response_id))
        )
        # A success update without a version pointer is invalid; the version insert rolls back too.
        connection.execute(documents.update().values(status="processed"))
    with engine.connect() as connection:
        assert connection.execute(sa.select(documents.c.status)).scalar_one() == "discovered"
        assert connection.execute(sa.select(notice_versions)).first() is None


def test_upgrade_0001_preserves_success_versions_and_legacy_responses(settings):
    settings.database.parent.mkdir(parents=True)
    original = make_engine(settings.database)
    try:
        with original.begin() as connection:
            config = storage.migration_config()
            config.attributes["connection"] = connection
            command.upgrade(config, "0001_initial")
            document_id = insert_document(connection)
            # Use explicit old columns: current metadata has columns absent in 0001.
            response_id = connection.exec_driver_sql(
                "INSERT INTO raw_responses "
                "(source_id,requested_url,final_url,fetched_at,status_code) "
                "VALUES ('whu-undergrad-student','https://example.org/','https://example.org/',1,304)"
            ).lastrowid
            version_id = connection.execute(
                notice_versions.insert().values(**version_values(document_id, response_id))
            ).inserted_primary_key[0]
            connection.execute(
                documents.update().values(
                    status="processed",
                    current_version_id=version_id,
                    last_attempt_at=102,
                    last_success_at=102,
                )
            )
        assert initialize_storage(settings) == "0009_opportunity_review"
        with original.connect() as connection:
            row = connection.execute(sa.select(raw_responses)).mappings().one()
            assert row["page_type"] is None
            assert row["document_id"] is None
            assert row["fetched_at"] == 1
            assert connection.execute(sa.select(notice_versions.c.id)).scalar_one() == version_id
            assert (
                connection.execute(sa.select(documents.c.current_version_id)).scalar_one()
                == version_id
            )
            assert connection.exec_driver_sql("PRAGMA foreign_key_check").all() == []
    finally:
        original.dispose()


@pytest.mark.parametrize(
    "values",
    [
        {"page_type": "notice", "document_id": None},
        {"page_type": "list", "document_id": 1},
        {"page_type": None, "document_id": 1},
        {"page_type": "unknown"},
        {"page_type": "notice", "document_id": 999},
    ],
)
def test_response_target_constraints(engine, values):
    with engine.begin() as connection:
        insert_document(connection)
    with pytest.raises(IntegrityError), engine.begin() as connection:
        insert_response(connection, **values)


def test_open_existing_missing_or_old_database_never_initializes(settings):
    from signalnest.storage import open_initialized_engine

    with pytest.raises(StorageError, match="storage-init"):
        open_initialized_engine(settings.database)
    assert not settings.database.exists()
    settings.database.parent.mkdir(parents=True)
    engine = make_engine(settings.database)
    try:
        with engine.begin() as connection:
            config = storage.migration_config()
            config.attributes["connection"] = connection
            command.upgrade(config, "0001_initial")
        with pytest.raises(StorageError, match="storage-init"):
            open_initialized_engine(settings.database)
        with engine.connect() as connection:
            assert MigrationContext.configure(connection).get_current_revision() == "0001_initial"
    finally:
        engine.dispose()
