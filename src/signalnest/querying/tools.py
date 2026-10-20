"""Fixed, read-only tools over existing independent campus instances."""

import json
from datetime import date, timedelta

from pydantic import ValidationError

from signalnest.config import Settings
from signalnest.model_client import ModelBudget
from signalnest.notifications.contracts import EventContext, Profile
from signalnest.notifications.decision import decide
from signalnest.notifications.facts import extract_facts
from signalnest.querying.config import QuerySettings
from signalnest.querying.contracts import (
    AnswerArgs,
    EmptyArgs,
    QueryError,
    Question,
    ReadArgs,
    SearchArgs,
    ToolTrace,
)
from signalnest.querying.dates import publication_window
from signalnest.querying.evidence import DeadlineEvidence
from signalnest.querying.snapshots import validate_snapshot
from signalnest.search import SearchError, SearchQuery, get_notice, search_notices
from signalnest.status import inspect_status
from signalnest.storage import StorageError, open_initialized_engine

# JSON schemas constrain model suggestions; Pydantic validates them again locally.
TOOL_INPUTS = {
    "search": (SearchArgs, "字面关键词查询所有实例或指定实例；发布日期范围由程序固定。"),
    "read_notice": (ReadArgs, "读取通知纯文本分块和有限规则事实；offset 为字符偏移。"),
    "read_profile": (EmptyArgs, "读取操作者配置的查询画像，不证明官方资格。"),
    "source_status": (EmptyArgs, "读取覆盖年龄、积压及来源诊断；不采集或修复。"),
    "answer": (AnswerArgs, "结束查询。只选择 read_notice 返回的 reference 和实际读到的原文引句。"),
}


def tool_definitions() -> list[dict]:
    return [
        {
            "type": "function",
            "function": {
                "name": name,
                "description": description,
                "parameters": model.model_json_schema(),
            },
        }
        for name, (model, description) in TOOL_INPUTS.items()
    ]


class QueryTools:
    """One question's snapshots and delivered evidence. No transactions span model calls."""

    def __init__(
        self,
        settings: QuerySettings,
        instances: dict[str, Settings],
        profile: Profile | None,
        question: Question,
        budget: ModelBudget,
    ):
        self.settings = settings
        self.instances = instances
        self.profile = profile
        self.question = question
        self.budget = budget
        self.window = publication_window(question.text, question.at, settings.timezone)
        self.trace: list[ToolTrace] = []
        self.chars = 0
        self.search_results: dict[tuple, dict] = {}
        self.retrieval_scope: list[dict] = []
        self.statuses: dict[str, dict] = {}
        self.retrieval_errors: set[str] = set()
        self._engines = {}
        self._provenance: dict[str, dict] = {}
        self._snapshots: dict[tuple, dict] = {}
        self._visible: dict[str, list[tuple[str, str]]] = {}
        self._finished = False

    def close(self):
        for engine in self._engines.values():
            engine.dispose()

    def _engine(self, instance_id):
        if instance_id not in self.instances:
            raise QueryError("unknown_instance")
        if instance_id not in self._engines:
            self._origin(instance_id)
            self._engines[instance_id] = open_initialized_engine(
                self.instances[instance_id].storage.database, read_only=True
            )
        return self._engines[instance_id]

    def _origin(self, instance_id):
        if instance_id not in self._provenance:
            config = next(item for item in self.settings.instances if item.id == instance_id)
            if config.snapshot_manifest is None:
                self._provenance[instance_id] = {"kind": "configured_database"}
            else:
                snapshot = validate_snapshot(
                    config.snapshot_manifest,
                    self.instances[instance_id].storage.database,
                    instance_id,
                    self.instances[instance_id].source.id,
                )
                self._provenance[instance_id] = {
                    "kind": "frozen_snapshot",
                    **snapshot.model_dump(mode="json"),
                    "live_database": False,
                }
        return self._provenance[instance_id]

    def call(self, name: str, arguments: str) -> dict:
        """Expected failures become finite tool results; programming defects propagate."""
        self.budget.remaining()
        if self._finished:
            raise QueryError("query_already_finished")
        if len(self.trace) >= self.settings.limits.max_tool_calls:
            raise QueryError("tool_call_budget")
        args = None
        try:
            if name not in TOOL_INPUTS:
                raise QueryError("unknown_tool")
            if len(arguments) > 16_000:
                raise QueryError("invalid_tool_arguments")
            try:
                args = TOOL_INPUTS[name][0].model_validate_json(arguments)
            except ValidationError as exc:
                raise QueryError("invalid_tool_arguments") from exc
            result = self._execute(name, args)
            self.budget.remaining()
            length = len(json.dumps(result, ensure_ascii=False))
            if self.chars + length > self.settings.limits.max_tool_chars:
                raise QueryError("tool_character_budget")
            self.chars += length
            if name == "read_notice":
                self._remember_visible(result)
            if name == "answer":
                self._finished = True
            self.trace.append(ToolTrace(tool=name, ok=True, **self._trace_target(args)))
            return {"ok": True, "data": result}
        except (QueryError, SearchError, StorageError) as exc:
            code = "storage_unavailable" if isinstance(exc, StorageError) else exc.code
            self.trace.append(
                ToolTrace(
                    tool=name if name in TOOL_INPUTS else "unknown",
                    ok=False,
                    error_code=code,
                    **self._trace_target(args),
                )
            )
            return {"ok": False, "error_code": code}

    @staticmethod
    def _trace_target(args):
        if isinstance(args, ReadArgs):
            return {"instance_id": args.instance_id, "document_id": args.document_id}
        if isinstance(args, SearchArgs):
            return {"instance_id": args.instance_id}
        return {}

    def _execute(self, name, args):
        if name == "search":
            return self._search(args)
        if name == "read_notice":
            return self._read(args)
        if name == "read_profile":
            return {
                "provided": self.profile is not None,
                "profile": self.profile.model_dump(mode="json") if self.profile else None,
                "official_eligibility_verified": False,
            }
        if name == "source_status":
            return self._status()
        if name == "answer":
            return self._answer(args)
        raise AssertionError("tool name checked before dispatch")

    def _search(self, args: SearchArgs):
        try:
            query = SearchQuery(
                query=args.query,
                offset=args.offset,
                limit=self.settings.limits.results_per_source,
                date_from=date.fromisoformat(self.window["date_from"])
                if self.window["date_from"]
                else None,
                date_to=date.fromisoformat(self.window["date_to"])
                if self.window["date_to"]
                else None,
            )
        except ValidationError as exc:
            raise QueryError("invalid_tool_arguments") from exc
        ids = (args.instance_id,) if args.instance_id else tuple(self.instances)
        pages = []
        for identifier in ids:
            self.budget.remaining()
            if identifier not in self.instances:
                raise QueryError("unknown_instance")
            try:
                page = search_notices(
                    self._engine(identifier),
                    query.model_copy(update={"source_id": self.instances[identifier].source.id}),
                )
            except (StorageError, SearchError, QueryError) as exc:
                self.retrieval_scope.append(
                    {
                        "instance_id": identifier,
                        "query": args.query,
                        "offset": args.offset,
                        "error_code": "storage_unavailable"
                        if isinstance(exc, StorageError)
                        else exc.code,
                    }
                )
                self.retrieval_errors.add(
                    "storage_unavailable" if isinstance(exc, StorageError) else exc.code
                )
                pages.append(
                    {
                        "instance_id": identifier,
                        "error_code": "storage_unavailable"
                        if isinstance(exc, StorageError)
                        else exc.code,
                    }
                )
                continue
            self.retrieval_scope.append(
                {
                    "instance_id": identifier,
                    "query": args.query,
                    "offset": page.offset,
                    "total": page.total,
                    "returned": len(page.items),
                }
            )
            items = []
            for hit in page.items:
                item = {"instance_id": identifier, **hit.model_dump(mode="json")}
                self.search_results[(identifier, hit.document_id, hit.version_id)] = item
                items.append(item)
            pages.append(
                {
                    "instance_id": identifier,
                    "query": args.query,
                    "total": page.total,
                    "offset": page.offset,
                    "items": items,
                    "expanded_terms": page.expanded_terms,
                    "index_version": page.index_version,
                    "query_version": page.query_version,
                }
            )
        # Per-source rank retained; BM25 scores across independent databases are not comparable.
        return {"publication_window": self.window, "sources": pages}

    def _status(self):
        result = []
        for identifier, settings in self.instances.items():
            self.budget.remaining()
            try:
                origin = self._origin(identifier)
                status = inspect_status(settings, at=int(self.question.at.timestamp()))
                item = {
                    "instance_id": identifier,
                    "source_id": settings.source.id,
                    "observed_at": status.observed_at,
                    "source": status.source.model_dump(),
                    "documents": status.documents.model_dump(),
                    "first_processing": status.first_processing.model_dump(),
                    "rechecks": status.rechecks.model_dump(),
                    "unadapted_references": status.unadapted_references,
                    "diagnostic_codes": [d.code for d in status.diagnostics],
                    "data_origin": origin,
                }
            except (StorageError, QueryError) as exc:
                item = {
                    "instance_id": identifier,
                    "error_code": "storage_unavailable"
                    if isinstance(exc, StorageError)
                    else exc.code,
                }
            self.statuses[identifier] = item
            result.append(item)
        return result

    def _snapshot(self, args: ReadArgs):
        key = (args.instance_id, args.document_id)
        if key not in self._snapshots:
            if len(self._snapshots) >= self.settings.limits.max_read_notices:
                raise QueryError("notice_read_budget")
            detail = get_notice(self._engine(args.instance_id), args.document_id)
            if detail.source_id != self.instances[args.instance_id].source.id:
                raise QueryError("document_source_mismatch")
            day = detail.content.published_date.isoformat()
            if (
                self.window["date_from"]
                and not self.window["date_from"] <= day <= self.window["date_to"]
            ):
                raise QueryError("notice_outside_publication_window")
            if len(detail.content.body_text) > 2_000_000:
                raise QueryError("notice_too_large")
            facts = extract_facts(detail.content)
            preview = (
                decide(
                    self.profile,
                    facts,
                    EventContext(
                        kind="historical", next_digest_at=self.question.at + timedelta(days=1)
                    ),
                    now=self.question.at,
                )
                if self.profile
                else None
            )
            self._snapshots[key] = {
                "detail": detail,
                "facts": facts,
                "deadline_evidence": DeadlineEvidence(detail.content, facts),
                "preview": preview,
                "reference": f"{args.instance_id}:{args.document_id}:v{detail.version_id}",
            }
        return self._snapshots[key]

    def _read(self, args: ReadArgs):
        saved = self._snapshot(args)
        detail, facts, preview = saved["detail"], saved["facts"], saved["preview"]
        text = detail.content.body_text
        if args.offset > len(text):
            raise QueryError("invalid_body_offset")
        end = min(len(text), args.offset + self.settings.limits.notice_chunk_chars)
        self.search_results[(args.instance_id, detail.document_id, detail.version_id)] = {
            "instance_id": args.instance_id,
            "document_id": detail.document_id,
            "source_id": detail.source_id,
            "source_document_id": detail.source_document_id,
            "version_id": detail.version_id,
            "title": detail.content.title,
            "published_date": detail.content.published_date.isoformat(),
            "original_url": detail.original_url,
            "snippet": text[args.offset : end][:240],
        }
        unknowns = list(
            dict.fromkeys(u.code for u in (preview.unknowns if preview else facts.unknowns))
        )
        return {
            "reference": saved["reference"],
            "instance_id": args.instance_id,
            "document_id": detail.document_id,
            "source_id": detail.source_id,
            "source_document_id": detail.source_document_id,
            "version_id": detail.version_id,
            "content_sha256": detail.content_sha256,
            "parser_version": detail.parser_version,
            "title": detail.content.title,
            "published_date": detail.content.published_date.isoformat(),
            "original_url": detail.original_url,
            "body_text": text[args.offset : end],
            "body_offset": args.offset,
            "next_offset": end if end < len(text) else None,
            "total_body_chars": len(text),
            "processing_status": detail.status,
            "last_error_code": detail.last_error_code,
            "facts": {
                "extractor_version": facts.extractor_version,
                "category": facts.category,
                "opportunity_evidence": [
                    e.model_dump(mode="json") for e in facts.opportunity_evidence[:3]
                ],
                "time_status": preview.time_status if preview else "unknown",
                "eligibility": preview.eligibility if preview else "unknown",
                "official_eligibility_verified": False,
                "constraints": [r.model_dump(mode="json") for r in preview.constraint_results[:6]]
                if preview
                else [c.model_dump(mode="json") for c in facts.constraints[:6]],
                "deadline_at": facts.deadline_at.isoformat() if facts.deadline_at else None,
                "scoped_deadlines": [
                    item.model_dump(mode="json") for item in facts.scoped_deadlines
                ],
                "deadline_lower_at": facts.deadline_lower_at.isoformat()
                if facts.deadline_lower_at
                else None,
                "time_evidence": [e.model_dump(mode="json") for e in facts.time_evidence[:4]],
                "deadline_evidence": [dict(p) for p in saved["deadline_evidence"].proofs],
                "unknown_codes": unknowns[:12],
            },
            "images": [r.model_dump(mode="json") for r in detail.content.images[:8]],
            "attachments": [r.model_dump(mode="json") for r in detail.content.attachments[:8]],
            "media_contents_read": False,
        }

    def _remember_visible(self, result):
        quotes = self._visible.setdefault(result["reference"], [])
        quotes.extend([("title", result["title"]), ("body_text", result["body_text"])])
        constraints = result["facts"]["constraints"]
        evidence = [c.get("constraint", c)["evidence"] for c in constraints]
        evidence.extend(result["facts"]["time_evidence"])
        evidence.extend(result["facts"]["deadline_evidence"])
        evidence.extend(result["facts"]["opportunity_evidence"])
        quotes.extend(
            (e["field"], e["excerpt"]) for e in evidence if e["field"] in {"title", "body_text"}
        )

    def _answer(self, args: AnswerArgs):
        if len({n.reference for n in args.notices}) != len(args.notices):
            raise QueryError("duplicate_answer_reference")
        output = []
        snapshots = {s["reference"]: s for s in self._snapshots.values()}
        for notice in args.notices:
            if notice.reference not in self._visible:
                raise QueryError("citation_not_read")
            for quote in notice.relevance + notice.conditions + notice.deadline:
                if not any(
                    field == quote.field and quote.text in text
                    for field, text in self._visible[notice.reference]
                ):
                    raise QueryError("citation_quote_invalid")
            saved = snapshots[notice.reference]
            detail, facts, preview = saved["detail"], saved["facts"], saved["preview"]
            unknowns = list(
                dict.fromkeys(u.code for u in (preview.unknowns if preview else facts.unknowns))
            )
            unknowns.append("official_eligibility_not_verified")
            if facts.deadline_at is None:
                unknowns.append("deadline_not_resolved")
            if detail.content.images or detail.content.attachments:
                unknowns.append("media_contents_not_read")
            deadline_supported = bool(notice.deadline) and all(
                saved["deadline_evidence"].supports(q) for q in notice.deadline
            )
            if notice.deadline and not deadline_supported:
                unknowns.append("deadline_quote_scope_unknown")
            output.append(
                {
                    **notice.model_dump(mode="json"),
                    "instance_id": notice.reference.split(":")[0],
                    "document_id": detail.document_id,
                    "version_id": detail.version_id,
                    "source_id": detail.source_id,
                    "source_document_id": detail.source_document_id,
                    "title": detail.content.title,
                    "published_date": detail.content.published_date.isoformat(),
                    "original_url": detail.original_url,
                    "content_sha256": detail.content_sha256,
                    "processing_status": detail.status,
                    "last_error_code": detail.last_error_code,
                    "official_eligibility_verified": False,
                    "rule_eligibility": preview.eligibility if preview else "unknown",
                    "rule_mismatch_evidence": [
                        {
                            "field": result.constraint.field,
                            "evidence": result.constraint.evidence.model_dump(mode="json"),
                        }
                        for result in (preview.constraint_results if preview else ())
                        if result.result == "mismatch"
                    ][:4],
                    "rule_category": facts.category,
                    "rule_time_status": preview.time_status if preview else "unknown",
                    "parser_version": detail.parser_version,
                    "facts_extractor_version": facts.extractor_version,
                    "deadline_evidence_status": "rule_supported"
                    if deadline_supported
                    else "unknown",
                    "extracted_deadline_at": facts.deadline_at.isoformat()
                    if facts.deadline_at
                    else None,
                    "deadline_lower_at": facts.deadline_lower_at.isoformat()
                    if facts.deadline_lower_at
                    else None,
                    "unknown_codes": list(dict.fromkeys(unknowns)),
                }
            )
        return output
