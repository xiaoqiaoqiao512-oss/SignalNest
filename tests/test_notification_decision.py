"""Visible rule behavior, with explicit fixed clocks and no storage or mail."""

import hashlib
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from signalnest.contracts import ImageReference, NoticeContent
from signalnest.notifications.contracts import (
    Action,
    EventContext,
    Evidence,
    NoticeFacts,
    Profile,
    QualificationConstraint,
    TopicMatch,
)
from signalnest.notifications.decision import compose_route, decide, policy_manifest, policy_sha256
from signalnest.notifications.facts import extract_facts

SHANGHAI = ZoneInfo("Asia/Shanghai")
NOW = datetime(2026, 10, 5, 12, tzinfo=SHANGHAI)


def profile(**changes):
    values = {
        "institution": "whu",
        "study_level": "undergraduate",
        "interest_topics": ("exchange",),
    }
    values.update(changes)
    return Profile(**values)


def evidence(text="交流报名", *, field="title"):
    return Evidence(field=field, start=0, end=len(text), excerpt=text)


def facts(**changes):
    text = "武汉大学本科生可报名。"
    values = {
        "content_sha256": hashlib.sha256(text.encode()).hexdigest(),
        "title": "交流报名",
        "published_date": date(2026, 10, 5),
        "body_text": text,
        "topic_matches": (TopicMatch(topic="exchange", evidence=evidence(), primary=True),),
        "category": "opportunity",
        "constraints": (
            QualificationConstraint(
                field="institution",
                values=("whu",),
                evidence=evidence("武汉大学", field="body_text"),
            ),
            QualificationConstraint(
                field="study_level",
                values=("undergraduate",),
                evidence=evidence("本科生", field="body_text"),
            ),
        ),
        "audience_declared": True,
        "eligibility_complete": True,
        "opens_at": NOW - timedelta(days=1),
        "deadline_at": NOW + timedelta(days=2),
        "opening_confirmed": True,
    }
    values.update(changes)
    return NoticeFacts(**values)


def context(**changes):
    values = {"next_digest_at": NOW + timedelta(hours=20)}
    values.update(changes)
    return EventContext(**values)


def evaluate(item=None, user=None, event=None, *, now=NOW):
    return decide(user or profile(), item or facts(), event or context(), now=now)


def test_same_notice_has_reasonable_profile_differences():
    eligible = evaluate()
    unrelated = evaluate(user=profile(interest_topics=("scholarship",)))
    mismatch = evaluate(user=profile(study_level="master"))
    unknown = evaluate(user=profile(study_level=None))
    assert (eligible.action, eligible.eligibility, eligible.needs_review) == (
        Action.PUSH_NOW,
        "eligible",
        False,
    )
    assert unrelated.action == Action.IGNORE
    assert mismatch.action == Action.IGNORE
    assert mismatch.eligibility == "ineligible"
    assert unknown.action == Action.PUSH_NOW
    assert unknown.eligibility == "unknown"
    assert unknown.needs_review
    assert any("不证明" in reason for reason in unknown.reasons)
    assert unknown.constraint_results[1].result == "unknown"


def test_policy_snapshot_contains_exact_hash_inputs_without_mutable_global_state():
    from signalnest.notifications.contracts import canonical_sha256

    user = profile()
    snapshot = policy_manifest(user)
    assert canonical_sha256(snapshot) == policy_sha256(user)
    assert snapshot["decision_rules"]["deadline_hours"] == 72
    assert snapshot["routing_rules"]["push_now_hybrid"] == "immediate"
    snapshot["facts_rules"]["topic_phrases"]["exchange"].append("改写")
    snapshot["routing_rules"]["push_now_hybrid"] = "none"
    assert canonical_sha256(snapshot) != policy_sha256(user)


def test_recent_date_alone_cannot_push():
    assert evaluate(facts(topic_matches=())).action == Action.IGNORE
    ordinary = evaluate(facts(deadline_at=NOW + timedelta(days=20)))
    assert ordinary.action == Action.DIGEST
    assert ordinary.reason_codes == ("relevant_digest",)


def test_old_published_short_deadline_opportunity_is_urgent():
    result = evaluate(facts(published_date=date(2025, 1, 1)))
    assert result.action == Action.PUSH_NOW
    assert result.reason_codes == ("deadline_soon",)
    assert result.time_status == "open"


@pytest.mark.parametrize(
    ("remaining", "action"),
    [
        (timedelta(0), Action.PUSH_NOW),
        (timedelta(hours=72), Action.PUSH_NOW),
        (timedelta(hours=72, seconds=1), Action.DIGEST),
        (timedelta(seconds=-1), Action.IGNORE),
    ],
)
def test_deadline_boundaries(remaining, action):
    assert evaluate(facts(deadline_at=NOW + remaining)).action == action


def test_known_future_opening_never_pushes():
    result = evaluate(facts(opens_at=NOW + timedelta(days=1)))
    assert result.action == Action.DIGEST
    assert result.time_status == "not_started"


def test_unknown_opening_is_not_assumed_open_or_urgent():
    result = evaluate(facts(opens_at=None, opening_confirmed=False))
    assert result.action == Action.DIGEST
    assert result.time_status == "unknown"
    assert result.needs_review
    assert "opening_unknown" in {item.code for item in result.unknowns}


@pytest.mark.parametrize("offset", [0, 6])
def test_high_value_new_uses_seven_shanghai_calendar_days(offset):
    item = facts(published_date=NOW.date() - timedelta(days=offset), deadline_at=None)
    result = evaluate(item, profile(high_value_topics=("exchange",)))
    assert result.action == Action.PUSH_NOW
    assert result.needs_review
    assert result.reason_codes == ("high_value_topic",)


def test_eighth_calendar_day_high_value_is_not_recent():
    item = facts(published_date=NOW.date() - timedelta(days=7), deadline_at=None)
    assert evaluate(item, profile(high_value_topics=("exchange",))).action == Action.DIGEST


def test_future_publication_goes_to_review_without_false_urgency():
    result = evaluate(facts(published_date=NOW.date() + timedelta(days=1)))
    assert result.action == Action.DIGEST
    assert result.needs_review
    assert result.reason_codes == ("future_publication",)


def test_reliable_exclusion_precedes_followed_update_and_urgency():
    result = evaluate(
        facts(),
        profile(exclude_topics=("exchange",)),
        context(kind="update", previous_facts=facts(), previous_effective_route="digest"),
    )
    assert result.action == Action.IGNORE
    assert result.effective_route == "none"
    assert result.reason_codes == ("excluded_topic",)


def test_body_only_excluded_topic_does_not_veto_primary_interest():
    item = facts(
        topic_matches=(
            TopicMatch(topic="exchange", evidence=evidence(), primary=True),
            TopicMatch(topic="scholarship", evidence=evidence("奖学金", field="body_text")),
        )
    )
    assert evaluate(item, profile(exclude_topics=("scholarship",))).action == Action.PUSH_NOW


def test_literal_interest_is_literal_and_returns_original_offsets():
    item = facts(topic_matches=(), body_text="学生报名参加科学创新活动。")
    result = evaluate(item, profile(interest_topics=(), include_phrases=("科学创新",)))
    assert result.action == Action.PUSH_NOW
    assert result.relevance == "matched"
    match = next(e for e in result.evidence if e.excerpt == "科学创新")
    assert item.body_text[match.start : match.end] == match.excerpt
    no_match = evaluate(item, profile(interest_topics=(), include_phrases=("科学.*活动",)))
    assert no_match.action == Action.IGNORE


def test_reference_and_store_policy_never_send():
    reference = evaluate(facts(category="reference"))
    retained = evaluate(user=profile(store_only_topics=("exchange",)))
    assert reference.action == Action.STORE_ONLY
    assert retained.action == Action.STORE_ONLY
    assert reference.effective_route == retained.effective_route == "none"


def test_explicit_store_topic_can_retain_without_interest_match():
    result = evaluate(
        user=profile(interest_topics=("scholarship",), store_only_topics=("exchange",))
    )
    assert result.action == Action.STORE_ONLY
    assert result.relevance == "matched"


def test_information_does_not_claim_unnecessary_application_eligibility():
    result = evaluate(facts(category="information", constraints=()))
    assert result.action == Action.DIGEST
    assert result.eligibility == "not_required"
    assert result.time_status == "not_required"


@pytest.mark.parametrize("field", ["college", "major", "grade", "language", "gpa", "other"])
def test_missing_or_unsupported_qualification_is_unknown(field):
    item = facts(
        constraints=(
            QualificationConstraint(field=field, values=("required",), evidence=evidence()),
        )
    )
    result = evaluate(item, profile(entry_year=2024))
    assert result.eligibility == "unknown"
    assert result.constraint_results[0].actual is None
    assert result.constraint_results[0].result == "unknown"
    assert result.needs_review


def test_one_reliable_mismatch_wins_over_unresolved_and_condition():
    item = facts(
        constraints=(
            QualificationConstraint(field="study_level", values=("master",), evidence=evidence()),
            QualificationConstraint(field="language", values=("CET6",), evidence=evidence()),
        )
    )
    result = evaluate(item)
    assert result.eligibility == "ineligible"
    assert result.action == Action.IGNORE


def test_absent_audience_never_means_everyone():
    result = evaluate(facts(constraints=(), audience_declared=False, eligibility_complete=False))
    assert result.eligibility == "unknown"
    assert result.needs_review
    assert {item.code for item in result.unknowns} >= {"audience_unknown", "eligibility_incomplete"}


def test_media_with_no_positive_interest_retained_as_unknown():
    item = facts(
        body_text="",
        topic_matches=(),
        information_incomplete=True,
        media=(Evidence(field="images", index=0, url="https://uc.whu.edu.cn/notice.png"),),
    )
    result = evaluate(item)
    assert result.action == Action.STORE_ONLY
    assert result.relevance == "unknown"
    assert result.needs_review
    assert result.reason_codes == ("media_interest_unknown",)


def test_critical_media_does_not_default_to_eligible():
    item = facts(information_incomplete=True, eligibility_complete=False)
    assert evaluate(item).eligibility == "unknown"


def test_historical_context_cannot_send_even_currently_urgent():
    result = evaluate(event=context(kind="historical"))
    assert result.action == Action.IGNORE
    assert result.effective_route == "none"


@pytest.mark.parametrize("kind", ["historical", "activation_recent"])
def test_expired_related_history_is_retained_without_mail(kind):
    result = evaluate(facts(deadline_at=NOW - timedelta(days=1)), event=context(kind=kind))
    assert result.action == Action.STORE_ONLY
    assert result.effective_route == "none"


def test_reliable_exclusion_still_wins_over_historical_retention():
    result = evaluate(
        facts(deadline_at=NOW - timedelta(days=1)),
        profile(exclude_topics=("exchange",)),
        context(kind="historical"),
    )
    assert result.action == Action.IGNORE
    assert result.reason_codes == ("excluded_topic",)


def test_activation_recent_retains_action_but_defaults_route_to_digest():
    result = evaluate(event=context(kind="activation_recent"))
    assert result.action == Action.PUSH_NOW
    assert result.effective_route == "digest"
    assert result.routing_reason_codes == ("activation_recent_digest",)


def test_activation_deadline_before_digest_keeps_urgent_route():
    result = evaluate(
        facts(deadline_at=NOW + timedelta(hours=5)), event=context(kind="activation_recent")
    )
    assert result.action == Action.PUSH_NOW
    assert result.effective_route == "immediate"
    assert result.routing_reason_codes == ("activation_urgency_exception",)


def test_explicit_digest_only_always_wins_activation_urgency_exception():
    result = evaluate(
        facts(deadline_at=NOW + timedelta(hours=1)),
        event=context(kind="activation_recent", notification_mode="digest_only"),
    )
    assert result.action == Action.PUSH_NOW
    assert result.effective_route == "digest"
    assert result.routing_reason_codes == ("digest_only_mode",)


def test_digest_only_changes_route_without_claiming_lower_priority():
    result = evaluate(event=context(notification_mode="digest_only"))
    assert result.action == Action.PUSH_NOW
    assert result.effective_route == "digest"


def test_activation_date_conflict_is_reviewable():
    result = evaluate(event=context(kind="activation_recent", activation_date_conflict=True))
    assert result.needs_review
    assert "activation_date_conflict" in {item.code for item in result.unknowns}


def test_followed_changed_deadline_to_past_is_not_silently_ignored():
    item = facts(deadline_at=NOW - timedelta(hours=2))
    result = evaluate(
        item,
        event=context(kind="update", previous_facts=facts(), previous_effective_route="digest"),
    )
    assert result.action == Action.DIGEST
    assert result.time_status == "closed"
    assert result.reason_codes == ("conditions_changed",)


def test_followed_tightened_qualification_is_not_silently_ignored():
    item = facts(
        constraints=(
            QualificationConstraint(field="study_level", values=("master",), evidence=evidence()),
        )
    )
    result = evaluate(
        item,
        event=context(kind="update", previous_facts=facts(), previous_effective_route="digest"),
    )
    assert result.action == Action.DIGEST
    assert result.eligibility == "ineligible"
    assert result.reason_codes == ("conditions_changed",)


def test_previous_action_does_not_replace_registered_route():
    result = evaluate(
        facts(deadline_at=NOW - timedelta(hours=2)),
        event=context(kind="update", previous_facts=facts(), previous_action=Action.PUSH_NOW),
    )
    assert result.action == Action.IGNORE


@pytest.mark.parametrize("has_prior", [False, True])
def test_followed_unreliable_comparison_is_explicit_review(has_prior):
    result = evaluate(
        facts(deadline_at=NOW - timedelta(hours=2)),
        event=context(
            kind="update",
            previous_facts=facts() if has_prior else None,
            previous_effective_route="digest",
            comparison_known=False,
        ),
    )
    assert result.action == Action.DIGEST
    assert result.needs_review
    assert result.reason_codes == ("update_comparison_unknown",)


def test_generic_update_with_unchanged_conditions_does_not_invent_urgency():
    item = facts(body_text="武汉大学本科生可报名。说明已修订。")
    result = evaluate(
        item,
        event=context(kind="update", previous_facts=facts(), previous_effective_route="immediate"),
    )
    assert result.action == Action.DIGEST
    assert result.reason_codes == ("relevant_update",)


def test_followed_changed_trusted_deadline_can_still_be_urgent():
    previous = facts(deadline_at=NOW + timedelta(days=30))
    result = evaluate(
        event=context(kind="update", previous_facts=previous, previous_effective_route="digest")
    )
    assert result.action == Action.PUSH_NOW
    assert result.reason_codes == ("conditions_changed",)


def test_raw_programmatic_cancellation_is_not_ignored_for_followed_notice():
    # The extractor deliberately does not enable cancellation from unsupported phrases.
    result = evaluate(
        facts(cancelled=True),
        event=context(kind="update", previous_facts=facts(), previous_effective_route="digest"),
    )
    assert result.action == Action.DIGEST
    assert result.time_status == "closed"


def test_identical_inputs_and_clock_produce_identical_complete_audit():
    first = evaluate()
    assert evaluate() == first
    assert evaluate(now=NOW.astimezone(UTC)) == first
    assert first.profile_sha256 == profile().sha256()
    assert first.facts_sha256 == facts().sha256()
    assert first.policy_sha256 == policy_sha256(profile())
    assert first.evaluated_at.tzinfo == UTC
    assert first.matched_rules == (
        "interest.topic.exchange",
        "qualification.institution.match",
        "qualification.study_level.match",
        "decision.deadline_soon",
        "route.hybrid_immediate",
    )
    assert first.decision_engine_version == "notification-decision-v10"


def test_audit_input_changes_with_profile_context_or_explicit_time():
    baseline = evaluate()
    for result in (
        evaluate(user=profile(study_level=None)),
        evaluate(event=context(notification_mode="digest_only")),
        evaluate(now=NOW + timedelta(minutes=1)),
    ):
        assert result.input_sha256 != baseline.input_sha256
    assert evaluate(now=NOW + timedelta(minutes=1)).policy_sha256 == baseline.policy_sha256


def test_unknown_constraint_does_not_claim_matched_qualification_rule():
    result = evaluate(user=profile(study_level=None))
    assert "qualification.study_level.unknown" in result.matched_rules
    assert "qualification.study_level.match" not in result.matched_rules


def test_body_topic_does_not_claim_matched_primary_exclusion_rule():
    item = facts(topic_matches=(TopicMatch(topic="exchange", evidence=evidence(), primary=False),))
    result = evaluate(item, profile(exclude_topics=("exchange",)))
    assert "interest.topic.exchange" in result.matched_rules
    assert "exclusion.topic.exchange" not in result.matched_rules


@pytest.mark.parametrize("target", ["facts", "previous_facts"])
def test_decision_rejects_mixed_or_unsupported_facts_version(target):
    unsupported = facts(extractor_version="unknown-extractor-v5")
    with pytest.raises(ValueError, match="current extractor_version"):
        if target == "facts":
            evaluate(unsupported)
        else:
            evaluate(event=context(kind="update", previous_facts=unsupported))


def test_decide_and_route_reject_naive_clock():
    with pytest.raises(ValueError, match="UTC offset"):
        evaluate(now=NOW.replace(tzinfo=None))
    with pytest.raises(ValueError, match="UTC offset"):
        compose_route(Action.DIGEST, context(), now=NOW.replace(tzinfo=None))


@pytest.mark.parametrize("digest_at", [NOW, NOW - timedelta(seconds=1)])
def test_decide_and_route_reject_past_next_digest(digest_at):
    event = context(next_digest_at=digest_at)
    with pytest.raises(ValueError, match="next_digest_at"):
        evaluate(event=event)
    with pytest.raises(ValueError, match="next_digest_at"):
        compose_route(Action.PUSH_NOW, event, now=NOW)


def test_route_rejects_unknown_action_status_and_naive_deadline():
    with pytest.raises(ValueError, match="Action"):
        compose_route("INVALID", context(), now=NOW)
    with pytest.raises(ValueError, match="time_status"):
        compose_route(Action.PUSH_NOW, context(), now=NOW, time_status="maybe")
    with pytest.raises(ValueError, match="UTC offset"):
        compose_route(
            Action.PUSH_NOW,
            context(),
            now=NOW,
            deadline_at=NOW.replace(tzinfo=None),
            time_status="open",
        )


def test_actual_extractor_and_decision_form_local_pipeline():
    content = NoticeContent(
        title="赴海外交流报名通知",
        published_date=date(2026, 9, 1),
        body_html="<p>武汉大学本科生可报名。</p>",
        body_text="武汉大学本科生可报名；报名时间：2026年10月1日至2026年10月6日。",
    )
    result = evaluate(extract_facts(content))
    assert result.action == Action.PUSH_NOW
    assert result.eligibility == "eligible"
    assert result.time_status == "open"
    assert result.content_sha256 == content.content_sha256()


def test_media_pipeline_never_claims_known_no_interest_or_eligibility():
    content = NoticeContent(
        title="事项通知",
        published_date=date(2026, 10, 5),
        body_html='<img src="https://uc.whu.edu.cn/a.png">',
        body_text="",
        images=(ImageReference(url="https://uc.whu.edu.cn/a.png"),),
    )
    result = evaluate(extract_facts(content))
    assert result.action == Action.STORE_ONLY
    assert result.relevance == "unknown"
    assert result.needs_review
