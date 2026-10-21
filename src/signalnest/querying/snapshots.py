"""Explicit, hash-bound provenance for frozen query copies, never live backups."""

import hashlib
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Literal

from pydantic import Field, ValidationError, model_validator

from signalnest.contracts import Contract, Digest
from signalnest.querying.contracts import InstanceID, QueryError


class SnapshotFacts(Contract):
    """Persisted source facts, not freshness inferred from the copy/export time."""

    source_id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,79}$")
    last_list_response_at: int | None = Field(default=None, strict=True, ge=0)
    last_list_registered_at: int | None = Field(default=None, strict=True, ge=0)
    last_complete_scan_at: int | None = Field(default=None, strict=True, ge=0)
    last_complete_scan_run_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_.:-]{1,128}$")
    latest_successful_body_fetched_at: int | None = Field(default=None, strict=True, ge=0)
    latest_successful_content_parsed_at: int | None = Field(default=None, strict=True, ge=0)
    documents: int = Field(strict=True, ge=0)
    successful_documents: int = Field(strict=True, ge=0)
    failed_without_success: int = Field(strict=True, ge=0)
    failed_with_success: int = Field(strict=True, ge=0)
    unadapted_references: int = Field(strict=True, ge=0)


class QuerySnapshotManifest(Contract):
    format_version: Literal["signalnest-query-snapshot-v1"] = "signalnest-query-snapshot-v1"
    instance_id: InstanceID
    database_file: Literal["signalnest.sqlite3"] = "signalnest.sqlite3"
    database_sha256: Digest
    parent_database_sha256: Digest
    snapshot_at: int = Field(strict=True, ge=0)
    prepared_at: int = Field(strict=True, ge=0)
    transformation: Literal[
        "pause_notification_channels_v1", "pause_and_upgrade_0008_to_0009_v1"
    ] = "pause_notification_channels_v1"
    facts: SnapshotFacts

    @model_validator(mode="after")
    def time_order(self):
        times = (
            self.facts.last_list_response_at,
            self.facts.last_list_registered_at,
            self.facts.last_complete_scan_at,
            self.facts.latest_successful_body_fetched_at,
            self.facts.latest_successful_content_parsed_at,
        )
        if self.prepared_at < self.snapshot_at or any(
            stamp is not None and stamp > self.snapshot_at for stamp in times
        ):
            raise ValueError("snapshot time precedes persisted data or preparation")
        return self


def safe_snapshot_path(path: Path) -> None:
    """A snapshot is a regular local file; do not follow symlink components."""
    if not path.is_absolute() or any(part.is_symlink() for part in (path, *path.parents)):
        raise QueryError("snapshot_path_unsafe")
    if not path.is_file():
        raise QueryError("snapshot_unavailable")


def database_sha256(path: Path) -> str:
    safe_snapshot_path(path)
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def snapshot_facts(database: Path, source_id: str) -> SnapshotFacts:
    """Read actual timestamps/counts from a frozen SQLite copy, not its filesystem mtime."""
    safe_snapshot_path(database)
    with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as connection:
        connection.execute("PRAGMA query_only=ON")
        connection.row_factory = sqlite3.Row
        # This one-instance export must not silently include another source's rows.
        source_ids = {
            row[0]
            for row in connection.execute(
                "SELECT source_id FROM documents UNION SELECT source_id FROM raw_responses "
                "UNION SELECT source_id FROM source_ingestion_state"
            )
        }
        if source_ids - {source_id}:
            raise QueryError("snapshot_binding_mismatch")
        source = connection.execute(
            "SELECT last_list_response_at,last_list_registered_at,last_complete_scan_at,"
            "last_complete_scan_run_id FROM source_ingestion_state WHERE source_id=?",
            (source_id,),
        ).fetchone()
        current = connection.execute(
            "SELECT MAX(r.fetched_at),MAX(v.parsed_at) FROM documents d "
            "JOIN notice_versions v ON v.id=d.current_version_id AND v.document_id=d.id "
            "JOIN raw_responses r ON r.id=v.raw_response_id WHERE d.source_id=?",
            (source_id,),
        ).fetchone()
        counts = connection.execute(
            "SELECT COUNT(*),COUNT(current_version_id),"
            "COALESCE(SUM(status='failed' AND current_version_id IS NULL),0),"
            "COALESCE(SUM(status='failed' AND current_version_id IS NOT NULL),0) "
            "FROM documents WHERE source_id=?",
            (source_id,),
        ).fetchone()
        references = connection.execute(
            "SELECT COUNT(*) FROM discovered_references WHERE source_id=?", (source_id,)
        ).fetchone()[0]
    return SnapshotFacts(
        source_id=source_id,
        **dict(source or {}),
        latest_successful_body_fetched_at=current[0],
        latest_successful_content_parsed_at=current[1],
        documents=counts[0],
        successful_documents=counts[1],
        failed_without_success=counts[2],
        failed_with_success=counts[3],
        unadapted_references=references,
    )


def validate_snapshot(
    manifest_path: Path, database: Path, instance_id: str, source_id: str
) -> QuerySnapshotManifest:
    """Validate a manifest/DB pair before a query reads it; never repair or initialize."""
    try:
        safe_snapshot_path(manifest_path)
        with manifest_path.open("rb") as stream:
            raw = stream.read(16 * 1024 + 1)
        if len(raw) > 16 * 1024:
            raise QueryError("snapshot_manifest_invalid")
        manifest = QuerySnapshotManifest.model_validate_json(raw)
        if manifest.instance_id != instance_id or manifest.facts.source_id != source_id:
            raise QueryError("snapshot_binding_mismatch")
        expected_database = manifest_path.parent / manifest.database_file
        safe_snapshot_path(expected_database)
        if expected_database.resolve() != database:
            raise QueryError("snapshot_binding_mismatch")
        # A copied active WAL database is not a self-contained frozen main file.
        if any(Path(str(database) + suffix).exists() for suffix in ("-wal", "-shm", "-journal")):
            raise QueryError("snapshot_database_not_frozen")
        if database_sha256(database) != manifest.database_sha256:
            raise QueryError("snapshot_database_changed")
        if snapshot_facts(database, source_id) != manifest.facts:
            raise QueryError("snapshot_binding_mismatch")
        with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as connection:
            connection.execute("PRAGMA query_only=ON")
            if manifest.transformation == "pause_and_upgrade_0008_to_0009_v1":
                if connection.execute("SELECT version_num FROM alembic_version").fetchall() != [
                    ("0009_opportunity_review",)
                ]:
                    raise QueryError("snapshot_binding_mismatch")
            if connection.execute(
                "SELECT 1 FROM notification_channel_state WHERE paused=0 LIMIT 1"
            ).fetchone():
                raise QueryError("snapshot_sending_not_paused")
        return manifest
    except ValidationError as exc:
        raise QueryError("snapshot_manifest_invalid") from exc
    except (OSError, sqlite3.Error) as exc:
        raise QueryError("snapshot_unavailable") from exc
