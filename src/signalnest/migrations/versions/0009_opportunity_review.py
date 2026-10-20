"""Explicit document-level opportunity review without altering live baselines."""

import sqlalchemy as sa
from alembic import op

revision = "0009_opportunity_review"
down_revision = "0008_list_references"
branch_labels = None
depends_on = None


def _event_checks(upgrade):
    connection = op.get_bind()
    # Events and decisions/outbox contain real circular references. FKs stay ON;
    # defer the temporary missing-parent interval, validate the entire resulting
    # graph, then clear SQLite's obsolete deferred DROP-parent counters. Simply
    # checking foreign_key_check without resetting those counters can still make
    # COMMIT fail. All DDL/data remains in the application's one transaction.
    connection.exec_driver_sql("PRAGMA defer_foreign_keys=ON")
    with op.batch_alter_table("notification_events", recreate="always") as batch:
        batch.drop_constraint("ck_event_sequence", type_="check")
        batch.drop_constraint("ck_event_kind", type_="check")
        batch.create_check_constraint(
            "ck_event_sequence",
            "(kind = 'opportunity_review' AND event_seq = 0) OR "
            "(kind != 'opportunity_review' AND event_seq > 0)"
            if upgrade
            else "event_seq > 0",
        )
        batch.create_check_constraint(
            "ck_event_kind",
            "kind IN ('new','update','activation_recent','opportunity_review')"
            if upgrade
            else "kind IN ('new','update','activation_recent')",
        )
    if connection.exec_driver_sql("PRAGMA foreign_key_check").first() is not None:
        raise RuntimeError("opportunity_review_migration_foreign_keys")
    connection.exec_driver_sql("PRAGMA defer_foreign_keys=OFF")


def upgrade():
    _event_checks(True)
    op.create_table(
        "notification_opportunity_review_operations",
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


def downgrade():
    connection = op.get_bind()
    if (
        connection.exec_driver_sql(
            "SELECT 1 FROM notification_events WHERE kind='opportunity_review' LIMIT 1"
        ).first()
        is not None
        or connection.exec_driver_sql(
            "SELECT 1 FROM notification_opportunity_review_operations LIMIT 1"
        ).first()
        is not None
    ):
        raise RuntimeError("opportunity_review_downgrade_would_lose_evidence")
    op.drop_table("notification_opportunity_review_operations")
    _event_checks(False)
