"""Regression tests for the review findings on the claudecode domain: what is
installed, what counts as infra, how C is measured, trial ordering, process
hygiene, smoke gates and the leakage/model/permission prechecks. Run:
    uvx --quiet pytest tests/test_claudecode_hardening.py -q
"""

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

PLUGIN = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLUGIN / "lib"))

from rrsi_evolve.critic import precheck  # noqa: E402
from rrsi_evolve.procenv import session_dir  # noqa: E402
from rrsi_evolve.domain import load_domain  # noqa: E402
from test_claudecode_adapter import FAKE, _write, make_repo  # noqa: E402


def _dom(tmp_path, monkeypatch, cfg=None):
    monkeypatch.setenv("RRSI_EVOLVE_POLICY_BIN", FAKE)
    repo = make_repo(tmp_path)
    return load_domain("claudecode", repo, {"harness_path": "harness",
                                            "tasks_dir": "tasks", **(cfg or {})})


def _res(runs, job, tid, j=0):
    return json.loads((runs / "jobs" / job / f"{tid}__{j}" / "result.json").read_text())


def _gone(pid: int, wait_s: float = 3.0) -> bool:
    end = time.time() + wait_s
    while time.time() < end:
        if not os.path.isdir("/proc"):          # macOS: no zombies to tell apart
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return True
            except PermissionError:
                pass
            time.sleep(0.05)
            continue
        try:
            state = Path(f"/proc/{pid}/stat").read_text().split()[2]
        except (FileNotFoundError, ProcessLookupError):
            return True
        if state == "Z":
            return True
        time.sleep(0.05)
    return False


# ------------------------------------------------------------- installation --
def test_gitignored_harness_files_not_installed(tmp_path, monkeypatch):
    dom = _dom(tmp_path, monkeypatch)
    repo = dom.repo
    _write(repo / ".gitignore", "settings.local.json\n")     # matches at any depth
    _write(repo / "harness" / ".claude" / "settings.local.json", "{}\n")
    _write(repo / "harness" / ".claude" / "skills" / "s" / "SKILL.md",
           "---\nname: s\ndescription: d\n---\nbody\n")
    ws = tempfile.mkdtemp(prefix="rrsi-test-ws-")
    try:
        dom._overlay(repo, ws)
        assert (Path(ws) / "CLAUDE.md").is_file()
        assert (Path(ws) / ".claude" / "skills" / "s" / "SKILL.md").is_file()
        assert not (Path(ws) / ".claude" / "settings.local.json").exists()
    finally:
        subprocess.run(["rm", "-rf", ws])
    ok, info = dom._smoke_static(repo)
    assert ok is False and "ignored by .gitignore" in info["detail"]


def test_overlay_marks_shebang_scripts_executable_and_skips_symlinks(tmp_path, monkeypatch):
    dom = _dom(tmp_path, monkeypatch)
    h = dom.repo / "harness"
    _write(h / ".claude" / "hooks" / "gate.sh", "#!/usr/bin/env bash\nexit 0\n")
    _write(h / "notes.txt", "plain\n")
    (h / "leak").symlink_to(dom.repo / "tasks" / "hello-file" / "check.sh")
    ws = tempfile.mkdtemp(prefix="rrsi-test-ws-")
    try:
        dom._overlay(dom.repo, ws)
        assert os.access(Path(ws) / ".claude" / "hooks" / "gate.sh", os.X_OK)
        assert not os.access(Path(ws) / "notes.txt", os.X_OK)
        assert not (Path(ws) / "leak").exists()
    finally:
        subprocess.run(["rm", "-rf", ws])


@pytest.mark.parametrize("cfg", [{"harness_path": "."}, {"harness_path": ""},
                                 {"harness_path": "../x"},
                                 {"harness_path": "suite", "tasks_dir": "suite/tasks"}])
def test_harness_path_containing_tasks_is_rejected(tmp_path, cfg):
    repo = make_repo(tmp_path)
    with pytest.raises(SystemExit):
        load_domain("claudecode", repo, cfg)


# -------------------------------------------------------------------- infra --
@pytest.mark.parametrize("result,pre,infra", [
    ({"is_error": True, "result": "You've hit your session limit · resets 3:45pm"}, [], True),
    ({"is_error": True, "result": "Not logged in · Please run /login"}, [], True),
    ({"is_error": True, "result": "Invalid API key · Fix external API key"}, [], True),
    ({"is_error": True, "subtype": "error_during_execution", "errors": []}, [], True),
    ({"is_error": True, "api_error_status": 529, "result": "x"}, [], True),
    ({"is_error": True, "result": "API Error: 500 Internal server error"}, [], True),
    ({"is_error": True, "result": "Request failed with status 503"}, [], True),
    ({"is_error": True, "subtype": "error_max_turns", "result": "I wrote 512 lines"}, [], False),
    ({"is_error": True, "result": "stopped"},
     [{"type": "rate_limit_event",
       "rate_limit_info": {"status": "rejected", "rateLimitType": "five_hour"}}], True),
    ({}, [{"type": "rate_limit_event",          # recovered (e.g. overage): not infra
           "rate_limit_info": {"status": "rejected", "rateLimitType": "five_hour"}}], False),
    ({}, [{"type": "rate_limit_event", "rate_limit_info": {"status": "allowed"}}], False),
])
def test_infra_detection(tmp_path, monkeypatch, result, pre, infra):
    dom = _dom(tmp_path, monkeypatch)
    monkeypatch.setenv("FAKE_RESULT_JSON", json.dumps(result))
    monkeypatch.setenv("FAKE_PRE_EVENTS", json.dumps(pre))
    runs = tmp_path / "runs"
    dom.run(dom.repo, runs, "j", ["hello-file"], 1)
    assert bool(_res(runs, "j", "hello-file")["infra_error"]) is infra


# ------------------------------------------------------------------ cost C --
def test_tokens_come_from_cumulative_model_usage(tmp_path, monkeypatch):
    dom = _dom(tmp_path, monkeypatch)
    monkeypatch.setenv("FAKE_RESULT_JSON", json.dumps({"modelUsage": {
        "main": {"inputTokens": 100, "outputTokens": 10, "cacheReadInputTokens": 1000,
                 "cacheCreationInputTokens": 0},
        "sub": {"inputTokens": 50, "outputTokens": 5, "cacheReadInputTokens": 0,
                "cacheCreationInputTokens": 7}}}))
    runs = tmp_path / "runs"
    dom.run(dom.repo, runs, "j", ["hello-file"], 1)
    assert _res(runs, "j", "hello-file")["tokens"] == 1172


def test_zero_usage_is_unknown_not_free(tmp_path, monkeypatch):
    dom = _dom(tmp_path, monkeypatch)
    monkeypatch.setenv("FAKE_RESULT_JSON", json.dumps({"modelUsage": {}, "usage": {
        "input_tokens": 0, "output_tokens": 0}}))
    runs = tmp_path / "runs"
    dom.run(dom.repo, runs, "j", ["hello-file"], 1)
    assert _res(runs, "j", "hello-file")["tokens"] is None


# ---------------------------------------------------------- trial ordering --
def _fake_trial(jdir, tid, j, reward, infra=None):
    td = jdir / f"{tid}__{j}"
    td.mkdir(parents=True)
    (td / "result.json").write_text(json.dumps(
        {"task_id": tid, "trial": j, "reward": reward, "infra_error": infra,
         "tokens": 10, "cost_usd": 0.0}))
    (td / "stream.jsonl").write_text("")


def test_load_trial_follows_score_order(tmp_path, monkeypatch):
    dom = _dom(tmp_path, monkeypatch)
    runs = tmp_path / "runs"
    jdir = runs / "jobs" / "j"
    _fake_trial(jdir, "hello-file", 0, 0.0, infra="api error")
    _fake_trial(jdir, "hello-file", 1, 0.0)
    per, _ = dom.score(runs, "j", ["hello-file"], 2)
    tr = per["hello-file"]
    assert tr.rewards == [0.0, 0.0] and tr.missing == 1
    worst = tr.rewards.index(min(tr.rewards))
    assert dom.load_trial(runs, "j", "hello-file", worst)["trial_dir"].endswith("__1")
    assert dom.load_trial(runs, "j", "hello-file", 1) is None      # the missing slot


def test_trials_ordered_numerically(tmp_path, monkeypatch):
    dom = _dom(tmp_path, monkeypatch)
    runs = tmp_path / "runs"
    jdir = runs / "jobs" / "j"
    for j in range(12):
        _fake_trial(jdir, "hello-file", j, 1.0 if j == 10 else 0.0)
    tr = dom.score(runs, "j", ["hello-file"], 12)[0]["hello-file"]
    best = tr.rewards.index(max(tr.rewards))
    assert best == 10
    assert dom.load_trial(runs, "j", "hello-file", best)["reward"] == 1.0


def test_checker_weight_reaches_score(tmp_path, monkeypatch):
    dom = _dom(tmp_path, monkeypatch)
    _write(dom.repo / "tasks" / "hello-file" / "check.sh",
           'echo \'{"reward": 0.5, "weight": 3}\'\n')
    runs = tmp_path / "runs"
    dom.run(dom.repo, runs, "j", ["hello-file"], 1)
    tr = dom.score(runs, "j", ["hello-file"], 1)[0]["hello-file"]
    assert tr.rewards == [0.5] and tr.weights == [3.0]


# ------------------------------------------------------------ robustness --
def test_non_utf8_checker_output_does_not_crash(tmp_path, monkeypatch):
    dom = _dom(tmp_path, monkeypatch)
    _write(dom.repo / "tasks" / "hello-file" / "check.sh", "printf 'caf\\xe9\\n'\necho 1.0\n")
    runs = tmp_path / "runs"
    dom.run(dom.repo, runs, "j", ["hello-file"], 1)
    assert _res(runs, "j", "hello-file")["reward"] == 1.0
    assert "caf" in (runs / "jobs" / "j" / "hello-file__0" / "verifier.txt").read_text()


def test_missing_checker_is_infra_not_zero(tmp_path, monkeypatch):
    dom = _dom(tmp_path, monkeypatch)
    (dom.repo / "tasks" / "hello-file" / "check.sh").unlink()
    runs = tmp_path / "runs"
    dom.run(dom.repo, runs, "j", ["hello-file"], 1)
    assert "no check.sh" in _res(runs, "j", "hello-file")["infra_error"]


def test_agent_background_processes_are_killed(tmp_path, monkeypatch):
    dom = _dom(tmp_path, monkeypatch)
    pidfile = tmp_path / "bg.pid"
    monkeypatch.setenv("FAKE_BG_PIDFILE", str(pidfile))
    runs = tmp_path / "runs"
    dom.run(dom.repo, runs, "j", ["hello-file"], 1)
    assert _gone(int(pidfile.read_text()))


def test_checker_children_are_killed_on_timeout(tmp_path, monkeypatch):
    dom = _dom(tmp_path, monkeypatch, {"check_timeout_s": 1})
    pidfile = tmp_path / "chk.pid"
    _write(dom.repo / "tasks" / "hello-file" / "check.sh",
           f'sleep 60 & echo $! > "{pidfile}"\nsleep 60\n')
    runs = tmp_path / "runs"
    dom.run(dom.repo, runs, "j", ["hello-file"], 1)
    assert _res(runs, "j", "hello-file")["reward"] == 0.0
    assert _gone(int(pidfile.read_text()))


def test_no_session_dirs_left_behind(tmp_path, monkeypatch):
    home = tmp_path / "cfg"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(home))
    dom = _dom(tmp_path, monkeypatch)
    A = sys.modules[type(dom).__module__]
    seen = []
    real = A.ClaudeCodeDomain._policy

    def policy(self, task, ws, trial_dir, state_dir, *rest):
        d = session_dir(ws) / "s" / "subagents"
        d.mkdir(parents=True)                         # what Claude Code leaves
        seen.append(d)
        return real(self, task, ws, trial_dir, state_dir, *rest)
    monkeypatch.setattr(A.ClaudeCodeDomain, "_policy", policy)
    dom.run(dom.repo, tmp_path / "runs", "j", ["hello-file"], 1)
    assert seen and not seen[0].parent.parent.exists()


# ---------------------------------------------------------- policy command --
def test_mcp_servers_and_skill_task_preapproved(tmp_path, monkeypatch):
    dom = _dom(tmp_path, monkeypatch, {"allowed_tools": ["Read", "Bash(npm test *)"]})
    ws = tempfile.mkdtemp(prefix="rrsi-test-ws-")
    try:
        (Path(ws) / ".mcp.json").write_text(json.dumps({"mcpServers": {"calc": {}}}))
        cmd = dom._policy_cmd(ws)
    finally:
        subprocess.run(["rm", "-rf", ws])
    allowed = cmd[cmd.index("--allowedTools") + 1].split(",")
    assert {"Read", "Bash(npm test *)", "Skill", "Task", "mcp__calc"} <= set(allowed)
    tools = cmd[cmd.index("--tools") + 1].split(",")
    assert "Bash" in tools and not any("(" in t for t in tools)


def test_policy_wrapper_prefix(tmp_path, monkeypatch):
    dom = _dom(tmp_path, monkeypatch, {"policy_wrapper": ["firejail", "--private={ws}"]})
    cmd = dom._policy_cmd("/tmp/rrsi-ws-x")
    assert cmd[:2] == ["firejail", "--private=/tmp/rrsi-ws-x"] and cmd[3] == "-p"


# ------------------------------------------------------------ smoke gates --
def _settings(dom, obj):
    _write(dom.repo / "harness" / ".claude" / "settings.json", json.dumps(obj))


def test_smoke_static_hook_shape_and_runnable_scripts(tmp_path, monkeypatch):
    dom = _dom(tmp_path, monkeypatch)
    _settings(dom, {"hooks": {"Stop": [{"type": "command", "command": "x"}]}})
    assert "'hooks' list" in dom._smoke_static(dom.repo)[1]["detail"]
    _write(dom.repo / "harness" / ".claude" / "hooks" / "gate.sh", "exit 0\n")
    direct = {"type": "command", "command": '"${CLAUDE_PROJECT_DIR}"/.claude/hooks/gate.sh'}
    _settings(dom, {"hooks": {"Stop": [{"hooks": [direct]}]}})
    assert "no #! line" in dom._smoke_static(dom.repo)[1]["detail"]
    missing = {"type": "command", "command": 'bash "${CLAUDE_PROJECT_DIR}"/.claude/hooks/nope.sh'}
    _settings(dom, {"hooks": {"Stop": [{"hooks": [missing]}]}})
    assert "not in the harness" in dom._smoke_static(dom.repo)[1]["detail"]
    ok = {"type": "command", "command": 'bash "${CLAUDE_PROJECT_DIR}"/.claude/hooks/gate.sh',
          "timeout": 10}
    _settings(dom, {"hooks": {"Stop": [{"hooks": [ok]}]}})
    assert dom._smoke_static(dom.repo) is None


def test_smoke_requires_declared_skills_loaded(tmp_path, monkeypatch):
    dom = _dom(tmp_path, monkeypatch)
    _write(dom.repo / "harness" / ".claude" / "skills" / "verify" / "SKILL.md",
           "---\nname: verify\ndescription: d\n---\nbody\n")
    runs = tmp_path / "runs"
    ok, info = dom.smoke(dom.repo, runs, "smoke", ["hello-file"])
    assert ok is False and "skill verify" in info["hello-file"]
    monkeypatch.setenv("FAKE_INIT_SKILLS", "verify")
    ok, info = dom.smoke(dom.repo, runs, "smoke", ["hello-file"])
    assert ok is True, info


def test_smoke_fails_on_error_result(tmp_path, monkeypatch):
    dom = _dom(tmp_path, monkeypatch)
    monkeypatch.setenv("FAKE_RESULT_JSON", json.dumps(
        {"is_error": True, "subtype": "error_max_turns", "result": "stopped"}))
    ok, info = dom.smoke(dom.repo, tmp_path / "runs", "smoke", ["hello-file"])
    assert ok is False and "error result" in info["hello-file"]


# ----------------------------------------------------------- prechecks --
SETTINGS = "diff --git a/harness/.claude/settings.json b/harness/.claude/settings.json\n" \
           "+++ b/harness/.claude/settings.json\n"
AGENT = "diff --git a/harness/.claude/agents/r.md b/harness/.claude/agents/r.md\n" \
        "+++ b/harness/.claude/agents/r.md\n@@ -0,0 +1,5 @@\n+---\n+name: r\n"
NOTES = "diff --git a/harness/CLAUDE.md b/harness/CLAUDE.md\n+++ b/harness/CLAUDE.md\n"
HELPER = "diff --git a/harness/tools/x.py b/harness/tools/x.py\n+++ b/harness/tools/x.py\n"


@pytest.mark.parametrize("diff,hit", [
    (AGENT + "+model: opus\n", "frontmatter"),
    (AGENT + "+effort: high\n", "frontmatter"),
    (AGENT + "+'model': opus\n", "frontmatter"),
    (SETTINGS + '+  "model": "opus",\n', "settings key"),
    (SETTINGS + '+  "alwaysThinkingEnabled": true,\n', "settings key"),
    (SETTINGS + '+  "advisorModel": "opus",\n', "settings key"),
    (SETTINGS + '+  "enabledPlugins": {"x@y": true},\n', "settings key"),
    (SETTINGS + '+    "defaultMode": "bypassPermissions"\n', "settings key"),
    (SETTINGS + '+    "additionalDirectories": ["/"]\n', "settings key"),
    (SETTINGS + '+    "env": {"ANTHROPIC_BASE_URL": "http://x"}\n', "provider variable"),
    (SETTINGS + '+    "env": {"CLAUDE_CODE_SUBAGENT_MODEL": "opus"}\n', "provider variable"),
    (SETTINGS + '+    "env": {"CLAUDE_CODE_EFFORT_LEVEL": "max"}\n', "provider variable"),
    (SETTINGS + '+    "env": {"MAX_THINKING_TOKENS": "0"}\n', "provider variable"),
    (SETTINGS + '+    "env": {"CLAUDE_CODE_DISABLE_THINKING": "1"}\n', "provider variable"),
    (HELPER + '+os.environ["RRSI_HARNESS_STATE_DIR"] = "/home/x"\n', "reassigned"),
    (HELPER + "+RRSI_HARNESS_STATE_DIR=/home/x/persist\n", "reassigned"),
    (HELPER + '+subprocess.run(["claude", "-p", "--model", "opus", q])\n', "model call"),
    ("+claude -p --model opus \"$1\"\n", "model call"),
    (HELPER + "+import anthropic\n", "model call"),
    (HELPER + "+curl https://api.anthropic.com/v1/messages\n", "model call"),
    ("+exec claude --dangerously-skip-permissions\n", "permission widening"),
    ("Binary files /dev/null and b/harness/tools/blob differ\n", "binary file"),
    ("diff --git a/harness/.gitattributes b/harness/.gitattributes\n"
     "+++ b/harness/.gitattributes\n+*.md -diff\n", ".gitattributes"),
    (NOTES + "+Run `bash tools/check.sh` first.\n", "check.sh"),
])
def test_precheck_rejects_frozen_policy_and_widening(tmp_path, monkeypatch, diff, hit):
    dom = _dom(tmp_path, monkeypatch)
    hits = precheck(diff, dom.critic_patterns)
    assert any(hit in h for h in hits), hits


@pytest.mark.parametrize("diff", [
    AGENT + "+model: inherit\n", AGENT + '+model: "inherit"\n', AGENT + "-model: opus\n",
    NOTES + "+Use the model's own judgment.\n",
    NOTES + "+effort: keep each fix minimal\n",
    NOTES + "+Never run commands that could dangerously delete files.\n",
    HELPER + '+print(json.dumps({"model": "linear", "fit": 1}))\n',
    HELPER + "+  model: resnet50\n",
    "diff --git a/harness/.claude/hooks/g.sh b/harness/.claude/hooks/g.sh\n"
    "+++ b/harness/.claude/hooks/g.sh\n+echo \"effort=$CLAUDE_EFFORT\" >&2\n",
    HELPER + '+state = os.environ["RRSI_HARNESS_STATE_DIR"]\n',
    NOTES + "+Ask the user before running long commands.\n",
])
def test_precheck_allows_ordinary_harness_content(tmp_path, monkeypatch, diff):
    dom = _dom(tmp_path, monkeypatch)
    hits = precheck(diff, dom.critic_patterns)
    assert not hits, hits


# ------------------------------------------------------------------- CLI --
def test_cli_relative_repo_path(tmp_path):
    repo = tmp_path / "proj"
    repo.mkdir()
    env = {**os.environ, "RRSI_EVOLVE_POLICY_BIN": FAKE, "FAKE_POLICY_MODE": "write",
           "FAKE_WRITE_FILE": "greeting.txt", "FAKE_WRITE_TEXT": "hello world"}
    cli = [sys.executable, str(PLUGIN / "bin" / "rrsi-evolve")]
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(cli + ["init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "i"],
                   cwd=repo, check=True)
    r = subprocess.run(cli + ["--repo", ".", "baseline"], cwd=repo, env=env,
                       capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stderr[-800:]
    res = json.loads((repo / ".rrsi" / "runs" / "claudecode" / "jobs" / "base"
                      / "hello-file__0" / "result.json").read_text())
    assert res["reward"] == 1.0
    assert not (repo / "info").exists()
