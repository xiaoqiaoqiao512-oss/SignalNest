"""Create private, paused, hash-bound query copies from verified consistent backups.

No production connection, HTTP, mail credentials or collection is used.
Schema upgrades require --upgrade-copy and affect only the private derivative.
The original backup is read only; the destination must not already exist.
"""

import argparse
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import time
from contextlib import closing
from pathlib import Path

from alembic import command
from alembic.script import ScriptDirectory
from alembic.util.exc import CommandError
from pydantic import Field, ValidationError, model_validator
from sqlalchemy.exc import SQLAlchemyError

# This repository-owned utility is also executable as ``python deploy/...py``.
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from deploy.verify_backup import BackupVerificationError, verify_backup
from signalnest.config import ConfigurationError, load_config
from signalnest.contracts import Contract
from signalnest.notifications.profile import ProfileError, load_profile
from signalnest.querying.config import load_query_config
from signalnest.querying.contracts import InstanceID, QueryError
from signalnest.querying.snapshots import (
    QuerySnapshotManifest,
    database_sha256,
    safe_snapshot_path,
    snapshot_facts,
    validate_snapshot,
)
from signalnest.sources import SourceName, source_binding
from signalnest.storage import make_engine, migration_config

UPGRADE_FROM = "0008_list_references"
UPGRADE_TO = "0009_opportunity_review"


class BackupInstance(Contract):
    id: InstanceID
    source_id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,79}$")
    parser: SourceName
    backup_dir: Path
    database: str = Field(
        default="signalnest.sqlite3", pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,79}$"
    )
    snapshot_at: int = Field(strict=True, ge=0)


class SnapshotSpec(Contract):
    format_version: int = Field(default=1, strict=True, ge=1, le=1)
    instances: tuple[BackupInstance, ...] = Field(min_length=1, max_length=8)

    @model_validator(mode="after")
    def distinct_instances(self):
        if len({item.id for item in self.instances}) != len(self.instances):
            raise ValueError("instance IDs must be unique")
        return self


def _private_text(path: Path, value: str):
    with path.open("x", encoding="utf-8") as stream:
        os.chmod(path, 0o600)
        stream.write(value)


def _no_symlink_directory(path: Path):
    if not path.is_dir() or any(part.is_symlink() for part in (path, *path.parents)):
        raise QueryError("snapshot_path_unsafe")


def _instance_config(instance: BackupInstance) -> str:
    binding = source_binding(instance.parser)
    # Fresh allowlisted configuration: never copy SMTP, account, or deployment config.
    return (
        '[storage]\ndata_dir = "."\ndatabase = "signalnest.sqlite3"\n\n'
        f'[source]\nparser = "{instance.parser}"\nid = "{instance.source_id}"\n'
        f'list_url = "https://{binding.host}{binding.list_path}.htm"\n\n'
        "[http]\nconnect_timeout_seconds = 10.0\nread_timeout_seconds = 20.0\n"
        'request_interval_seconds = 5.0\nuser_agent = "SignalNest-Query-Snapshot/1"\n\n'
        "[mail_runtime]\nenabled = false\n"
    )


def _parent_revision(database: Path) -> str:
    safe_snapshot_path(database)
    with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as connection:
        connection.execute("PRAGMA query_only=ON")
        if connection.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
            raise QueryError("snapshot_database_integrity")
        if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise QueryError("snapshot_foreign_keys")
        revisions = connection.execute("SELECT version_num FROM alembic_version").fetchall()
        if len(revisions) != 1:
            raise QueryError("snapshot_revision_unsupported")
        return revisions[0][0]


def _upgrade_query_copy(database: Path) -> None:
    """Explicit migration of our unpublished copy; never initialize paths or raw/."""
    engine = make_engine(database, must_exist=True)
    try:
        with engine.begin() as connection:
            config = migration_config()
            config.attributes["connection"] = connection
            command.upgrade(config, UPGRADE_TO)
    except (SQLAlchemyError, CommandError) as exc:
        raise QueryError("snapshot_copy_upgrade_failed") from exc
    finally:
        engine.dispose()


def prepare_query_snapshot(
    spec: SnapshotSpec,
    output: Path,
    *,
    profile: Path | None = None,
    prepared_at: int | None = None,
    upgrade_copy: bool = False,
) -> dict:
    """Publish one new local directory only after all sources pass validation.

    `snapshot_at` is supplied by the backup producer, not assigned from import time.
    Originals, raw files, mail histories and source timestamps are never rewritten.
    The derived DB retains historical tables but pauses every notification channel.
    The only supported optional upgrade is 0008 -> 0009, in the unpublished copy.
    """
    output = output.expanduser().absolute()
    _no_symlink_directory(output.parent)
    if output.exists() or output.is_symlink():
        raise QueryError("snapshot_destination_exists")
    at = int(time.time()) if prepared_at is None else prepared_at
    if type(at) is not int or at < 0:
        raise QueryError("snapshot_time_invalid")
    if any(item.snapshot_at > at for item in spec.instances):
        raise QueryError("snapshot_time_invalid")
    # Validate before creating anything, and copy only this explicit nonsecret profile.
    if profile is not None:
        profile = profile.expanduser().absolute()
        safe_snapshot_path(profile)
        load_profile(profile)
    stage = Path(tempfile.mkdtemp(prefix=".query-snapshot-", dir=output.parent))
    try:
        reports = []
        for item in spec.instances:
            backup = item.backup_dir.expanduser().absolute()
            _no_symlink_directory(backup)
            marker = backup / "backup.complete"
            safe_snapshot_path(marker)
            original = backup / item.database
            # Reject active journals: input must be the self-contained consistent backup.
            if any(Path(str(original) + s).exists() for s in ("-wal", "-shm", "-journal")):
                raise QueryError("snapshot_database_not_frozen")
            parent_sha = database_sha256(original)
            parent_revision = _parent_revision(original)
            head = ScriptDirectory.from_config(migration_config()).get_current_head()
            needs_upgrade = parent_revision != head
            if needs_upgrade:
                if not upgrade_copy:
                    raise QueryError("snapshot_schema_upgrade_required")
                if (parent_revision, head) != (UPGRADE_FROM, UPGRADE_TO):
                    raise QueryError("snapshot_revision_unsupported")
            else:
                verified = verify_backup(original, backup)
            destination = stage / item.id
            destination.mkdir(mode=0o700)
            database = destination / "signalnest.sqlite3"
            shutil.copyfile(original, database)
            database.chmod(0o600)
            if database_sha256(database) != parent_sha or database_sha256(original) != parent_sha:
                raise QueryError("snapshot_parent_changed")
            with closing(sqlite3.connect(database)) as connection:
                # Frozen copies must not rely on a WAL/rollback journal beside the DB.
                if connection.execute("PRAGMA journal_mode=DELETE").fetchone()[0] != "delete":
                    raise QueryError("snapshot_database_not_frozen")
                connection.execute("PRAGMA foreign_keys=ON")
                with connection:
                    connection.execute(
                        "UPDATE notification_channel_state "
                        "SET paused=1,pause_reason='manual',paused_at=?",
                        (at,),
                    )
                if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
                    raise QueryError("snapshot_binding_mismatch")
            if needs_upgrade:
                _upgrade_query_copy(database)
                # Full verification uses the original raw store without copying it.
                verified = verify_backup(database, backup)
            if database_sha256(original) != parent_sha:
                raise QueryError("snapshot_parent_changed")
            config = destination / "instance.toml"
            _private_text(config, _instance_config(item))
            settings = load_config(config)
            manifest = QuerySnapshotManifest(
                instance_id=item.id,
                database_sha256=database_sha256(database),
                parent_database_sha256=parent_sha,
                snapshot_at=item.snapshot_at,
                prepared_at=at,
                transformation=(
                    "pause_and_upgrade_0008_to_0009_v1"
                    if needs_upgrade
                    else "pause_notification_channels_v1"
                ),
                facts=snapshot_facts(database, item.source_id),
            )
            manifest_path = destination / "snapshot.json"
            _private_text(manifest_path, manifest.model_dump_json(indent=2) + "\n")
            validate_snapshot(manifest_path, settings.storage.database, item.id, item.source_id)
            database.chmod(0o400)
            reports.append(
                {
                    "instance_id": item.id,
                    "database_sha256": manifest.database_sha256,
                    "parent_database_sha256": parent_sha,
                    "snapshot_at": item.snapshot_at,
                    "source_id": item.source_id,
                    "verified_revision": verified["revision"],
                    "parent_revision": parent_revision,
                    "schema_upgraded_in_copy": needs_upgrade,
                }
            )
        if profile is not None:
            _private_text(stage / "profile.toml", profile.read_text(encoding="utf-8"))
        query = 'timezone = "Asia/Shanghai"\n'
        if profile is not None:
            query += 'profile = "profile.toml"\n'
        for item in spec.instances:
            query += (
                f'\n[[instances]]\nid = "{item.id}"\nconfig = "{item.id}/instance.toml"\n'
                f'snapshot_manifest = "{item.id}/snapshot.json"\n'
            )
        _private_text(stage / "ask.local.toml", query)
        load_query_config(stage / "ask.local.toml")
        report = {
            "format_version": 1,
            "kind": "private_frozen_query_snapshot",
            "prepared_at": at,
            "instances": reports,
            "smtp_configuration_copied": False,
            "raw_files_copied": False,
            "all_derived_notification_channels_paused": True,
            "production_pause_state_changed": False,
        }
        _private_text(
            stage / "prepared.json", json.dumps(report, ensure_ascii=False, indent=2) + "\n"
        )
        # A rename publishes a complete local bundle; it is not a power-loss guarantee.
        stage.rename(output)
        return report
    finally:
        if stage.exists():
            shutil.rmtree(stage)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--spec", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--profile", type=Path)
    parser.add_argument(
        "--upgrade-copy",
        action="store_true",
        help="explicitly upgrade only a private 0008 derivative to 0009; originals stay read-only",
    )
    args = parser.parse_args()
    try:
        safe_snapshot_path(args.spec.expanduser().absolute())
        with args.spec.open("rb") as stream:
            raw = stream.read(32 * 1024 + 1)
        if len(raw) > 32 * 1024:
            raise QueryError("snapshot_spec_invalid")
        spec = SnapshotSpec.model_validate_json(raw)
        report = prepare_query_snapshot(
            spec, args.output, profile=args.profile, upgrade_copy=args.upgrade_copy
        )
    except (OSError, sqlite3.Error, ConfigurationError, ProfileError):
        print("查询快照准备失败: snapshot_input_unavailable", file=sys.stderr)
        return 1
    except ValidationError:
        print("查询快照准备失败: snapshot_spec_or_evidence_invalid", file=sys.stderr)
        return 1
    except (QueryError, BackupVerificationError) as exc:
        print(f"查询快照准备失败: {exc.code}", file=sys.stderr)
        return 1
    print(json.dumps(report, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
