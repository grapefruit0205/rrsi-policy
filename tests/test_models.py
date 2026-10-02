"""Model inheritance: every model knob defaults to the model you use."""
import json
import subprocess
import sys
from pathlib import Path

import pytest

PLUGIN = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLUGIN / "lib"))

from rrsi_policy import models  # noqa: E402


@pytest.fixture
def clean(monkeypatch, tmp_path):
    for v in ("RRSI_MODEL", "ANTHROPIC_MODEL"):
        monkeypatch.delenv(v, raising=False)
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    return home


def _settings(d: Path, model, name="settings.json"):
    (d / ".claude").mkdir(parents=True, exist_ok=True)
    (d / ".claude" / name).write_text(json.dumps({"model": model}))


def test_explicit_name_wins(clean):
    assert models.resolve("haiku") == ("haiku", "config")


def test_order_session_env_project_user(clean, tmp_path, monkeypatch):
    proj = clean / "work" / "proj"
    proj.mkdir(parents=True)
    assert models.resolve("inherit", cwd=proj) == (models.FALLBACK, "fallback (no model configured)")
    _settings(clean, "opus[1m]")
    assert models.resolve("inherit", cwd=proj)[0] == "opus[1m]"
    _settings(clean / "work", "sonnet")                      # a parent project
    assert models.resolve(None, cwd=proj)[0] == "sonnet"
    _settings(proj, "haiku", "settings.local.json")
    assert models.resolve("", cwd=proj)[0] == "haiku"
    monkeypatch.setenv("ANTHROPIC_MODEL", "claude-x")
    assert models.resolve("default", cwd=proj) == ("claude-x", "ANTHROPIC_MODEL")
    tr = tmp_path / "t.jsonl"
    tr.write_text("\n".join(json.dumps(e) for e in [
        {"type": "assistant", "message": {"model": "claude-opus-5-5"}},
        {"type": "assistant", "isSidechain": True, "message": {"model": "claude-haiku"}},
        {"type": "assistant", "message": {"model": "<synthetic>"}},
        {"type": "user", "message": {"content": "hi"}},
    ]) + "\n")
    assert models.resolve("inherit", cwd=proj, transcript=tr) == ("claude-opus-5-5", "session")
    monkeypatch.setenv("RRSI_MODEL", "pinned")
    assert models.resolve("inherit", cwd=proj, transcript=tr) == ("pinned", "RRSI_MODEL")


def test_claude_config_dir_and_bad_files(clean, tmp_path, monkeypatch):
    cfg = tmp_path / "cfgdir"
    cfg.mkdir()
    (cfg / "settings.json").write_text("{not json")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(cfg))
    assert models.resolve(None, cwd=tmp_path)[0] == models.FALLBACK
    (cfg / "settings.json").write_text(json.dumps({"model": "opus"}))
    assert models.resolve(None, cwd=tmp_path) == ("opus", str(cfg / "settings.json"))


def test_adapter_and_cli_pin(clean, tmp_path, monkeypatch):
    from rrsi_evolve.domain import load_domain
    from rrsi_evolve import cli
    from rrsi_evolve.config import RRSIConfig
    repo = tmp_path / "repo"
    (repo / "tasks").mkdir(parents=True)
    _settings(clean, "opus[1m]")
    dom = load_domain("claudecode", repo, {})
    assert (dom.policy_model, dom.policy_model_source) == ("opus[1m]", str(clean / ".claude" / "settings.json"))
    cfg = RRSIConfig()
    run_dir = tmp_path / "runs" / "claudecode"
    cli._resolve_models(cfg, dom, repo, run_dir, "baseline")
    assert cfg.proposer_model == cfg.critic_model == cfg.analyst_model == "opus[1m]"
    assert json.loads((run_dir / "policy_model.json").read_text())["policy_model"] == "opus[1m]"
    cli._resolve_models(RRSIConfig(), dom, repo, run_dir, "round")       # same model: fine
    _settings(clean, "sonnet")
    dom2 = load_domain("claudecode", repo, {})
    with pytest.raises(SystemExit) as e:
        cli._resolve_models(RRSIConfig(), dom2, repo, run_dir, "round")
    assert "opus[1m]" in str(e.value) and "sonnet" in str(e.value)
    cli._resolve_models(RRSIConfig(), dom2, repo, run_dir, "status")      # reads nothing new
    dom3 = load_domain("claudecode", repo, {"policy_model": "opus[1m]"})
    cli._resolve_models(RRSIConfig(), dom3, repo, run_dir, "round")       # explicit pin continues


def test_runtime_critic_uses_session_model(clean, tmp_path, monkeypatch):
    from rrsi_policy import engine
    seen = {}

    def fake_call(system, payload, schema, model, cfg):
        seen["model"] = model
        return {"verdict": "accept", "reasons": []}, {"model": model}
    monkeypatch.setattr(engine, "call_claude", fake_call)
    tr = tmp_path / "t.jsonl"
    tr.write_text(json.dumps({"type": "assistant", "message": {"model": "claude-opus-5-5"}}) + "\n")
    proj = tmp_path / "p"
    proj.mkdir()
    ev = {"tool_name": "Write", "cwd": str(proj), "session_id": "s", "transcript_path": str(tr),
          "tool_input": {"file_path": str(proj / "CLAUDE.md"), "content": "# rules\n- be brief\n"}}
    cfg = engine.load_config(str(engine.DEFAULT_CONFIG))
    call = engine.from_hook(ev)
    route, pol = engine.route(cfg, call)
    assert pol, "CLAUDE.md should route to a critic policy"
    engine.evaluate(cfg, pol, call, route, engine.Ledger(tmp_path / "l.jsonl"))
    assert seen["model"] == "claude-opus-5-5"
