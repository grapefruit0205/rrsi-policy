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
    # the live session beats RRSI_MODEL; RRSI_MODEL beats everything else
    assert models.resolve("inherit", cwd=proj, transcript=tr) == ("claude-opus-5-5", "session")
    assert models.resolve("inherit", cwd=proj) == ("pinned", "RRSI_MODEL")


def test_transcript_tail_and_default_setting(clean, tmp_path, monkeypatch):
    tr = tmp_path / "t.jsonl"
    big = json.dumps({"type": "user", "message": {"content": "x" * 300_000}})
    with open(tr, "w") as f:
        f.write(json.dumps({"type": "assistant", "message": {"model": "old"}}) + "\n")
        f.write(big + "\n")
        f.write(json.dumps({"type": "assistant", "message": {"model": "new", "content": "y" * 700_000}}) + "\n")
        f.write(big + "\n")
    assert models.session_model(tr) == "new"
    monkeypatch.setattr(models, "_TAIL_BYTES", 400_000)       # newest turn cut off
    assert models.session_model(tr) is None or models.session_model(tr) == "old"
    _settings(clean, "default")
    assert models.resolve(None, cwd=tmp_path)[0] == models.FALLBACK


def test_same_model():
    assert models.same_model("opus[1m]", "claude-opus-5-5")
    assert models.same_model("OPUS", "claude-opus-5-5[1m]")
    assert not models.same_model("opus", "sonnet")


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
    dom4 = load_domain("claudecode", repo, {"policy_model": "claude-opus-5-5"})
    cli._resolve_models(RRSIConfig(), dom4, repo, run_dir, "round")       # same model, other name
    (run_dir / "policy_model.json").write_text("{broken")
    with pytest.raises(SystemExit) as e:
        cli._resolve_models(RRSIConfig(), dom3, repo, run_dir, "round")
    assert "unreadable" in str(e.value)


def test_roles_resolve_against_the_repo(clean, tmp_path, monkeypatch):
    from rrsi_evolve import llm
    repo, other = tmp_path / "repo", tmp_path / "elsewhere"
    _settings(repo, "opus")
    _settings(other, "sonnet")
    monkeypatch.chdir(other)
    seen = {}

    def fake(prompt, sys_prompt, mdl, max_tokens, cache_prefix, json_only):
        seen["m"] = mdl
        return "{}", 0.0
    monkeypatch.setattr(llm, "_call_fake", fake)
    monkeypatch.setattr(llm, "_cwd", None)
    monkeypatch.setenv("RRSI_EVOLVE_LLM", "fake:/nonexistent.py")
    llm.configure(cwd=repo)
    llm.generate("hi")
    assert seen["m"] == "opus"


def test_gateway_policy_model_is_still_policed(clean, tmp_path):
    from rrsi_evolve.domain import load_domain
    repo = tmp_path / "repo"
    (repo / "tasks").mkdir(parents=True)
    dom = load_domain("claudecode", repo, {"policy_model": "zz-gateway/big"})
    mu = {"modelUsage": {"zz-gateway/big": {}, "claude-haiku-4-5": {}}}
    assert dom._foreign_models(mu) == []
    assert dom._foreign_models({"modelUsage": {"claude-sonnet-5-5": {}}}) == ["claude-sonnet-5-5"]
    dom.policy_model = "opus[1m]"
    assert dom._foreign_models({"modelUsage": {"claude-opus-5-5[1m]": {}, "claude-opus-5-5": {}}}) == []


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
