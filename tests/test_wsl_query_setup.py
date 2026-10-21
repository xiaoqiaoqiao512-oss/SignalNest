"""The WSL handoff starts disabled; later configuration keeps explicit bounds."""

from pathlib import Path

from test_query_agent import campus as campus
from test_query_snapshots import snapshots as snapshots

from signalnest.querying.agent import ask
from signalnest.querying.config import load_query_config
from signalnest.querying.contracts import Question

ROOT = Path(__file__).resolve().parents[1]


def test_wsl_template_uses_real_snapshot_bindings_without_enabling_model(snapshots):
    directory, *_ = snapshots
    target = directory / "ask.wsl.toml"
    target.write_bytes((ROOT / "ask.wsl.example.toml").read_bytes())
    config = load_query_config(target)
    assert config.model is None
    assert {item.id for item in config.instances} == {"uc", "cs"}
    assert all(item.snapshot_manifest is not None for item in config.instances)
    report = ask(config, Question(text="助教招聘", at="2026-10-20T12:00:00+08:00"), local=True)
    assert report.status == "local" and report.error_code is None
    assert report.search_results and report.model_usage["http_requests"] == 0


def test_model_block_requires_explicit_operator_edit_and_retains_bounds(tmp_path):
    template = (ROOT / "ask.wsl.example.toml").read_text()
    # Only enable the documented block, replace its two nonfunctional placeholders.
    base, block = template.split("# [model]", 1)
    body = "[model]" + "\n".join(
        line[2:] if line.startswith("# ") and " = " in line else line for line in block.splitlines()
    )
    body = body.replace("REPLACE_WITH_EXACT_MODEL_ID", "operator-model-id")
    body = body.replace("https://your-model-endpoint.example/v1", "http://127.0.0.1:8001/v1")
    config = tmp_path / "ask.model.toml"
    config.write_text(base + body)
    settings = load_query_config(config)
    assert settings.model.model == "operator-model-id"
    assert settings.model.api_key_env == "SIGNALNEST_MODEL_API_KEY"
    assert settings.model.max_attempts == 1
    assert settings.model.pricing_mode == "unpriced_proxy"
    assert settings.model.max_cost_usd is None
    assert settings.limits.max_requests == settings.limits.max_model_calls == 6
    assert settings.limits.run_seconds == 90.0
    assert settings.model.max_completion_tokens == 2048
    assert not list(tmp_path.glob("**/*.sqlite*"))
