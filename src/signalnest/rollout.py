"""Read-only rollout evidence; local preparation never proves external acceptance.

No credentials, notice bodies, addresses, migration or writes are needed here.
An operator appends returned observations to a separate log, outside the instance.
"""

import gzip
import hashlib
import io
import json
import os
import platform
import re
import sqlite3
import stat
import subprocess
import tarfile
import tempfile
import tomllib
import zlib
from collections.abc import Mapping
from pathlib import Path

import sqlalchemy as sa
from alembic.script import ScriptDirectory
from sqlalchemy.exc import SQLAlchemyError

from signalnest.config import Settings
from signalnest.errors import IngestError, validate_time
from signalnest.mail.contracts import MailError
from signalnest.mail.sending import mail_status
from signalnest.notifications.contracts import canonical_sha256
from signalnest.notifications.decision import policy_manifest
from signalnest.notifications.profile import ProfileError, load_profile
from signalnest.schema import (
    mail_delivery,
    mail_messages,
    mail_plan_errors,
    notification_channel_state,
    notification_events,
    notification_policy_revisions,
)
from signalnest.status import _read_only_engine, _safe_error, inspect_status
from signalnest.storage import StorageError, migration_config

MAX_RELEASE_FILE_BYTES = 16 * 1024 * 1024
MAX_RELEASE_ARCHIVE_BYTES = 64 * 1024 * 1024
MAX_RELEASE_CONTENT_BYTES = 128 * 1024 * 1024
MAX_RELEASE_FILES = 4096
RELEASE_REQUIRED_FILES = frozenset(
    {"pyproject.toml", "uv.lock", "README.md", "src/signalnest/__init__.py"}
)
# Explicit public examples and tools only. Never widen this to *.toml, *.env,
# deploy/** or docs/**: those trees may contain local data or personal evidence.
RELEASE_PUBLIC_FILES = frozenset(
    {
        "ask.example.toml",
        "ask.proxy.example.toml",
        "ask.wsl.example.toml",
        "config.example.toml",
        "config.cs.example.toml",
        "profile.example.toml",
        "profile.cs-undergrad.example.toml",
        "deploy/verify_backup.py",
        "deploy/evaluate_notifications.py",
        "deploy/evaluate_queries.py",
        "deploy/evaluate_search.py",
        "deploy/prepare_query_snapshot.py",
        "deploy/replay_query_snapshot.py",
        "deploy/ask_saved.py",
        "deploy/query_readiness.py",
        "deploy/release_snapshot.py",
        "deploy/observe_instance.py",
        "deploy/summarize_observations.py",
        "deploy/human_review.py",
        "deploy/windows/repair-keepalive.ps1",
        "deploy/windows/wsl-keepalive.ps1",
        "docs/query-release.md",
        "docs/query-agent.md",
        "docs/query-snapshots.md",
        "docs/query-snapshot-replay.md",
        "docs/wsl-query-setup.md",
        "docs/operations.md",
    }
)
EXTERNAL_VERIFICATIONS = (
    "target_service_and_credentials_injection",
    "live_complete_scan_and_activation_baseline",
    "recipient_confirmed_immediate_and_digest_receipt",
    "isolated_restart_timeout_and_restore",
    "seven_days_continuous_observation",
)


class RolloutError(RuntimeError):
    """A finite local evidence failure, without filesystem or subprocess output."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _file_bytes(path: Path, root: Path, *, limit: int = MAX_RELEASE_FILE_BYTES) -> bytes:
    if not hasattr(os, "O_NOFOLLOW") or not hasattr(os, "O_NONBLOCK"):
        raise RolloutError("release_platform_unsupported")
    if path.is_symlink() or any(parent.is_symlink() for parent in path.parents if parent != root):
        raise RolloutError("release_path_unsafe")
    try:
        if not path.resolve().is_relative_to(root):
            raise RolloutError("release_path_unsafe")
        # A FIFO must fail fstat instead of waiting for a writer during open.
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, "rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise RolloutError("release_path_unsafe")
            content = stream.read(limit + 1)
    except OSError as exc:
        raise RolloutError("release_file_unavailable") from exc
    if len(content) > limit:
        raise RolloutError("release_file_too_large")
    return content


def _file_hash(path: Path, root: Path) -> str:
    return hashlib.sha256(_file_bytes(path, root)).hexdigest()


def _git(root: Path, *arguments: str) -> str | None:
    try:
        result = subprocess.run(
            ["git", "--no-optional-locks", "-C", str(root), *arguments],
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout.strip() if result.returncode == 0 else None


def _package_hashes(package: Path) -> dict[str, str]:
    return {
        str(path.relative_to(package)): _file_hash(path, package)
        for path in sorted(package.rglob("*.py"))
    }


def build_release_manifest(repo_root: Path) -> dict:
    """Hash a fixed code/lock/template allowlist, never personal files or research.

    HEAD is supplementary: modified and untracked allowlisted code is included in
    the content fingerprint. Retain those exact files to reproduce a dirty release.
    This creates evidence, not an archive, commit, tag or installed release.
    """
    if repo_root.is_symlink():
        raise RolloutError("release_path_unsafe")
    root = repo_root.expanduser().resolve()
    package = root / "src/signalnest"
    if not package.is_dir() or not (root / "pyproject.toml").is_file():
        raise RolloutError("release_root_invalid")
    try:
        project = tomllib.loads(_file_bytes(root / "pyproject.toml", root).decode("utf-8"))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
        raise RolloutError("release_root_invalid") from exc
    project_metadata = project.get("project")
    if not isinstance(project_metadata, dict) or project_metadata.get("name") != "signalnest":
        raise RolloutError("release_root_invalid")
    paths = {root / "pyproject.toml", root / "uv.lock", root / "README.md"}
    paths.update(package.rglob("*.py"))
    paths.update((package / "migrations").glob("*.mako"))
    paths.update((root / "deploy/systemd").glob("*.service"))
    paths.update((root / "deploy/systemd").glob("*.timer"))
    paths.update((root / "deploy/systemd").glob("*.conf"))
    for name in RELEASE_PUBLIC_FILES:
        candidate = root / name
        if candidate.exists() or candidate.is_symlink():
            paths.add(candidate)
    hashes = {str(path.relative_to(root)): _file_hash(path, root) for path in sorted(paths)}
    head = _git(root, "rev-parse", "HEAD")
    if head is not None and not re.fullmatch(r"[a-f0-9]{40,64}", head):
        head = None
    dirty = _git(root, "status", "--porcelain=v1", "--untracked-files=all", "--", *hashes)
    # Compare the runtime package too: a supplied checkout is not proof that this
    # process executes that checkout. Installation layout does not enter the hash.
    package_hashes = {
        key.removeprefix("src/signalnest/"): value
        for key, value in hashes.items()
        if key.startswith("src/signalnest/") and key.endswith(".py")
    }
    runtime_matches = package_hashes == _package_hashes(Path(__file__).resolve().parent)
    return {
        "format": "signalnest-release-v1",
        "git_head": head,
        "allowlisted_worktree_dirty": bool(dirty) if dirty is not None else None,
        "content_sha256": canonical_sha256(hashes),
        "file_sha256": hashes,
        "uv_lock_sha256": hashes["uv.lock"],
        "runtime_code_matches_release": runtime_matches,
        "runtime": {
            "python": platform.python_version(),
            "implementation": platform.python_implementation(),
            "sqlite": sqlite3.sqlite_version,
            "zlib": zlib.ZLIB_RUNTIME_VERSION,
            "platform": platform.system(),
            "machine": platform.machine(),
        },
        "schema_head": ScriptDirectory.from_config(migration_config()).get_current_head(),
    }


def _archive_entry(archive: tarfile.TarFile, name: str, content: bytes) -> None:
    entry = tarfile.TarInfo(name)
    entry.size = len(content)
    entry.mode = 0o644
    entry.mtime = 0
    entry.uid = entry.gid = 0
    entry.uname = entry.gname = ""
    archive.addfile(entry, io.BytesIO(content))


def _output_path(path: Path, root: Path) -> Path:
    expanded = path.expanduser().absolute()
    if expanded.is_symlink() or any(parent.is_symlink() for parent in expanded.parents):
        raise RolloutError("release_output_unsafe")
    target = expanded.resolve()
    if target.is_relative_to(root):
        raise RolloutError("release_output_inside_source")
    if not target.parent.is_dir():
        raise RolloutError("release_output_parent_missing")
    if target.exists():
        raise RolloutError("release_output_exists")
    return target


def create_release_snapshot(repo_root: Path, output: Path) -> dict:
    """Save actual allowlisted bytes, including uncommitted code, without overwrite.

    The gzip/tar bytes depend only on input file names and bytes. Runtime/Git facts
    live in a sidecar, never in the deterministic archive. Each file is published
    atomically with a same-directory hard link, but the pair is not a cross-file
    atomic transaction. Both must exist and match before declaring a release saved.
    Interrupted incomplete pairs are retained for diagnosis, not silently reused.
    """
    evidence = build_release_manifest(repo_root)
    _validated_release_hashes(evidence)
    root = repo_root.expanduser().resolve()
    if not output.name.endswith(".tar.gz"):
        raise RolloutError("release_output_suffix_invalid")
    target = _output_path(output, root)
    sidecar = _output_path(target.with_name(target.name + ".manifest.json"), root)
    content_manifest = {
        "format": "signalnest-release-content-v1",
        "content_sha256": evidence["content_sha256"],
        "file_sha256": evidence["file_sha256"],
        "uv_lock_sha256": evidence["uv_lock_sha256"],
    }
    temporary_paths = []
    published = []
    try:
        with tempfile.NamedTemporaryFile(
            prefix=".signalnest-release-", dir=target.parent, delete=False
        ) as stream:
            temporary_archive = Path(stream.name)
            temporary_paths.append(temporary_archive)
            with gzip.GzipFile(filename="", fileobj=stream, mode="wb", mtime=0) as compressed:
                with tarfile.open(
                    fileobj=compressed, mode="w", format=tarfile.PAX_FORMAT
                ) as archive:
                    for relative in sorted((*evidence["file_sha256"], "RELEASE.json")):
                        if relative == "RELEASE.json":
                            data = (
                                json.dumps(
                                    content_manifest,
                                    sort_keys=True,
                                    ensure_ascii=False,
                                    separators=(",", ":"),
                                ).encode("utf-8")
                                + b"\n"
                            )
                        else:
                            data = _file_bytes(root / relative, root)
                            if (
                                hashlib.sha256(data).hexdigest()
                                != evidence["file_sha256"][relative]
                            ):
                                raise RolloutError("release_changed_during_snapshot")
                        _archive_entry(archive, "signalnest/" + relative, data)
            stream.flush()
            os.fsync(stream.fileno())
        archive_bytes = temporary_archive.read_bytes()
        archive_sha256 = hashlib.sha256(archive_bytes).hexdigest()
        saved_manifest = {
            "format": "signalnest-release-snapshot-v1",
            "archive_sha256": archive_sha256,
            "archive_bytes": len(archive_bytes),
            "release": evidence,
        }
        with tempfile.NamedTemporaryFile(
            prefix=".signalnest-release-", dir=target.parent, delete=False
        ) as stream:
            temporary_manifest = Path(stream.name)
            temporary_paths.append(temporary_manifest)
            stream.write(
                json.dumps(saved_manifest, sort_keys=True, ensure_ascii=False, indent=2).encode(
                    "utf-8"
                )
                + b"\n"
            )
            stream.flush()
            os.fsync(stream.fileno())
        # link refuses an existing name, including a symlink inserted after checks.
        for temporary, destination in ((temporary_archive, target), (temporary_manifest, sidecar)):
            os.link(temporary, destination, follow_symlinks=False)
            published.append(destination)
        directory_fd = os.open(target.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except FileExistsError as exc:
        raise RolloutError("release_output_exists") from exc
    except OSError as exc:
        raise RolloutError("release_snapshot_write_failed") from exc
    finally:
        # Clean up files published by this call on an ordinary failure. Abrupt
        # process termination can still leave a recognizable incomplete pair.
        if len(published) != 2:
            for destination in published:
                destination.unlink(missing_ok=True)
        for temporary in temporary_paths:
            temporary.unlink(missing_ok=True)
    return {
        "archive": str(target),
        "manifest": str(sidecar),
        "archive_sha256": archive_sha256,
        "content_sha256": evidence["content_sha256"],
        "git_head": evidence["git_head"],
        "allowlisted_worktree_dirty": evidence["allowlisted_worktree_dirty"],
    }


def _release_name_allowed(name: str) -> bool:
    return bool(
        name in RELEASE_REQUIRED_FILES | RELEASE_PUBLIC_FILES
        or re.fullmatch(r"src/signalnest/(?:[A-Za-z0-9_]+/)*[A-Za-z0-9_]+\.py", name)
        or re.fullmatch(r"src/signalnest/migrations/[A-Za-z0-9_.-]+\.mako", name)
        or re.fullmatch(r"deploy/systemd/[A-Za-z0-9_@.-]+\.(?:service|timer|conf)", name)
    )


def _validated_release_hashes(manifest: object) -> dict[str, str]:
    if not isinstance(manifest, dict):
        raise RolloutError("release_manifest_invalid")
    hashes = manifest.get("file_sha256")
    if (
        not isinstance(hashes, dict)
        or not RELEASE_REQUIRED_FILES.issubset(hashes)
        or len(hashes) > MAX_RELEASE_FILES
        or any(
            not isinstance(name, str)
            or not _release_name_allowed(name)
            or not isinstance(digest, str)
            or not re.fullmatch(r"[a-f0-9]{64}", digest)
            for name, digest in hashes.items()
        )
        or manifest.get("content_sha256") != canonical_sha256(hashes)
        or manifest.get("uv_lock_sha256") != hashes["uv.lock"]
    ):
        raise RolloutError("release_manifest_invalid")
    return hashes


def verify_release_snapshot(
    snapshot: Path,
    *,
    repo_root: Path | None = None,
    expected_content_sha256: str | None = None,
) -> dict:
    """Check a frozen archive/sidecar, optionally against an unpacked source tree.

    No extraction, installation, instance access or writes occur. This verifies
    consistency, not publisher authenticity: retain the expected fingerprint via
    an independent trusted channel. Old v1 snapshots remain verifiable.
    """
    if expected_content_sha256 is not None and not re.fullmatch(
        r"[a-f0-9]{64}", expected_content_sha256
    ):
        raise RolloutError("release_expected_content_invalid")
    archive_path = snapshot.expanduser().absolute()
    root = archive_path.parent.resolve()
    sidecar = archive_path.with_name(archive_path.name + ".manifest.json")
    try:
        saved = json.loads(_file_bytes(sidecar, root))
    except (ValueError, UnicodeDecodeError) as exc:
        raise RolloutError("release_manifest_invalid") from exc
    if not isinstance(saved, dict) or saved.get("format") != "signalnest-release-snapshot-v1":
        raise RolloutError("release_manifest_invalid")
    evidence = saved.get("release")
    hashes = _validated_release_hashes(evidence)
    if evidence.get("format") != "signalnest-release-v1":
        raise RolloutError("release_manifest_invalid")
    content_sha256 = evidence["content_sha256"]
    if expected_content_sha256 is not None and content_sha256 != expected_content_sha256:
        raise RolloutError("release_content_mismatch")
    data = _file_bytes(archive_path, root, limit=MAX_RELEASE_ARCHIVE_BYTES)
    archive_sha256 = hashlib.sha256(data).hexdigest()
    if (
        type(saved.get("archive_bytes")) is not int
        or saved["archive_bytes"] != len(data)
        or saved.get("archive_sha256") != archive_sha256
    ):
        raise RolloutError("release_archive_mismatch")
    expected_names = {"signalnest/" + name for name in hashes} | {"signalnest/RELEASE.json"}
    seen = set()
    total_bytes = 0
    try:
        # Bound gzip expansion before tarfile parses extension headers as well as
        # normal files; a large PAX header must not bypass the per-entry checks.
        with gzip.GzipFile(fileobj=io.BytesIO(data), mode="rb") as compressed:
            unpacked = compressed.read(MAX_RELEASE_CONTENT_BYTES + 1)
        if len(unpacked) > MAX_RELEASE_CONTENT_BYTES:
            raise RolloutError("release_archive_too_large")
        with tarfile.open(fileobj=io.BytesIO(unpacked), mode="r:") as archive:
            for entry in archive:
                if (
                    entry.name not in expected_names
                    or entry.name in seen
                    or not entry.isfile()
                    or entry.mode != 0o644
                    or entry.uid != 0
                    or entry.gid != 0
                    or entry.mtime != 0
                    or entry.uname != ""
                    or entry.gname != ""
                    or set(entry.pax_headers) - {"path"}
                ):
                    raise RolloutError("release_archive_unsafe")
                total_bytes += entry.size
                if entry.size > MAX_RELEASE_FILE_BYTES or total_bytes > MAX_RELEASE_CONTENT_BYTES:
                    raise RolloutError("release_archive_too_large")
                seen.add(entry.name)
                stream = archive.extractfile(entry)
                if stream is None:
                    raise RolloutError("release_archive_invalid")
                with stream:
                    content = stream.read(MAX_RELEASE_FILE_BYTES + 1)
                if len(content) != entry.size:
                    raise RolloutError("release_archive_invalid")
                if entry.name == "signalnest/RELEASE.json":
                    embedded = json.loads(content)
                    if (
                        not isinstance(embedded, dict)
                        or embedded.get("format") != "signalnest-release-content-v1"
                        or _validated_release_hashes(embedded) != hashes
                        or embedded.get("content_sha256") != content_sha256
                    ):
                        raise RolloutError("release_manifest_mismatch")
                elif (
                    hashlib.sha256(content).hexdigest()
                    != hashes[entry.name.removeprefix("signalnest/")]
                ):
                    raise RolloutError("release_archive_mismatch")
    except (tarfile.TarError, OSError, EOFError, ValueError, UnicodeDecodeError, zlib.error) as exc:
        raise RolloutError("release_archive_invalid") from exc
    if seen != expected_names:
        raise RolloutError("release_archive_incomplete")
    runtime_matches = None
    if repo_root is not None:
        actual = build_release_manifest(repo_root)
        if actual["file_sha256"] != hashes:
            raise RolloutError("release_source_mismatch")
        runtime_matches = actual["runtime_code_matches_release"]
    return {
        "format": "signalnest-release-verification-v1",
        "archive_sha256": archive_sha256,
        "content_sha256": content_sha256,
        "file_count": len(hashes),
        "source_matches": True if repo_root is not None else None,
        "runtime_code_matches_release": runtime_matches,
    }


def _check(code: str, state: str) -> dict[str, str]:
    return {"code": code, "state": state}


def writing_platform_supported() -> bool:
    """Instance writers require the existing POSIX advisory lock and raw store."""
    return os.name == "posix"


def _mail_snapshot(settings: Settings, at: int, profile) -> dict:
    engine = _read_only_engine(settings.storage.database)
    try:
        with engine.connect() as connection:
            channel = (
                connection.execute(sa.select(notification_channel_state)).mappings().one_or_none()
            )
            if channel is None:
                return {"activated": False, "source_matches": None}
            if channel["source_id"] != settings.source.id:
                return {"activated": True, "source_matches": False}
            active_policy = connection.execute(
                sa.select(notification_policy_revisions.c.policy_sha256).where(
                    notification_policy_revisions.c.id == channel["policy_revision_id"]
                )
            ).scalar_one()
            accepted_count, max_delay = connection.execute(
                sa.select(
                    sa.func.count(),
                    sa.func.max(mail_delivery.c.accepted_at - mail_messages.c.frozen_at),
                )
                .select_from(mail_messages.join(mail_delivery))
                .where(
                    mail_messages.c.installation_id == channel["installation_id"],
                    mail_delivery.c.state == "accepted",
                )
            ).one()
            blocked_plan_items = connection.execute(
                sa.select(sa.func.count())
                .select_from(mail_plan_errors)
                .join(notification_events, notification_events.c.id == mail_plan_errors.c.event_id)
                .where(notification_events.c.installation_id == channel["installation_id"])
            ).scalar_one()
        status = mail_status(engine, settings.source.id, at=at)
    except (SQLAlchemyError, MailError) as exc:
        raise RolloutError("rollout_mail_read_failed") from exc
    finally:
        engine.dispose()
    oldest = status["oldest_pending_at"]
    return {
        "activated": True,
        "source_matches": True,
        "active_policy_sha256": active_policy,
        "profile_policy_matches": (
            canonical_sha256(policy_manifest(profile)) == active_policy
            if profile is not None
            else None
        ),
        "paused": status["paused"],
        "pause_reason": _safe_error(status["pause_reason"]),
        "counts": status["counts"],
        "due_count": status["due_count"],
        "uncertain_count": status["uncertain_count"],
        "uncertain_attempt_count": status["uncertain_attempt_count"],
        "unplanned_immediate": status["unplanned_immediate"],
        "unallocated_digest": status["unallocated_digest"],
        "plan_error_records": blocked_plan_items,
        "oldest_nonaccepted_frozen_at": oldest,
        "oldest_nonaccepted_wait_seconds": max(0, at - oldest) if oldest is not None else None,
        "accepted_count": accepted_count,
        "max_frozen_to_local_acceptance_seconds": max_delay,
        "recent_error_codes": sorted(
            {_safe_error(item["error_code"]) for item in status["messages"] if item["error_code"]}
        ),
        "message_diagnostics_truncated": status["messages_truncated"],
    }


def inspect_rollout(
    settings: Settings,
    *,
    at: int,
    release_root: Path,
    profile_path: Path | None = None,
    profile_confirmed: bool = False,
    environment: Mapping[str, str] | None = None,
) -> dict:
    """Return local checks and operational evidence without creating or repairing.

    profile_confirmed is only an operator declaration about the supplied profile.
    It cannot verify qualification, deployment, SMTP acceptance, receipt or uptime.
    Credentials are inspected only for nonempty presence, never serialized or hashed.
    Individual status/mail reads are separate read-only snapshots, not one global
    transaction; concurrent committed writes may appear between them.
    """
    try:
        validate_time(at)
    except IngestError as exc:
        raise RolloutError("rollout_time_invalid") from exc
    if type(profile_confirmed) is not bool:
        raise RolloutError("profile_confirmation_invalid")
    writing_supported = writing_platform_supported()
    checks = []
    checks.append(_check("writing_platform_supported", "pass" if writing_supported else "blocked"))
    try:
        release = build_release_manifest(release_root)
        checks.append(
            _check(
                "release_matches_runtime",
                "pass" if release["runtime_code_matches_release"] else "blocked",
            )
        )
        checks.append(
            _check(
                "python_312",
                "pass" if platform.python_version_tuple()[:2] == ("3", "12") else "blocked",
            )
        )
    except RolloutError as exc:
        release = {"error_code": exc.code}
        checks.append(_check(exc.code, "blocked"))
    profile = None
    if profile_path is None:
        checks.append(_check("personal_profile_missing", "blocked"))
    else:
        try:
            profile = load_profile(profile_path)
            checks.append(_check("profile_valid", "pass"))
        except ProfileError:
            checks.append(_check("profile_invalid", "blocked"))
    checks.append(
        _check(
            "personal_profile_operator_declaration",
            "pass" if profile_confirmed and profile is not None else "blocked",
        )
    )
    profile_info = {
        "provided": profile_path is not None,
        "valid": profile is not None,
        "operator_declared_personal": profile_confirmed,
        "sha256": profile.sha256() if profile is not None else None,
    }
    try:
        collection = inspect_status(settings, at=at).model_dump(mode="json")
        checks.append(_check("database_initialized", "pass"))
    except StorageError:
        collection = {"error_code": "storage_unavailable_or_uninitialized"}
        checks.append(_check("database_initialized", "blocked"))
    raw = settings.storage.data_dir / "raw"
    raw_available = raw.is_dir() and not raw.is_symlink()
    checks.append(_check("raw_directory_available", "pass" if raw_available else "blocked"))
    mail = {"activated": False, "source_matches": None}
    if "error_code" not in collection:
        try:
            mail = _mail_snapshot(settings, at, profile)
        except RolloutError as exc:
            mail = {"error_code": exc.code}
            checks.append(_check(exc.code, "blocked"))
    if mail.get("source_matches") is False:
        checks.append(_check("notification_source_mismatch", "blocked"))
    elif not mail.get("activated"):
        checks.append(_check("notifications_not_activated", "attention"))
    elif mail.get("profile_policy_matches") is False:
        checks.append(_check("active_policy_differs", "attention"))
    if mail.get("paused"):
        checks.append(_check("mail_sending_paused", "attention"))
    smtp = settings.smtp
    environment = os.environ if environment is None else environment
    credentials = {
        "configured": bool(smtp and smtp.username_env),
        "username_present": bool(environment.get(smtp.username_env))
        if smtp and smtp.username_env
        else None,
        "password_present": bool(environment.get(smtp.password_env))
        if smtp and smtp.password_env
        else None,
    }
    credential_ready = not credentials["configured"] or (
        credentials["username_present"] and credentials["password_present"]
    )
    checks.append(_check("smtp_configured", "pass" if smtp is not None else "attention"))
    if smtp is not None:
        checks.append(
            _check("smtp_credentials_present", "pass" if credential_ready else "attention")
        )
    checks.append(
        _check("background_mail_enabled", "pass" if settings.mail_runtime.enabled else "attention")
    )
    return {
        "format": "signalnest-rollout-v1",
        "at": at,
        "source_id": settings.source.id,
        "prepared": not any(check["state"] == "blocked" for check in checks),
        "mail_locally_ready": bool(
            writing_supported
            and smtp is not None
            and credential_ready
            and settings.mail_runtime.enabled
            and mail.get("activated")
            and mail.get("source_matches")
            and mail.get("profile_policy_matches")
            and not mail.get("paused")
        ),
        "externally_verified": False,
        "external_verifications_required": list(EXTERNAL_VERIFICATIONS),
        "checks": checks,
        "release": release,
        "profile": profile_info,
        "smtp_credentials": credentials,
        "collection": collection,
        "mail": mail,
        "read_consistency": "separate_read_only_snapshots",
    }


def observe_instance(settings: Settings, **options) -> dict:
    """Emit one explicit-time observation; never start a monitor or append a file."""
    report = inspect_rollout(settings, **options)
    report["format"] = "signalnest-observation-v1"
    # Round-trip gives callers an ordinary JSON-only value; no paths or models leak.
    return json.loads(json.dumps(report, ensure_ascii=False, sort_keys=True))
