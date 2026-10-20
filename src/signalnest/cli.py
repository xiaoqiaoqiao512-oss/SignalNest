"""Explicit CLI entry point; validation never creates directories or a database."""

import argparse
import json
import logging
import sys
import time
from pathlib import Path
from uuid import uuid4

from signalnest.config import ConfigurationError, load_config
from signalnest.eventlog import Event, configure_logging, log_event


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="signalnest", description="个人校园信息助手（离线处理与有界采集）"
    )
    commands = parser.add_subparsers(dest="command", required=True)
    query_check = commands.add_parser(
        "ask-config-check", help="校验查询 TOML 与实例/画像配置；不打开存储或读取密钥"
    )
    query_check.add_argument("--config", type=Path, required=True)
    ask_parser = commands.add_parser("ask", help="有界只读校园查询；模型失败返回已有检索结果")
    ask_parser.add_argument("--config", type=Path, required=True, help="查询配置（不是采集配置）")
    ask_parser.add_argument("--question", required=True)
    ask_parser.add_argument("--at", help="显式查询时间 ISO-8601，含 UTC offset；默认本次实际时间")
    ask_parser.add_argument(
        "--local", action="store_true", help="只运行本地检索，不调用模型或读取密钥"
    )
    ask_parser.add_argument("--json", action="store_true", help="输出含证据与诊断的 JSON")
    profile = commands.add_parser("profile-check", help="校验本地示例/个人画像；不访问存储或网络")
    profile.add_argument("--profile", type=Path, required=True)
    preview = commands.add_parser("decision-preview", help="纯本地预览事实、决策理由与路线；不发送")
    preview.add_argument("--profile", type=Path, required=True)
    input_group = preview.add_mutually_exclusive_group(required=True)
    input_group.add_argument("--file", type=Path, help="原始 HTML；同时给出 --url")
    input_group.add_argument("--notice-json", type=Path, help="NoticeContent 规范 JSON 文件")
    preview.add_argument("--url", help="HTML 对应的最终详情 URL，不猜文件名")
    preview.add_argument(
        "--parser",
        choices=("whu-student-notices", "ems-notices", "cs-undergrad-notices"),
        default="whu-student-notices",
        help="仅用于本地 HTML；EMS/计算机学院详情预览不启用该来源的网络采集",
    )
    preview.add_argument("--at", required=True, help="显式决策时间 ISO-8601，必须含 UTC offset")
    preview.add_argument(
        "--next-digest-at", required=True, help="显式下次 Digest 时间，含 UTC offset"
    )
    preview.add_argument(
        "--event-kind",
        choices=("new", "update", "activation_recent", "historical"),
        default="new",
        help="仅为预览上下文，不建立真实事件",
    )
    preview.add_argument("--mode", choices=("hybrid", "digest_only"), default="hybrid")
    preview.add_argument(
        "--previous-notice-json", type=Path, help="update 的旧 NoticeContent；本地读取"
    )
    preview.add_argument(
        "--previous-route", choices=("none", "immediate", "digest"), default="none"
    )
    for command, help_text in (
        ("notifications-preview", "只读预览通知启用边界与最近回顾；不发送"),
        ("notifications-activate", "显式保存通知启用边界与最近回顾；不发送"),
    ):
        activation = commands.add_parser(command, help=help_text)
        activation.add_argument("--config", type=Path, required=True)
        activation.add_argument("--profile", type=Path, required=True)
        activation.add_argument("--activation-id", required=True)
        activation.add_argument("--sender", required=True)
        activation.add_argument("--recipient", required=True)
        activation.add_argument("--mode", metavar="{hybrid,digest_only}", default="hybrid")
        activation.add_argument("--digest-hour", default=9)
        activation.add_argument("--digest-minute", default=0)
        activation.add_argument("--no-initial-recent", action="store_true")
        activation.add_argument("--at", help="本次处理 UTC Unix 秒，默认当前处理时间")
    notification_status = commands.add_parser(
        "notifications-status", help="只读检查通知启用与事件状态；不联网、不发送"
    )
    notification_status.add_argument("--config", type=Path, required=True)
    policy_update = commands.add_parser(
        "notifications-policy-update", help="显式保存政策版本；已冻结邮件保持原样，不发送"
    )
    policy_update.add_argument("--config", type=Path, required=True)
    policy_update.add_argument("--profile", type=Path, required=True)
    policy_update.add_argument("--operation-id", required=True, help="本次操作的稳定标识")
    policy_update.add_argument("--at", required=True, help="显式 UTC Unix 秒")
    policy_update.add_argument("--preview", action="store_true", help="只读预览，不保存")
    reevaluate = commands.add_parser(
        "notifications-reevaluate", help="对显式事件集合有界重评，或恢复原操作；不发送"
    )
    reevaluate.add_argument("--config", type=Path, required=True)
    reevaluate.add_argument("--operation-id", required=True, help="新建/恢复操作的稳定标识")
    reevaluate.add_argument("--event-id", action="append", help="显式事件 ID，可重复，最多 100 个")
    reevaluate.add_argument("--at", help="新建操作的显式 UTC Unix 秒；恢复时省略")
    reevaluate.add_argument("--preview", action="store_true", help="只读预览显式事件集合")
    review = commands.add_parser(
        "notifications-review-open", help="预览较早发布的当前机会；显式 --apply 登记回顾，不发送"
    )
    review.add_argument("--config", type=Path, required=True)
    review.add_argument("--operation-id", help="显式登记或恢复的稳定操作标识")
    review.add_argument("--source-document-id", action="append", help="来源内稳定身份，可重复")
    review.add_argument("--at", help="预览/新建操作的显式 UTC Unix 秒")
    review.add_argument("--lookback-days", type=int, default=90, help="自动预览范围，1–366天")
    review.add_argument("--limit", type=int, default=100, help="自动预览上限，1–100条")
    mode = review.add_mutually_exclusive_group()
    mode.add_argument("--preview", action="store_true", help="只读预览（默认）")
    mode.add_argument("--apply", action="store_true", help="登记明确选定的确认开放机会；不发送")
    review.add_argument(
        "--model-config", type=Path, help="仅预览：复用查询模型配置，显式启用语义复核"
    )
    mail_plan = commands.add_parser("mail-plan", help="有界计划并冻结本地邮件；不发送")
    mail_plan.add_argument("--config", type=Path, required=True)
    mail_plan.add_argument("--at", help="本次计划 UTC Unix 秒，默认当前处理时间")
    mail_plan.add_argument("--max-messages", default=5, help="本次邮件上限，1–20，默认 5")
    mail_plan.add_argument("--max-events", default=50, help="本次事件上限，1–100，默认 50")
    mail_plan.add_argument(
        "--max-bytes", default=131072, help="每封邮件字节上限，1024–1048576，默认 131072"
    )
    mail_plan.add_argument(
        "--preview", action="store_true", help="只读预览计划，不保存或获取写入锁"
    )
    mail_preview = commands.add_parser("mail-preview", help="查看已冻结邮件的地址、正文与精确成员")
    mail_preview.add_argument("--config", type=Path, required=True)
    mail_preview.add_argument("--mail-id", required=True, help="已冻结邮件的正整数 ID")
    background_mail = commands.add_parser(
        "scheduled-mail", help="按配置计划并发送邮件；须显式启用后台邮件"
    )
    background_mail.add_argument("--config", type=Path, required=True)
    drain = commands.add_parser("mail-drain", help="显式有界发送已冻结邮件；需要 SMTP 配置")
    drain.add_argument("--config", type=Path, required=True)
    drain.add_argument("--max-messages", help="本次尝试上限，1–20；默认取 mail_sending 配置")
    drain.add_argument("--run-seconds", help="运行预算秒，大于 0 且不超过 3600；在邮件之间检查")
    mail_status = commands.add_parser("mail-status", help="只读诊断发送积压与未知结果；不发送")
    mail_status.add_argument("--config", type=Path, required=True)
    mail_status.add_argument("--at", help="诊断 UTC Unix 秒，默认当前时间")
    retry = commands.add_parser("mail-retry", help="显式授权一封失败邮件多尝试一次；不立即发送")
    retry.add_argument("--config", type=Path, required=True)
    retry.add_argument("--mail-id", required=True, help="失败邮件的正整数 ID")
    retry.add_argument("--at", help="授权 UTC Unix 秒，默认当前时间")
    for command, description in (
        ("mail-pause", "暂停发送通道；不发送"),
        ("mail-resume", "显式恢复发送通道；不发送"),
    ):
        control = commands.add_parser(command, help=description)
        control.add_argument("--config", type=Path, required=True)
        control.add_argument("--at", help="操作 UTC Unix 秒，默认当前时间")
    validate = commands.add_parser("config-check", help="校验 TOML 配置，不创建本地存储")
    validate.add_argument("--config", type=Path, required=True, help="TOML 配置文件路径")
    initialize = commands.add_parser("storage-init", help="显式初始化本地存储并升级到最新迁移")
    initialize.add_argument("--config", type=Path, required=True, help="TOML 配置文件路径")
    local = commands.add_parser("import-page", help="归档并解析已有页面；不发起 HTTP 获取")
    local.add_argument("--config", type=Path, required=True)
    local.add_argument(
        "--metadata", type=Path, required=True, help="包含获取时间和来源的 JSON 文件"
    )
    local.add_argument("--file", type=Path, help="完整原始 HTML 文件；304/不可用正文时省略")
    local.add_argument("--processed-at", type=int, help="本次处理 UTC Unix 秒，默认当前处理时间")
    replay = commands.add_parser("reparse", help="验证并重新解析已登记原文，复用原获取证据")
    replay.add_argument("--config", type=Path, required=True)
    replay.add_argument("--response-id", type=int, required=True)
    replay.add_argument("--processed-at", type=int, help="本次处理 UTC Unix 秒，默认当前处理时间")
    status = commands.add_parser("status", help="只读诊断积压、冷却、覆盖与最近运行；不联网")
    status.add_argument("--config", type=Path, required=True)
    references = commands.add_parser("references-list", help="只读查看待适配列表引用；不抓外部正文")
    references.add_argument("--config", type=Path, required=True)
    references.add_argument("--limit", type=int, default=20, help="返回 1–100 条，默认 20")
    references.add_argument("--offset", type=int, default=0, help="结果偏移 0–10000，默认 0")
    search = commands.add_parser("search", help="只读查询当前成功通知；不联网、不修复索引")
    search.add_argument("--config", type=Path, required=True)
    search.add_argument("--query", required=True, help="字面关键词，空格分隔；固定别名见查询结果")
    search.add_argument("--from", dest="date_from", help="站点发布日期下界 YYYY-MM-DD，含当天")
    search.add_argument("--to", dest="date_to", help="站点发布日期上界 YYYY-MM-DD，含当天")
    search.add_argument("--source-id", help="来源精确过滤，省略则查询全部已保存来源")
    search.add_argument("--limit", type=int, default=20, help="返回 1–100 条，默认 20")
    search.add_argument("--offset", type=int, default=0, help="结果偏移 0–10000，默认 0")
    show = commands.add_parser("notice-show", help="只读查看单条通知的当前成功正文及诊断")
    show.add_argument("--config", type=Path, required=True)
    show.add_argument("--document-id", type=int, required=True)
    rebuild = commands.add_parser("search-rebuild", help="持写入锁重建派生索引；不读取原文或联网")
    rebuild.add_argument("--config", type=Path, required=True)
    for name, description in (
        ("rollout-check", "本地部署准备检查；不发送、不证明真实上线"),
        ("observe", "输出一条只读运行观察 JSON；由操作者保存"),
    ):
        rollout = commands.add_parser(name, help=description)
        rollout.add_argument("--config", type=Path, required=True)
        rollout.add_argument("--release-root", type=Path, required=True, help="本次发布源码根目录")
        rollout.add_argument("--at", type=int, required=True, help="明确的观察 UTC Unix 秒")
        rollout.add_argument("--profile", type=Path, help="真实个人画像的文件路径")
        rollout.add_argument(
            "--profile-confirmed",
            action="store_true",
            help="操作者声明已核对个人画像；不代替实采验收",
        )
    policy = commands.add_parser(
        "apply-recheck-policy", help="显式保守重算成功记录到期时间；不联网"
    )
    policy.add_argument("--config", type=Path, required=True)
    scheduled = commands.add_parser("scheduled-run", help="按 TOML 中有界预算执行一次定时任务")
    scheduled.add_argument("--config", type=Path, required=True)
    scheduled.add_argument("--mode", choices=("regular", "full"), required=True)
    crawl = commands.add_parser("crawl-once", help="有界执行一次列表扫描与独立详情处理")
    crawl.add_argument("--config", type=Path, required=True)
    crawl.add_argument("--scan", dest="scan_mode", choices=("full", "limited"), required=True)
    crawl.add_argument("--max-pages", type=int, help="列表页上限；full 默认 64，limited 默认 2")
    crawl.add_argument("--max-details", type=int, default=20, help="详情尝试上限，默认 20")
    crawl.add_argument("--max-requests", type=int, default=120, help="实际 HTTP 请求上限，默认 120")
    crawl.add_argument("--run-seconds", type=float, default=600.0, help="运行时间预算，默认 600 秒")
    crawl.add_argument(
        "--resource-seconds", type=float, default=60.0, help="每个资源时间预算，默认 60 秒"
    )
    crawl.add_argument(
        "--max-body-bytes", type=int, default=2 * 1024 * 1024, help="每份响应正文上限，默认 2 MiB"
    )
    args = parser.parse_args(argv)
    if args.command in {"ask", "ask-config-check"}:
        from signalnest.querying.cli import query_command

        return query_command(args)
    if args.command in {"profile-check", "decision-preview"}:
        return _notification_preview(args)
    logger = configure_logging()
    run_id = uuid4().hex
    try:
        settings = load_config(args.config)
    except ConfigurationError as exc:
        log_event(logger, Event.CONFIG_INVALID, level=logging.ERROR, run_id=run_id)
        print(f"配置错误: {exc}", file=sys.stderr)
        return 2
    if args.command == "storage-init":
        from signalnest.storage import StorageError, initialize_storage

        try:
            revision = initialize_storage(settings.storage)
        except StorageError as exc:
            log_event(
                logger,
                Event.STORAGE_INIT_FAILED,
                level=logging.ERROR,
                source_id=settings.source.id,
                run_id=run_id,
            )
            print(f"存储错误: {exc}", file=sys.stderr)
            return 1
        log_event(logger, Event.STORAGE_INITIALIZED, source_id=settings.source.id, run_id=run_id)
        print(f"存储就绪: database={settings.storage.database}, revision={revision}")
        return 0
    if args.command in {"import-page", "reparse"}:
        return _offline_command(args, settings, logger, run_id)
    if args.command == "crawl-once":
        return _crawl_command(args, settings, logger, run_id)
    if args.command == "scheduled-mail":
        return _background_command(args, settings, logger, run_id)
    if args.command == "scheduled-run":
        if settings.mail_runtime.enabled:
            return _background_command(args, settings, logger, run_id)
        budget = getattr(settings.runtime, args.mode)
        args.scan_mode = "limited" if args.mode == "regular" else "full"
        for key, value in budget.model_dump().items():
            setattr(args, key, value)
        return _crawl_command(args, settings, logger, run_id)
    if args.command in {"status", "apply-recheck-policy"}:
        return _maintenance_command(args, settings, logger, run_id)
    if args.command == "references-list":
        return _reference_command(args, settings, logger, run_id)
    if args.command in {"search", "notice-show", "search-rebuild"}:
        return _search_command(args, settings, logger, run_id)
    if args.command in {"rollout-check", "observe"}:
        return _rollout_command(args, settings, logger, run_id)
    if args.command in {
        "notifications-preview",
        "notifications-activate",
        "notifications-status",
    }:
        return _notification_state_command(args, settings, logger, run_id)
    if args.command in {"mail-plan", "mail-preview"}:
        return _mail_command(args, settings, logger, run_id)
    if args.command in {"mail-drain", "mail-status", "mail-retry", "mail-pause", "mail-resume"}:
        return _mail_sending_command(args, settings, logger, run_id)
    if args.command in {"notifications-policy-update", "notifications-reevaluate"}:
        return _notification_maintenance_command(args, settings, logger, run_id)
    if args.command == "notifications-review-open":
        from signalnest.notifications.review_cli import run_review_command

        return run_review_command(args, settings, logger, run_id)
    log_event(logger, Event.CONFIG_VALIDATED, source_id=settings.source.id, run_id=run_id)
    print(f"配置有效: source_id={settings.source.id}")
    print(f"data_dir={settings.storage.data_dir}")
    print(f"database={settings.storage.database}")
    return 0


def _search_command(args, settings, logger, run_id) -> int:
    from contextlib import nullcontext
    from datetime import date

    from pydantic import ValidationError
    from sqlalchemy.exc import SQLAlchemyError

    from signalnest.instance_lock import WriterLockError, writer_lock
    from signalnest.search import (
        SearchError,
        SearchQuery,
        get_notice,
        rebuild_index,
        search_notices,
    )
    from signalnest.storage import StorageError, open_initialized_engine

    try:
        query = None
        if args.command == "search":
            query = SearchQuery(
                query=args.query,
                date_from=date.fromisoformat(args.date_from) if args.date_from else None,
                date_to=date.fromisoformat(args.date_to) if args.date_to else None,
                source_id=args.source_id,
                limit=args.limit,
                offset=args.offset,
            )
        elif args.command == "notice-show" and args.document_id <= 0:
            raise ValueError("invalid document ID")
    except (ValueError, ValidationError):
        print("查询参数错误：请检查关键词、日期范围、数量或通知 ID", file=sys.stderr)
        return 2
    engine = None
    writing = args.command == "search-rebuild"
    try:
        guard = writer_lock(settings.storage.database) if writing else nullcontext()
        with guard:
            engine = open_initialized_engine(settings.storage.database, read_only=not writing)
            if writing:
                result = rebuild_index(engine)
            elif query is not None:
                result = search_notices(engine, query)
            else:
                result = get_notice(engine, args.document_id)
        log_event(
            logger,
            Event.SEARCH_INDEX_REBUILT if writing else Event.SEARCH_READ,
            source_id=settings.source.id,
            run_id=run_id,
        )
        print(result.model_dump_json())
        return 0
    except (StorageError, WriterLockError, SearchError, SQLAlchemyError) as exc:
        code = (
            getattr(exc, "code", None) if isinstance(exc, (SearchError, WriterLockError)) else None
        )
        message = str(exc) if isinstance(exc, StorageError) else (code or "search_database_error")
        print(f"历史查询错误：{message}；索引陈旧时请显式执行 search-rebuild", file=sys.stderr)
        return 1
    finally:
        if engine is not None:
            engine.dispose()


def _rollout_command(args, settings, logger, run_id) -> int:
    from signalnest.rollout import RolloutError, inspect_rollout, observe_instance

    try:
        function = observe_instance if args.command == "observe" else inspect_rollout
        result = function(
            settings,
            at=args.at,
            release_root=args.release_root,
            profile_path=args.profile,
            profile_confirmed=args.profile_confirmed,
        )
    except RolloutError as exc:
        print(f"部署检查错误：{exc.code}", file=sys.stderr)
        return 1
    log_event(
        logger, Event.STATUS_READ, source_id=settings.source.id, run_id=run_id, stage="rollout"
    )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    # Observations remain useful with attention items; readiness reports a failing check.
    return 0 if args.command == "observe" or result["prepared"] else 1


def _background_command(args, settings, logger, run_id) -> int:
    from sqlalchemy.exc import SQLAlchemyError

    from signalnest.errors import IngestError
    from signalnest.instance_lock import WriterLockError
    from signalnest.mail.background import run_mail_pass, run_scheduled_cycle
    from signalnest.mail.contracts import MailError
    from signalnest.rawstore import RawStoreError
    from signalnest.storage import StorageError

    if settings.mail_runtime.enabled and settings.smtp is None:
        print("后台邮件配置错误: 启用 mail_runtime 后必须配置 smtp；未执行本轮", file=sys.stderr)
        return 2
    try:
        result = (
            run_mail_pass(settings, run_id=run_id)
            if args.command == "scheduled-mail"
            else run_scheduled_cycle(settings, args.mode, run_id=run_id)
        )
    except KeyboardInterrupt:
        print("后台运行中断；发送尝试可能未知，下次持锁后恢复", file=sys.stderr)
        return 130
    except (
        MailError,
        IngestError,
        StorageError,
        RawStoreError,
        WriterLockError,
        SQLAlchemyError,
    ) as exc:
        code = (
            exc.code
            if isinstance(exc, (MailError, IngestError, RawStoreError, WriterLockError))
            else "database_unavailable"
        )
        log_event(
            logger,
            Event.PROCESSING_FAILED,
            level=logging.ERROR,
            source_id=settings.source.id,
            run_id=run_id,
            stage="background_abort",
            error_code=code,
        )
        print(f"后台运行停止: {code}；未继续后续阶段，请检查状态与日志", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 1 if result["needs_attention"] else 0


def _mail_command(args, settings, logger, run_id) -> int:
    from pydantic import ValidationError
    from sqlalchemy.exc import SQLAlchemyError

    from signalnest.errors import IngestError
    from signalnest.instance_lock import WriterLockError, writer_lock
    from signalnest.mail.contracts import MailError, PlanOptions
    from signalnest.notifications.state import notification_time
    from signalnest.storage import StorageError, open_initialized_engine

    options = None
    at = None
    mail_id = None
    try:
        # Check untrusted local arguments before opening storage or acquiring a lock.
        if args.command == "mail-plan":
            options = PlanOptions(
                max_messages=int(args.max_messages),
                max_events=int(args.max_events),
                max_bytes=int(args.max_bytes),
            )
            at = int(args.at) if args.at is not None else int(time.time())
            notification_time(at)
        else:
            mail_id = int(args.mail_id)
            if mail_id <= 0 or mail_id > 2**63 - 1:
                raise ValueError("mail ID must fit a positive SQLite integer")
    except (ValueError, ValidationError, IngestError):
        print("邮件参数错误: 请检查正整数邮件 ID、数量、字节上限及处理时间", file=sys.stderr)
        return 2

    from signalnest.mail.planning import plan_mail, preview_mail, preview_plan

    engine = None
    stage = "mail_plan" if args.command == "mail-plan" else "mail_preview"
    try:
        if not settings.storage.database.is_file():
            raise StorageError("数据库不可用或未初始化，请先执行 storage-init")
        if args.command == "mail-plan" and not args.preview:
            with writer_lock(settings.storage.database):
                engine = open_initialized_engine(settings.storage.database)
                result = plan_mail(engine, settings.source.id, options, at=at)
        else:
            engine = open_initialized_engine(settings.storage.database)
            result = (
                preview_plan(engine, settings.source.id, options, at=at)
                if args.command == "mail-plan"
                else preview_mail(engine, settings.source.id, mail_id)
            )
    except (MailError, StorageError, WriterLockError, SQLAlchemyError) as exc:
        code = exc.code if isinstance(exc, (MailError, WriterLockError)) else "database_unavailable"
        log_event(
            logger,
            Event.PROCESSING_FAILED,
            level=logging.ERROR,
            source_id=settings.source.id,
            run_id=run_id,
            stage=stage,
            error_code=code,
        )
        print(f"邮件计划或预览失败: {code}；请检查启用状态、存储和锁占用", file=sys.stderr)
        return 1
    finally:
        if engine is not None:
            engine.dispose()
    event = (
        Event.MAIL_PLANNED
        if args.command == "mail-plan" and not args.preview
        else Event.MAIL_PREVIEWED
    )
    log_event(logger, event, source_id=settings.source.id, run_id=run_id, stage=stage)
    # Body and addresses are explicit preview output, never structured log fields.
    print(json.dumps(result, ensure_ascii=False))
    return 0


def _mail_sending_command(args, settings, logger, run_id) -> int:
    from pydantic import ValidationError
    from sqlalchemy.exc import SQLAlchemyError

    from signalnest.errors import IngestError
    from signalnest.instance_lock import WriterLockError, writer_lock
    from signalnest.mail.contracts import MailError
    from signalnest.mail.sending import (
        DrainOptions,
        drain_mail,
        mail_status,
        retry_mail,
        set_sending_paused,
    )
    from signalnest.notifications.state import notification_time
    from signalnest.storage import StorageError, open_initialized_engine

    options = None
    at = None
    mail_id = None
    try:
        if args.command == "mail-drain":
            budgets = settings.mail_sending.model_dump()
            if args.max_messages is not None:
                budgets["max_messages"] = int(args.max_messages)
            if args.run_seconds is not None:
                budgets["run_seconds"] = float(args.run_seconds)
            options = DrainOptions.model_validate(budgets)
            if settings.smtp is None:
                print("邮件发送配置错误: 请先明确配置 smtp；本次没有发送", file=sys.stderr)
                return 2
        else:
            at = int(args.at) if args.at is not None else int(time.time())
            notification_time(at)
            if args.command == "mail-retry":
                mail_id = int(args.mail_id)
                if not 1 <= mail_id <= 2**63 - 1:
                    raise ValueError("mail ID must fit a positive SQLite integer")
    except (ValueError, ValidationError, IngestError):
        print("邮件发送参数错误: 请检查数量、预算、正整数邮件 ID 和处理时间", file=sys.stderr)
        return 2

    engine = None
    stage = args.command.replace("-", "_")
    try:
        if not settings.storage.database.is_file():
            raise StorageError("数据库不可用或未初始化，请先执行 storage-init")
        if args.command == "mail-status":
            engine = open_initialized_engine(settings.storage.database)
            result = mail_status(engine, settings.source.id, at=at)
        else:
            with writer_lock(settings.storage.database):
                engine = open_initialized_engine(settings.storage.database)
                if args.command == "mail-drain":
                    result = drain_mail(engine, settings.source.id, settings.smtp, options)
                elif args.command == "mail-retry":
                    result = retry_mail(engine, settings.source.id, mail_id, at=at)
                else:
                    result = set_sending_paused(
                        engine, settings.source.id, args.command == "mail-pause", at=at
                    )
    except (MailError, StorageError, IngestError, WriterLockError, SQLAlchemyError) as exc:
        code = (
            exc.code
            if isinstance(exc, (MailError, IngestError, WriterLockError))
            else "database_unavailable"
        )
        log_event(
            logger,
            Event.PROCESSING_FAILED,
            level=logging.ERROR,
            source_id=settings.source.id,
            run_id=run_id,
            stage=stage,
            error_code=code,
            mail_id=mail_id,
        )
        print(f"邮件发送状态操作失败: {code}；未继续发送，请检查存储、状态和锁", file=sys.stderr)
        return 1
    finally:
        if engine is not None:
            engine.dispose()
    event = (
        Event.MAIL_STATUS
        if args.command == "mail-status"
        else Event.MAIL_RETRIED
        if args.command == "mail-retry"
        else Event.MAIL_SEND_FINISHED
        if args.command == "mail-drain"
        else Event.MAIL_PAUSED
    )
    log_event(
        logger, event, source_id=settings.source.id, run_id=run_id, stage=stage, mail_id=mail_id
    )
    print(json.dumps(result, ensure_ascii=False))
    return 1 if args.command == "mail-drain" and result.get("needs_attention", False) else 0


def _notification_maintenance_command(args, settings, logger, run_id) -> int:
    import re

    from sqlalchemy.exc import SQLAlchemyError

    from signalnest.errors import IngestError
    from signalnest.instance_lock import WriterLockError, writer_lock
    from signalnest.notifications.maintenance import (
        preview_policy_update,
        reevaluate_events,
        update_policy,
    )
    from signalnest.notifications.profile import ProfileError, load_profile
    from signalnest.notifications.state import notification_time
    from signalnest.storage import StorageError, open_initialized_engine

    profile = None
    event_ids = None
    at = None
    try:
        if re.fullmatch(r"[A-Za-z0-9_.:-]{1,64}", args.operation_id) is None:
            raise ValueError("invalid operation ID")
        if args.command == "notifications-policy-update":
            profile = load_profile(args.profile)
            at = int(args.at)
            notification_time(at)
        else:
            if args.event_id is not None:
                if not 1 <= len(args.event_id) <= 100:
                    raise ValueError("event selection must contain at most 100 IDs")
                event_ids = tuple(sorted({int(value) for value in args.event_id}))
                if any(not 1 <= event_id <= 2**63 - 1 for event_id in event_ids):
                    raise ValueError("event IDs must fit positive SQLite integers")
            if args.at is not None:
                at = int(args.at)
                notification_time(at)
            if (event_ids is None) != (at is None) or (args.preview and event_ids is None):
                raise ValueError("new/preview operations need both event IDs and explicit time")
    except (ProfileError, ValueError, IngestError):
        print(
            "通知维护参数错误: 请检查画像、操作标识、事件集合与显式时间；恢复时省略事件和时间",
            file=sys.stderr,
        )
        return 2

    engine = None
    stage = args.command.replace("-", "_")
    try:
        if not settings.storage.database.is_file():
            raise StorageError("数据库不可用或未初始化，请先执行 storage-init")
        if args.preview:
            engine = open_initialized_engine(settings.storage.database)
            if args.command == "notifications-policy-update":
                result = preview_policy_update(engine, settings.source.id, profile, at=at)
            else:
                result = reevaluate_events(
                    engine,
                    settings.source.id,
                    args.operation_id,
                    event_ids=event_ids,
                    at=at,
                    preview=True,
                )
        else:
            with writer_lock(settings.storage.database):
                engine = open_initialized_engine(settings.storage.database)
                if args.command == "notifications-policy-update":
                    result = update_policy(
                        engine, settings.source.id, profile, args.operation_id, at=at
                    )
                else:
                    result = reevaluate_events(
                        engine, settings.source.id, args.operation_id, event_ids=event_ids, at=at
                    )
    except (StorageError, IngestError, WriterLockError, SQLAlchemyError) as exc:
        code = (
            exc.code if isinstance(exc, (IngestError, WriterLockError)) else "database_unavailable"
        )
        log_event(
            logger,
            Event.PROCESSING_FAILED,
            level=logging.ERROR,
            source_id=settings.source.id,
            run_id=run_id,
            stage=stage,
            error_code=code,
        )
        print(f"通知维护失败: {code}；未报告完成，请检查启用状态、存储和锁", file=sys.stderr)
        return 1
    finally:
        if engine is not None:
            engine.dispose()
    needs_attention = not args.preview and any(
        member.get("status") != "applied" for member in result.get("results", [])
    )
    log_event(
        logger,
        Event.PROCESSING_FAILED
        if needs_attention
        else Event.STATUS_READ
        if args.preview
        else Event.POLICY_APPLIED,
        level=logging.WARNING if needs_attention else logging.INFO,
        source_id=settings.source.id,
        run_id=run_id,
        stage=stage,
        error_code="notification_reevaluation_incomplete" if needs_attention else None,
    )
    print(json.dumps(result, ensure_ascii=False))
    return 1 if needs_attention else 0


def _notification_state_command(args, settings, logger, run_id) -> int:
    from pydantic import ValidationError
    from sqlalchemy.exc import SQLAlchemyError

    from signalnest.errors import IngestError
    from signalnest.instance_lock import WriterLockError, writer_lock
    from signalnest.notifications.profile import ProfileError, load_profile
    from signalnest.notifications.state import (
        ActivationOptions,
        activate_notifications,
        notification_status,
        notification_time,
        preview_activation,
    )
    from signalnest.storage import StorageError, open_initialized_engine

    profile = None
    options = None
    at = None
    if args.command != "notifications-status":
        try:
            # Personal input is validated before any database or writer-lock operation.
            profile = load_profile(args.profile)
            options = ActivationOptions(
                activation_id=args.activation_id,
                notification_mode=args.mode,
                initial_recent_review=not args.no_initial_recent,
                digest_hour=int(args.digest_hour),
                digest_minute=int(args.digest_minute),
                sender=args.sender,
                recipient=args.recipient,
            )
            at = int(args.at) if args.at is not None else int(time.time())
            notification_time(at)
        except ProfileError:
            print("画像错误: 请检查本地画像文件、TOML 格式及画像字段", file=sys.stderr)
            return 2
        except (ValidationError, ValueError):
            print("通知参数错误: 请检查启用标识、邮箱地址、Digest 时间及处理时间", file=sys.stderr)
            return 2
        except IngestError as exc:
            print(f"通知参数错误: {exc.code}；请检查通知启用参数", file=sys.stderr)
            return 2

    engine = None
    try:
        if not settings.storage.database.is_file():
            raise StorageError("数据库不可用或未初始化，请先执行 storage-init")
        if args.command == "notifications-activate":
            with writer_lock(settings.storage.database):
                engine = open_initialized_engine(settings.storage.database)
                result = activate_notifications(engine, settings.source.id, profile, options, at=at)
        else:
            engine = open_initialized_engine(settings.storage.database)
            if args.command == "notifications-preview":
                result = preview_activation(engine, settings.source.id, profile, options, at=at)
            else:
                result = notification_status(engine, settings.source.id)
    except (StorageError, IngestError, WriterLockError, SQLAlchemyError) as exc:
        code = (
            exc.code if isinstance(exc, (IngestError, WriterLockError)) else "database_unavailable"
        )
        log_event(
            logger,
            Event.PROCESSING_FAILED,
            level=logging.ERROR,
            source_id=settings.source.id,
            run_id=run_id,
            stage="notification",
            error_code=code,
        )
        print(f"通知状态操作失败: {code}；请检查初始化、启用参数或锁占用", file=sys.stderr)
        return 1
    except Exception:
        log_event(
            logger,
            Event.PROCESSING_FAILED,
            level=logging.ERROR,
            source_id=settings.source.id,
            run_id=run_id,
            stage="notification",
            error_code="unexpected_error",
        )
        print(
            "通知状态操作失败: unexpected_error；未报告成功，请检查程序与本地存储",
            file=sys.stderr,
        )
        return 1
    finally:
        if engine is not None:
            engine.dispose()
    event = (
        Event.NOTIFICATIONS_ENABLED
        if args.command == "notifications-activate"
        else Event.STATUS_READ
    )
    log_event(
        logger,
        event,
        source_id=settings.source.id,
        run_id=run_id,
        stage="notification",
    )
    print(json.dumps(result, ensure_ascii=False))
    return 0


def _notification_preview(args):
    from datetime import datetime

    from pydantic import ValidationError

    from signalnest.contracts import NoticeContent, PageInput
    from signalnest.notifications.contracts import EventContext, aware_time
    from signalnest.notifications.decision import decide
    from signalnest.notifications.facts import extract_facts
    from signalnest.notifications.profile import ProfileError, load_profile
    from signalnest.parsing import ParseError, parse_notice

    # Reads are explicit local files. No config, database, lock, HTTP client or clock.
    limit = 4 * 1024 * 1024

    def read(path):
        with path.open("rb") as stream:
            value = stream.read(limit + 1)
        if len(value) > limit:
            raise ValueError("local preview input exceeds 4 MiB")
        return value

    def content(path):
        return NoticeContent.model_validate_json(read(path))

    def explicit_time(value, field):
        try:
            return aware_time(datetime.fromisoformat(value))
        except ValueError as exc:
            raise ValueError(f"{field} requires ISO-8601 with an explicit UTC offset") from exc

    try:
        profile = load_profile(args.profile)
        if args.command == "profile-check":
            print(
                json.dumps(
                    {"profile_valid": True, "profile_sha256": profile.sha256()}, ensure_ascii=False
                )
            )
            return 0
        now = explicit_time(args.at, "--at")
        next_digest = explicit_time(args.next_digest_at, "--next-digest-at")
        if args.file:
            if not args.url:
                raise ValueError("HTML input requires --url with its final page URL")
            notice_parser = parse_notice
            if args.parser == "ems-notices":
                from signalnest.ems_parsing import parse_ems_notice

                notice_parser = parse_ems_notice
            elif args.parser == "cs-undergrad-notices":
                from signalnest.cs_parsing import parse_cs_notice

                notice_parser = parse_cs_notice
            notice = notice_parser(PageInput(content=read(args.file), page_url=args.url)).content
        else:
            if args.url:
                raise ValueError("--url is used only with --file")
            if args.parser != "whu-student-notices":
                raise ValueError(
                    f"--parser {args.parser} requires --file; --notice-json is already normalized"
                )
            notice = content(args.notice_json)
        previous = content(args.previous_notice_json) if args.previous_notice_json else None
        if (previous is not None or args.previous_route != "none") and args.event_kind != "update":
            raise ValueError("previous content/route is only valid for update previews")
        context = EventContext(
            kind=args.event_kind,
            notification_mode=args.mode,
            next_digest_at=next_digest,
            previous_facts=extract_facts(previous) if previous is not None else None,
            previous_effective_route=args.previous_route,
            comparison_known=args.event_kind != "update" or previous is not None,
        )
        facts = extract_facts(notice)
        decision = decide(profile, facts, context, now=now)
    except ProfileError as exc:
        print(f"画像错误: {exc}", file=sys.stderr)
        return 2
    except ParseError as exc:
        print(f"解析失败: {exc.code}；未生成决策，请检查页面及 Parser 支持范围", file=sys.stderr)
        return 1
    except OSError:
        print("预览输入不可读；请检查本地文件路径和权限", file=sys.stderr)
        return 2
    except ValidationError:
        print("预览输入无效；检查 NoticeContent、HTTP(S) URL 及带时区的时间", file=sys.stderr)
        return 2
    except ValueError as exc:
        # Local validation failures have no successful empty-result fallback.
        print(f"预览参数错误: {exc}", file=sys.stderr)
        return 2
    print(
        json.dumps(
            {
                "preview_only": True,
                "facts": facts.model_dump(mode="json", exclude={"body_text"}),
                "decision": decision.model_dump(mode="json"),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def _reference_command(args, settings, logger, run_id):
    from sqlalchemy.exc import SQLAlchemyError

    from signalnest.errors import IngestError
    from signalnest.ingestion import list_references
    from signalnest.storage import StorageError, open_initialized_engine

    if not 1 <= args.limit <= 100 or not 0 <= args.offset <= 10000:
        print("引用查询参数错误：limit 为 1–100，offset 为 0–10000", file=sys.stderr)
        return 2
    try:
        engine = open_initialized_engine(settings.storage.database, read_only=True)
        try:
            result = list_references(
                engine, settings.source.id, limit=args.limit, offset=args.offset
            )
        finally:
            engine.dispose()
    except (StorageError, IngestError, SQLAlchemyError) as exc:
        code = exc.code if isinstance(exc, IngestError) else "database_unavailable"
        print(f"引用查询失败：{code}；请检查存储并先执行 storage-init", file=sys.stderr)
        return 1
    log_event(
        logger, Event.STATUS_READ, source_id=settings.source.id, run_id=run_id, stage="references"
    )
    print(json.dumps(result, ensure_ascii=False))
    return 0


def _maintenance_command(args, settings, logger, run_id):
    from sqlalchemy.exc import SQLAlchemyError

    from signalnest.errors import IngestError
    from signalnest.instance_lock import WriterLockError, writer_lock
    from signalnest.runtime_policy import apply_recheck_policy
    from signalnest.status import inspect_status
    from signalnest.storage import StorageError, open_initialized_engine

    try:
        if args.command == "status":
            result = inspect_status(settings).model_dump(mode="json")
            event = Event.STATUS_READ
        else:
            if not settings.storage.database.is_file():
                raise StorageError("数据库不可用或未初始化，请先执行 storage-init")
            with writer_lock(settings.storage.database):
                engine = open_initialized_engine(settings.storage.database)
                try:
                    at = int(time.time())
                    changed = apply_recheck_policy(engine, settings.source.id, at, settings.runtime)
                finally:
                    engine.dispose()
            result = {"source_id": settings.source.id, "processed_at": at, "changed": changed}
            event = Event.POLICY_APPLIED
    except (StorageError, IngestError, WriterLockError, SQLAlchemyError) as exc:
        code = (
            exc.code if isinstance(exc, (IngestError, WriterLockError)) else "database_unavailable"
        )
        log_event(
            logger,
            Event.PROCESSING_FAILED,
            level=logging.ERROR,
            source_id=settings.source.id,
            run_id=run_id,
            stage=args.command,
            error_code=code,
        )
        print(f"状态或策略操作失败: {code}；请检查存储路径、初始化和锁占用", file=sys.stderr)
        return 1
    log_event(logger, event, source_id=settings.source.id, run_id=run_id)
    print(json.dumps(result, ensure_ascii=False))
    return 0


def _crawl_command(args, settings, logger, run_id) -> int:
    # Importing help/config-check neither builds an HTTP client nor acquires a lock.
    from pydantic import ValidationError
    from sqlalchemy.exc import SQLAlchemyError

    from signalnest.crawling import CrawlOptions, crawl_once
    from signalnest.fetching import FetchLimits
    from signalnest.ingestion import IngestError
    from signalnest.instance_lock import WriterLockError
    from signalnest.rawstore import RawStoreError
    from signalnest.storage import StorageError

    try:
        options = CrawlOptions(
            scan_mode=args.scan_mode,
            lock_wait_seconds=(
                settings.runtime.full_lock_wait_seconds
                if args.command == "scheduled-run" and args.mode == "full"
                else 0.0
            ),
            max_pages=args.max_pages
            if args.max_pages is not None
            else (64 if args.scan_mode == "full" else 2),
            max_details=args.max_details,
            fetch_limits=FetchLimits(
                max_requests=args.max_requests,
                run_seconds=args.run_seconds,
                resource_seconds=args.resource_seconds,
                max_body_bytes=args.max_body_bytes,
            ),
        )
    except ValidationError:
        print("采集参数错误: 检查页数、详情数、请求数、时间预算与正文上限", file=sys.stderr)
        return 2
    try:
        summary = crawl_once(settings, options, run_id=run_id)
    except KeyboardInterrupt:
        print("采集已中断；请检查运行记录，下一次采集会从数据库事实恢复待办", file=sys.stderr)
        return 130
    except (StorageError, IngestError, RawStoreError, WriterLockError, SQLAlchemyError) as exc:
        code = (
            exc.code
            if isinstance(exc, (IngestError, RawStoreError, WriterLockError))
            else "database_unavailable"
        )
        log_event(
            logger,
            Event.PROCESSING_FAILED,
            level=logging.ERROR,
            source_id=settings.source.id,
            run_id=run_id,
            error_code=code,
            stage=exc.stage if isinstance(exc, IngestError) else "storage",
            response_id=exc.response_id if isinstance(exc, IngestError) else None,
            document_id=exc.document_id if isinstance(exc, IngestError) else None,
        )
        detail = str(exc) if not isinstance(exc, SQLAlchemyError) else code
        print(f"采集失败: {detail}；未报告成功，请检查运行证据与本地存储", file=sys.stderr)
        return 1
    except Exception:
        # Fatal CLI boundary: no arbitrary exception text or claim of saved recovery.
        log_event(
            logger,
            Event.PROCESSING_FAILED,
            level=logging.ERROR,
            source_id=settings.source.id,
            run_id=run_id,
            error_code="unexpected_error",
            stage="internal",
        )
        print("内部采集错误: 未报告成功，也未确认恢复状态已保存；请修复后重试", file=sys.stderr)
        return 1
    print(summary.model_dump_json())
    return 0 if summary.result == "succeeded" else 1


def _offline_command(args, settings, logger, run_id) -> int:
    # Lazy imports keep help/config-check independent of storage operations.
    from pydantic import ValidationError
    from sqlalchemy.exc import SQLAlchemyError

    from signalnest.ingestion import IngestError, ResponseInput, import_page, process_response
    from signalnest.instance_lock import WriterLockError, writer_lock
    from signalnest.rawstore import RawStore, RawStoreError
    from signalnest.storage import StorageError, open_initialized_engine

    evidence = None
    content = None
    if args.command == "import-page":
        try:
            evidence = ResponseInput.model_validate_json(args.metadata.read_bytes())
        except OSError:
            print("导入元数据错误: 无法读取 --metadata 文件", file=sys.stderr)
            return 2
        except ValidationError as exc:
            # Locations only; Pydantic messages/input values may contain untrusted data.
            fields = ", ".join(
                ".".join(str(part) for part in error["loc"]) or "metadata"
                for error in exc.errors(include_input=False)
                if error["loc"] and error["loc"][0] in ResponseInput.model_fields
            )
            print(f"导入元数据错误: 检查 JSON 格式及必要字段 ({fields})", file=sys.stderr)
            return 2
        if evidence.source_id != settings.source.id:
            print("导入元数据错误: source_id 必须与配置一致", file=sys.stderr)
            return 2
        if args.file is not None and (
            evidence.status_code == 304 or evidence.body_state == "unavailable"
        ):
            print("导入参数错误: 304 或不可用正文必须省略 --file", file=sys.stderr)
            return 2
        if args.file is not None:
            try:
                content = args.file.read_bytes()
            except OSError:
                print("导入文件错误: 无法读取 --file 文件", file=sys.stderr)
                return 1
    engine = None
    try:
        from signalnest.sources import source_binding

        list_parser, notice_parser, parser_version = source_binding(
            settings.source.parser
        ).parsers()
        if not settings.storage.database.is_file():
            raise StorageError("数据库不可用或未初始化，请先执行 storage-init")
        with writer_lock(settings.storage.database):
            engine = open_initialized_engine(settings.storage.database)
            raw_store = RawStore(settings.storage.data_dir)
            processed_at = args.processed_at if args.processed_at is not None else int(time.time())
            if evidence is not None:
                result = import_page(
                    engine,
                    raw_store,
                    evidence,
                    content,
                    processed_at,
                    run_id=run_id,
                    list_parser=list_parser,
                    notice_parser=notice_parser,
                    list_parser_version=parser_version,
                )
            else:
                result = process_response(
                    engine,
                    raw_store,
                    args.response_id,
                    processed_at,
                    expected_source_id=settings.source.id,
                    run_id=run_id,
                    processing_origin="maintenance",
                    list_parser=list_parser,
                    notice_parser=notice_parser,
                    list_parser_version=parser_version,
                )
    except (StorageError, IngestError, RawStoreError, WriterLockError, SQLAlchemyError) as exc:
        code = (
            exc.code
            if isinstance(exc, (IngestError, RawStoreError, WriterLockError))
            else "database_unavailable"
        )
        stage = exc.stage if isinstance(exc, IngestError) else "storage"
        log_event(
            logger,
            Event.PROCESSING_FAILED,
            level=logging.ERROR,
            source_id=settings.source.id,
            run_id=run_id,
            error_code=code,
            stage=stage,
            response_id=exc.response_id if isinstance(exc, IngestError) else None,
            document_id=exc.document_id if isinstance(exc, IngestError) else None,
        )
        # Never print SQLAlchemy's statements/parameters or arbitrary exception messages.
        detail = (
            str(exc)
            if isinstance(exc, (StorageError, IngestError, RawStoreError, WriterLockError))
            else code
        )
        print(f"离线处理失败: {detail}；未报告成功，请检查原文、元数据或本地存储", file=sys.stderr)
        return 1
    except Exception:
        # Last CLI boundary only: business functions still propagate programming defects.
        # Do not expose arbitrary exception values, or claim failure recovery was saved.
        log_event(
            logger,
            Event.PROCESSING_FAILED,
            level=logging.ERROR,
            source_id=settings.source.id,
            run_id=run_id,
            error_code="unexpected_error",
            stage="internal",
        )
        print(
            "内部处理错误: 未报告成功，也未确认失败状态已保存；请检查响应证据并修复程序后重试",
            file=sys.stderr,
        )
        return 1
    finally:
        if engine is not None:
            engine.dispose()
    from dataclasses import asdict

    payload = asdict(result)
    if result.pagination is not None:
        payload["pagination"] = result.pagination.model_dump(mode="json")
    print(json.dumps(payload, ensure_ascii=False))
    return 0
