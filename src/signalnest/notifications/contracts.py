"""Strict immutable inputs and evidence for local notification decisions."""

import hashlib
import json
from datetime import date, datetime
from enum import StrEnum
from typing import Literal

from pydantic import Field, field_validator, model_serializer, model_validator

from signalnest.contracts import Contract, Digest, NonemptyText, WebUrl

FACTS_EXTRACTOR_VERSION = "whu-notice-facts-v10"
RULES_VERSION = "notification-rules-v10"
DECISION_ENGINE_VERSION = "notification-decision-v10"
ROUTING_VERSION = "notification-routing-v1"

Topic = Literal[
    "exchange",
    "scholarship",
    "research",
    "competition",
    "course_enrollment",
    "minor",
    "recommendation",
    "teaching_assistant",
]
ProfileField = Literal[
    "role",
    "institution",
    "study_level",
    "college",
    "major",
    "entry_year",
    "grade",
    "gpa",
    "language",
    "student_status",
    "other",
]


def canonical_json(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def canonical_sha256(value) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def aware_time(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("time must include an explicit UTC offset")
    return value


class Action(StrEnum):
    PUSH_NOW = "PUSH_NOW"
    DIGEST = "DIGEST"
    STORE_ONLY = "STORE_ONLY"
    IGNORE = "IGNORE"


class Profile(Contract):
    """One explicit local profile. None means unknown, never an inferred campus fact."""

    schema_version: int = Field(default=1, ge=1, le=1, strict=True)
    profile_id: Literal["self"] = "self"
    institution: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9-]{0,79}$")
    study_level: Literal["undergraduate", "master", "doctoral", "faculty"] | None = None
    # An application role is independent of study level; omission remains unknown.
    role: Literal["student", "faculty"] | None = None
    college: str | None = Field(default=None, min_length=1, max_length=100)
    major: str | None = Field(default=None, min_length=1, max_length=100)
    entry_year: int | None = Field(default=None, ge=1900, le=2100, strict=True)
    interest_topics: tuple[Topic, ...] = ()
    include_phrases: tuple[str, ...] = Field(default=(), max_length=32)
    exclude_topics: tuple[Topic, ...] = ()
    high_value_topics: tuple[Topic, ...] = ()
    store_only_topics: tuple[Topic, ...] = ()

    @field_validator("college", "major")
    @classmethod
    def visible_text(cls, value):
        if value is not None and (
            value != value.strip() or any(ord(c) < 32 or ord(c) == 127 for c in value)
        ):
            raise ValueError("must be visible text without surrounding whitespace or controls")
        return value

    @field_validator("include_phrases")
    @classmethod
    def literal_phrases(cls, values):
        if any(
            not 1 <= len(v) <= 120 or v != v.strip() or any(ord(c) < 32 or ord(c) == 127 for c in v)
            for v in values
        ):
            raise ValueError(
                "phrases must be 1–120 visible characters without surrounding whitespace"
            )
        if len(set(values)) != len(values):
            raise ValueError("phrases must be unique")
        return tuple(sorted(values))

    @field_validator("interest_topics", "exclude_topics", "high_value_topics", "store_only_topics")
    @classmethod
    def unique_topics(cls, values):
        if len(set(values)) != len(values):
            raise ValueError("topics must be unique")
        return tuple(sorted(values))

    @model_validator(mode="after")
    def explicit_interests(self):
        if not self.interest_topics and not self.include_phrases:
            raise ValueError("at least one interest topic or literal phrase is required")
        if set(self.high_value_topics) & set(self.store_only_topics):
            raise ValueError("high-value and store-only topics must not overlap")
        return self

    def sha256(self) -> str:
        return canonical_sha256(self.model_dump(mode="json"))

    @model_serializer(mode="wrap")
    def compatible_snapshot(self, handler):
        snapshot = handler(self)
        # Persisted v1 profiles did not have this optional field. Preserve their
        # exact normalized JSON/hash when no v2 role fact has been supplied.
        if self.role is None:
            snapshot.pop("role", None)
        return snapshot


class Evidence(Contract):
    field: Literal["title", "body_text", "images", "attachments"]
    start: int | None = Field(default=None, ge=0, strict=True)
    end: int | None = Field(default=None, ge=0, strict=True)
    excerpt: str = Field(default="", max_length=120)
    index: int | None = Field(default=None, ge=0, strict=True)
    url: WebUrl | None = None

    @model_validator(mode="after")
    def positioned(self):
        if self.field in {"title", "body_text"}:
            if (
                self.start is None
                or self.end is None
                or self.end <= self.start
                or self.end - self.start != len(self.excerpt)
                or self.index is not None
                or self.url is not None
            ):
                raise ValueError("text evidence requires exact Unicode offsets and excerpt")
        elif (
            self.index is None or self.url is None or self.start is not None or self.end is not None
        ):
            raise ValueError("media evidence requires a reference index and URL, not text offsets")
        return self


class UnknownItem(Contract):
    code: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    field: NonemptyText
    message: str = Field(min_length=1, max_length=280)
    evidence: tuple[Evidence, ...] = ()


class TopicMatch(Contract):
    topic: Topic
    evidence: Evidence
    primary: bool = Field(default=False, strict=True)
    # None preserves old serialized facts. Current extraction always classifies
    # mentions; a title subject need not be a newly offered opportunity.
    context: Literal["subject", "opportunity", "incidental", "uncertain"] | None = None
    # Cross-field links retain the actual body instruction independently of
    # the title's Unicode offsets. Omission preserves all older fact hashes.
    supporting_evidence: tuple[Evidence, ...] = ()

    @model_serializer(mode="wrap")
    def compatible_snapshot(self, handler):
        snapshot = handler(self)
        if self.context is None:
            snapshot.pop("context", None)
        if not self.supporting_evidence:
            snapshot.pop("supporting_evidence", None)
        return snapshot


class QualificationConstraint(Contract):
    field: ProfileField
    values: tuple[str | int, ...] = ()
    operator: Literal["equals", "one_of", "unsupported"] = "one_of"
    evidence: Evidence


class ScopedDeadline(Contract):
    """A witnessed deadline for one applicant group, never a global latest date."""

    applicant_group: Literal["general", "undergraduate", "graduate", "international"]
    deadline_at: datetime
    deadline_lower_at: datetime | None = None
    evidence: tuple[Evidence, ...] = Field(min_length=1)

    @field_validator("deadline_at", "deadline_lower_at")
    @classmethod
    def explicit_time(cls, value):
        return aware_time(value) if value is not None else None

    @model_validator(mode="after")
    def ordered_bounds(self):
        if self.deadline_lower_at is not None and self.deadline_lower_at > self.deadline_at:
            raise ValueError("deadline bounds must be ordered")
        return self


class NoticeFacts(Contract):
    content_sha256: Digest
    extractor_version: NonemptyText = FACTS_EXTRACTOR_VERSION
    title: NonemptyText
    published_date: date
    # Literal profile phrases require the original visible text. Not logged or persisted by N0.
    body_text: str = Field(default="", repr=False)
    topic_matches: tuple[TopicMatch, ...] = ()
    category: Literal["opportunity", "information", "reference", "unknown"] = "unknown"
    opportunity_evidence: tuple[Evidence, ...] = ()
    constraints: tuple[QualificationConstraint, ...] = ()
    audience_declared: bool = Field(default=False, strict=True)
    eligibility_complete: bool = Field(default=False, strict=True)
    opens_at: datetime | None = None
    deadline_at: datetime | None = None
    # For 日前, deadline_at is only the conservative latest boundary; this lower
    # bound and an unknown item preserve the uncertainty instead of inventing precision.
    deadline_lower_at: datetime | None = None
    scoped_deadlines: tuple[ScopedDeadline, ...] = ()
    opening_confirmed: bool = Field(default=False, strict=True)
    time_evidence: tuple[Evidence, ...] = ()
    information_incomplete: bool = Field(default=False, strict=True)
    media: tuple[Evidence, ...] = ()
    unknowns: tuple[UnknownItem, ...] = ()
    # Cancellation extraction is disabled pending a trustworthy positive site sample.
    cancelled: bool = Field(default=False, strict=True)

    @field_validator("opens_at", "deadline_at", "deadline_lower_at")
    @classmethod
    def explicit_time(cls, value):
        return aware_time(value) if value is not None else None

    def sha256(self) -> str:
        return canonical_sha256(self.model_dump(mode="json"))

    @model_validator(mode="after")
    def deadline_bounds(self):
        if self.deadline_lower_at is not None and (
            self.deadline_at is None or self.deadline_lower_at > self.deadline_at
        ):
            raise ValueError("deadline_lower_at requires an ordered deadline_at boundary")
        return self

    @model_serializer(mode="wrap")
    def compatible_snapshot(self, handler):
        snapshot = handler(self)
        if self.deadline_lower_at is None:
            snapshot.pop("deadline_lower_at", None)
        if not self.scoped_deadlines:
            snapshot.pop("scoped_deadlines", None)
        return snapshot


class EventContext(Contract):
    """Preview claims only. N1 must later supply durable, verified event provenance."""

    kind: Literal["new", "update", "activation_recent", "historical", "opportunity_review"] = "new"
    notification_mode: Literal["hybrid", "digest_only"] = "hybrid"
    previous_facts: NoticeFacts | None = Field(default=None, repr=False)
    previous_action: Action | None = None
    previous_effective_route: Literal["none", "immediate", "digest"] = "none"
    comparison_known: bool = Field(default=True, strict=True)
    activation_date_conflict: bool = Field(default=False, strict=True)
    next_digest_at: datetime

    @field_validator("next_digest_at")
    @classmethod
    def explicit_time(cls, value):
        return aware_time(value)


class ConstraintResult(Contract):
    constraint: QualificationConstraint
    actual: str | int | None = None
    result: Literal["match", "mismatch", "unknown"]


class Decision(Contract):
    action: Action
    needs_review: bool
    effective_route: Literal["none", "immediate", "digest"]
    routing_reason_codes: tuple[str, ...]
    reason_codes: tuple[str, ...]
    reasons: tuple[str, ...]
    matched_rules: tuple[str, ...]
    relevance: Literal["matched", "unmatched", "unknown"]
    eligibility: Literal["eligible", "ineligible", "unknown", "not_required"]
    time_status: Literal["open", "closed", "not_started", "unknown", "not_required"]
    constraint_results: tuple[ConstraintResult, ...]
    unknowns: tuple[UnknownItem, ...]
    evidence: tuple[Evidence, ...]
    evaluated_at: datetime
    profile_sha256: Digest
    content_sha256: Digest
    facts_sha256: Digest
    policy_sha256: Digest
    input_sha256: Digest
    facts_extractor_version: str
    decision_engine_version: str = DECISION_ENGINE_VERSION
    rules_version: str = RULES_VERSION
    routing_version: str = ROUTING_VERSION

    @field_validator("evaluated_at")
    @classmethod
    def explicit_time(cls, value):
        return aware_time(value)
