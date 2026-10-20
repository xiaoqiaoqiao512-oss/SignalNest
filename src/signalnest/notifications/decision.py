"""Deterministic local rules; every evaluation receives its clock explicitly.

This module does not inspect environment configuration or open storage. A rule
decision describes the visible evidence, not official confirmation of eligibility.
"""

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from signalnest.notifications.contracts import (
    DECISION_ENGINE_VERSION,
    FACTS_EXTRACTOR_VERSION,
    ROUTING_VERSION,
    RULES_VERSION,
    Action,
    ConstraintResult,
    Decision,
    EventContext,
    Evidence,
    NoticeFacts,
    Profile,
    UnknownItem,
    aware_time,
    canonical_json,
    canonical_sha256,
)
from signalnest.notifications.facts import rule_manifest

_SHANGHAI = ZoneInfo("Asia/Shanghai")
_SUPPORTED_FIELDS = {"role", "institution", "study_level", "college", "major", "entry_year"}
_REASON_TEXT = {
    "excluded_topic": "通知标题可靠命中用户明确排除的主题。",
    "no_interest_match": "可见标题和正文未命中关注主题或字面词组。",
    "reference_only": "这条通知属于结果、结题或参考资料，保存供检索。",
    "store_only_topic": "命中用户选择仅保存的主题。",
    "audience_mismatch": "按可见正文可识别的必要条件，个人画像有明确不匹配。",
    "deadline_passed": "可信截止时间已经过去，不主动提醒这项机会。",
    "not_started": "已识别的报名开始时间尚未到，不将其视为已开放的紧急机会。",
    "deadline_soon": "相关机会已经开放，可信截止在 72 小时内。",
    "high_value_topic": "新通知处于最近 7 个上海自然日内，并命中用户高价值主题。",
    "relevant_digest": "通知命中关注规则，按普通汇总处理。",
    "relevant_update": "已确认发生内容更新，按普通汇总处理；不推断为紧急条件变化。",
    "conditions_changed": "之前已取得邮件资格的机会，其可识别条件发生变化。",
    "update_comparison_unknown": "无法可靠核对本次更新与先前内容，提醒核对而不声称条件已经改变。",
    "media_interest_unknown": "关键内容可能仅在未解析图片或附件中，相关性不足以确认，保留待核对。",
    "topic_relation_unknown": "可见主题与报名行动的关系不明确，保留候选待核对。",
    "historical_context": "显式历史上下文只供本地存档，不产生主动邮件路线。",
    "future_publication": "站点发布日期在评估日期之后，不能将其作为近期已发布机会。",
}


def policy_manifest(profile: Profile) -> dict:
    """Return a fresh JSON-ready policy snapshot for audit and future N1 storage."""
    return {
        "profile": profile.model_dump(mode="json"),
        "facts_rules": rule_manifest(),
        "decision_rules": {
            "deadline_hours": 72,
            "urgent_deadline_boundary": "lower bound, or now after entering its uncertain interval",
            "closed_deadline_boundary": "upper bound only; uncertainty is not confirmed expiration",
            "uncertain_deadline_requires_review": True,
            "group_deadlines": (
                "select explicit applicant study group; never use another group's extension"
            ),
            "recent_calendar_days": 7,
            "timezone": "Asia/Shanghai",
            "store_only_scope": (
                "only matching topics; another active interest or literal phrase wins"
            ),
            "active_interest": (
                "subject/opportunity topics; incidental mentions are evidence only; "
                "uncertain interests retained without mail eligibility"
            ),
            "urgent_topic": (
                "new urgency/high value requires a linked opportunity or explicit phrase"
            ),
            "uncertain_followed_update": (
                "preserve previously eligible update/comparison reminders without new urgency"
            ),
            "order": (
                "reliable_exclusion",
                "historical",
                "interest_or_reference",
                "reference_or_store_only",
                "followed_update",
                "closed_or_ineligible",
                "urgent_or_high_value",
                "ordinary_digest",
            ),
        },
        "routing_rules": {
            "store_only_or_ignore": "none",
            "digest": "digest",
            "push_now_digest_only": "digest",
            "push_now_hybrid": "immediate",
            "activation_recent_default": "digest",
            "activation_recent_exception": "open_deadline_at_or_before_next_digest",
            "digest_only_precedes_activation_exception": True,
            "opportunity_review_default": "digest",
            "opportunity_review_exception": "open_deadline_at_or_before_next_digest",
        },
        "versions": {
            "facts_extractor": FACTS_EXTRACTOR_VERSION,
            "rules": RULES_VERSION,
            "decision_engine": DECISION_ENGINE_VERSION,
            "routing": ROUTING_VERSION,
        },
    }


def policy_sha256(profile: Profile) -> str:
    """Hash the exact profile and fixed rule/version inputs, without secrets or time."""
    return canonical_sha256(policy_manifest(profile))


def _unique(values):
    """Preserve explanatory order while removing identical immutable contracts."""
    seen = set()
    output = []
    for value in values:
        key = canonical_json(value.model_dump(mode="json"))
        if key not in seen:
            seen.add(key)
            output.append(value)
    return tuple(output)


def _unknown(code: str, field: str, message: str, evidence=()) -> UnknownItem:
    return UnknownItem(code=code, field=field, message=message, evidence=tuple(evidence))


def _qualification(profile: Profile, facts: NoticeFacts):
    results = []
    unknowns = []
    for constraint in facts.constraints:
        actual = (
            getattr(profile, constraint.field) if constraint.field in _SUPPORTED_FIELDS else None
        )
        if (
            constraint.operator == "unsupported"
            or constraint.field not in _SUPPORTED_FIELDS
            or not constraint.values
            or actual is None
        ):
            result = "unknown"
            unknowns.append(
                _unknown(
                    "qualification_unknown",
                    constraint.field,
                    "画像缺少所需事实，或当前规则不支持此条件；不能默认符合。",
                    (constraint.evidence,),
                )
            )
        elif actual in constraint.values:
            result = "match"
        else:
            result = "mismatch"
        results.append(ConstraintResult(constraint=constraint, actual=actual, result=result))
    if facts.category != "opportunity" and not facts.constraints:
        return "not_required", tuple(results), tuple(unknowns)
    if any(r.result == "mismatch" for r in results):
        return "ineligible", tuple(results), tuple(unknowns)
    if not facts.audience_declared:
        unknowns.append(_unknown("audience_unknown", "audience", "可见正文未明确报名对象。"))
    if not facts.eligibility_complete or facts.information_incomplete:
        unknowns.append(
            _unknown("eligibility_incomplete", "eligibility", "可识别条件不足以确认全部报名资格。")
        )
    if unknowns or any(r.result == "unknown" for r in results):
        return "unknown", tuple(results), tuple(unknowns)
    return "eligible", tuple(results), ()


def deadline_for_profile(facts: NoticeFacts, profile: Profile) -> NoticeFacts:
    """Return a derived time view; callers retain the original extraction for hashes.

    Nationality is not in Profile. International extensions remain visible as
    unknown exceptions and are never borrowed for the ordinary student group.
    """
    if not facts.scoped_deadlines:
        return facts
    group = {"undergraduate": "undergraduate", "master": "graduate", "doctoral": "graduate"}.get(
        profile.study_level
    )
    selected = [item for item in facts.scoped_deadlines if item.applicant_group == group]
    if not selected and group is not None:
        selected = [item for item in facts.scoped_deadlines if item.applicant_group == "general"]
    values = {(item.deadline_at, item.deadline_lower_at) for item in selected}
    unknowns = list(facts.unknowns)
    if len(values) != 1:
        unknowns.append(
            _unknown(
                "deadline_group_unknown",
                "deadline",
                "未能唯一确定画像对应的报名组别和截止时间。",
                tuple(proof for item in facts.scoped_deadlines for proof in item.evidence),
            )
        )
        return facts.model_copy(
            update={"deadline_at": None, "deadline_lower_at": None, "unknowns": _unique(unknowns)}
        )
    deadline, lower = next(iter(values))
    if any(item.applicant_group == "international" for item in facts.scoped_deadlines):
        unknowns.append(
            _unknown(
                "international_deadline_exception",
                "deadline",
                "正文另有留学生延期；画像未声明该身份，此处仅采用已识别常规组期限，请核对适用范围。",
                tuple(
                    proof
                    for item in facts.scoped_deadlines
                    if item.applicant_group == "international"
                    for proof in item.evidence
                ),
            )
        )
    return facts.model_copy(
        update={
            "deadline_at": deadline,
            "deadline_lower_at": lower,
            "time_evidence": _unique(
                facts.time_evidence + tuple(p for s in selected for p in s.evidence)
            ),
            "unknowns": _unique(unknowns),
        }
    )


def _time(facts: NoticeFacts, now: datetime):
    if facts.category != "opportunity":
        return "not_required", ()
    if facts.cancelled:
        return "closed", ()
    if facts.deadline_at is not None and now > facts.deadline_at:
        later_exception = tuple(
            proof
            for item in facts.scoped_deadlines
            if item.applicant_group == "international" and item.deadline_at >= now
            for proof in item.evidence
        )
        if later_exception:
            return "unknown", (
                _unknown(
                    "deadline_exception_unresolved",
                    "deadline",
                    "常规组期限已过，但留学生延期可能仍有效；画像未明确此身份，不能确认仍开放或已关闭。",
                    later_exception,
                ),
            )
        return "closed", ()
    if facts.opens_at is not None and now < facts.opens_at:
        return "not_started", ()
    if facts.deadline_at is not None and (facts.opening_confirmed or facts.opens_at is not None):
        return "open", ()
    unknowns = []
    if facts.deadline_at is None:
        unknowns.append(_unknown("time_unknown", "deadline", "没有唯一可信的完整年份截止时间。"))
    if not facts.opening_confirmed and facts.opens_at is None:
        unknowns.append(_unknown("opening_unknown", "opening", "无法确认报名是否已经开放。"))
    return "unknown", tuple(unknowns)


def _urgent_deadline(facts: NoticeFacts, now: datetime) -> datetime | None:
    """Send before the earliest possible boundary without declaring early expiry.

    During a 日前 uncertainty interval, immediate review is safer than waiting
    for a digest after the possible cutoff. Closing still uses deadline_at.
    """
    if facts.deadline_lower_at is not None:
        return max(facts.deadline_lower_at, now)
    return facts.deadline_at


def _phrase_evidence(profile: Profile, facts: NoticeFacts):
    evidence = []
    for phrase in profile.include_phrases:
        for field, text in (("title", facts.title), ("body_text", facts.body_text)):
            start = text.find(phrase)
            if start >= 0:
                evidence.append(
                    Evidence(field=field, start=start, end=start + len(phrase), excerpt=phrase)
                )
    return tuple(evidence)


def _conditions(facts: NoticeFacts):
    # Evidence position and raw/content hashes are not conditions. This projection
    # does not prove that all other body changes are merely formatting.
    return {
        "constraints": sorted(
            (
                constraint.field,
                constraint.operator,
                canonical_json(list(constraint.values)),
            )
            for constraint in facts.constraints
        ),
        "opens_at": facts.opens_at,
        "deadline_at": facts.deadline_at,
        "deadline_lower_at": facts.deadline_lower_at,
        "cancelled": facts.cancelled,
    }


def compose_route(
    action: Action,
    context: EventContext,
    *,
    now: datetime,
    deadline_at: datetime | None = None,
    time_status: str = "unknown",
):
    """Freeze local route semantics before a later stage registers mail eligibility."""
    aware_time(now)
    if not isinstance(action, Action):
        raise ValueError("action must be a supported Action")
    if deadline_at is not None:
        aware_time(deadline_at)
    if time_status not in {"open", "closed", "not_started", "unknown", "not_required"}:
        raise ValueError("time_status must be a supported status")
    if context.next_digest_at <= now:
        raise ValueError("next_digest_at must be later than evaluated_at")
    if action in {Action.IGNORE, Action.STORE_ONLY}:
        return "none", ("no_mail_action",)
    if action == Action.DIGEST:
        return "digest", ("digest_action",)
    if context.notification_mode == "digest_only":
        return "digest", ("digest_only_mode",)
    if context.kind in {"activation_recent", "opportunity_review"}:
        prefix = "activation" if context.kind == "activation_recent" else "opportunity_review"
        if (
            time_status == "open"
            and deadline_at is not None
            and now <= deadline_at <= context.next_digest_at
        ):
            return "immediate", (prefix + "_urgency_exception",)
        return "digest", (
            "activation_recent_digest" if prefix == "activation" else "opportunity_review_digest",
        )
    return "immediate", ("hybrid_immediate",)


def decide(
    profile: Profile, facts: NoticeFacts, context: EventContext, *, now: datetime
) -> Decision:
    """Evaluate fixed transparent rules without mutating input or reading a clock."""
    evaluated_at = aware_time(now).astimezone(UTC)
    if facts.extractor_version != FACTS_EXTRACTOR_VERSION:
        raise ValueError("facts must use the current extractor_version; re-extract before deciding")
    if (
        context.previous_facts is not None
        and context.previous_facts.extractor_version != FACTS_EXTRACTOR_VERSION
    ):
        raise ValueError("previous_facts must use the current extractor_version; re-extract first")
    if context.next_digest_at <= evaluated_at:
        raise ValueError("next_digest_at must be later than evaluated_at")
    extracted_facts = facts
    facts = deadline_for_profile(facts, profile)
    eligibility, constraints, qualification_unknowns = _qualification(profile, facts)
    time_status, time_unknowns = _time(facts, evaluated_at)
    unknowns = list(facts.unknowns + qualification_unknowns + time_unknowns)
    if facts.deadline_lower_at is not None and not any(
        item.code == "imprecise_deadline" for item in unknowns
    ):
        unknowns.append(
            _unknown(
                "imprecise_deadline",
                "deadline",
                "截止仅有上下边界，精确时刻须核对。",
                facts.time_evidence,
            )
        )
    phrases = _phrase_evidence(profile, facts)
    topics = {
        match.topic
        for match in facts.topic_matches
        if match.context not in {"incidental", "uncertain"}
    }
    uncertain_topics = {
        match.topic
        for match in facts.topic_matches
        if match.context == "uncertain" and match.topic in profile.interest_topics
    }
    primary_topics = {match.topic for match in facts.topic_matches if match.primary}
    opportunity_topics = {
        match.topic for match in facts.topic_matches if match.context in {None, "opportunity"}
    }
    active_topics = topics & (set(profile.interest_topics) - set(profile.store_only_topics))
    matched = bool(active_topics or phrases)
    relevant_opportunity = bool(active_topics & opportunity_topics or phrases)
    stored = bool(topics & set(profile.store_only_topics))
    relevance = "matched" if matched or stored else "unmatched"
    uncertain_interest = (
        not matched
        and not stored
        and (
            uncertain_topics
            or facts.information_incomplete
            or (not facts.body_text.strip() and facts.media)
        )
    )
    if uncertain_interest:
        relevance = "unknown"
        if not uncertain_topics:
            unknowns.append(
                _unknown(
                    "interest_unknown", "interest", "未解析媒体可能包含关注信息。", facts.media
                )
            )
    future = facts.published_date > evaluated_at.astimezone(_SHANGHAI).date()
    if future:
        unknowns.append(
            _unknown("future_publication", "published_date", "发布日期晚于本次评估的上海日期。")
        )
    if context.activation_date_conflict:
        unknowns.append(
            _unknown("activation_date_conflict", "published_date", "首启列表与详情日期证据不一致。")
        )
    followed = context.kind == "update" and context.previous_effective_route != "none"
    uncertain_comparison = context.kind == "update" and (
        not context.comparison_known or context.previous_facts is None
    )
    if uncertain_comparison:
        unknowns.append(
            _unknown("comparison_unknown", "previous_content", "缺少可信的前后内容比较证据。")
        )
    condition_change = (
        context.kind == "update"
        and context.comparison_known
        and context.previous_facts is not None
        and _conditions(facts) != _conditions(deadline_for_profile(context.previous_facts, profile))
    )
    recent_days = (evaluated_at.astimezone(_SHANGHAI).date() - facts.published_date).days
    urgent_deadline = _urgent_deadline(facts, evaluated_at)
    short_deadline = (
        facts.category == "opportunity"
        and relevant_opportunity
        and time_status == "open"
        and urgent_deadline is not None
        and timedelta(0) <= urgent_deadline - evaluated_at <= timedelta(hours=72)
    )
    fresh_high_value = (
        context.kind == "new"
        and facts.category == "opportunity"
        and 0 <= recent_days <= 6
        and bool(opportunity_topics & active_topics & set(profile.high_value_topics))
    )

    # The primary title classification is the only evidence allowed to veto via
    # excluded_topics. A quoted topic appearing somewhere in the body is weaker.
    if primary_topics & set(profile.exclude_topics):
        action, code = Action.IGNORE, "excluded_topic"
    elif context.kind == "historical":
        action = (
            Action.STORE_ONLY
            if relevance == "matched" and (time_status == "closed" or facts.category == "reference")
            else Action.IGNORE
        )
        code = "historical_context"
    elif relevance == "unknown" and not (followed and uncertain_topics):
        action = Action.STORE_ONLY
        code = "topic_relation_unknown" if uncertain_topics else "media_interest_unknown"
    elif relevance == "unmatched" and not followed:
        action, code = Action.IGNORE, "no_interest_match"
    elif facts.category == "reference" and not followed:
        action, code = Action.STORE_ONLY, "reference_only"
    elif stored and not matched and not followed:
        action, code = Action.STORE_ONLY, "store_only_topic"
    elif followed and uncertain_comparison:
        action, code = Action.DIGEST, "update_comparison_unknown"
    elif followed and condition_change:
        action, code = Action.DIGEST, "conditions_changed"
        if short_deadline and eligibility != "ineligible" and not future:
            action = Action.PUSH_NOW
    elif eligibility == "ineligible":
        action, code = Action.IGNORE, "audience_mismatch"
    elif time_status == "closed":
        action = Action.STORE_ONLY if context.kind == "activation_recent" else Action.IGNORE
        code = "deadline_passed"
    elif future:
        action, code = Action.DIGEST, "future_publication"
    elif time_status == "not_started":
        action, code = Action.DIGEST, "not_started"
    elif (short_deadline and context.kind != "update") or fresh_high_value:
        action = Action.PUSH_NOW
        code = "deadline_soon" if short_deadline else "high_value_topic"
    else:
        action = Action.DIGEST
        code = "relevant_update" if context.kind == "update" else "relevant_digest"

    unique_unknowns = _unique(unknowns)
    reasons = [_REASON_TEXT[code]]
    if eligibility == "unknown":
        reasons.append("资格待核实：规则命中只证明相关性，不证明个人已符合官方报名资格。")
    if time_status == "unknown":
        reasons.append("时间待核对：站点发布日期不能代替报名开始或截止时间。")
    if facts.media:
        reasons.append("图片和附件仅保留引用，本次未读取其内容。")
    route, routing_codes = compose_route(
        action, context, now=evaluated_at, deadline_at=urgent_deadline, time_status=time_status
    )
    evidence = _unique(
        tuple(match.evidence for match in facts.topic_matches)
        + tuple(proof for match in facts.topic_matches for proof in match.supporting_evidence)
        + phrases
        + tuple(constraint.evidence for constraint in facts.constraints)
        + facts.opportunity_evidence
        + facts.time_evidence
        + facts.media
        + tuple(e for unknown in unique_unknowns for e in unknown.evidence)
    )
    policy_digest = policy_sha256(profile)
    matched_rules = (
        tuple(f"interest.topic.{topic}" for topic in sorted(topics & set(profile.interest_topics)))
        + (("interest.literal_phrase",) if phrases else ())
        + tuple(
            f"exclusion.topic.{topic}"
            for topic in sorted(primary_topics & set(profile.exclude_topics))
        )
        + tuple(
            f"retention.topic.{topic}" for topic in sorted(topics & set(profile.store_only_topics))
        )
        + tuple(
            f"qualification.{result.constraint.field}.{result.result}" for result in constraints
        )
        + (f"decision.{code}",)
        + tuple(f"route.{route_code}" for route_code in routing_codes)
    )
    return Decision(
        action=action,
        needs_review=bool(unique_unknowns),
        effective_route=route,
        routing_reason_codes=routing_codes,
        reason_codes=(code,),
        reasons=tuple(reasons),
        matched_rules=tuple(dict.fromkeys(matched_rules)),
        relevance=relevance,
        eligibility=eligibility,
        time_status=time_status,
        constraint_results=constraints,
        unknowns=unique_unknowns,
        evidence=evidence,
        evaluated_at=evaluated_at,
        profile_sha256=profile.sha256(),
        content_sha256=facts.content_sha256,
        facts_sha256=extracted_facts.sha256(),
        policy_sha256=policy_digest,
        input_sha256=canonical_sha256(
            {
                "profile": profile.model_dump(mode="json"),
                "facts": extracted_facts.model_dump(mode="json"),
                "context": context.model_dump(mode="json"),
                "evaluated_at": evaluated_at.isoformat(),
                "policy_sha256": policy_digest,
            }
        ),
        facts_extractor_version=facts.extractor_version,
    )
