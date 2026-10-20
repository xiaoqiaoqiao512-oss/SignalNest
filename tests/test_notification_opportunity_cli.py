"""Offline CLI preview, explicit registration and shared model-advice boundaries."""

import json
from pathlib import Path

import pytest
from test_notification_opportunity_review import AT, IDS
from test_notification_opportunity_review import review_env as review_env
from test_notification_service import rows

from signalnest.cli import main
from signalnest.instance_lock import writer_lock
from signalnest.schema import notification_events


@pytest.fixture
def config(review_env, tmp_path):
    text = (Path(__file__).parents[1] / "config.example.toml").read_text()
    text = text.replace('data_dir = "data"', f'data_dir = "{review_env.settings.data_dir}"')
    text = text.replace(
        'database = "data/signalnest.sqlite3"', f'database = "{review_env.settings.database}"'
    )
    p = tmp_path / "review.toml"
    p.write_text(text)
    return p


def args(config, *extra):
    return ["notifications-review-open", "--config", str(config), *extra]


def test_help_is_side_effect_free(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SystemExit) as result:
        main(["notifications-review-open", "--help"])
    assert result.value.code == 0
    assert "--model-config" in capsys.readouterr().out
    assert list(tmp_path.iterdir()) == []


def test_default_preview_is_read_only_even_while_writer_holds_lock(config, review_env, capsys):
    before = review_env.settings.database.read_bytes()
    with writer_lock(review_env.settings.database):
        assert main(args(config, "--at", str(AT))) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["preview"] and result["open_count"] == 3
    assert result["selection"]["selected"] == 6
    assert review_env.settings.database.read_bytes() == before
    assert rows(review_env, notification_events) == []


def test_missing_database_preview_does_not_initialize_storage(config, tmp_path, capsys):
    missing = tmp_path / "not-created.sqlite3"
    text = config.read_text()
    text = text.replace(
        next(line for line in text.splitlines() if line.startswith("database =")),
        f'database = "{missing}"',
    )
    config.write_text(text)
    assert main(args(config, "--at", str(AT))) == 1
    assert not missing.exists() and not Path(str(missing) + ".writer.lock").exists()
    assert "storage-init" in capsys.readouterr().err


def test_explicit_apply_and_resume_are_real_and_idempotent(config, review_env, capsys):
    selected = [arg for identity in IDS for arg in ("--source-document-id", identity)]
    assert main(args(config, "--apply", "--operation-id", "cli", "--at", str(AT), *selected)) == 0
    assert json.loads(capsys.readouterr().out)["complete"]
    assert len(rows(review_env, notification_events)) == 3
    assert main(args(config, "--apply", "--operation-id", "cli")) == 0
    assert json.loads(capsys.readouterr().out)["reused"]
    assert len(rows(review_env, notification_events)) == 3


@pytest.mark.parametrize(
    "extra",
    [
        [],
        ["--at", "bad"],
        ["--apply"],
        ["--apply", "--operation-id", "auto", "--at", str(AT)],
        ["--at", str(AT), "--limit", "0"],
        ["--apply", "--operation-id", "x", "--model-config", "ask.toml"],
    ],
)
def test_invalid_inputs_fail_without_creating_an_event(config, review_env, extra, capsys):
    assert main(args(config, *extra)) == 2
    assert rows(review_env, notification_events) == []
    assert "参数错误" in capsys.readouterr().err


def test_write_lock_conflict_does_not_create_review(config, review_env, capsys):
    with writer_lock(review_env.settings.database):
        assert (
            main(
                args(
                    config,
                    "--apply",
                    "--operation-id",
                    "locked",
                    "--at",
                    str(AT),
                    "--source-document-id",
                    IDS[0],
                )
            )
            == 1
        )
    assert "writer_lock_busy" in capsys.readouterr().err
    assert rows(review_env, notification_events) == []


def test_explicit_model_preview_uses_current_policy_and_one_shared_budget(
    config, review_env, tmp_path, monkeypatch, capsys
):
    from signalnest.notifications.semantic_review import SemanticReviewReport

    model = tmp_path / "ask.toml"
    model.write_text("""[[instances]]
id = "not-opened"
config = "nonexistent-instance.toml"
[model]
model = "synthetic-review-model"
input_usd_per_million = "1"
output_usd_per_million = "1"
max_cost_usd = "1"
""")
    seen = []

    def fake_review(content, profile, *, at, model, limits, budget):
        seen.append((content.content_sha256(), profile.interest_topics, id(budget)))
        return SemanticReviewReport(
            status="fallback",
            error_code="model_credentials_missing",
            content_sha256=content.content_sha256(),
            profile_sha256=profile.sha256(),
            evaluated_at=at,
            model=model.model,
        )

    monkeypatch.setattr("signalnest.notifications.semantic_review.review_notice", fake_review)
    assert main(args(config, "--at", str(AT), "--model-config", str(model))) == 1
    result = json.loads(capsys.readouterr().out)
    assert result["open_count"] == 3 and result["semantic_failures"] == 6
    assert len(seen) == 6 and len({x[2] for x in seen}) == 1
    assert all(x[1] == ("competition",) for x in seen)
    assert rows(review_env, notification_events) == []
