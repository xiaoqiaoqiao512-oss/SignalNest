"""The notification preview is local, explicit and independent of business storage."""

import json
import os
import sqlite3
import subprocess
import sys
from datetime import date
from pathlib import Path

import httpx
import pytest
from pydantic import ValidationError

from signalnest.cli import main
from signalnest.contracts import NoticeContent
from signalnest.notifications.contracts import Evidence, Profile
from signalnest.notifications.profile import ProfileError, load_profile

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "research/fixtures"


def write_profile(tmp_path, text='interest_topics = ["exchange"]\n'):
    path = tmp_path / "profile.toml"
    path.write_text(text, encoding="utf-8")
    return path


def write_notice(tmp_path):
    notice = NoticeContent(
        title="本科生国际交流项目报名通知",
        published_date=date(2026, 10, 5),
        body_html="<p>本项目面向全校本科生。报名时间：即日起至2026年10月5日23:59。</p>",
        body_text="本项目面向全校本科生。报名时间：即日起至2026年10月5日23:59。",
    )
    path = tmp_path / "notice.json"
    path.write_text(notice.model_dump_json(), encoding="utf-8")
    return path


def preview_args(profile, notice):
    return [
        "decision-preview",
        "--profile",
        str(profile),
        "--notice-json",
        str(notice),
        "--at",
        "2026-10-05T20:00:00+08:00",
        "--next-digest-at",
        "2026-10-06T09:00:00+08:00",
    ]


def test_example_is_explicitly_fictional_and_incomplete():
    path = ROOT / "profile.example.toml"
    assert "EXAMPLE ONLY" in path.read_text()
    profile = load_profile(path)
    assert profile.entry_year is None and profile.college is None and profile.major is None
    assert profile.profile_id == "self"
    assert profile.high_value_topics == ("exchange",)


@pytest.mark.parametrize(
    "change",
    [
        {"unknown": "field"},
        {"schema_version": 2},
        {"schema_version": True},
        {"profile_id": "other"},
        {"institution": "武大"},
        {"study_level": "student"},
        {"entry_year": "2023"},
        {"entry_year": True},
        {"entry_year": 2023.0},
        {"entry_year": 1800},
        {"interest_topics": ["unsupported"]},
        {"interest_topics": ["exchange", "exchange"]},
        {"interest_topics": []},
        {"include_phrases": [""]},
        {"include_phrases": [" a"]},
        {"include_phrases": ["a\nb"]},
        {"include_phrases": ["a", "a"]},
        {"include_phrases": ["a" * 121]},
        {"include_phrases": [str(i) for i in range(33)]},
        {"major": " "},
        {"college": "学院\n"},
        {"high_value_topics": ["exchange"], "store_only_topics": ["exchange"]},
    ],
)
def test_profile_rejects_unknown_fields_coercion_and_ambiguous_settings(change):
    with pytest.raises(ValidationError):
        Profile.model_validate({"interest_topics": ["exchange"], **change})


def test_profile_hash_is_order_independent_but_changes_with_values():
    first = Profile(interest_topics=("research", "exchange"), include_phrases=("创新", "项目"))
    same = Profile(interest_topics=("exchange", "research"), include_phrases=("项目", "创新"))
    assert first == same and first.sha256() == same.sha256()
    assert first.sha256() != first.model_copy(update={"entry_year": 2023}).sha256()
    with pytest.raises(ValidationError):
        first.entry_year = 2023


@pytest.mark.parametrize("raw", [b"\xff", b"[", b'interest_topics = ["madeup"]'])
def test_profile_loader_has_finite_clear_errors(tmp_path, raw):
    path = tmp_path / "bad.toml"
    path.write_bytes(raw)
    with pytest.raises(ProfileError) as caught:
        load_profile(path)
    assert len(str(caught.value)) < 500
    assert "madeup" not in str(caught.value)


def test_profile_size_limit_and_missing_file(tmp_path):
    with pytest.raises(ProfileError, match="cannot read"):
        load_profile(tmp_path / "missing")
    path = tmp_path / "large"
    path.write_bytes(b"#" * (64 * 1024 + 1))
    with pytest.raises(ProfileError, match="64 KiB"):
        load_profile(path)


def test_evidence_is_positioned_without_fabricating_media_text():
    with pytest.raises(ValidationError):
        Evidence(field="body_text", start=0, end=8, excerpt="本科生")
    with pytest.raises(ValidationError):
        Evidence(field="images", index=0)
    evidence = Evidence(field="images", index=0, url="https://uc.whu.edu.cn/image.jpg")
    assert evidence.excerpt == "" and evidence.start is None


def test_profile_check_and_preview_do_not_touch_database_http_or_lock(
    tmp_path, monkeypatch, capsys
):
    from signalnest import config, instance_lock

    def forbidden(*args, **kwargs):
        raise AssertionError("N0 must not use configuration, database, lock or HTTP")

    monkeypatch.setattr(sqlite3, "connect", forbidden)
    monkeypatch.setattr(httpx, "Client", forbidden)
    monkeypatch.setattr(config, "load_config", forbidden)
    monkeypatch.setattr("signalnest.cli.load_config", forbidden)
    monkeypatch.setattr(instance_lock, "writer_lock", forbidden)
    profile = write_profile(tmp_path)
    notice = write_notice(tmp_path)
    before = set(tmp_path.iterdir())
    assert main(["profile-check", "--profile", str(profile)]) == 0
    checked = json.loads(capsys.readouterr().out)
    assert checked["profile_valid"] is True and len(checked["profile_sha256"]) == 64
    assert main(preview_args(profile, notice)) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["preview_only"] is True
    assert "body_text" not in output["facts"]
    assert output["decision"]["evaluated_at"] == "2026-10-05T12:00:00Z"
    assert output["decision"]["relevance"] == "matched"
    assert output["decision"]["action"] == "PUSH_NOW"
    assert output["decision"]["effective_route"] == "immediate"
    assert set(tmp_path.iterdir()) == before


def test_same_notice_different_profiles_explains_different_decisions(tmp_path, capsys):
    notice = write_notice(tmp_path)
    first = write_profile(tmp_path)
    assert main(preview_args(first, notice)) == 0
    related = json.loads(capsys.readouterr().out)["decision"]
    first.write_text('interest_topics = ["scholarship"]\n', encoding="utf-8")
    assert main(preview_args(first, notice)) == 0
    unrelated = json.loads(capsys.readouterr().out)["decision"]
    assert related["action"] == "PUSH_NOW" and unrelated["action"] == "IGNORE"
    assert related["profile_sha256"] != unrelated["profile_sha256"]
    assert related["reason_codes"] != unrelated["reason_codes"]
    assert unrelated["effective_route"] == "none"


def test_preview_html_fixture_uses_final_url_not_filename(tmp_path, capsys):
    profile = write_profile(tmp_path, 'interest_topics = ["research", "course_enrollment"]\n')
    path = FIXTURES / "notifications/notice-128291-20261005T133533Z.html"
    renamed = tmp_path / "not-an-article.html"
    renamed.write_bytes(path.read_bytes())
    args = preview_args(profile, renamed)
    args[args.index("--notice-json")] = "--file"
    args += ["--url", "https://uc.whu.edu.cn/info/1517/128291.htm"]
    assert main(args) == 0
    output = json.loads(capsys.readouterr().out)
    assert "科研训练" in output["facts"]["title"]
    assert output["facts"]["deadline_at"].startswith("2026-09-28T23:59")
    assert output["decision"]["effective_route"] == "none"


@pytest.mark.parametrize("flag", ["--at", "--next-digest-at"])
@pytest.mark.parametrize("value", ["2026-10-05T20:00:00", "secret-invalid-time"])
def test_preview_requires_explicit_valid_time_without_echoing_input(tmp_path, capsys, flag, value):
    args = preview_args(write_profile(tmp_path), write_notice(tmp_path))
    args[args.index(flag) + 1] = value
    assert main(args) == 2
    output = capsys.readouterr()
    assert output.out == "" and flag in output.err
    assert value not in output.err


def test_missing_url_invalid_json_previous_context_and_size_are_errors(tmp_path, capsys):
    profile = write_profile(tmp_path)
    notice = write_notice(tmp_path)
    args = preview_args(profile, notice)
    args[args.index("--notice-json")] = "--file"
    assert main(args) == 2
    assert "--url" in capsys.readouterr().err
    args = preview_args(profile, notice) + ["--previous-route", "digest"]
    assert main(args) == 2
    assert "update" in capsys.readouterr().err
    notice.write_text("not json secret", encoding="utf-8")
    assert main(preview_args(profile, notice)) == 2
    assert "secret" not in capsys.readouterr().err
    notice.write_bytes(b"x" * (4 * 1024 * 1024 + 1))
    assert main(preview_args(profile, notice)) == 2
    assert "4 MiB" in capsys.readouterr().err


def test_real_competition_preview_preserves_unknown_deadline(tmp_path, capsys):
    profile = write_profile(tmp_path)
    args = preview_args(profile, FIXTURES / "notifications/notice-18135-20261005T133554Z.html")
    args[args.index("--notice-json")] = "--file"
    args += ["--url", "https://uc.whu.edu.cn/info/1517/18135.htm"]
    assert main(args) == 0
    output = capsys.readouterr()
    result = json.loads(output.out)
    assert output.err == ""
    assert result["facts"]["category"] == "opportunity"
    assert result["facts"]["deadline_at"] is None
    assert result["decision"]["time_status"] == "unknown"
    assert result["decision"]["needs_review"]
    assert not result["facts"]["cancelled"]


@pytest.mark.parametrize("command", ["profile-check", "decision-preview"])
def test_real_subprocess_help_has_no_side_effects(tmp_path, command):
    result = subprocess.run(
        [sys.executable, "-m", "signalnest", command, "--help"],
        cwd=tmp_path,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0 and "--profile" in result.stdout
    assert result.stderr == "" and list(tmp_path.iterdir()) == []


def test_real_profile_check_error_exit(tmp_path):
    result = subprocess.run(
        [sys.executable, "-m", "signalnest", "profile-check", "--profile", "missing.toml"],
        cwd=tmp_path,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 2 and "画像错误" in result.stderr and result.stdout == ""


def test_decision_evidence_is_deterministic_across_process_hash_seeds(tmp_path):
    notice = NoticeContent(
        title="国际交流项目报名通知",
        published_date=date(2026, 10, 5),
        body_html="<p>两份矛盾的对象声明。</p>",
        body_text=(
            "报名对象：2023级本科生。"
            "仅限2024级本科生报名。报名对象：教师。"
            "即日起报名。报名截止：2026年10月6日。"
        ),
    )
    code = """
import sys
from datetime import datetime
from signalnest.contracts import NoticeContent
from signalnest.notifications.contracts import EventContext, Profile
from signalnest.notifications.facts import extract_facts
from signalnest.notifications.decision import decide
facts = extract_facts(NoticeContent.model_validate_json(sys.stdin.buffer.read()))
decision = decide(
    Profile(interest_topics=('exchange',)), facts,
    EventContext(next_digest_at=datetime.fromisoformat('2026-10-06T09:00:00+08:00')),
    now=datetime.fromisoformat('2026-10-05T20:00:00+08:00'),
)
print(decision.model_dump_json())
"""
    outputs = []
    for seed in ("1", "23", "77"):
        result = subprocess.run(
            [sys.executable, "-c", code],
            input=notice.model_dump_json(),
            text=True,
            capture_output=True,
            check=False,
            cwd=tmp_path,
            env={**os.environ, "PYTHONHASHSEED": seed},
        )
        assert result.returncode == 0 and result.stderr == ""
        output = json.loads(result.stdout)
        # The entry-year declarations conflict. A bare teacher object is no
        # longer a competing *study level*: retain its unresolved applicant
        # scope separately, without turning that word into a hard exclusion.
        conflicts = [item for item in output["unknowns"] if item["code"] == "conflicting_evidence"]
        assert len(conflicts) == 1
        assert any("2023级" in proof["excerpt"] for proof in conflicts[0]["evidence"]) and any(
            "2024级" in proof["excerpt"] for proof in conflicts[0]["evidence"]
        )
        assert any(item["code"] == "applicant_scope_unknown" for item in output["unknowns"])
        outputs.append(output)
    assert outputs[0] == outputs[1] == outputs[2]
    assert list(tmp_path.iterdir()) == []
