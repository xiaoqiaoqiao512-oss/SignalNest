"""Current SQLAlchemy Core schema; only Alembic creates or alters tables.

Times are integer UTC Unix seconds; publication dates are site calendar dates.
"""

import sqlalchemy as sa

metadata = sa.MetaData()

# A document's stable identity is independent of its URL, title, and content digest.
documents = sa.Table(
    "documents",
    metadata,
    sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
    sa.Column("source_id", sa.Text, nullable=False),
    sa.Column("source_document_id", sa.Text, nullable=False),
    sa.Column("detail_url", sa.Text, nullable=False),
    sa.Column("discovered_title", sa.Text, nullable=False),
    sa.Column("discovered_at", sa.Integer, nullable=False),
    sa.Column("status", sa.Text, nullable=False, server_default="discovered"),
    sa.Column("current_version_id", sa.Integer),
    sa.Column("last_attempt_at", sa.Integer),
    sa.Column("last_success_at", sa.Integer),
    sa.Column("last_error_code", sa.Text),
    sa.Column("next_due_at", sa.Integer),
    sa.Column("discovery_origin", sa.Text, nullable=False, server_default="unknown"),
    sa.Column("first_discovery_run_id", sa.Text, sa.ForeignKey("ingestion_runs.id")),
    sa.CheckConstraint(
        "discovery_origin IN ('unknown', 'bootstrap', 'regular', 'historical')",
        name="ck_document_origin",
    ),
    sa.UniqueConstraint("source_id", "source_document_id", name="uq_document_identity"),
    sa.ForeignKeyConstraint(
        ["id", "current_version_id"],
        ["notice_versions.document_id", "notice_versions.id"],
        name="fk_document_current_version",
        use_alter=True,
    ),
    sa.CheckConstraint(
        "(current_version_id IS NULL AND last_success_at IS NULL) OR "
        "(current_version_id IS NOT NULL AND last_success_at IS NOT NULL)",
        name="ck_document_success_reference",
    ),
    sa.CheckConstraint(
        "status IN ('discovered', 'processed', 'failed')", name="ck_document_status"
    ),
    sa.CheckConstraint(
        "(status = 'discovered' AND last_attempt_at IS NULL AND last_success_at IS NULL "
        "AND last_error_code IS NULL) OR "
        "(status = 'processed' AND last_attempt_at IS NOT NULL AND last_success_at IS NOT NULL "
        "AND last_error_code IS NULL) OR "
        "(status = 'failed' AND last_attempt_at IS NOT NULL AND last_error_code IS NOT NULL)",
        name="ck_document_processing_state",
    ),
    sa.CheckConstraint(
        "last_success_at IS NULL OR last_success_at <= last_attempt_at",
        name="ck_document_success_time",
    ),
    sa.Index("ix_documents_next_due_at", "next_due_at"),
)

raw_responses = sa.Table(
    "raw_responses",
    metadata,
    sa.Column("id", sa.Integer, primary_key=True),
    sa.Column("source_id", sa.Text, nullable=False),
    sa.Column("requested_url", sa.Text, nullable=False),
    sa.Column("final_url", sa.Text, nullable=False),
    sa.Column("fetched_at", sa.Integer, nullable=False),
    sa.Column("status_code", sa.Integer, nullable=False),
    sa.Column("content_type", sa.Text),
    sa.Column("etag", sa.Text),
    sa.Column("last_modified", sa.Text),
    sa.Column("body_path", sa.Text),
    sa.Column("body_sha256", sa.Text),
    # NULL is reserved for responses written before the offline ingestion migration.
    sa.Column("page_type", sa.Text),
    sa.Column("document_id", sa.ForeignKey("documents.id", name="fk_response_document")),
    sa.Column("last_attempt_at", sa.Integer),
    sa.Column("last_error_code", sa.Text),
    sa.Column("body_state", sa.Text, nullable=False, server_default="unknown"),
    sa.Column("resource_id", sa.Integer, sa.ForeignKey("http_resources.id")),
    sa.Column("validated_response_id", sa.Integer, sa.ForeignKey("raw_responses.id")),
    sa.Column("vary", sa.Text),
    sa.Column("cache_control", sa.Text),
    sa.Column("content_encoding", sa.Text),
    sa.CheckConstraint(
        "body_state IN ('unknown', 'complete', 'unavailable') AND "
        "(body_state != 'complete' OR (body_path IS NOT NULL AND status_code != 304)) AND "
        "(body_state != 'unavailable' OR body_path IS NULL)",
        name="ck_response_body_state",
    ),
    sa.CheckConstraint(
        "validated_response_id IS NULL OR "
        "(status_code = 304 AND resource_id IS NOT NULL AND validated_response_id != id)",
        name="ck_response_validation_binding",
    ),
    sa.CheckConstraint(
        "page_type IS NULL OR page_type IN ('list', 'notice')", name="ck_response_page_type"
    ),
    sa.CheckConstraint(
        "(page_type IS NULL AND document_id IS NULL) OR "
        "(page_type IS NOT NULL AND ((page_type = 'list' AND document_id IS NULL) OR "
        "(page_type = 'notice' AND document_id IS NOT NULL)))",
        name="ck_response_target",
    ),
    sa.CheckConstraint("status_code BETWEEN 100 AND 599", name="ck_response_status"),
    sa.CheckConstraint(
        "(body_path IS NULL AND body_sha256 IS NULL) OR "
        "(body_path IS NOT NULL AND body_sha256 IS NOT NULL AND length(body_sha256) = 64)",
        name="ck_response_body_reference",
    ),
    sa.CheckConstraint(
        "status_code != 304 OR body_path IS NULL", name="ck_response_304_has_no_body"
    ),
)

# A list link is not an article identity or a fetch task. Only actually archived
# list responses can supply first/latest observations. No adapter promotion yet.
discovered_references = sa.Table(
    "discovered_references",
    metadata,
    sa.Column("id", sa.Integer, primary_key=True),
    sa.Column("source_id", sa.Text, nullable=False),
    sa.Column("candidate_key", sa.Text, nullable=False),
    sa.Column("normalization_version", sa.Text, nullable=False),
    sa.Column("resolved_url", sa.Text, nullable=False),
    sa.Column("raw_href", sa.Text, nullable=False),
    sa.Column("title", sa.Text, nullable=False),
    sa.Column("published_date", sa.Date, nullable=False),
    sa.Column("reference_kind", sa.Text, nullable=False),
    sa.Column("status", sa.Text, nullable=False, server_default="pending_adapter"),
    sa.Column("first_seen_at", sa.Integer, nullable=False),
    sa.Column("last_seen_at", sa.Integer, nullable=False),
    sa.Column("discovery_origin", sa.Text, nullable=False),
    sa.Column("first_discovery_run_id", sa.Text, sa.ForeignKey("ingestion_runs.id")),
    sa.Column(
        "first_body_response_id", sa.Integer, sa.ForeignKey("raw_responses.id"), nullable=False
    ),
    sa.Column(
        "first_observed_response_id", sa.Integer, sa.ForeignKey("raw_responses.id"), nullable=False
    ),
    sa.Column("first_row_index", sa.Integer, nullable=False),
    sa.Column("first_parser_version", sa.Text, nullable=False),
    sa.Column("last_run_id", sa.Text, sa.ForeignKey("ingestion_runs.id")),
    sa.Column(
        "last_body_response_id", sa.Integer, sa.ForeignKey("raw_responses.id"), nullable=False
    ),
    sa.Column(
        "last_observed_response_id", sa.Integer, sa.ForeignKey("raw_responses.id"), nullable=False
    ),
    sa.Column("last_row_index", sa.Integer, nullable=False),
    sa.Column("last_parser_version", sa.Text, nullable=False),
    sa.UniqueConstraint("source_id", "candidate_key", name="uq_discovered_reference"),
    sa.CheckConstraint(
        "length(candidate_key) = 72 AND substr(candidate_key,1,8) = 'link:v1:'",
        name="ck_reference_key",
    ),
    sa.CheckConstraint("status = 'pending_adapter'", name="ck_reference_status"),
    sa.CheckConstraint(
        "reference_kind IN ('external','unsupported_column','unsupported_route')",
        name="ck_reference_kind",
    ),
    sa.CheckConstraint(
        "discovery_origin IN ('unknown','bootstrap','regular','historical')",
        name="ck_reference_origin",
    ),
    sa.CheckConstraint(
        "first_seen_at >= 0 AND last_seen_at >= first_seen_at "
        "AND first_row_index >= 0 AND last_row_index >= 0",
        name="ck_reference_observation",
    ),
)

notice_versions = sa.Table(
    "notice_versions",
    metadata,
    sa.Column("id", sa.Integer, primary_key=True),
    sa.Column("document_id", sa.ForeignKey("documents.id"), nullable=False),
    sa.Column("raw_response_id", sa.ForeignKey("raw_responses.id"), nullable=False),
    sa.Column("content_sha256", sa.Text, nullable=False),
    sa.Column("parser_version", sa.Text, nullable=False),
    sa.Column("parsed_at", sa.Integer, nullable=False),
    sa.Column("title", sa.Text, nullable=False),
    sa.Column("published_date", sa.Date, nullable=False),
    sa.Column("normalized_content", sa.JSON(none_as_null=True), nullable=False),
    sa.UniqueConstraint(
        "document_id", "content_sha256", "parser_version", name="uq_notice_version"
    ),
    sa.UniqueConstraint("document_id", "id", name="uq_version_document_id"),
    sa.CheckConstraint("length(content_sha256) = 64", name="ck_version_digest"),
    sa.CheckConstraint("length(parser_version) > 0", name="ck_version_parser"),
)

# Rebuildable lexical index of the current successful version only. The FTS5
# virtual table and its triggers are migration-owned SQLite implementation details.
search_documents = sa.Table(
    "search_documents",
    metadata,
    sa.Column("document_id", sa.Integer, sa.ForeignKey("documents.id"), primary_key=True),
    sa.Column("version_id", sa.Integer, nullable=False),
    sa.Column("title_text", sa.Text, nullable=False),
    sa.Column("body_text", sa.Text, nullable=False),
    sa.Column("index_version", sa.Text, nullable=False),
    sa.ForeignKeyConstraint(
        ["document_id", "version_id"],
        ["notice_versions.document_id", "notice_versions.id"],
        name="fk_search_current_version",
    ),
    sa.CheckConstraint("length(index_version) > 0", name="ck_search_index_version"),
)

source_ingestion_state = sa.Table(
    "source_ingestion_state",
    metadata,
    sa.Column("source_id", sa.Text, primary_key=True),
    sa.Column("last_list_attempt_at", sa.Integer),
    sa.Column("last_list_response_at", sa.Integer),
    sa.Column("last_list_registered_at", sa.Integer),
    sa.Column("last_complete_scan_at", sa.Integer),
    sa.Column(
        "last_complete_scan_run_id",
        sa.Text,
        sa.ForeignKey("ingestion_runs.id", name="fk_source_complete_run", use_alter=True),
    ),
    sa.Column("bootstrap_completed_at", sa.Integer),
    sa.Column("not_before_at", sa.Integer),
    sa.CheckConstraint(
        "(last_complete_scan_at IS NULL) = (last_complete_scan_run_id IS NULL)",
        name="ck_source_scan_reference",
    ),
)

ingestion_runs = sa.Table(
    "ingestion_runs",
    metadata,
    sa.Column("id", sa.Text, primary_key=True),
    sa.Column(
        "source_id", sa.Text, sa.ForeignKey("source_ingestion_state.source_id"), nullable=False
    ),
    sa.Column("origin", sa.Text, nullable=False),
    sa.Column("parser_version", sa.Text, nullable=False),
    sa.Column("started_at", sa.Integer, nullable=False),
    sa.Column("finished_at", sa.Integer),
    sa.Column("result", sa.Text, nullable=False, server_default="running"),
    sa.Column("coverage", sa.Text, nullable=False, server_default="pending"),
    sa.Column("coverage_at", sa.Integer),
    sa.Column("coverage_error_code", sa.Text),
    sa.Column("coverage_evidence", sa.JSON(none_as_null=True)),
    sa.Column("error_code", sa.Text),
    sa.CheckConstraint("origin IN ('bootstrap', 'regular', 'historical')", name="ck_run_origin"),
    sa.CheckConstraint(
        "result IN ('running','succeeded','partial_failure','failed','interrupted')",
        name="ck_run_result",
    ),
    sa.CheckConstraint(
        "coverage IN ('pending','complete','limited','interrupted')", name="ck_run_coverage"
    ),
    sa.CheckConstraint(
        "(result = 'running' AND finished_at IS NULL) OR "
        "(result != 'running' AND finished_at IS NOT NULL AND finished_at >= started_at)",
        name="ck_run_finished",
    ),
    sa.CheckConstraint(
        "(coverage = 'pending' AND coverage_at IS NULL) OR "
        "(coverage != 'pending' AND coverage_at IS NOT NULL AND coverage_at >= started_at)",
        name="ck_run_coverage_time",
    ),
)

http_resources = sa.Table(
    "http_resources",
    metadata,
    sa.Column("id", sa.Integer, primary_key=True),
    sa.Column("source_id", sa.Text, nullable=False),
    sa.Column("request_uri", sa.Text, nullable=False),
    sa.Column("profile_sha256", sa.Text, nullable=False),
    sa.Column("request_profile", sa.JSON(none_as_null=True), nullable=False),
    sa.Column(
        "blocked_by_response_id",
        sa.Integer,
        sa.ForeignKey("raw_responses.id", name="fk_resource_blocked_response", use_alter=True),
    ),
    sa.Column(
        "latest_response_id",
        sa.Integer,
        sa.ForeignKey("raw_responses.id", name="fk_resource_latest_response", use_alter=True),
    ),
    sa.Column(
        "last_processed_response_id",
        sa.Integer,
        sa.ForeignKey("raw_responses.id", name="fk_resource_processed_response", use_alter=True),
    ),
    sa.Column("last_processed_parser_version", sa.Text),
    sa.Column("last_processed_at", sa.Integer),
    sa.UniqueConstraint("source_id", "request_uri", "profile_sha256", name="uq_http_resource"),
    sa.CheckConstraint("length(profile_sha256) = 64", name="ck_resource_profile"),
    sa.CheckConstraint(
        "(last_processed_response_id IS NULL AND last_processed_parser_version IS NULL "
        "AND last_processed_at IS NULL) OR (last_processed_response_id IS NOT NULL "
        "AND last_processed_parser_version IS NOT NULL AND last_processed_at IS NOT NULL)",
        name="ck_resource_processed_reference",
    ),
)

# Notification activation is explicit. Migrating creates no policies or mail work.
notification_policy_revisions = sa.Table(
    "notification_policy_revisions",
    metadata,
    sa.Column("id", sa.Integer, primary_key=True),
    sa.Column("policy_sha256", sa.Text, nullable=False, unique=True),
    sa.Column("manifest", sa.JSON, nullable=False),
    sa.Column("created_at", sa.Integer, nullable=False),
    sa.CheckConstraint("length(policy_sha256) = 64", name="ck_notification_policy_digest"),
)

notification_channel_state = sa.Table(
    "notification_channel_state",
    metadata,
    sa.Column("id", sa.Text, primary_key=True),
    sa.Column("installation_id", sa.Text, nullable=False, unique=True),
    sa.Column("source_id", sa.Text, nullable=False),
    sa.Column("activation_id", sa.Text, nullable=False),
    sa.Column("activation_at", sa.Integer, nullable=False),
    sa.Column("parameters_sha256", sa.Text, nullable=False),
    sa.Column(
        "policy_revision_id",
        sa.Integer,
        sa.ForeignKey("notification_policy_revisions.id"),
        nullable=False,
    ),
    sa.Column("notification_mode", sa.Text, nullable=False),
    sa.Column("initial_recent_review", sa.Boolean, nullable=False),
    sa.Column("digest_hour", sa.Integer, nullable=False),
    sa.Column("digest_minute", sa.Integer, nullable=False),
    sa.Column("sender", sa.Text, nullable=False),
    sa.Column("recipient", sa.Text, nullable=False),
    sa.Column("paused", sa.Boolean, nullable=False, server_default=sa.false()),
    sa.Column("pause_reason", sa.Text),
    sa.Column("paused_at", sa.Integer),
    sa.CheckConstraint("id = 'primary'", name="ck_notification_single_channel"),
    sa.CheckConstraint(
        "notification_mode IN ('hybrid','digest_only')", name="ck_notification_mode"
    ),
    sa.CheckConstraint(
        "digest_hour BETWEEN 0 AND 23 AND digest_minute BETWEEN 0 AND 59",
        name="ck_notification_calendar",
    ),
)

notification_listing_evidence = sa.Table(
    "notification_listing_evidence",
    metadata,
    sa.Column("document_id", sa.Integer, sa.ForeignKey("documents.id"), primary_key=True),
    sa.Column("published_date", sa.Date, nullable=False),
    sa.Column("body_response_id", sa.Integer, sa.ForeignKey("raw_responses.id")),
    sa.Column("observed_response_id", sa.Integer, sa.ForeignKey("raw_responses.id")),
    sa.Column("parser_version", sa.Text, nullable=False),
    sa.Column("registered_at", sa.Integer, nullable=False),
    sa.Column("processing_origin", sa.Text, nullable=False),
    # Set only when this identity was actually inserted by a production list page.
    sa.Column("live_discovered_run_id", sa.Text, sa.ForeignKey("ingestion_runs.id")),
    sa.Column("live_discovered_at", sa.Integer),
    sa.CheckConstraint(
        "processing_origin IN ('live','offline','maintenance')", name="ck_listing_processing_origin"
    ),
    sa.CheckConstraint(
        "(live_discovered_run_id IS NULL) = (live_discovered_at IS NULL)",
        name="ck_listing_live_discovery",
    ),
)

notification_activation_members = sa.Table(
    "notification_activation_members",
    metadata,
    sa.Column(
        "installation_id",
        sa.Text,
        sa.ForeignKey("notification_channel_state.installation_id"),
        primary_key=True,
    ),
    sa.Column("source_id", sa.Text, primary_key=True),
    sa.Column("source_document_id", sa.Text, primary_key=True),
    sa.Column("candidate_state", sa.Text, nullable=False),
    sa.Column("list_evidence", sa.JSON),
    sa.Column("notice_evidence", sa.JSON),
    sa.Column("selection_evidence", sa.JSON),
    sa.Column("conflict_evidence", sa.JSON),
    sa.Column("date_conflict", sa.Boolean, nullable=False, server_default=sa.false()),
    sa.Column(
        "generated_event_id", sa.Integer, sa.ForeignKey("notification_events.id", use_alter=True)
    ),
    sa.CheckConstraint(
        "candidate_state IN ('unknown','selected','not_recent','disabled','generated')",
        name="ck_activation_candidate",
    ),
    sa.CheckConstraint(
        "(candidate_state = 'generated') = (generated_event_id IS NOT NULL)",
        name="ck_activation_generated",
    ),
)

notification_observations = sa.Table(
    "notification_observations",
    metadata,
    sa.Column(
        "installation_id",
        sa.Text,
        sa.ForeignKey("notification_channel_state.installation_id"),
        primary_key=True,
    ),
    sa.Column("document_id", sa.Integer, sa.ForeignKey("documents.id"), primary_key=True),
    sa.Column("version_id", sa.Integer, nullable=False),
    sa.Column("body_response_id", sa.Integer, sa.ForeignKey("raw_responses.id"), nullable=False),
    sa.Column(
        "observed_response_id", sa.Integer, sa.ForeignKey("raw_responses.id"), nullable=False
    ),
    sa.Column("observed_at", sa.Integer, nullable=False),
    sa.Column("event_seq", sa.Integer, nullable=False),
    sa.Column("comparison_error_code", sa.Text),
    sa.Column("comparison_error_at", sa.Integer),
    sa.Column("comparison_evidence", sa.JSON),
    sa.ForeignKeyConstraint(
        ["document_id", "version_id"],
        ["notice_versions.document_id", "notice_versions.id"],
        name="fk_observation_version",
    ),
    sa.CheckConstraint("event_seq >= 0", name="ck_observation_sequence"),
    sa.CheckConstraint(
        "(comparison_error_code IS NULL) = (comparison_error_at IS NULL)",
        name="ck_observation_comparison",
    ),
)

notification_events = sa.Table(
    "notification_events",
    metadata,
    sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
    sa.Column(
        "installation_id",
        sa.Text,
        sa.ForeignKey("notification_channel_state.installation_id"),
        nullable=False,
    ),
    sa.Column("document_id", sa.Integer, sa.ForeignKey("documents.id"), nullable=False),
    sa.Column("event_seq", sa.Integer, nullable=False),
    sa.Column("kind", sa.Text, nullable=False),
    sa.Column("previous_version_id", sa.Integer),
    sa.Column("version_id", sa.Integer, nullable=False),
    sa.Column("previous_body_response_id", sa.Integer, sa.ForeignKey("raw_responses.id")),
    sa.Column("body_response_id", sa.Integer, sa.ForeignKey("raw_responses.id"), nullable=False),
    sa.Column(
        "observed_response_id", sa.Integer, sa.ForeignKey("raw_responses.id"), nullable=False
    ),
    sa.Column("occurred_at", sa.Integer, nullable=False),
    sa.Column("selected_decision_id", sa.Integer),
    sa.Column("effective_route", sa.Text, nullable=False, server_default="none"),
    sa.Column("delivery_intent_registered_at", sa.Integer),
    sa.Column("outbox_id", sa.Integer),
    sa.UniqueConstraint(
        "installation_id", "document_id", "event_seq", name="uq_notification_event_sequence"
    ),
    sa.ForeignKeyConstraint(
        ["document_id", "version_id"],
        ["notice_versions.document_id", "notice_versions.id"],
        name="fk_event_version",
    ),
    sa.ForeignKeyConstraint(
        ["document_id", "previous_version_id"],
        ["notice_versions.document_id", "notice_versions.id"],
        name="fk_event_previous_version",
    ),
    sa.ForeignKeyConstraint(
        ["id", "selected_decision_id"],
        ["notification_decisions.event_id", "notification_decisions.id"],
        name="fk_event_selected_decision",
        use_alter=True,
    ),
    sa.ForeignKeyConstraint(
        ["id", "outbox_id"],
        ["email_outbox.event_id", "email_outbox.id"],
        name="fk_event_outbox",
        use_alter=True,
    ),
    sa.CheckConstraint(
        "(kind = 'opportunity_review' AND event_seq = 0) OR "
        "(kind != 'opportunity_review' AND event_seq > 0)",
        name="ck_event_sequence",
    ),
    sa.CheckConstraint(
        "kind IN ('new','update','activation_recent','opportunity_review')", name="ck_event_kind"
    ),
    sa.CheckConstraint("effective_route IN ('none','immediate','digest')", name="ck_event_route"),
    sa.CheckConstraint(
        "(effective_route = 'none') = (delivery_intent_registered_at IS NULL)",
        name="ck_event_eligibility",
    ),
    sa.CheckConstraint(
        "outbox_id IS NULL OR effective_route = 'immediate'", name="ck_event_outbox_route"
    ),
)

notification_decisions = sa.Table(
    "notification_decisions",
    metadata,
    sa.Column("id", sa.Integer, primary_key=True),
    sa.Column("event_id", sa.Integer, sa.ForeignKey("notification_events.id"), nullable=False),
    sa.Column(
        "policy_revision_id",
        sa.Integer,
        sa.ForeignKey("notification_policy_revisions.id"),
        nullable=False,
    ),
    sa.Column("evaluation_key", sa.Text, nullable=False),
    sa.Column("evaluated_at", sa.Integer, nullable=False),
    sa.Column("decision", sa.JSON, nullable=False),
    sa.Column("facts", sa.JSON, nullable=False),
    sa.Column("context", sa.JSON, nullable=False),
    sa.UniqueConstraint(
        "event_id", "policy_revision_id", "evaluation_key", name="uq_event_evaluation"
    ),
    sa.UniqueConstraint("event_id", "id", name="uq_decision_event_id"),
)

email_outbox = sa.Table(
    "email_outbox",
    metadata,
    sa.Column("id", sa.Integer, primary_key=True),
    sa.Column(
        "event_id", sa.Integer, sa.ForeignKey("notification_events.id"), nullable=False, unique=True
    ),
    sa.Column("decision_id", sa.Integer, nullable=False),
    sa.Column("delivery_key", sa.Text, nullable=False, unique=True),
    sa.Column("recipient_key", sa.Text, nullable=False, server_default="primary"),
    sa.Column("kind", sa.Text, nullable=False, server_default="immediate"),
    sa.Column("state", sa.Text, nullable=False, server_default="planned"),
    sa.Column("sender", sa.Text, nullable=False),
    sa.Column("recipient", sa.Text, nullable=False),
    sa.Column("created_at", sa.Integer, nullable=False),
    sa.UniqueConstraint("event_id", "id", name="uq_outbox_event_id"),
    sa.ForeignKeyConstraint(
        ["event_id", "decision_id"],
        ["notification_decisions.event_id", "notification_decisions.id"],
        name="fk_outbox_decision",
    ),
    sa.CheckConstraint(
        "state = 'planned' AND kind = 'immediate' AND recipient_key = 'primary'",
        name="ck_outbox_planned_only",
    ),
)

# N1 intents stay intact; N2 allocates immutable bytes and one precise mail path.
mail_messages = sa.Table(
    "mail_messages",
    metadata,
    sa.Column("id", sa.Integer, primary_key=True),
    sa.Column(
        "installation_id",
        sa.Text,
        sa.ForeignKey("notification_channel_state.installation_id"),
        nullable=False,
    ),
    sa.Column("immediate_intent_id", sa.Integer, sa.ForeignKey("email_outbox.id"), unique=True),
    sa.Column("kind", sa.Text, nullable=False),
    sa.Column("delivery_key", sa.Text, nullable=False, unique=True),
    sa.Column("digest_slot", sa.Integer),
    sa.Column("part", sa.Integer, nullable=False),
    sa.Column("sender", sa.Text, nullable=False),
    sa.Column("recipient", sa.Text, nullable=False),
    sa.Column("subject", sa.Text, nullable=False),
    sa.Column("message_id", sa.Text, nullable=False, unique=True),
    sa.Column("date_at", sa.Integer, nullable=False),
    sa.Column("frozen_at", sa.Integer, nullable=False),
    sa.Column("rendering_version", sa.Text, nullable=False),
    sa.Column("payload_bytes", sa.LargeBinary, nullable=False),
    sa.Column("payload_sha256", sa.Text, nullable=False),
    sa.Column("members_sha256", sa.Text, nullable=False),
    sa.Column("max_bytes", sa.Integer, nullable=False),
    sa.Column("max_events", sa.Integer, nullable=False),
    sa.Column("state", sa.Text, nullable=False, server_default="pending"),
    sa.UniqueConstraint("installation_id", "digest_slot", "part", name="uq_digest_part"),
    sa.CheckConstraint(
        "(kind = 'immediate' AND immediate_intent_id IS NOT NULL "
        "AND digest_slot IS NULL AND part = 1) OR (kind = 'digest' "
        "AND immediate_intent_id IS NULL AND digest_slot IS NOT NULL AND part > 0)",
        name="ck_mail_kind",
    ),
    sa.CheckConstraint("state = 'pending'", name="ck_mail_pending_only"),
    sa.CheckConstraint(
        "length(payload_sha256) = 64 AND length(members_sha256) = 64", name="ck_mail_digests"
    ),
    sa.CheckConstraint(
        "length(payload_bytes) > 0 AND length(payload_bytes) <= max_bytes AND max_events > 0",
        name="ck_mail_payload_limit",
    ),
)

mail_message_members = sa.Table(
    "mail_message_members",
    metadata,
    sa.Column(
        "event_id",
        sa.Integer,
        sa.ForeignKey("notification_events.id"),
        primary_key=True,
        autoincrement=False,
    ),
    sa.Column("mail_id", sa.Integer, sa.ForeignKey("mail_messages.id"), nullable=False),
    sa.Column("position", sa.Integer, nullable=False),
    sa.Column("decision_id", sa.Integer, nullable=False),
    sa.Column("snapshot", sa.JSON, nullable=False),
    sa.ForeignKeyConstraint(
        ["event_id", "decision_id"],
        ["notification_decisions.event_id", "notification_decisions.id"],
        name="fk_mail_member_decision",
    ),
    sa.UniqueConstraint("mail_id", "position", name="uq_mail_member_position"),
    sa.CheckConstraint("position >= 0", name="ck_mail_member_position"),
)

mail_plan_errors = sa.Table(
    "mail_plan_errors",
    metadata,
    sa.Column(
        "event_id",
        sa.Integer,
        sa.ForeignKey("notification_events.id"),
        primary_key=True,
        autoincrement=False,
    ),
    sa.Column("decision_id", sa.Integer, nullable=False),
    sa.Column("rendering_version", sa.Text, nullable=False),
    sa.Column("max_bytes", sa.Integer, nullable=False),
    sa.Column("error_code", sa.Text, nullable=False),
    sa.Column("attempted_at", sa.Integer, nullable=False),
    sa.ForeignKeyConstraint(
        ["event_id", "decision_id"],
        ["notification_decisions.event_id", "notification_decisions.id"],
        name="fk_mail_error_decision",
    ),
    sa.CheckConstraint(
        "error_code IN ('mail_item_too_large','render_input_invalid','render_content_mismatch')",
        name="ck_mail_render_error",
    ),
)

# Frozen MIME and membership never change. This is the sole SMTP state.
mail_delivery = sa.Table(
    "mail_delivery",
    metadata,
    sa.Column("mail_id", sa.Integer, sa.ForeignKey("mail_messages.id"), primary_key=True),
    sa.Column("state", sa.Text, nullable=False, server_default="pending"),
    sa.Column("attempt_count", sa.Integer, nullable=False, server_default="0"),
    sa.Column("manual_retry_pending", sa.Boolean, nullable=False, server_default=sa.false()),
    sa.Column("next_attempt_at", sa.Integer),
    sa.Column("accepted_at", sa.Integer),
    sa.Column("updated_at", sa.Integer, nullable=False),
    sa.Column("blocked_reason", sa.Text),
    sa.CheckConstraint(
        "state IN ('pending','sending','retry','uncertain','accepted','blocked')",
        name="ck_delivery_state",
    ),
    sa.CheckConstraint("attempt_count >= 0", name="ck_delivery_attempt_count"),
    sa.CheckConstraint(
        "(state IN ('pending','retry','uncertain') AND next_attempt_at IS NOT NULL) "
        "OR (state IN ('sending','accepted','blocked') AND next_attempt_at IS NULL)",
        name="ck_delivery_due",
    ),
    sa.CheckConstraint(
        "(state = 'accepted') = (accepted_at IS NOT NULL)", name="ck_delivery_accepted"
    ),
    sa.CheckConstraint(
        "(state = 'blocked' AND blocked_reason IS NOT NULL AND blocked_reason IN "
        "('permanent','retry_exhausted','manual_attempt_failed','frozen_corrupt')) "
        "OR (state != 'blocked' AND blocked_reason IS NULL)",
        name="ck_delivery_blocked",
    ),
)

mail_attempts = sa.Table(
    "mail_attempts",
    metadata,
    sa.Column("mail_id", sa.Integer, sa.ForeignKey("mail_delivery.mail_id"), primary_key=True),
    sa.Column("attempt_no", sa.Integer, primary_key=True),
    sa.Column("started_at", sa.Integer, nullable=False),
    sa.Column("finished_at", sa.Integer),
    sa.Column("outcome", sa.Text),
    sa.Column("stage", sa.Text),
    sa.Column("error_code", sa.Text),
    sa.Column("smtp_code", sa.Integer),
    sa.Column("scope", sa.Text),
    sa.Column("uncertain_until", sa.Integer),
    sa.Column("cleanup_failed", sa.Boolean, nullable=False, server_default=sa.false()),
    sa.Column("manual", sa.Boolean, nullable=False, server_default=sa.false()),
    sa.Column("recovered", sa.Boolean, nullable=False, server_default=sa.false()),
    sa.CheckConstraint("attempt_no > 0", name="ck_mail_attempt_number"),
    sa.CheckConstraint(
        "(finished_at IS NULL AND outcome IS NULL AND stage IS NULL "
        "AND error_code IS NULL AND smtp_code IS NULL AND scope IS NULL) OR "
        "(finished_at IS NOT NULL AND outcome IS NOT NULL AND stage IS NOT NULL "
        "AND scope IS NOT NULL AND finished_at >= started_at AND outcome IN "
        "('accepted','retryable','uncertain','permanent') AND stage IN "
        "('config','connect','hello','tls','auth','mail','rcpt',"
        "'body_or_final','accepted','unknown') "
        "AND scope IN ('message','channel'))",
        name="ck_mail_attempt_result",
    ),
    sa.CheckConstraint(
        "smtp_code IS NULL OR smtp_code BETWEEN 100 AND 599", name="ck_mail_smtp_code"
    ),
    sa.CheckConstraint(
        "outcome IS NULL OR (outcome = 'accepted' AND stage = 'accepted' "
        "AND smtp_code IS NOT NULL AND smtp_code = 250 AND error_code IS NULL) OR "
        "(outcome != 'accepted' AND stage != 'accepted' AND error_code IS NOT NULL)",
        name="ck_mail_acceptance_evidence",
    ),
    sa.CheckConstraint(
        "outcome IS NULL OR outcome != 'uncertain' OR "
        "(stage IN ('body_or_final','unknown') AND uncertain_until IS NOT NULL "
        "AND uncertain_until >= finished_at + 1800)",
        name="ck_mail_uncertain_stage",
    ),
    sa.CheckConstraint(
        "error_code IS NULL OR error_code IN "
        "('invalid_frozen_mail','credentials_missing','credentials_invalid',"
        "'tls_verification_failed','tls_not_supported','tls_failed','auth_not_supported',"
        "'auth_mechanism_not_supported','authentication_rejected','server_rejected',"
        "'connection_failed','timeout','disconnected','protocol_error','process_interrupted')",
        name="ck_mail_attempt_error",
    ),
)

notification_operations = sa.Table(
    "notification_operations",
    metadata,
    sa.Column("id", sa.Text, primary_key=True),
    sa.Column(
        "installation_id",
        sa.Text,
        sa.ForeignKey("notification_channel_state.installation_id"),
        nullable=False,
    ),
    sa.Column("source_id", sa.Text, nullable=False),
    sa.Column("kind", sa.Text, nullable=False),
    sa.Column("parameters_sha256", sa.Text, nullable=False),
    sa.Column("parameters", sa.JSON, nullable=False),
    sa.Column("snapshot", sa.JSON, nullable=False),
    sa.Column("results", sa.JSON, nullable=False),
    sa.Column("created_at", sa.Integer, nullable=False),
    sa.Column("finished_at", sa.Integer),
    sa.CheckConstraint(
        "kind IN ('policy_update','reevaluate')", name="ck_notification_operation_kind"
    ),
    sa.CheckConstraint("length(parameters_sha256) = 64", name="ck_notification_operation_digest"),
    sa.CheckConstraint(
        "finished_at IS NULL OR finished_at >= created_at",
        name="ck_notification_operation_finished",
    ),
)

# Document-level explicit review is separate from live content sequencing. One
# reserved sequence-zero event per installation/document prevents duplicate
# opportunity reminders across operation IDs without changing live baselines.
notification_opportunity_review_operations = sa.Table(
    "notification_opportunity_review_operations",
    metadata,
    sa.Column("id", sa.Text, primary_key=True),
    sa.Column(
        "installation_id",
        sa.Text,
        sa.ForeignKey("notification_channel_state.installation_id"),
        nullable=False,
    ),
    sa.Column("source_id", sa.Text, nullable=False),
    sa.Column("parameters", sa.JSON, nullable=False),
    sa.Column("parameters_sha256", sa.Text, nullable=False),
    sa.Column("snapshot", sa.JSON, nullable=False),
    sa.Column("results", sa.JSON, nullable=False),
    sa.Column("created_at", sa.Integer, nullable=False),
    sa.Column("finished_at", sa.Integer),
    sa.CheckConstraint("length(parameters_sha256) = 64", name="ck_opportunity_review_digest"),
    sa.CheckConstraint(
        "finished_at IS NULL OR finished_at >= created_at",
        name="ck_opportunity_review_finished",
    ),
)
