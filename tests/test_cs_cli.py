"""Explicit CS local decision preview stays separate from source collection."""

import builtins
import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import httpx
import pytest
from test_cs_parsing import FIXTURES, NOTICE, NOTICE_URL, capture

from signalnest.cli import main
from signalnest.cs_parsing import parse_cs_notice
from signalnest.notifications.profile import load_profile

ROOT = Path(__file__).resolve().parents[1]
PROFILE = ROOT / "profile.cs-undergrad.example.toml"


def arguments(*, parser="cs-undergrad-notices", file=None, url=NOTICE_URL):
    args = [
        "decision-preview",
        "--profile",
        str(PROFILE),
        "--file",
        str(file or FIXTURES / f"{NOTICE}.html"),
        "--url",
        url,
        "--at",
        "2026-10-10T09:00:00+08:00",
        "--next-digest-at",
        "2026-10-11T09:00:00+08:00",
    ]
    if parser is not None:
        args += ["--parser", parser]
    return args


def test_real_cs_preview_and_profile_check_preserve_explicit_unknowns(capsys):
    assert main(["profile-check", "--profile", str(PROFILE)]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["profile_valid"] and result["profile_sha256"] == load_profile(PROFILE).sha256()
    assert main(arguments()) == 0
    captured = capsys.readouterr()
    result = json.loads(captured.out)
    assert captured.err == "" and result["preview_only"]
    assert "body_text" not in result["facts"]
    assert result["facts"]["title"].startswith("2026-2027学年第一学期计算机学院")
    assert result["facts"]["published_date"] == "2026-07-13"
    assert result["facts"]["deadline_at"] is None
    assert not result["facts"]["opening_confirmed"]
    assert result["decision"]["action"] == "DIGEST"
    assert result["decision"]["eligibility"] == result["decision"]["time_status"] == "unknown"
    assert result["decision"]["needs_review"]
    assert any(
        item["code"] == "recruitment_year_conflict" for item in result["decision"]["unknowns"]
    )
    assert result["decision"]["rules_version"] == "notification-rules-v10"


@pytest.mark.parametrize("parser", [None, "whu-student-notices", "ems-notices"])
def test_no_parser_guess_or_fallback_for_cs_input(parser, capsys):
    assert main(arguments(parser=parser)) == 1
    captured = capsys.readouterr()
    assert captured.out == "" and "unsupported_identity" in captured.err
    assert NOTICE_URL not in captured.err and "<html" not in captured.err


@pytest.mark.parametrize(
    "url",
    [
        "https://uc.whu.edu.cn/info/1517/64481.htm",
        "https://cs.whu.edu.cn/system/resource/code/auth/caslogin.jsp",
    ],
)
def test_cs_parser_rejects_wrong_source_or_login_identity(url, capsys):
    assert main(arguments(url=url)) == 1
    captured = capsys.readouterr()
    assert "unsupported_identity" in captured.err and captured.out == ""


def test_cs_html_parser_option_does_not_apply_to_normalized_json(tmp_path, capsys):
    path = tmp_path / "notice.json"
    path.write_text(parse_cs_notice(capture(NOTICE)).content.model_dump_json())
    args = arguments(file=path)
    args[args.index("--file")] = "--notice-json"
    position = args.index("--url")
    del args[position : position + 2]
    assert main(args) == 2
    captured = capsys.readouterr()
    assert captured.out == "" and "--parser cs-undergrad-notices requires --file" in captured.err


def test_help_preview_and_pure_imports_do_not_load_application_config_storage_lock_or_network(
    tmp_path, monkeypatch, capsys
):
    from signalnest import instance_lock, storage

    def forbidden(*args, **kwargs):
        raise AssertionError("CS local preview cannot use config, storage, lock or HTTP")

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sqlite3, "connect", forbidden)
    monkeypatch.setattr(httpx, "Client", forbidden)
    monkeypatch.setattr(storage, "open_initialized_engine", forbidden)
    monkeypatch.setattr(storage, "initialize_storage", forbidden)
    monkeypatch.setattr(instance_lock, "writer_lock", forbidden)
    monkeypatch.setattr("signalnest.cli.load_config", forbidden)
    before = set(tmp_path.iterdir())
    with pytest.raises(SystemExit) as caught:
        main(["decision-preview", "--help"])
    assert caught.value.code == 0
    assert "cs-undergrad-notices" in capsys.readouterr().out
    assert main(arguments()) == 0
    assert json.loads(capsys.readouterr().out)["preview_only"]
    assert set(tmp_path.iterdir()) == before


def test_default_uc_preview_never_imports_optional_cs_parser(monkeypatch, capsys):
    original = builtins.__import__

    def without_cs(name, *args, **kwargs):
        if name == "signalnest.cs_parsing":
            raise AssertionError("Optional CS adapter must stay lazy")
        return original(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", without_cs)
    args = arguments(
        parser=None,
        file=ROOT / "research/fixtures/current-notice-detail.html",
        url="https://uc.whu.edu.cn/info/1517/128231.htm",
    )
    assert main(args) == 0
    assert json.loads(capsys.readouterr().out)["preview_only"]


@pytest.mark.parametrize("help_only", [False, True])
def test_real_subprocess_local_preview_with_network_and_database_disabled(tmp_path, help_only):
    code = """
import socket, sqlite3, httpx, sys
def forbidden(*args, **kwargs):
    raise AssertionError('CS preview must be local')
socket.socket.connect = forbidden
socket.socket.connect_ex = forbidden
socket.create_connection = forbidden
sqlite3.connect = forbidden
httpx.Client = forbidden
from signalnest.cli import main
raise SystemExit(main(sys.argv[1:]))
"""
    before = set(tmp_path.iterdir())
    args = ["decision-preview", "--help"] if help_only else arguments()
    result = subprocess.run(
        [sys.executable, "-c", code, *args],
        cwd=tmp_path,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0 and result.stderr == ""
    if help_only:
        assert "cs-undergrad-notices" in result.stdout
    else:
        assert json.loads(result.stdout)["decision"]["action"] == "DIGEST"
    assert set(tmp_path.iterdir()) == before
