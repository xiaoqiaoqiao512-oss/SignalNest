"""Explicit local opportunity review; only opt-in preview may use a model."""

import json
import logging
import sys
from datetime import UTC, datetime, timedelta

import sqlalchemy as sa
from sqlalchemy.exc import SQLAlchemyError

from signalnest.config import ConfigurationError
from signalnest.contracts import NoticeContent
from signalnest.errors import IngestError
from signalnest.eventlog import Event, log_event
from signalnest.instance_lock import WriterLockError, writer_lock
from signalnest.notifications.contracts import Profile
from signalnest.notifications.opportunity_review import (
    preview_open_opportunities,
    review_open_opportunities,
)
from signalnest.notifications.state import SHANGHAI, notification_time
from signalnest.schema import documents, notice_versions, notification_policy_revisions
from signalnest.storage import StorageError, open_initialized_engine


def _select(engine, source_id, at, days, limit):
    since = datetime.fromtimestamp(at, UTC).astimezone(SHANGHAI).date() - timedelta(days=days - 1)
    stmt = (
        sa.select(documents.c.source_document_id)
        .join(notice_versions, documents.c.current_version_id == notice_versions.c.id)
        .where(documents.c.source_id == source_id, notice_versions.c.published_date >= since)
        .order_by(notice_versions.c.published_date.desc(), documents.c.source_document_id)
    )
    with engine.connect() as connection:
        rows = connection.execute(stmt.limit(limit + 1)).scalars().all()
    return tuple(rows[:limit]), dict(
        publication_since=since.isoformat(),
        limit=limit,
        truncated=len(rows) > limit,
        selected=len(rows[:limit]),
        scope="bounded_publication_window_not_all_open_opportunities",
    )


def _model_advice(engine, source_id, result, config_path, at):
    from signalnest.model_client import ModelBudget
    from signalnest.notifications.semantic_review import review_notice
    from signalnest.querying.config import load_query_config

    settings = load_query_config(config_path)
    if settings.model is None:
        raise ConfigurationError("model configuration is required for explicit semantic review")
    budget = ModelBudget(
        max_calls=settings.limits.max_model_calls,
        max_requests=settings.limits.max_requests,
        run_seconds=settings.limits.run_seconds,
    )
    if not result["items"]:
        result.update(model_usage=budget.snapshot(), semantic_advice_only=True, semantic_failures=0)
        return
    # Close the database read before a model request. The model config's source
    # paths/profile never replace this instance's policy or open another store.
    with engine.connect() as connection:
        policy = (
            connection.execute(
                sa.select(notification_policy_revisions).where(
                    notification_policy_revisions.c.id == result["policy_revision_id"]
                )
            )
            .mappings()
            .one()
        )
        profile = Profile.model_validate(policy["manifest"]["profile"])
        inputs = []
        for item in result["items"]:
            if item.get("version_id") is None:
                continue
            row = (
                connection.execute(
                    sa.select(notice_versions).where(
                        notice_versions.c.id == item["version_id"],
                        notice_versions.c.document_id == item["document_id"],
                    )
                )
                .mappings()
                .one()
            )
            content = NoticeContent.model_validate(row["normalized_content"])
            if content.content_sha256() != item["content_sha256"]:
                raise IngestError("notification_review_stale_input", "notification")
            inputs.append((item, content))
    for item, content in inputs:
        item["semantic_review"] = review_notice(
            content,
            profile,
            at=datetime.fromtimestamp(at, UTC),
            model=settings.model,
            limits=settings.limits,
            budget=budget,
        ).model_dump(mode="json")
    result["model_usage"] = budget.snapshot()
    result["semantic_advice_only"] = True
    result["semantic_failures"] = sum(
        item.get("semantic_review", {}).get("status") == "fallback" for item in result["items"]
    )


def run_review_command(args, settings, logger, run_id):
    """Default preview is query-only; writes use the ordinary instance lock."""
    engine = None
    try:
        at = int(args.at) if args.at is not None else None
        if at is not None:
            notification_time(at)
        ids = tuple(args.source_document_id) if args.source_document_id else None
        if not 1 <= args.lookback_days <= 366 or not 1 <= args.limit <= 100:
            raise ValueError("invalid preview range")
        if ids is not None and not 1 <= len(ids) <= 100:
            raise ValueError("invalid identity selection")
        if args.apply:
            if not args.operation_id or args.model_config or ((ids is None) != (at is None)):
                raise ValueError(
                    "apply requires explicit identities/time, or operation-only resume"
                )
        elif at is None:
            raise ValueError("preview requires explicit time")
    except (ValueError, IngestError):
        print(
            "机会回顾参数错误：预览需 --at；登记需操作ID、明确身份和时间；恢复仅给操作ID",
            file=sys.stderr,
        )
        return 2
    try:
        if args.apply:
            with writer_lock(settings.storage.database):
                engine = open_initialized_engine(settings.storage.database)
                result = review_open_opportunities(
                    engine, settings.source.id, args.operation_id, ids, at=at
                )
        else:
            engine = open_initialized_engine(settings.storage.database, read_only=True)
            selection = {"scope": "explicit_source_document_ids"}
            if ids is None:
                ids, selection = _select(
                    engine, settings.source.id, at, args.lookback_days, args.limit
                )
            result = (
                preview_open_opportunities(engine, settings.source.id, ids, at=at)
                if ids
                else {
                    "preview": True,
                    "creates_delivery": False,
                    "items": [],
                    "open_count": 0,
                    "source_id": settings.source.id,
                    "evaluated_at": at,
                }
            )
            result["selection"] = selection
            if args.model_config:
                _model_advice(engine, settings.source.id, result, args.model_config, at)
    except (StorageError, IngestError, WriterLockError, SQLAlchemyError, ConfigurationError) as exc:
        code = (
            exc.code
            if isinstance(exc, (IngestError, WriterLockError))
            else "storage_unavailable_or_not_upgraded"
            if isinstance(exc, StorageError)
            else "review_unavailable"
        )
        log_event(
            logger,
            Event.PROCESSING_FAILED,
            level=logging.ERROR,
            source_id=settings.source.id,
            run_id=run_id,
            stage="opportunity_review",
            error_code=code,
        )
        hint = "；请核对数据库路径并显式 storage-init" if isinstance(exc, StorageError) else ""
        print(f"机会回顾失败: {code}{hint}；未报告完成", file=sys.stderr)
        return 1
    finally:
        if engine is not None:
            engine.dispose()
    log_event(
        logger,
        Event.STATUS_READ if not args.apply else Event.POLICY_APPLIED,
        source_id=settings.source.id,
        run_id=run_id,
        stage="opportunity_review",
    )
    print(json.dumps(result, ensure_ascii=False))
    return 1 if result.get("semantic_failures", 0) else 0
