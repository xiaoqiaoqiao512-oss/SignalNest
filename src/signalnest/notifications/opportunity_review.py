"""Bounded, explicit review of existing documents; never a fabricated live event.

Preparation/preview is read only. An immutable operation is registered first;
all eligible review events/decisions and completion then commit together. A
sequence-zero review slot does not advance or replace the live observation.
Callers hold the ordinary instance writer lock for registration/recovery.
"""

import json
import re
import time
from dataclasses import dataclass, field
from datetime import UTC, date, datetime

import sqlalchemy as sa
from pydantic import ValidationError
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError

from signalnest.contracts import NoticeContent
from signalnest.errors import IngestError
from signalnest.notifications.contracts import (
    Decision,
    EventContext,
    NoticeFacts,
    Profile,
    canonical_json,
    canonical_sha256,
)
from signalnest.notifications.decision import deadline_for_profile, decide, policy_manifest
from signalnest.notifications.facts import extract_facts
from signalnest.notifications.state import SHANGHAI, channel_on, next_digest_at, notification_time
from signalnest.schema import (
    documents,
    email_outbox,
    mail_message_members,
    notice_versions,
    notification_decisions,
    notification_events,
    notification_observations,
    notification_policy_revisions,
    raw_responses,
)
from signalnest.schema import (
    notification_opportunity_review_operations as operations,
)

MAX_DOCUMENTS = 100


def _error(code):
    return IngestError("notification_review_" + code, "notification")


def _row(connection, table, *where):
    row = connection.execute(sa.select(table).where(*where)).mappings().one_or_none()
    return dict(row) if row is not None else None


def _jsonable(value):
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, dict):
        return {key: _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return value


def _ids(values):
    if not isinstance(values, (tuple, list)) or not 1 <= len(values) <= MAX_DOCUMENTS:
        raise _error("selection_invalid")
    if any(
        not isinstance(value, str)
        or not value.strip()
        or len(value) > 120
        or any(ord(character) < 32 for character in value)
        for value in values
    ):
        raise _error("selection_invalid")
    return tuple(sorted(set(values)))


def _operation_id(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,64}", value):
        raise _error("operation_id_invalid")


def _state(connection, source_id, at):
    state = channel_on(connection)
    if state is None or state["source_id"] != source_id:
        raise _error("channel_unavailable")
    if at < state["activation_at"]:
        raise _error("time_invalid")
    policy = _row(
        connection,
        notification_policy_revisions,
        notification_policy_revisions.c.id == state["policy_revision_id"],
    )
    try:
        profile = Profile.model_validate(policy["manifest"]["profile"])
        if (
            canonical_sha256(policy["manifest"]) != policy["policy_sha256"]
            or canonical_sha256(policy_manifest(profile)) != policy["policy_sha256"]
        ):
            raise _error("policy_outdated")
    except (TypeError, KeyError, ValidationError) as exc:
        raise _error("policy_invalid") from exc
    # A change in policy or digest calendar after preparation is explicit stale
    # context. Pause affects SMTP only; it does not rewrite this review decision.
    context = {
        key: state[key]
        for key in (
            "installation_id",
            "source_id",
            "policy_revision_id",
            "notification_mode",
            "digest_hour",
            "digest_minute",
            "sender",
            "recipient",
        )
    }
    context["policy_sha256"] = policy["policy_sha256"]
    return state, profile, context


def _capture(connection, state, identity):
    doc = _row(
        connection,
        documents,
        documents.c.source_id == state["source_id"],
        documents.c.source_document_id == identity,
    )
    if doc is None:
        return dict(source_document_id=identity, document=None)
    version = _row(connection, notice_versions, notice_versions.c.id == doc["current_version_id"])
    observation = _row(
        connection,
        notification_observations,
        notification_observations.c.installation_id == state["installation_id"],
        notification_observations.c.document_id == doc["id"],
    )
    body = observed = None
    if observation is not None:
        body = _row(
            connection, raw_responses, raw_responses.c.id == observation["body_response_id"]
        )
        observed = _row(
            connection,
            raw_responses,
            raw_responses.c.id == observation["observed_response_id"],
        )
    reminded = [
        dict(row)
        for row in connection.execute(
            sa.select(
                notification_events.c.id,
                notification_events.c.effective_route,
                notification_events.c.kind,
                mail_message_members.c.mail_id,
            )
            .outerjoin(
                mail_message_members, mail_message_members.c.event_id == notification_events.c.id
            )
            .where(
                notification_events.c.installation_id == state["installation_id"],
                notification_events.c.document_id == doc["id"],
                sa.or_(
                    notification_events.c.effective_route != "none",
                    notification_events.c.delivery_intent_registered_at.is_not(None),
                    mail_message_members.c.mail_id.is_not(None),
                    notification_events.c.kind == "opportunity_review",
                ),
            )
            .order_by(notification_events.c.id)
        ).mappings()
    ]
    return _jsonable(
        dict(
            source_document_id=identity,
            document={
                key: doc[key]
                for key in ("id", "source_id", "source_document_id", "current_version_id")
            },
            version=version,
            observation=observation,
            body=body,
            observed=observed,
            reminded=reminded,
        )
    )


def _input_token(item):
    # Current normalized content is referenced by its verified digest/version,
    # not duplicated as a full body in the persistent operation.
    compact = dict(item)
    if compact.get("version") is not None:
        compact["version"] = {
            key: value for key, value in compact["version"].items() if key != "normalized_content"
        }
    return canonical_sha256(compact)


def _content(item):
    version = item.get("version")
    if version is None:
        return None
    try:
        content = NoticeContent.model_validate(version["normalized_content"])
    except ValidationError as exc:
        raise _error("content_invalid") from exc
    if content.content_sha256() != version["content_sha256"]:
        raise _error("content_digest")
    return content


def _live_evidence(item, at):
    doc, version, observation = item["document"], item.get("version"), item.get("observation")
    body, observed = item.get("body"), item.get("observed")
    if any(value is None for value in (version, observation, body, observed)):
        return False
    if (
        version["document_id"] != doc["id"]
        or observation["version_id"] != version["id"]
        or observation["observed_at"] > at
        or observation["comparison_error_code"] is not None
        or body["status_code"] != 200
        or body["body_state"] != "complete"
        or body["body_path"] is None
        or body["body_sha256"] is None
        or any(
            row["document_id"] != doc["id"]
            or row["source_id"] != doc["source_id"]
            or row["page_type"] != "notice"
            or row["fetched_at"] > observation["observed_at"]
            for row in (body, observed)
        )
    ):
        return False
    return observed["id"] == body["id"] or (
        observed["status_code"] == 304
        and observed["body_path"] is None
        and observed["validated_response_id"] == body["id"]
        and body["resource_id"] is not None
        and all(observed[key] == body[key] for key in ("resource_id", "requested_url", "final_url"))
    )


def _classify(item, profile, state, at):
    result = dict(
        source_document_id=item["source_document_id"],
        input_token=_input_token(item),
        can_register=False,
    )
    if item["document"] is None:
        return result | dict(status="unavailable", reason="document_missing")
    result["document_id"] = item["document"]["id"]
    if item["reminded"]:
        return result | dict(status="already_notified", reason="prior_delivery_or_review")
    content = _content(item)
    if content is None or not _live_evidence(item, at):
        return result | dict(status="unknown", reason="current_live_evidence_unavailable")
    facts = extract_facts(content)
    context = EventContext(
        kind="opportunity_review",
        notification_mode=state["notification_mode"],
        next_digest_at=next_digest_at(state, at),
    )
    decision = decide(profile, facts, context, now=datetime.fromtimestamp(at, UTC))
    effective_time = deadline_for_profile(facts, profile)
    if facts.category == "unknown" or decision.relevance == "unknown":
        status, reason = "unknown", "opportunity_relation_unknown"
    elif facts.category != "opportunity" or decision.relevance != "matched":
        status, reason = "excluded", "not_relevant_opportunity"
    elif content.published_date > datetime.fromtimestamp(at, UTC).astimezone(SHANGHAI).date():
        status, reason = "unknown", "future_publication"
    elif decision.eligibility == "ineligible":
        status, reason = "excluded", "ineligible"
    elif decision.time_status in {"closed", "not_started"}:
        status, reason = "excluded", decision.time_status
    elif decision.time_status != "open" or effective_time.deadline_at is None:
        status, reason = "unknown", "current_application_time_unknown"
    elif decision.effective_route not in {"digest", "immediate"}:
        status, reason = "excluded", "no_mail_route"
    else:
        status, reason = "open", "confirmed_time_personal_eligibility_unverified"
    return result | dict(
        status=status,
        reason=reason,
        can_register=status == "open",
        version_id=item["version"]["id"],
        title=content.title,
        published_date=content.published_date.isoformat(),
        live_observed_at=item["observation"]["observed_at"],
        content_sha256=content.content_sha256(),
        body_response_id=item["body"]["id"],
        observed_response_id=item["observed"]["id"],
        body_sha256=item["body"]["body_sha256"],
        parser_version=item["version"]["parser_version"],
        facts=facts.model_dump(mode="json", exclude={"body_text"}),
        decision=decision.model_dump(mode="json"),
        context=context.model_dump(mode="json"),
    )


@dataclass(frozen=True)
class PreparedOpportunityReview:
    operation_id: str
    source_id: str
    installation_id: str
    parameters_json: str
    snapshot_json: str = field(repr=False)
    evaluated_at: int


def _prepare(engine, source_id, identities, at):
    notification_time(at)
    selected = _ids(identities)
    with engine.connect() as connection:
        state, profile, context = _state(connection, source_id, at)
        inputs = [_capture(connection, state, identity) for identity in selected]
    items = [_classify(item, profile, state, at) for item in inputs]
    parameters = dict(source_id=source_id, source_document_ids=list(selected), evaluated_at=at)
    payload = dict(context=context, items=items)
    return state, parameters, dict(payload=payload, sha256=canonical_sha256(payload))


def _public(items):
    return [{key: value for key, value in item.items() if key != "input_token"} for item in items]


def preview_open_opportunities(
    engine: Engine, source_id: str, source_document_ids, *, at: int
) -> dict:
    """Read-only classification; unknown opening/deadline never becomes open."""
    _, parameters, snapshot = _prepare(engine, source_id, source_document_ids, at)
    items = snapshot["payload"]["items"]
    return parameters | dict(
        preview=True,
        creates_delivery=False,
        policy_revision_id=snapshot["payload"]["context"]["policy_revision_id"],
        policy_sha256=snapshot["payload"]["context"]["policy_sha256"],
        items=_public(items),
        open_count=sum(item["can_register"] for item in items),
    )


def prepare_opportunity_review(engine, source_id, operation_id, source_document_ids, *, at):
    _operation_id(operation_id)
    state, parameters, snapshot = _prepare(engine, source_id, source_document_ids, at)
    return PreparedOpportunityReview(
        operation_id,
        source_id,
        state["installation_id"],
        canonical_json(parameters),
        canonical_json(snapshot),
        at,
    )


def _operation(connection, source_id, operation_id):
    row = _row(connection, operations, operations.c.id == operation_id)
    if row is None:
        return None
    if row["source_id"] != source_id:
        raise _error("operation_conflict")
    try:
        valid = (
            canonical_sha256(row["parameters"]) == row["parameters_sha256"]
            and canonical_sha256(row["snapshot"]["payload"]) == row["snapshot"]["sha256"]
            and row["parameters"]["source_id"] == source_id
            and row["parameters"]["evaluated_at"] == row["created_at"]
            and row["snapshot"]["payload"]["context"]["installation_id"] == row["installation_id"]
            and len(row["snapshot"]["payload"]["items"])
            == len(row["parameters"]["source_document_ids"])
            and [x["source_document_id"] for x in row["snapshot"]["payload"]["items"]]
            == row["parameters"]["source_document_ids"]
        )
    except (KeyError, TypeError):
        valid = False
    if not valid:
        raise _error("operation_invalid")
    if row["finished_at"] is not None:
        items = row["snapshot"]["payload"]["items"]
        if len(row["results"]) != len(items) or row["finished_at"] < row["created_at"]:
            raise _error("operation_invalid")
        for item, result in zip(items, row["results"], strict=True):
            if result.get("source_document_id") != item["source_document_id"]:
                raise _error("operation_invalid")
            if not item["can_register"]:
                if result.get("status") != item["status"]:
                    raise _error("operation_invalid")
                continue
            event = _row(
                connection, notification_events, notification_events.c.id == result.get("event_id")
            )
            decision = _row(
                connection,
                notification_decisions,
                notification_decisions.c.id == result.get("decision_id"),
            )
            if (
                result.get("status") != "applied"
                or event is None
                or decision is None
                or event["kind"] != "opportunity_review"
                or event["event_seq"] != 0
                or event["document_id"] != item["document_id"]
                or event["installation_id"] != row["installation_id"]
                or event["selected_decision_id"] != decision["id"]
                or decision["event_id"] != event["id"]
                or decision["decision"] != item["decision"]
                or event["effective_route"] != item["decision"]["effective_route"]
            ):
                raise _error("operation_invalid")
    return row


def _summary(row, *, reused=False):
    return dict(
        operation_id=row["id"],
        source_id=row["source_id"],
        evaluated_at=row["created_at"],
        complete=row["finished_at"] is not None,
        reused=reused,
        results=row["results"],
        items=_public(row["snapshot"]["payload"]["items"]),
    )


def _apply(engine, source_id, operation_id, *, clock):
    try:
        with engine.begin() as connection:
            operation = _operation(connection, source_id, operation_id)
            if operation is None:
                raise _error("operation_missing")
            if operation["finished_at"] is not None:
                return _summary(operation, reused=True)
            parameters = operation["parameters"]
            state, _, context = _state(connection, source_id, parameters["evaluated_at"])
            payload = operation["snapshot"]["payload"]
            if (
                context != payload["context"]
                or state["installation_id"] != operation["installation_id"]
            ):
                raise _error("stale_context")
            # Validate every frozen item before writing any event. SQL errors
            # after this point still roll back the complete group.
            for item in payload["items"]:
                current = _capture(connection, state, item["source_document_id"])
                _content(current)
                if _input_token(current) != item["input_token"]:
                    raise _error("stale_input")
            results = []
            for item in payload["items"]:
                if not item["can_register"]:
                    results.append(
                        dict(source_document_id=item["source_document_id"], status=item["status"])
                    )
                    continue
                decision = Decision.model_validate(item["decision"])
                facts = NoticeFacts.model_validate(item["facts"])
                if (
                    decision.effective_route not in {"digest", "immediate"}
                    or decision.time_status != "open"
                    or decision.eligibility == "ineligible"
                    or decision.relevance != "matched"
                    or decision.policy_sha256 != context["policy_sha256"]
                    or decision.content_sha256 != item["content_sha256"]
                    or facts.content_sha256 != item["content_sha256"]
                ):
                    raise _error("operation_invalid")
                event_id = connection.execute(
                    notification_events.insert().values(
                        installation_id=state["installation_id"],
                        document_id=item["document_id"],
                        event_seq=0,
                        kind="opportunity_review",
                        version_id=item["version_id"],
                        body_response_id=item["body_response_id"],
                        observed_response_id=item["observed_response_id"],
                        occurred_at=operation["created_at"],
                    )
                ).inserted_primary_key[0]
                decision_id = connection.execute(
                    notification_decisions.insert().values(
                        event_id=event_id,
                        policy_revision_id=state["policy_revision_id"],
                        evaluation_key="opportunity-review:" + operation_id,
                        evaluated_at=operation["created_at"],
                        decision=item["decision"],
                        facts=item["facts"],
                        context=item["context"],
                    )
                ).inserted_primary_key[0]
                outbox_id = None
                if decision.effective_route == "immediate":
                    delivery_key = canonical_sha256(
                        dict(
                            installation_id=state["installation_id"],
                            source_id=source_id,
                            source_document_id=item["source_document_id"],
                            event_seq=0,
                            recipient_key="primary",
                        )
                    )
                    outbox_id = connection.execute(
                        email_outbox.insert().values(
                            event_id=event_id,
                            decision_id=decision_id,
                            delivery_key=delivery_key,
                            sender=state["sender"],
                            recipient=state["recipient"],
                            created_at=operation["created_at"],
                        )
                    ).inserted_primary_key[0]
                connection.execute(
                    notification_events.update()
                    .where(notification_events.c.id == event_id)
                    .values(
                        selected_decision_id=decision_id,
                        effective_route=decision.effective_route,
                        delivery_intent_registered_at=operation["created_at"],
                        outbox_id=outbox_id,
                    )
                )
                results.append(
                    dict(
                        source_document_id=item["source_document_id"],
                        status="applied",
                        event_id=event_id,
                        decision_id=decision_id,
                    )
                )
            finished_at = clock()
            notification_time(finished_at)
            if finished_at < operation["created_at"]:
                raise _error("time_invalid")
            connection.execute(
                operations.update()
                .where(operations.c.id == operation_id)
                .values(results=results, finished_at=finished_at)
            )
            operation.update(results=results, finished_at=finished_at)
            return _summary(operation)
    except SQLAlchemyError as exc:
        raise _error("database_write_failed") from exc


def register_opportunity_review(engine, prepared: PreparedOpportunityReview, *, clock=None):
    """Freeze preparation, then atomically apply the whole group; no rule rerun."""
    parameters, snapshot = json.loads(prepared.parameters_json), json.loads(prepared.snapshot_json)
    try:
        with engine.begin() as connection:
            existing = _operation(connection, prepared.source_id, prepared.operation_id)
            if existing is not None:
                if existing["parameters"] != parameters or existing["snapshot"] != snapshot:
                    raise _error("operation_parameters_mismatch")
            else:
                connection.execute(
                    operations.insert().values(
                        id=prepared.operation_id,
                        installation_id=prepared.installation_id,
                        source_id=prepared.source_id,
                        parameters=parameters,
                        parameters_sha256=canonical_sha256(parameters),
                        snapshot=snapshot,
                        results=[],
                        created_at=prepared.evaluated_at,
                    )
                )
    except SQLAlchemyError as exc:
        raise _error("database_write_failed") from exc
    return _apply(
        engine, prepared.source_id, prepared.operation_id, clock=clock or (lambda: int(time.time()))
    )


def review_open_opportunities(
    engine, source_id, operation_id, source_document_ids=None, *, at=None, clock=None
):
    """Create an explicitly selected operation or resume its original frozen input."""
    _operation_id(operation_id)
    with engine.connect() as connection:
        existing = _operation(connection, source_id, operation_id)
    if existing is not None:
        if (at is not None and at != existing["created_at"]) or (
            source_document_ids is not None
            and list(_ids(source_document_ids)) != existing["parameters"]["source_document_ids"]
        ):
            raise _error("operation_parameters_mismatch")
        return _apply(engine, source_id, operation_id, clock=clock or (lambda: int(time.time())))
    if source_document_ids is None or at is None:
        raise _error("operation_missing")
    prepared = prepare_opportunity_review(
        engine, source_id, operation_id, source_document_ids, at=at
    )
    return register_opportunity_review(engine, prepared, clock=clock)
