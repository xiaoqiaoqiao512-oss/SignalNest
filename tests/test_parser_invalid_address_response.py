"""A real HTTP 200 site error stays failed and cannot replace a successful notice."""

import hashlib
import json
from pathlib import Path

import pytest
from test_ingestion import NOTICE_URL, SOURCE, evidence, raw, rows, target

from signalnest.contracts import PageInput
from signalnest.ingestion import IngestError, ResponseInput, import_page, process_response
from signalnest.parsing import ParseError, parse_notice
from signalnest.schema import (
    documents,
    notice_versions,
    notification_decisions,
    notification_events,
    raw_responses,
)

FIXTURES = Path(__file__).parent / "fixtures/processing-failures"
METADATA = json.loads((FIXTURES / "uc-invalid-address-200.json").read_text())


def error_body():
    return (FIXTURES / METADATA["fixture"]).read_bytes()


def test_exact_invalid_address_response_is_not_a_notice():
    body = error_body()
    assert len(body) == METADATA["body_length"] == 101
    assert hashlib.sha256(body).hexdigest() == METADATA["body_sha256"]
    assert body.decode("utf-8").endswith("</html>\n")
    with pytest.raises(ParseError) as caught:
        parse_notice(PageInput(content=body, page_url=METADATA["source_url"]))
    assert caught.value.code.value == METADATA["expected_error"]["code"]
    assert caught.value.field == METADATA["expected_error"]["field"]


def test_original_error_identity_is_archived_without_a_success_version(state_env):
    env = state_env
    at = METADATA["fetched_at"]
    with env.engine.begin() as connection:
        connection.execute(
            documents.insert().values(
                source_id=METADATA["source_id"],
                source_document_id=METADATA["source_document_id"],
                detail_url=METADATA["source_url"],
                discovered_title="Offline error-response target",
                discovered_at=at - 1,
            )
        )
    response = ResponseInput(
        source_id=METADATA["source_id"],
        source_document_id=METADATA["source_document_id"],
        page_type="notice",
        requested_url=METADATA["source_url"],
        final_url=METADATA["source_url"],
        fetched_at=at,
        status_code=METADATA["status_code"],
        body_state=METADATA["body_state"],
    )
    with pytest.raises(IngestError, match="parse_missing_structure") as caught:
        import_page(env.engine, env.store, response, error_body(), at + 1)
    failed = rows(env, documents)[0]
    retained = rows(env, raw_responses)[0]
    assert failed["status"] == "failed" and failed["current_version_id"] is None
    assert failed["last_success_at"] is None
    assert failed["last_error_code"] == "parse_missing_structure"
    assert retained["id"] == caught.value.response_id
    assert retained["status_code"] == 200 and retained["body_state"] == "complete"
    assert retained["requested_url"] == retained["final_url"] == METADATA["source_url"]
    assert retained["fetched_at"] == at
    assert retained["body_sha256"] == METADATA["body_sha256"]
    assert retained["last_error_code"] == "parse_missing_structure"
    assert env.store.read(retained["body_path"], retained["body_sha256"]) == error_body()
    assert rows(env, notice_versions) == []
    assert rows(env, notification_events) == rows(env, notification_decisions) == []


def test_error_recheck_and_reparse_preserve_success_and_allow_later_normal_content(state_env):
    env = state_env
    # Reuse the existing 128231 fixture, without pretending it is the original
    # 128721 article. Only the exact site-error bytes are shared with production.
    parsed = parse_notice(PageInput(content=raw(), page_url=NOTICE_URL))
    with env.engine.begin() as connection:
        connection.execute(
            documents.insert().values(
                source_id=SOURCE,
                source_document_id=parsed.source_document_id,
                detail_url=NOTICE_URL,
                discovered_title=parsed.content.title,
                discovered_at=99,
            )
        )
    success = import_page(env.engine, env.store, evidence(), raw(), 101)
    versions_before = rows(env, notice_versions)
    notifications_before = rows(env, notification_events), rows(env, notification_decisions)
    with pytest.raises(IngestError, match="parse_missing_structure") as caught:
        import_page(
            env.engine,
            env.store,
            evidence(fetched_at=102, body_state="complete"),
            error_body(),
            103,
        )
    failed_response_id = caught.value.response_id
    for at in (104, 105):
        with pytest.raises(IngestError, match="parse_missing_structure"):
            process_response(env.engine, env.store, failed_response_id, at)
        failed = target(env)
        assert failed["status"] == "failed"
        assert failed["current_version_id"] == success.version_id
        assert failed["last_success_at"] == 101 and failed["last_attempt_at"] == at
        assert failed["last_error_code"] == "parse_missing_structure"
        assert rows(env, notice_versions) == versions_before
        assert (rows(env, notification_events), rows(env, notification_decisions)) == (
            notifications_before
        )
    assert len(rows(env, raw_responses)) == 2
    retained = rows(env, raw_responses)[-1]
    assert retained["id"] == failed_response_id
    assert retained["fetched_at"] == 102 and retained["last_attempt_at"] == 105
    assert retained["body_sha256"] == METADATA["body_sha256"]
    assert retained["last_error_code"] == "parse_missing_structure"
    assert env.store.read(retained["body_path"], retained["body_sha256"]) == error_body()

    recovered = import_page(env.engine, env.store, evidence(fetched_at=106), raw(), 107)
    assert recovered.outcome == "processed" and recovered.version_id == success.version_id
    assert target(env)["status"] == "processed"
    assert target(env)["last_success_at"] == 107 and target(env)["last_error_code"] is None
    assert rows(env, notice_versions) == versions_before
    assert (rows(env, notification_events), rows(env, notification_decisions)) == (
        notifications_before
    )
    assert rows(env, raw_responses)[1] == retained
