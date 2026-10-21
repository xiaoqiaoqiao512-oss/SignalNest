"""Frozen public releases can be checked without reading or modifying instances."""

import gzip
import hashlib
import importlib.util
import io
import json
import tarfile
from pathlib import Path

import pytest
from test_rollout import write_minimal_release

from signalnest.notifications.contracts import canonical_sha256
from signalnest.rollout import (
    RolloutError,
    build_release_manifest,
    create_release_snapshot,
    verify_release_snapshot,
)

ROOT = Path(__file__).resolve().parents[1]


def freeze(tmp_path):
    source = tmp_path / "source"
    write_minimal_release(source)
    result = create_release_snapshot(source, tmp_path / "candidate.tar.gz")
    return source, Path(result["archive"]), Path(result["manifest"])


def revise_archive(archive_path, sidecar_path, transform):
    """Keep outer checksums valid while constructing an invalid archive member."""
    content = io.BytesIO()
    with tarfile.open(archive_path, "r:gz") as original:
        entries = [(entry, original.extractfile(entry).read()) for entry in original]
    with tarfile.open(fileobj=content, mode="w") as archive:
        for entry, data in transform(entries):
            archive.addfile(entry, io.BytesIO(data) if entry.isfile() else None)
    data = gzip.compress(content.getvalue(), mtime=0)
    archive_path.write_bytes(data)
    sidecar = json.loads(sidecar_path.read_text())
    sidecar["archive_sha256"] = hashlib.sha256(data).hexdigest()
    sidecar["archive_bytes"] = len(data)
    sidecar_path.write_text(json.dumps(sidecar))


def tool():
    spec = importlib.util.spec_from_file_location(
        "release_snapshot_verification", ROOT / "deploy/release_snapshot.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_public_query_and_windows_delivery_files_have_explicit_allowlist():
    manifest = build_release_manifest(ROOT)
    expected = {
        "ask.example.toml",
        "ask.proxy.example.toml",
        "ask.wsl.example.toml",
        "config.example.toml",
        "config.cs.example.toml",
        "profile.example.toml",
        "profile.cs-undergrad.example.toml",
        "deploy/evaluate_queries.py",
        "deploy/evaluate_search.py",
        "deploy/prepare_query_snapshot.py",
        "deploy/query_readiness.py",
        "deploy/replay_query_snapshot.py",
        "deploy/windows/repair-keepalive.ps1",
        "deploy/windows/wsl-keepalive.ps1",
        "docs/query-release.md",
        "docs/query-snapshots.md",
        "docs/query-snapshot-replay.md",
        "docs/wsl-query-setup.md",
        "docs/operations.md",
    }
    assert expected <= manifest["file_sha256"].keys()
    for name in expected:
        assert (
            manifest["file_sha256"][name] == hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
        )


def test_optional_public_files_affect_fingerprint_without_reading_private_neighbors(tmp_path):
    source = tmp_path / "source"
    write_minimal_release(source)
    before = build_release_manifest(source)
    (source / "ask.proxy.example.toml").write_text("# public example\n")
    after = build_release_manifest(source)
    assert after["content_sha256"] != before["content_sha256"]
    (source / "deploy/windows").mkdir(parents=True)
    (source / "deploy/windows/operator-private.ps1").symlink_to("/unreadable-private-value")
    (source / "ask.private.toml").write_text("secret-private-api-key")
    (source / "smtp.env").write_text("secret-private-smtp-key")
    (source / "snapshot.sqlite").write_bytes(b"private notice bodies")
    assert build_release_manifest(source) == after


def test_explicit_public_symlink_is_rejected_instead_of_skipped(tmp_path):
    source = tmp_path / "source"
    write_minimal_release(source)
    (source / "ask.proxy.example.toml").symlink_to(source / "absent-private-key")
    with pytest.raises(RolloutError, match="release_path_unsafe"):
        build_release_manifest(source)


def test_verification_checks_legacy_v1_archive_and_unpacked_tree_read_only(tmp_path):
    source, archive, sidecar = freeze(tmp_path)
    before = {p.name: p.read_bytes() for p in tmp_path.iterdir() if p.is_file()}
    frozen = json.loads(sidecar.read_text())
    result = verify_release_snapshot(
        archive,
        repo_root=source,
        expected_content_sha256=frozen["release"]["content_sha256"],
    )
    assert result["source_matches"] is True
    assert result["runtime_code_matches_release"] is False
    assert result["file_count"] == 4
    assert result["archive_sha256"] == frozen["archive_sha256"]
    assert result["content_sha256"] == frozen["release"]["content_sha256"]
    assert {p.name: p.read_bytes() for p in tmp_path.iterdir() if p.is_file()} == before
    assert verify_release_snapshot(archive)["source_matches"] is None


def test_frozen_source_can_be_extracted_checked_and_reconstructed_identically(tmp_path):
    source, archive, sidecar = freeze(tmp_path)
    verify_release_snapshot(archive)
    restored = tmp_path / "restored"
    restored.mkdir()
    with tarfile.open(archive, "r:gz") as package:
        package.extractall(restored, filter="data")
    result = verify_release_snapshot(archive, repo_root=restored / "signalnest")
    assert result["source_matches"] is True
    second = create_release_snapshot(restored / "signalnest", tmp_path / "restored.tar.gz")
    assert Path(second["archive"]).read_bytes() == archive.read_bytes()
    assert json.loads(sidecar.read_text())["release"]["content_sha256"] == second["content_sha256"]
    assert (source / "src/signalnest/__init__.py").is_file()


@pytest.mark.parametrize("change", ["changed", "missing", "extra"])
def test_changed_missing_or_added_allowlisted_source_is_rejected(tmp_path, change):
    source, archive, _ = freeze(tmp_path)
    code = source / "src/signalnest/__init__.py"
    if change == "changed":
        code.write_text("# changed code\n")
    elif change == "missing":
        code.unlink()
    else:
        (code.parent / "new.py").write_text("# additional code\n")
    with pytest.raises(RolloutError, match="release_source_mismatch"):
        verify_release_snapshot(archive, repo_root=source)


def test_snapshot_fingerprint_can_be_compared_to_independently_recorded_expected_value(tmp_path):
    _, archive, _ = freeze(tmp_path)
    with pytest.raises(RolloutError, match="release_content_mismatch"):
        verify_release_snapshot(archive, expected_content_sha256="f" * 64)
    with pytest.raises(RolloutError, match="release_expected_content_invalid"):
        verify_release_snapshot(archive, expected_content_sha256="not a fingerprint")


@pytest.mark.parametrize("target", ["archive", "sidecar"])
def test_missing_or_symlinked_evidence_is_rejected_without_reading_private_target(tmp_path, target):
    _, archive, sidecar = freeze(tmp_path)
    destination = archive if target == "archive" else sidecar
    destination.unlink()
    with pytest.raises(RolloutError, match="release_file_unavailable"):
        verify_release_snapshot(archive)
    destination.symlink_to(tmp_path / "missing-secret")
    with pytest.raises(RolloutError, match="release_path_unsafe"):
        verify_release_snapshot(archive)


def test_damaged_archive_has_finite_error(tmp_path):
    _, archive, _ = freeze(tmp_path)
    archive.write_bytes(b"damaged archive")
    with pytest.raises(RolloutError, match="release_archive_mismatch"):
        verify_release_snapshot(archive)


@pytest.mark.parametrize("bad_manifest", ["not-json", "[]", '{"format":"other"}'])
def test_invalid_sidecar_has_finite_error(tmp_path, bad_manifest):
    _, archive, sidecar = freeze(tmp_path)
    sidecar.write_text(bad_manifest)
    with pytest.raises(RolloutError, match="release_manifest_invalid"):
        verify_release_snapshot(archive)


def test_manifest_cannot_request_private_files_even_with_a_recomputed_content_hash(tmp_path):
    _, archive, sidecar = freeze(tmp_path)
    saved = json.loads(sidecar.read_text())
    saved["release"]["file_sha256"]["smtp.env"] = "a" * 64
    saved["release"]["content_sha256"] = canonical_sha256(saved["release"]["file_sha256"])
    sidecar.write_text(json.dumps(saved))
    with pytest.raises(RolloutError, match="release_manifest_invalid"):
        verify_release_snapshot(archive)


@pytest.mark.parametrize(
    "kind", ["extra", "duplicate", "symlink", "traversal", "missing", "mode", "owner", "pax"]
)
def test_archive_member_shape_is_checked_even_when_outer_digest_matches(tmp_path, kind):
    _, archive, sidecar = freeze(tmp_path)

    def changed(entries):
        if kind == "missing":
            return entries[1:]
        if kind == "duplicate":
            return entries + [entries[0]]
        if kind == "mode":
            entries[0][0].mode = 0o4755
            return entries
        if kind == "owner":
            entries[0][0].uid = 123
            return entries
        if kind == "pax":
            entries[0][0].pax_headers["comment"] = "unexpected header"
            return entries
        name = "signalnest/extra.txt" if kind != "traversal" else "../escaped.py"
        entry = tarfile.TarInfo(name)
        if kind == "symlink":
            entry.name = entries[0][0].name
            entry.type = tarfile.SYMTYPE
            entry.linkname = "/private-file"
            return [(entry, b"")] + entries[1:]
        return entries + [(entry, b"")]

    revise_archive(archive, sidecar, changed)
    code = "release_archive_incomplete" if kind == "missing" else "release_archive_unsafe"
    with pytest.raises(RolloutError, match=code):
        verify_release_snapshot(archive)
    assert not (tmp_path / "escaped.py").exists()


def test_member_body_checksum_is_independently_checked(tmp_path):
    _, archive, sidecar = freeze(tmp_path)

    def changed(entries):
        for entry, data in entries:
            if entry.name == "signalnest/README.md":
                data = b"Different content"
                entry.size = len(data)
            yield entry, data

    revise_archive(archive, sidecar, changed)
    with pytest.raises(RolloutError, match="release_archive_mismatch"):
        verify_release_snapshot(archive)


def test_embedded_manifest_must_match_sidecar(tmp_path):
    _, archive, sidecar = freeze(tmp_path)

    def changed(entries):
        for entry, data in entries:
            if entry.name == "signalnest/RELEASE.json":
                embedded = json.loads(data)
                embedded["format"] = "other"
                data = json.dumps(embedded).encode()
                entry.size = len(data)
            yield entry, data

    revise_archive(archive, sidecar, changed)
    with pytest.raises(RolloutError, match="release_manifest_mismatch"):
        verify_release_snapshot(archive)


def test_decompression_limit_applies_before_tar_extension_parsing(tmp_path, monkeypatch):
    _, archive, _ = freeze(tmp_path)
    monkeypatch.setattr("signalnest.rollout.MAX_RELEASE_CONTENT_BYTES", 128)
    with pytest.raises(RolloutError, match="release_archive_too_large"):
        verify_release_snapshot(archive)


def test_tool_verification_real_result_and_clear_failure(tmp_path, capsys):
    source, archive, _ = freeze(tmp_path)
    module = tool()
    arguments = ["--verify", str(archive), "--release-root", str(source)]
    assert module.main(arguments) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["source_matches"] is True
    (source / "uv.lock").write_text("version = 2\n")
    assert module.main(arguments) == 1
    captured = capsys.readouterr()
    assert not captured.out
    assert json.loads(captured.err) == {"error_code": "release_source_mismatch"}


@pytest.mark.parametrize(
    "arguments", [["--output", "/absent/archive.tar.gz"], ["--verify", "x", "--output", "y"]]
)
def test_tool_invalid_modes_return_usage_error_without_file_io(arguments):
    with pytest.raises(SystemExit) as exc:
        tool().main(arguments)
    assert exc.value.code == 2
