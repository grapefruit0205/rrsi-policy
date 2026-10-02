"""Regression tests for the second review round on the claudecode domain:
frozen-policy settings, the sandbox and process sweep, the checker's output
handling, per-job state and harness snapshots, smoke gates, weights and the
role-call isolation. Run:
    uvx --quiet pytest tests/test_claudecode_reverify.py -q
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

PLUGIN = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLUGIN / "lib"))
sys.path.insert(0, str(PLUGIN / "tests"))

from rrsi_evolve import procenv  # noqa: E402
from rrsi_evolve.critic import precheck  # noqa: E402
from rrsi_evolve.domain import load_domain  # noqa: E402
from test_claudecode_adapter import FAKE, _write, make_repo  # noqa: E402
from test_claudecode_hardening import _dom, _gone, _res  # noqa: E402


def _mod(dom):
    return sys.modules[type(dom).__module__]


def _static(dom):
    return dom._smoke_static(dom.repo)


# ------------------------------------------------- frozen policy (static) --
@pytest.mark.parametrize("settings,needle", [
    ({"alwaysThinkingEnabled": True}, "alwaysThinkingEnabled"),
    ({"advisorModel": "opus"}, "advisorModel"),
    ({"fallbackModel": ["opus"]}, "fallbackModel"),
    ({"enabledPlugins": {"x@y": True}}, "enabledPlugins"),
    ({"autoMemoryEnabled": True, "autoMemoryDirectory": "~/m"}, "autoMemoryEnabled"),
    ({"effortLevel": "max"}, "effortLevel"),
    ({"agent": "big"}, "agent"),
    ({"apiKeyHelper": "x.sh"}, "apiKeyHelper"),
    ({"permissions": {"defaultMode": "bypassPermissions"}}, "permissions.defaultMode"),
    ({"env": {"CLAUDE_CODE_EFFORT_LEVEL": "max"}}, "env.CLAUDE_CODE_EFFORT_LEVEL"),
    ({"env": {"MAX_THINKING_TOKENS": "0"}}, "env.MAX_THINKING_TOKENS"),
    ({"env": {"PATH": "/usr/bin"}}, "env.PATH"),
    ({"env": {"RRSI_HARNESS_STATE_DIR": "/home/x"}}, "env.RRSI_HARNESS_STATE_DIR"),
    ({"env": {"HTTPS_PROXY": "http://p"}}, "env.HTTPS_PROXY"),
    ({"hooks": {"Stop": [{"hooks": [{"type": "prompt", "prompt": "ok?",
                                     "model": "opus"}]}]}}, "may not set 'model'"),
])
def test_static_rejects_frozen_policy_settings(tmp_path, monkeypatch, settings, needle):
    dom = _dom(tmp_path, monkeypatch)
    _write(dom.repo / "harness" / ".claude" / "settings.json", json.dumps(settings))
    ok, info = _static(dom)
    assert ok is False and needle in info["detail"], info


def test_static_allows_harness_levers(tmp_path, monkeypatch):
    dom = _dom(tmp_path, monkeypatch)
    _write(dom.repo / "harness" / ".claude" / "settings.json", json.dumps({
        "env": {"BASH_MAX_OUTPUT_LENGTH": "20000", "MY_TOOL_MODE": "fast"},
        "permissions": {"deny": ["Edit(./.claude/settings.json)"],
                        "disableBypassPermissionsMode": "disable"},
        "outputStyle": "terse",
        "hooks": {"Stop": [{"hooks": [{"type": "command", "command": "true"}]}]}}))
    assert _static(dom) is None


@pytest.mark.parametrize("fm", [
    '"model": opus', "'effort': high", "? model\n: opus", "!!str model: opus",
    "x: &a {model: opus}\n<<: *a", '"mod\\u0065l": opus',
])
def test_static_rejects_frontmatter_model_spellings(tmp_path, monkeypatch, fm):
    dom = _dom(tmp_path, monkeypatch)
    _write(dom.repo / "harness" / ".claude" / "agents" / "r.md",
           f"---\nname: r\ndescription: reviewer\n{fm}\n---\nbody\n")
    ok, info = _static(dom)
    assert ok is False and "frozen" in info["detail"], info


def test_static_rejects_flow_mapping_frontmatter(tmp_path, monkeypatch):
    dom = _dom(tmp_path, monkeypatch)
    _write(dom.repo / "harness" / ".claude" / "skills" / "s" / "SKILL.md",
           "---\n{name: s, description: d, model: opus}\n---\nbody\n")
    ok, info = _static(dom)
    assert ok is False and "frozen" in info["detail"], info


@pytest.mark.parametrize("fm", ["model: inherit", 'model: "inherit"',
                                "description2: pick a model: carefully"])
def test_static_allows_inherit_and_prose(tmp_path, monkeypatch, fm):
    dom = _dom(tmp_path, monkeypatch)
    _write(dom.repo / "harness" / ".claude" / "agents" / "r.md",
           f"---\nname: r\ndescription: reviewer\n{fm}\n---\nbody\n")
    assert _static(dom) is None


# ----------------------------------------------------------- hook checks --
@pytest.mark.parametrize("hooks,needle", [
    ({"stop": [{"hooks": [{"type": "command", "command": "true"}]}]}, "unknown hook event"),
    ({"OnStop": [{"hooks": [{"type": "command", "command": "true"}]}]}, "unknown hook event"),
    ({"Stop": [{"hooks": [{"type": "shell", "command": "true"}]}]}, "not one of"),
])
def test_hooks_need_known_events_and_types(tmp_path, monkeypatch, hooks, needle):
    dom = _dom(tmp_path, monkeypatch)
    _write(dom.repo / "harness" / ".claude" / "settings.json", json.dumps({"hooks": hooks}))
    ok, info = _static(dom)
    assert ok is False and needle in info["detail"], info


@pytest.mark.parametrize("cmd", [
    '"$CLAUDE_PROJECT_DIR"/.venv/bin/python -m pytest -x -q',
    '"${CLAUDE_PROJECT_DIR}"/node_modules/.bin/prettier --check .',
    "$CLAUDE_PROJECT_DIR/gradlew test",
])
def test_hooks_may_run_workspace_tools(tmp_path, monkeypatch, cmd):
    dom = _dom(tmp_path, monkeypatch)
    _write(dom.repo / "harness" / ".claude" / "settings.json", json.dumps(
        {"hooks": {"Stop": [{"hooks": [{"type": "command", "command": cmd}]}]}}))
    assert _static(dom) is None


def test_hook_harness_script_still_checked(tmp_path, monkeypatch):
    dom = _dom(tmp_path, monkeypatch)
    _write(dom.repo / "harness" / ".claude" / "settings.json", json.dumps(
        {"hooks": {"Stop": [{"hooks": [{"type": "command", "command":
                                        'bash "$CLAUDE_PROJECT_DIR"/.claude/hooks/x.sh'}]}]}}))
    ok, info = _static(dom)
    assert ok is False and "not in the harness" in info["detail"]


# ------------------------------------------------- symlinks and fixtures --
def test_symlink_inside_harness_installs_its_content(tmp_path, monkeypatch):
    dom = _dom(tmp_path, monkeypatch)
    h = dom.repo / "harness"
    _write(h / "AGENTS.md", "shared rules\n")
    (h / "CLAUDE.md").unlink()
    (h / "CLAUDE.md").symlink_to("AGENTS.md")
    (h / "docs").mkdir()
    _write(h / "docs" / "style.md", "style\n")
    (h / ".claude" / "rules").mkdir(parents=True)
    (h / ".claude" / "rules" / "style.md").symlink_to("../../docs/style.md")
    assert _static(dom) is None
    ws = tmp_path / "ws"
    ws.mkdir()
    dom._overlay(dom.repo, str(ws))
    assert (ws / "CLAUDE.md").read_text() == "shared rules\n"
    assert not (ws / "CLAUDE.md").is_symlink()
    assert (ws / ".claude" / "rules" / "style.md").read_text() == "style\n"


def test_symlink_leaving_harness_fails_smoke(tmp_path, monkeypatch):
    dom = _dom(tmp_path, monkeypatch)
    outside = tmp_path / "outside.md"
    outside.write_text("x\n")
    (dom.repo / "harness" / "LINK.md").symlink_to(outside)
    ok, info = _static(dom)
    assert ok is False and "symlinks must point inside" in info["detail"]


def test_harness_may_not_replace_fixture_files(tmp_path, monkeypatch):
    dom = _dom(tmp_path, monkeypatch)
    _write(dom.repo / "tasks" / "hello-file" / "workspace" / "src" / "app.py", "x = 1\n")
    _write(dom.repo / "harness" / "src" / "app.py", "x = 2\n")
    ok, info = _static(dom)
    assert ok is False and "src/app.py" in info["detail"]
    assert "hello-file" not in info["detail"]       # no task names to the proposer


def test_smoke_accepts_quoted_skill_names(tmp_path, monkeypatch):
    dom = _dom(tmp_path, monkeypatch)
    _write(dom.repo / "harness" / ".claude" / "skills" / "verify" / "SKILL.md",
           '---\nname: "verify"  # the gate\ndescription: "Use when: done"\n---\nRun it.\n')
    monkeypatch.setenv("FAKE_INIT_SKILLS", "verify,debug")
    ok, info = dom.smoke(dom.repo, tmp_path / "runs", "smoke", ["hello-file"])
    assert ok is True, info


# ----------------------------------------------------------- checker ------
def test_checker_timeout_returns_despite_setsid_pipe_holder(tmp_path, monkeypatch):
    dom = _dom(tmp_path, monkeypatch, {"check_timeout_s": 2})
    _write(dom.repo / "tasks" / "hello-file" / "check.sh",
           "setsid sleep 25 & sleep 30\n")
    t0 = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired):
        dom._check(dom.repo / "tasks" / "hello-file", str(tmp_path))
    assert time.monotonic() - t0 < 10


def test_checker_exit_returns_despite_setsid_pipe_holder(tmp_path, monkeypatch):
    dom = _dom(tmp_path, monkeypatch, {"check_timeout_s": 20})
    _write(dom.repo / "tasks" / "hello-file" / "check.sh",
           "setsid sleep 25 & echo 1.0\n")
    t0 = time.monotonic()
    r, w, rc, out = dom._check(dom.repo / "tasks" / "hello-file", str(tmp_path))
    assert (r, rc) == (1.0, 0) and time.monotonic() - t0 < 10


# ------------------------------------------------------ process sweep -----
def test_detached_agent_processes_are_killed(tmp_path, monkeypatch):
    dom = _dom(tmp_path, monkeypatch)
    pidfile = tmp_path / "bg.pid"
    monkeypatch.setenv("FAKE_BG_PIDFILE", str(pidfile))
    monkeypatch.setenv("FAKE_BG_SETSID", "1")
    dom.run(dom.repo, tmp_path / "runs", "j", ["hello-file"], 1)
    pid = int(pidfile.read_text())
    assert _gone(pid)


def test_detached_processes_killed_on_timeout(tmp_path, monkeypatch):
    dom = _dom(tmp_path, monkeypatch, {"task_timeout_s": 2})
    pidfile = tmp_path / "bg.pid"
    monkeypatch.setenv("FAKE_POLICY_MODE", "hang")
    monkeypatch.setenv("FAKE_BG_PIDFILE", str(pidfile))
    monkeypatch.setenv("FAKE_BG_SETSID", "1")
    _write(dom.repo / "tasks" / "hello-file" / "task.json",
           json.dumps({"prompt": "FILE: hello.txt", "timeout_s": 2}))
    dom.run(dom.repo, tmp_path / "runs", "j", ["hello-file"], 1)
    assert _res(tmp_path / "runs", "j", "hello-file")["timed_out"] is True
    assert _gone(int(pidfile.read_text()))


# ------------------------------------------------- policy env and argv ----
def test_policy_env_and_pinned_settings(tmp_path, monkeypatch):
    dom = _dom(tmp_path, monkeypatch, {"policy_effort": "low"})
    dump = tmp_path / "env.json"
    monkeypatch.setenv("FAKE_ENV_DUMP", str(dump))
    monkeypatch.setenv("OLDPWD", str(dom.repo))
    monkeypatch.setenv("PWD", str(dom.repo))
    monkeypatch.setenv("MAX_THINKING_TOKENS", "99")
    monkeypatch.setenv("BASH_ENV", "/etc/x")
    dom.run(dom.repo, tmp_path / "runs", "j", ["hello-file"], 1)
    got = json.loads(dump.read_text())
    env, argv = got["env"], got["argv"]
    ws = env["PWD"]
    assert "rrsi-ws-" in ws and str(dom.repo) not in ws
    assert "OLDPWD" not in env and "MAX_THINKING_TOKENS" not in env and "BASH_ENV" not in env
    assert env["DISABLE_AUTOUPDATER"] == "1"
    assert "/rrsi-run-" in env["RRSI_HARNESS_STATE_DIR"]
    assert str(dom.repo) not in env["RRSI_HARNESS_STATE_DIR"]
    assert len(env["RRSI_TRIAL_TOKEN"]) == 32
    settings = json.loads(argv[argv.index("--settings") + 1])
    pins = settings["env"]
    assert pins["CLAUDE_CODE_SUBAGENT_MODEL_FORCE"] == "1"
    assert pins["RRSI_HARNESS_STATE_DIR"] == env["RRSI_HARNESS_STATE_DIR"]
    assert pins["RRSI_TRIAL_TOKEN"] == env["RRSI_TRIAL_TOKEN"]
    assert pins["CLAUDE_CODE_EFFORT_LEVEL"] == "low" and "--effort" in argv
    assert pins["PATH"].split(os.pathsep)[0].endswith("/bin")
    assert settings["autoMemoryEnabled"] is False
    parent = str(Path(ws).parent)
    assert f"{parent}/CLAUDE.md" in settings["claudeMdExcludes"]
    assert "/tmp/CLAUDE.md" in settings["claudeMdExcludes"] or \
        not str(Path(ws)).startswith("/tmp/")


def test_nested_claude_call_is_disabled(tmp_path, monkeypatch):
    dom = _dom(tmp_path, monkeypatch, {"keep_workspaces": True})
    monkeypatch.setenv("FAKE_PROBE", "probe.json")
    dom.run(dom.repo, tmp_path / "runs", "j", ["hello-file"], 1)
    ws = Path(_res(tmp_path / "runs", "j", "hello-file")["workspace"])
    probe = json.loads((ws / "probe.json").read_text())
    shutil.rmtree(ws, ignore_errors=True)
    assert probe["nested_rc"] == 126 and "frozen" in probe["nested_err"]


def test_bad_policy_effort_rejected(tmp_path, monkeypatch):
    with pytest.raises(SystemExit):
        _dom(tmp_path, monkeypatch, {"policy_effort": "huge"})


# --------------------------------------------------------- state dir ------
def test_state_copy_back_survives_fifo_and_dangling_symlink(tmp_path, monkeypatch):
    dom = _dom(tmp_path, monkeypatch)
    A = _mod(dom)
    real = A.ClaudeCodeDomain._policy
    seen = []

    def policy(self, task, ws, trial_dir, state_dir, *rest):
        seen.append(state_dir)
        os.mkfifo(state_dir / "pipe")
        (state_dir / "notes-link").symlink_to(Path(ws) / "notes.md")   # dies with ws
        (state_dir / "lesson.json").write_text("{}")
        return real(self, task, ws, trial_dir, state_dir, *rest)
    monkeypatch.setattr(A.ClaudeCodeDomain, "_policy", policy)
    runs = tmp_path / "runs"
    dom.run(dom.repo, runs, "j", ["hello-file"], 1)
    persisted = runs / "jobs" / "j" / "state"
    assert (persisted / "lesson.json").exists()
    assert (persisted / "notes-link").is_symlink() and not (persisted / "pipe").exists()
    assert not seen[0].parent.exists()                 # the private run dir is gone
    # the next run of the same job starts from the persisted state, no crash
    monkeypatch.setattr(A.ClaudeCodeDomain, "_policy", real)
    shutil.rmtree(runs / "jobs" / "j" / "hello-file__0")
    dom.run(dom.repo, runs, "j", ["hello-file"], 1)
    assert (persisted / "notes-link").is_symlink()
    assert _res(runs, "j", "hello-file")["reward"] == 1.0


def test_state_dir_is_private_and_unpredictable(tmp_path, monkeypatch):
    dom = _dom(tmp_path, monkeypatch)
    A = _mod(dom)
    real = A.ClaudeCodeDomain._policy
    seen = []

    def policy(self, task, ws, trial_dir, state_dir, *rest):
        seen.append((state_dir, os.stat(state_dir.parent).st_mode & 0o777))
        return real(self, task, ws, trial_dir, state_dir, *rest)
    monkeypatch.setattr(A.ClaudeCodeDomain, "_policy", policy)
    dom.run(dom.repo, tmp_path / "runs", "j", ["hello-file"], 1)
    dom.run(dom.repo, tmp_path / "runs", "j2", ["hello-file"], 1)
    (s1, m1), (s2, _) = seen
    assert m1 == 0o700 and s1 != s2


# -------------------------------------------------- harness snapshot ------
def test_harness_snapshot_isolates_trials_from_worktree_edits(tmp_path, monkeypatch):
    dom = _dom(tmp_path, monkeypatch, {"concurrency": 1})
    A = _mod(dom)
    real = A.ClaudeCodeDomain._policy
    seen = []

    def policy(self, task, ws, trial_dir, state_dir, *rest):
        seen.append((Path(ws) / "CLAUDE.md").read_text())
        (dom.repo / "harness" / "CLAUDE.md").write_text("TAMPERED\n")
        return real(self, task, ws, trial_dir, state_dir, *rest)
    monkeypatch.setattr(A.ClaudeCodeDomain, "_policy", policy)
    dom.run(dom.repo, tmp_path / "runs", "j", ["hello-file"], 2)
    assert seen == ["# neutral harness\n", "# neutral harness\n"]
    # resuming the job with a changed harness would mix two harnesses
    shutil.rmtree(tmp_path / "runs" / "jobs" / "j" / "hello-file__1")
    with pytest.raises(RuntimeError, match="harness differs"):
        dom.run(dom.repo, tmp_path / "runs", "j", ["hello-file"], 2)


# ------------------------------------------------------------ weights -----
def _put(runs, job, tid, j, **res):
    td = runs / "jobs" / job / f"{tid}__{j}"
    td.mkdir(parents=True, exist_ok=True)
    (td / "result.json").write_text(json.dumps({"task_id": tid, "trial": j, **res}))


def test_missing_heavy_trial_never_raises_S(tmp_path, monkeypatch):
    from rrsi_evolve.evaluate import aggregate
    dom = _dom(tmp_path, monkeypatch)
    runs = tmp_path / "runs"
    for job, missing in (("A", False), ("B", True)):
        _put(runs, job, "hello-file", 0, reward=0.0, weight=20.0, checker_weight=20.0)
        if missing:
            _put(runs, job, "hello-file", 1, reward=0.0, weight=1.0, checker_weight=None,
                 infra_error="api error 529")
        else:
            _put(runs, job, "hello-file", 1, reward=0.0, weight=20.0, checker_weight=20.0)
        _put(runs, job, "failing-task", 0, reward=1.0, weight=2.0, checker_weight=2.0)
        _put(runs, job, "failing-task", 1, reward=1.0, weight=2.0, checker_weight=2.0)
    ids = ["hello-file", "failing-task"]
    pa, _ = dom.score(runs, "A", ids, 2)
    pb, _ = dom.score(runs, "B", ids, 2)
    assert pb["hello-file"].weights == [20.0, 20.0]
    sa, sb = aggregate("A", 2, pa).S, aggregate("B", 2, pb).S
    assert sb <= sa


def test_task_weight_edit_applies_to_all_slots(tmp_path, monkeypatch):
    dom = _dom(tmp_path, monkeypatch)
    runs = tmp_path / "runs"
    _put(runs, "A", "hello-file", 0, reward=1.0, weight=1.0, checker_weight=None)
    _put(runs, "A", "hello-file", 1, reward=0.0, weight=1.0, checker_weight=None,
         infra_error="x")
    _write(dom.repo / "tasks" / "hello-file" / "task.json",
           json.dumps({"prompt": "FILE: hello.txt", "weight": 3}))
    per, _ = dom.score(runs, "A", ["hello-file"], 2)
    assert per["hello-file"].weights == [3.0, 3.0]


# ------------------------------------------------------------ manifest ----
def test_checker_gets_pre_run_manifest(tmp_path, monkeypatch):
    dom = _dom(tmp_path, monkeypatch)
    _write(dom.repo / "tasks" / "hello-file" / "workspace" / "seed.txt", "s\n")
    _write(dom.repo / "tasks" / "hello-file" / "check.sh",
           'cat "$RRSI_PRE_MANIFEST"; echo 1.0\n')
    runs = tmp_path / "runs"
    dom.run(dom.repo, runs, "j", ["hello-file"], 1)
    out = (runs / "jobs" / "j" / "hello-file__0" / "verifier.txt").read_text()
    assert "seed.txt" in out and "CLAUDE.md" in out and "hello.txt" not in out


# -------------------------------------------------------------- render ----
def test_render_interleaved_subagent_keeps_message_step():
    sys.path.insert(0, str(PLUGIN / "lib" / "rrsi_evolve" / "domains" / "claudecode"))
    import render
    ev = [
        {"type": "assistant", "message": {"id": "m1", "content": [
            {"type": "tool_use", "id": "T1", "name": "Task", "input": {}}]}},
        {"type": "user", "parent_tool_use_id": "T1",
         "message": {"content": "check the tests"}},
        {"type": "assistant", "parent_tool_use_id": "T1", "message": {"id": "s1", "content": [
            {"type": "tool_use", "id": "G1", "name": "Glob", "input": {}}]}},
        {"type": "assistant", "message": {"id": "m1", "content": [
            {"type": "tool_use", "id": "R1", "name": "Read", "input": {}}]}},
        {"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": "R1", "content": "print(1)"}]}},
    ]
    out = render.render_steps(ev)
    assert "[step 1] TOOL_USE Read" in out and "[step 1] TOOL_RESULT: print(1)" in out
    assert "[step 3] SUBAGENT(T1) TOOL_USE Glob" in out


# --------------------------------------------------------------- infra ----
@pytest.mark.parametrize("text", [
    "Invalid auth token · Fix external auth token",
    "Your org is out of usage · contact your admin",
    "You've hit your team's shared budget. /model to switch models.",
    "You’ve reached your weekly limit",
    "Your seat type doesn't include usage credits",
])
def test_more_infra_texts(tmp_path, monkeypatch, text):
    dom = _dom(tmp_path, monkeypatch)
    run = {"result": {"is_error": True, "result": text}, "timed_out": False,
           "exit_code": 1}
    assert dom._infra_error(run)


# ------------------------------------------------------------- procenv ----
def test_session_dir_long_path_matches_claude_code():
    p = "/tmp/" + "a" * 230 + "/rrsi-ws-é1"
    # from Claude Code 2.1.280's own sanitizer, run under node
    want = "-tmp-" + "a" * 195 + "-8vg5o6"
    assert procenv.session_dir(p).name == want


def test_session_residue_includes_session_env(monkeypatch, tmp_path):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    got = procenv.session_residue("/tmp/x", ["abc-123", "../evil", None])
    assert tmp_path / "session-env" / "abc-123" in got
    assert not any("evil" in str(p) for p in got)


def test_role_call_excludes_ancestor_instructions(tmp_path, monkeypatch):
    from rrsi_evolve import llm
    fake = tmp_path / "fake_claude.py"
    dump = tmp_path / "argv.json"
    fake.write_text(
        "#!/usr/bin/env python3\nimport json, os, sys\n"
        f"json.dump({{'argv': sys.argv[1:], 'cwd': os.getcwd(), 'env': dict(os.environ)}},"
        f" open({str(dump)!r}, 'w'))\n"
        "print(json.dumps({'result': 'ok', 'session_id': 'sid-1', 'total_cost_usd': 0}))\n")
    fake.chmod(0o755)
    monkeypatch.setenv("RRSI_EVOLVE_CLAUDE_BIN", str(fake))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "cfg"))
    (tmp_path / "cfg" / "session-env" / "sid-1").mkdir(parents=True)
    text, _ = llm._call_claude_cli("hi", "sys", "haiku", 100, None)
    got = json.loads(dump.read_text())
    argv = got["argv"]
    settings = json.loads(argv[argv.index("--settings") + 1])
    parent = str(Path(got["cwd"]).parent)
    assert f"{parent}/CLAUDE.md" in settings["claudeMdExcludes"]
    assert f"{parent}/.claude/rules/**" in settings["claudeMdExcludes"]
    assert got["env"]["PWD"] == got["cwd"] and "OLDPWD" not in got["env"]
    assert not (tmp_path / "cfg" / "session-env" / "sid-1").exists()
    assert text == "ok"


# ------------------------------------------------------------- sandbox ----
def _bwrap_works() -> bool:
    if not shutil.which("bwrap"):
        return False
    r = subprocess.run(["bwrap", "--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc",
                        "--unshare-pid", "--die-with-parent", "true"], capture_output=True)
    return r.returncode == 0


@pytest.mark.skipif(not _bwrap_works(), reason="bubblewrap not usable here")
def test_bwrap_sandbox_hides_repo_runs_and_other_processes(monkeypatch):
    # the repo and runs dir outside /tmp, so hiding them is the sandbox's own work
    top = Path(tempfile.mkdtemp(dir=Path.home(), prefix=".rrsi-test-"))
    try:
        _bwrap_case(top, monkeypatch)
    finally:
        shutil.rmtree(top, ignore_errors=True)


def _bwrap_case(top, monkeypatch):
    dom = _dom(top, monkeypatch, {"sandbox": "bwrap", "keep_workspaces": True})
    runs = top / "runs"
    marker = Path(tempfile.mkdtemp(prefix="rrsi-marker-")) / "marker-outside"
    marker.write_text("x")
    cfg = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")
    monkeypatch.setenv("FAKE_PROBE", "probe.json")
    monkeypatch.setenv("FAKE_PROBE_LEAK", "1")
    monkeypatch.setenv("FAKE_PROBE_PATHS", os.pathsep.join(
        [str(dom.repo / "tasks" / "hello-file" / "check.sh"), str(runs / "jobs"), str(marker),
         str(Path.home()), str(top), str(cfg / "projects"), str(cfg / "settings.json")]))
    pidfile = "bg.pid"
    monkeypatch.setenv("FAKE_BG_PIDFILE", pidfile)
    monkeypatch.setenv("FAKE_BG_SETSID", "1")
    try:
        dom.run(dom.repo, runs, "j", ["hello-file"], 1)
        res = _res(runs, "j", "hello-file")
        ws = Path(res["workspace"])
        probe = json.loads((ws / "probe.json").read_text())
        assert res["reward"] == 1.0, res
        vis = probe["visible"]
        assert vis[str(dom.repo / "tasks" / "hello-file" / "check.sh")] is False
        assert vis[str(runs / "jobs")] is False and vis[str(marker)] is False  # private /tmp
        # a private HOME: the real one is not there, writes stay in the sandbox,
        # the toolchain dirs (sandbox_home) are bound back read-only
        assert vis[str(Path.home())] is True and vis[str(top)] is False
        assert probe["home_writable"] is True
        assert not (Path.home() / ".rrsi-probe-leak").exists()
        assert ".rrsi-probe-leak" in probe["home_entries"]
        # what may show: the sandbox_home dirs, PATH dirs under HOME and the
        # (fake, script) policy's interpreter
        home = Path.home().resolve()
        tops = [Path(x).parts[0] for x in _mod(dom).SANDBOX_HOME]
        for d in os.environ.get("PATH", "").split(os.pathsep) + \
                [os.path.realpath(shutil.which("python3") or "/")]:
            r = Path(d).resolve() if d else home
            if home in r.parents:
                tops.append(r.relative_to(home).parts[0])
        top_dirs = set(tops) - {".claude"}
        assert not set(probe["home_entries"]) - {".rrsi-probe-leak", *top_dirs}
        # Claude Code's own data is not the policy's: a private config dir
        assert vis[str(cfg / "projects")] is False and vis[str(cfg / "settings.json")] is False
        assert "/rrsi-run-" in probe["config_dir"]
        assert probe["global_config"] is None and probe["claude_dir"] is None
        # no session bus, display or agent socket; no network but the proxy
        assert probe["session_env"] == [] and probe["bus_visible"] is False
        assert probe["direct"] != "connected" and probe["x11"] != "connected"
        assert probe["proxy"] == "http://127.0.0.1:3128"
        assert probe["proxy_denied"].startswith("HTTP/1.1 403"), probe
        # no host daemon (docker, the system bus, systemd) answers
        assert "connected" not in probe["host_sockets"].values(), probe["host_sockets"]
        assert probe["nproc"] < 10 and probe["pwd"] == str(ws)
        assert probe["nested_rc"] == 126
    finally:
        shutil.rmtree(marker.parent, ignore_errors=True)
        try:
            shutil.rmtree(Path(_res(runs, "j", "hello-file")["workspace"]), ignore_errors=True)
        except Exception:  # noqa: BLE001
            pass


def test_sandbox_none_and_wrapper_skip_bwrap(tmp_path, monkeypatch):
    dom = _dom(tmp_path, monkeypatch, {"sandbox": "none"})
    assert dom._sandbox_mode() == "none"
    dom2 = load_domain("claudecode", dom.repo, {"policy_wrapper": ["env"],
                                                "sandbox": "bwrap"})
    assert dom2._sandbox_mode() == "none"
    with pytest.raises(SystemExit):
        load_domain("claudecode", dom.repo, {"sandbox": "docker"})


# ------------------------------------------------- precheck, round 3 -----
SET3 = "diff --git a/harness/.claude/settings.json b/harness/.claude/settings.json\n" \
       "+++ b/harness/.claude/settings.json\n"
AG3 = "diff --git a/harness/.claude/agents/r.md b/harness/.claude/agents/r.md\n" \
      "+++ b/harness/.claude/agents/r.md\n"
NEW_AG3 = AG3 + "@@ -0,0 +1,8 @@\n+---\n+name: r\n"
MD3 = "diff --git a/harness/CLAUDE.md b/harness/CLAUDE.md\n+++ b/harness/CLAUDE.md\n"
PY3 = "diff --git a/harness/tools/x.py b/harness/tools/x.py\n+++ b/harness/tools/x.py\n"
SH3 = "diff --git a/harness/.claude/hooks/h.sh b/harness/.claude/hooks/h.sh\n" \
      "+++ b/harness/.claude/hooks/h.sh\n"


@pytest.mark.parametrize("diff,hit", [
    (AG3 + "@@ -1,4 +1,5 @@\n ---\n name: r\n+model: opus\n ---\n", "frontmatter"),
    (NEW_AG3 + "+hooks:\n+  Stop:\n+    - hooks:\n+        - type: prompt\n"
     "+          model: opus\n+---\n", "frontmatter"),
    (NEW_AG3 + "+- model: opus\n", "frontmatter"),
    (NEW_AG3 + "+&a model: opus\n", "frontmatter"),
    (AG3 + "@@ -0,0 +1,3 @@\n+﻿---\n+model: opus\n+---\n", "frontmatter"),
    ("diff --git w/harness/.claude/settings.json i/harness/.claude/settings.json\n"
     "+++ w/harness/.claude/settings.json\n+  \"model\": \"opus\",\n", "settings key"),
    (SET3 + '+  "env": {"FALLBACK_FOR_ALL_PRIMARY_MODELS": "1"}\n', "provider variable"),
    (SH3 + "+~/.local/bin/claude -p \"$1\"\n", "model call"),
    (SH3 + "+/usr/bin/claude --output-format json -p x\n", "model call"),
    (SH3 + "+npx -y @anthropic-ai/claude-code -p hi\n", "model call"),
    (PY3 + "+import openai\n", "model call"),
    (PY3 + '+r = requests.post("https://api.openai.com/v1/chat")\n', "model call"),
    (SH3 + "+CLI=claude\n", "claude command"),
    (PY3 + '+subprocess.run([shutil.which("claude"), q])\n', "claude command"),
    (PY3 + '+open(os.path.expanduser("~/notes"), "a").write(x)\n', "home-directory"),
    (SH3 + '+echo "$x" >> "$HOME/.harness-notes"\n', "home-directory"),
    (MD3 + "+Read ~/.claude/projects for earlier runs.\n", "own data"),
    (MD3 + "+" + "x" * 120_001 + "\n", "longer than the critic reads"),
])
def test_precheck_round3_rejects(tmp_path, monkeypatch, diff, hit):
    dom = _dom(tmp_path, monkeypatch)
    hits = precheck(diff, dom.critic_patterns)
    assert any(hit in h for h in hits), hits


@pytest.mark.parametrize("diff", [
    NEW_AG3 + "+description: d\n+---\n+\n+## Data\n+model: the domain model\n",
    NEW_AG3 + "+model: inherit  # keep the policy's\n",
    SET3 + '+    "PreModelSwitch": [\n',
    SET3 + '+    "disableBypassPermissionsMode": "disable",\n',
    SET3 + '+    "env": {"HF_MODEL_ID": "x", "PYTEST_PLUGINS": "y"}\n',
    SH3 + '+[ $RRSI_HARNESS_STATE_DIR = "" ] && exit 0\n',
    SH3 + "+# hand claude the failing test names\n",
    MD3 + "+Ask claude to run pytest -p no:cacheprovider when caches break.\n",
    MD3 + "+Write notes under .claude/notes/ in the project.\n",
    PY3 + "+import harness_helpers_local\n",
])
def test_precheck_round3_allows(tmp_path, monkeypatch, diff):
    dom = _dom(tmp_path, monkeypatch)
    hits = precheck(diff, dom.critic_patterns)
    assert not hits, hits


# ------------------------------------------- round 3: fixtures, state ----
def test_fixture_instruction_and_settings_files_merge(tmp_path, monkeypatch):
    dom = _dom(tmp_path, monkeypatch)
    fx = dom.repo / "tasks" / "hello-file" / "workspace"
    _write(fx / "CLAUDE.md", "TASK RULES\n")
    _write(fx / ".claude" / "settings.json",
           json.dumps({"permissions": {"allow": ["Bash(make:*)"]}}))
    _write(dom.repo / "harness" / ".claude" / "settings.json",
           json.dumps({"permissions": {"allow": ["Bash(pytest:*)"]}}))
    assert dom._smoke_static(dom.repo) is None
    A = _mod(dom)
    real = A.ClaudeCodeDomain._policy
    seen = {}

    def policy(self, task, ws, trial_dir, state_dir, *rest):
        seen["md"] = (Path(ws) / "CLAUDE.md").read_text()
        seen["set"] = json.loads((Path(ws) / ".claude" / "settings.json").read_text())
        return real(self, task, ws, trial_dir, state_dir, *rest)
    monkeypatch.setattr(A.ClaudeCodeDomain, "_policy", policy)
    dom.run(dom.repo, tmp_path / "runs", "j", ["hello-file"], 1)
    assert seen["md"].startswith("TASK RULES\n") and "# neutral harness" in seen["md"]
    assert seen["set"]["permissions"]["allow"] == ["Bash(make:*)", "Bash(pytest:*)"]


def test_fixture_code_collision_still_rejected(tmp_path, monkeypatch):
    dom = _dom(tmp_path, monkeypatch)
    _write(dom.repo / "tasks" / "hello-file" / "workspace" / "src" / "a.py", "x = 1\n")
    _write(dom.repo / "harness" / "src" / "a.py", "x = 2\n")
    ok, info = dom._smoke_static(dom.repo)
    assert ok is False and "src/a.py" in info["detail"] and "hello-file" not in info["detail"]


def test_hook_may_call_a_fixture_tool(tmp_path, monkeypatch):
    dom = _dom(tmp_path, monkeypatch)
    _write(dom.repo / "tasks" / "hello-file" / "workspace" / "tools" / "lint.sh", "#!/bin/sh\n")
    _write(dom.repo / "harness" / "tools" / "mine.sh", "#!/bin/sh\n")
    _write(dom.repo / "harness" / ".claude" / "settings.json", json.dumps({"hooks": {
        "Stop": [{"hooks": [{"type": "command",
                             "command": "bash $CLAUDE_PROJECT_DIR/tools/lint.sh"}]}]}}))
    assert dom._smoke_static(dom.repo) is None


def test_state_persists_after_every_trial(tmp_path, monkeypatch):
    dom = _dom(tmp_path, monkeypatch, {"concurrency": 1})
    A = _mod(dom)
    real = A.ClaudeCodeDomain._policy
    jdir = tmp_path / "runs" / "jobs" / "j"
    seen = []

    def policy(self, task, ws, trial_dir, state_dir, *rest):
        seen.append(sorted(p.name for p in (jdir / "state").glob("note-*")))
        (state_dir / f"note-{len(seen)}").write_text("x")
        return real(self, task, ws, trial_dir, state_dir, *rest)
    monkeypatch.setattr(A.ClaudeCodeDomain, "_policy", policy)
    dom.run(dom.repo, tmp_path / "runs", "j", ["hello-file"], 2)
    assert seen == [[], ["note-1"]]
    assert sorted(p.name for p in (jdir / "state").glob("note-*")) == ["note-1", "note-2"]


def test_harness_snapshot_is_not_on_disk(tmp_path, monkeypatch):
    dom = _dom(tmp_path, monkeypatch)
    A = _mod(dom)
    real = A.ClaudeCodeDomain._policy
    seen = []

    def policy(self, task, ws, trial_dir, state_dir, *rest):
        seen.append(sorted(p.name for p in state_dir.parent.iterdir()))
        return real(self, task, ws, trial_dir, state_dir, *rest)
    monkeypatch.setattr(A.ClaudeCodeDomain, "_policy", policy)
    dom.run(dom.repo, tmp_path / "runs", "j", ["hello-file"], 1)
    assert "harness" not in seen[0]


def test_stale_run_dirs_are_removed(tmp_path, monkeypatch):
    dom = _dom(tmp_path, monkeypatch)
    dead = subprocess.Popen(["true"])
    dead.wait()
    stale = Path(tempfile.mkdtemp(prefix="rrsi-run-"))
    (stale / ".owner").write_text(f"{dead.pid}\n")
    (stale / "ro").mkdir()
    (stale / "ro").chmod(0o500)
    live = Path(tempfile.mkdtemp(prefix="rrsi-run-"))
    (live / ".owner").write_text(f"{os.getpid()}\n")
    young = Path(tempfile.mkdtemp(prefix="rrsi-run-"))   # not locked yet
    (young / ".owner").write_text("")
    old = time.time() - 3600
    for d in (stale, live):
        os.utime(d, (old, old))
    # a live run holds an flock on its .owner (pids do not cross namespaces)
    import fcntl
    held = open(live / ".owner")
    fcntl.flock(held, fcntl.LOCK_EX)
    try:
        dom.run(dom.repo, tmp_path / "runs", "j", ["hello-file"], 1)
        assert not stale.exists() and live.exists() and young.exists()
    finally:
        held.close()
        for d in (stale, live, young):
            if d.exists():
                for sub in d.rglob("*"):
                    sub.chmod(0o700)
                shutil.rmtree(d, ignore_errors=True)


# ------------------------------------------------- round 3: weights ------
def test_checker_timeout_trial_weighs_like_the_measured_ones(tmp_path, monkeypatch):
    dom = _dom(tmp_path, monkeypatch)
    runs = tmp_path / "runs"
    _put(runs, "A", "hello-file", 0, reward=1.0, weight=10.0, checker_weight=10.0)
    _put(runs, "A", "hello-file", 1, reward=0.0, weight=1.0, checker_weight=None)
    per, _ = dom.score(runs, "A", ["hello-file"], 2)
    assert per["hello-file"].weights == [10.0, 10.0]


def test_all_missing_task_weighs_what_earlier_jobs_measured(tmp_path, monkeypatch):
    dom = _dom(tmp_path, monkeypatch)
    runs = tmp_path / "runs"
    _put(runs, "base", "hello-file", 0, reward=1.0, weight=10.0, checker_weight=10.0)
    dom.score(runs, "base", ["hello-file"], 1)
    _put(runs, "B", "hello-file", 0, reward=0.0, weight=1.0, checker_weight=None,
         infra_error="api error 529")
    per, _ = dom.score(runs, "B", ["hello-file"], 1)
    assert per["hello-file"].weights == [10.0]


# ------------------------------------------- round 3: static & env -------
def test_bom_frontmatter_model_is_rejected(tmp_path, monkeypatch):
    dom = _dom(tmp_path, monkeypatch)
    _write(dom.repo / "harness" / ".claude" / "agents" / "r.md",
           "﻿---\nname: r\ndescription: d\nmodel: opus\n---\nbody\n")
    ok, info = dom._smoke_static(dom.repo)
    assert ok is False and "model" in info["detail"]


def test_frontmatter_hook_model_is_rejected(tmp_path, monkeypatch):
    dom = _dom(tmp_path, monkeypatch)
    _write(dom.repo / "harness" / ".claude" / "skills" / "s" / "SKILL.md",
           "---\nname: s\ndescription: d\nhooks: {Stop: [{hooks: [{type: prompt, "
           "prompt: x, model: opus}]}]}\n---\nbody\n")
    ok, info = dom._smoke_static(dom.repo)
    assert ok is False and "model" in info["detail"]


def test_symlinked_file_in_symlinked_dir(tmp_path, monkeypatch):
    dom = _dom(tmp_path, monkeypatch)
    h = dom.repo / "harness"
    _write(h / "real" / "a.md", "A\n")
    (h / "real" / "b.md").symlink_to(h / "real" / "a.md")
    _write(tmp_path / "outside.md", "secret\n")
    (h / "real" / "c.md").symlink_to(tmp_path / "outside.md")
    (h / "linked").symlink_to(h / "real")
    entries, problems = dom._harness_entries(dom.repo)
    rels = dict(entries)
    assert rels["linked/b.md"].read_text() == "A\n"
    assert "linked/c.md" not in rels and any("c.md" in p for p in problems)


def test_static_env_names_are_anchored(tmp_path, monkeypatch):
    A = _mod(_dom(tmp_path, monkeypatch))
    assert A._frozen_settings({"env": {"HF_MODEL_ID": "x", "PYTEST_PLUGINS": "y",
                                       "BASH_DEFAULT_TIMEOUT_MS": "9"}}) is None
    for bad in ("CLAUDE_CODE_FUTURE_MODEL", "FALLBACK_FOR_ALL_PRIMARY_MODELS",
                "DISABLE_INTERLEAVED_THINKING", "MAX_THINKING_TOKENS"):
        assert A._frozen_settings({"env": {bad: "1"}}), bad


def test_checker_runs_with_workspace_pwd(tmp_path, monkeypatch):
    dom = _dom(tmp_path, monkeypatch)
    monkeypatch.setenv("OLDPWD", "/somewhere/else")
    _write(dom.repo / "tasks" / "hello-file" / "check.sh",
           'echo "pwd=$PWD old=${OLDPWD:-none}"; echo 1.0\n')
    runs = tmp_path / "runs"
    dom.run(dom.repo, runs, "j", ["hello-file"], 1)
    out = (runs / "jobs" / "j" / "hello-file__0" / "verifier.txt").read_text()
    assert "old=none" in out and "pwd=/" in out and "/rrsi-ws-" in out


def test_policy_crash_reports_stderr_tail(tmp_path, monkeypatch):
    dom = _dom(tmp_path, monkeypatch)
    monkeypatch.setenv("FAKE_POLICY_MODE", "crash")
    runs = tmp_path / "runs"
    dom.run(dom.repo, runs, "j", ["hello-file"], 1)
    err = _res(runs, "j", "hello-file")["infra_error"]
    assert err.startswith("no result event (exit 1)")


def test_pins_disable_the_advisor_and_excludes_cover_agents_md(tmp_path, monkeypatch):
    dom = _dom(tmp_path, monkeypatch)
    assert dom._pins("/w", None, None, None)["CLAUDE_CODE_DISABLE_ADVISOR_TOOL"] == "1"
    ex = procenv.ancestor_excludes("/a/b/ws")
    assert "/a/b/AGENTS.md" in ex and "/a/AGENTS.md" in ex


def test_precheck_stays_fast_on_adversarial_diffs(tmp_path, monkeypatch):
    dom = _dom(tmp_path, monkeypatch)
    h = "+++ b/harness/tools/x.sh\n"
    for diff in (h + "+claude " + "--x y " * 15000 + "\n",
                 h + "+" + "npx " * 20000 + "\n",
                 h + "+claude " + "-a b --c=d -e " * 6000 + "\n"):
        t = time.monotonic()
        precheck(diff, dom.critic_patterns)
        assert time.monotonic() - t < 3.0


# ------------------------------------------- round 4: precheck anchors ---
@pytest.mark.parametrize("diff,hit", [
    (SH3 + "+systemd-run --user --pipe bash -c id\n", "sandbox"),
    (SH3 + "+cat /proc/1/root/etc/hostname\n", "sandbox"),
    (SH3 + "+dbus-send --session --dest=org.x /o m\n", "sandbox"),
    (MD3 + "+Read $CLAUDE_CONFIG_DIR/projects first.\n", "own data"),
    (MD3 + "+See ~/.claude.json\n", "own data"),
    (PY3 + '+p = os.path.expanduser("~/.notes")\n', "home-directory"),
    (PY3 + '+h = os.environ["HOME"]\n', "home-directory"),
    (AG3 + "@@ -0,0 +1,3 @@\n+---\n+{name: r, model: opus}\n+---\n", "frontmatter"),
])
def test_precheck_round4_rejects(tmp_path, monkeypatch, diff, hit):
    dom = _dom(tmp_path, monkeypatch)
    hits = precheck(diff, dom.critic_patterns)
    assert any(hit in h for h in hits), hits


@pytest.mark.parametrize("diff", [
    # history, file-history etc. only as Claude Code's paths, not as words
    MD3 + "+Keep a history.jsonl of the commands you ran in the workspace.\n",
    MD3 + "+Use git log, not file-history, to see what changed.\n",
    # a { in a description is not a flow mapping
    NEW_AG3 + "+description: fix {flaky, slow} tests, effort: medium is fine\n+---\n",
    # expanduser of a workspace-relative variable; a trailing comment naming claude
    PY3 + "+p = os.path.expanduser(path)\n",
    SH3 + "+pytest -q  # claude reads this output\n",
    # an added line that looks like another file's header does not move the scope
    MD3 + "+```\n+++ b/harness/tools/run.sh\n+claude is the name of the agent\n+```\n",
])
def test_precheck_round4_allows(tmp_path, monkeypatch, diff):
    dom = _dom(tmp_path, monkeypatch)
    hits = precheck(diff, dom.critic_patterns)
    assert not hits, hits


def test_precheck_scope_stays_linear_over_many_files(tmp_path, monkeypatch):
    dom = _dom(tmp_path, monkeypatch)
    one = "diff --git a/harness/d/{i}.md b/harness/d/{i}.md\n+++ b/harness/d/{i}.md\n" \
          "@@ -0,0 +1,2 @@\n+line one\n+line two\n"
    diff = "".join(one.format(i=i) for i in range(1500))
    t = time.monotonic()
    precheck(diff, dom.critic_patterns)
    assert time.monotonic() - t < 3.0


# ------------------------------------------- round 4: harness entries ----
def test_harness_link_loops_are_problems_not_hangs(tmp_path, monkeypatch):
    dom = _dom(tmp_path, monkeypatch)
    h = dom.repo / "harness"
    _write(h / "real" / "y.md", "y\n")
    (h / "real" / "self").symlink_to("..")
    (h / "loop").symlink_to(".")
    (h / "l1").symlink_to("l2")
    (h / "l2").symlink_to("l1")
    (h / "link").symlink_to("real")
    entries, problems = dom._harness_entries(dom.repo)
    rels = dict(entries)
    assert "link/y.md" in rels and "real/y.md" in rels
    assert not any(r.startswith(("loop/", "real/self/")) for r in rels)
    assert any("loop" in p for p in problems) and any("l1" in p for p in problems)


# ------------------------------------------- round 4: install merging ----
def test_mcp_servers_merge_per_server_and_agents_md_stays(tmp_path, monkeypatch):
    A = _mod(_dom(tmp_path, monkeypatch))
    ws = tmp_path / "ws"
    _write(ws / ".mcp.json", json.dumps({"mcpServers": {
        "db": {"command": "db-mcp", "args": ["--ro"]}, "fs": {"command": "fs"}}}))
    _write(ws / "AGENTS.md", "TASK AGENTS RULES\n")
    A.ClaudeCodeDomain._install([
        (".mcp.json", json.dumps({"mcpServers": {"db": {"command": "db2"}}}).encode()),
        ("CLAUDE.md", b"HARNESS\n")], ws, merge=True)
    mcp = json.loads((ws / ".mcp.json").read_text())["mcpServers"]
    assert mcp["db"] == {"command": "db2"} and mcp["fs"] == {"command": "fs"}
    md = (ws / "CLAUDE.md").read_text()
    assert md.index("TASK AGENTS RULES") < md.index("HARNESS")


# ------------------------------------------- round 4: weights, tasks -----
def test_weight_fill_prefers_this_jobs_measurement(tmp_path, monkeypatch):
    dom = _dom(tmp_path, monkeypatch)
    runs = tmp_path / "runs"
    (runs).mkdir(parents=True, exist_ok=True)
    (runs / "checker_weights.json").write_text(json.dumps({"hello-file": 50.0}))
    _put(runs, "A", "hello-file", 0, reward=1.0, weight=10.0, checker_weight=10.0)
    _put(runs, "A", "hello-file", 1, reward=0.0, weight=1.0, checker_weight=None)
    per, _ = dom.score(runs, "A", ["hello-file"], 2)
    assert per["hello-file"].weights == [10.0, 10.0]


@pytest.mark.parametrize("w", [0, -1, "nan", "inf"])
def test_task_weight_must_be_positive_and_finite(tmp_path, monkeypatch, w):
    dom = _dom(tmp_path, monkeypatch)
    tj = dom.repo / "tasks" / "hello-file" / "task.json"
    meta = json.loads(tj.read_text()) if tj.exists() else {}
    meta["weight"] = float(w) if isinstance(w, str) else w
    tj.write_text(json.dumps(meta))
    with pytest.raises(SystemExit):
        dom._tasks()


def test_trials_ignore_the_state_dir(tmp_path, monkeypatch):
    dom = _dom(tmp_path, monkeypatch)
    runs = tmp_path / "runs"
    _put(runs, "A", "hello-file", 0, reward=1.0, weight=1.0, checker_weight=1.0)
    st = runs / "jobs" / "A" / "state"
    st.mkdir(parents=True)
    (st / "result.json").write_text(json.dumps({"reward": 0.0}))
    per, _ = dom.score(runs, "A", ["hello-file"], 1)
    assert per["hello-file"].rewards == [1.0]


def test_persist_state_replaces_a_read_only_tree(tmp_path, monkeypatch):
    dom = _dom(tmp_path, monkeypatch)
    A = _mod(dom)
    jdir = tmp_path / "jobs" / "j"
    (jdir / "state" / "ro").mkdir(parents=True)
    (jdir / "state" / "ro" / "old").write_text("x")
    (jdir / "state" / "ro").chmod(0o500)
    new = tmp_path / "live"
    (new / "ro2").mkdir(parents=True)
    (new / "ro2" / "n").write_text("y")
    (new / "ro2").chmod(0o500)
    try:
        dom._persist_state(new, jdir)
        assert (jdir / "state" / "ro2" / "n").read_text() == "y"
        assert not (jdir / "state" / "ro").exists() and not (jdir / "state.old").exists()
    finally:
        for d in (jdir, new):
            for p in d.rglob("*"):
                if p.is_dir():
                    p.chmod(0o700)


# ------------------------------------------- round 4: sandbox layout -----
def test_sandbox_rw_cannot_reexpose_a_hidden_path(tmp_path, monkeypatch):
    dom = _dom(tmp_path, monkeypatch)
    hidden = Path("/srv/rrsi-hidden-repo")
    dom.sandbox_rw = [str(hidden / "sub")]
    argv = dom._bwrap_argv("/w", [], [], {"hidden": [hidden], "layout": {}})
    rw = argv.index(str(hidden / "sub"))
    tm = max(i for i, a in enumerate(argv) if a == str(hidden) and argv[i - 1] == "--tmpfs") \
        if hidden.is_dir() else None
    # hidden dirs that exist are covered after the writable binds
    if tm is not None:
        assert tm > rw
    home = str(Path.home().resolve())
    assert ["--tmpfs", home] == argv[argv.index(home) - 1:argv.index(home) + 1]


def test_session_sockets_never_reach_the_policy(tmp_path, monkeypatch):
    dom = _dom(tmp_path, monkeypatch)
    A = _mod(dom)
    assert {"DBUS_SESSION_BUS_ADDRESS", "XDG_RUNTIME_DIR", "SSH_AUTH_SOCK", "DISPLAY",
            "WAYLAND_DISPLAY", "XAUTHORITY", "ALL_PROXY"} <= set(A._SESSION_VARS)
    argv = dom._bwrap_argv("/w", [], [], {"hidden": [], "layout": {}})
    pairs = list(zip(argv, argv[1:]))
    if Path("/run").is_dir():
        # docker, the system and session buses, systemd: all under /run
        assert ("--tmpfs", "/run") in pairs
    run_user = Path(os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}")
    if run_user.is_dir() and Path("/run") not in run_user.resolve().parents:
        assert ("--tmpfs", str(run_user.resolve())) in pairs


def test_host_sockets_outside_the_fresh_dirs_are_masked(tmp_path, monkeypatch):
    import socket
    dom = _dom(tmp_path, monkeypatch)
    A = _mod(dom)
    d = tmp_path / "rw"
    d.mkdir()
    path = d / "s.sock"
    if len(os.fsencode(str(path))) > 100:
        pytest.skip("temp path too long for a unix socket")
    if not os.path.isfile("/usr/bin/python3"):
        pytest.skip("no /usr/bin/python3 for the probe")
    srv = socket.socket(socket.AF_UNIX)
    srv.bind(str(path))
    srv.listen(1)
    try:
        assert path in A._host_sockets()
        dom.sandbox_rw = [str(d)]       # re-exposed: its socket must be masked
        argv = dom._bwrap_argv("/", [], [], {"hidden": [], "layout": {}})
        i = argv.index(str(path))
        assert argv[i - 2:i] == ["--ro-bind", "/dev/null"] and i > argv.index(str(d))
        r = subprocess.run(argv + ["/usr/bin/python3", "-c",
                                   "import socket,sys; s=socket.socket(socket.AF_UNIX)\n"
                                   "try:\n s.connect(sys.argv[1]); print('open')\n"
                                   "except OSError: print('closed')", str(path)],
                           capture_output=True, text=True, timeout=60)
        assert r.stdout.strip() == "closed", r
    finally:
        srv.close()


# ------------------------------------------- round 4: proxy --------------
def test_proxy_allowlist_rules(tmp_path):
    from rrsi_evolve import netproxy
    px = netproxy.Proxy(tmp_path / "p.sock", ["api.anthropic.com", "*.example.com"])
    try:
        assert px.allowed("api.anthropic.com", 443)
        assert px.allowed("API.Anthropic.com.", 443)
        assert px.allowed("a.example.com", 443) and not px.allowed("example.com", 443)
        assert not px.allowed("api.anthropic.com", 80)
        for h in ("1.1.1.1", "[::1]", "::1", "localhost", ""):
            assert not px.allowed(h, 443), h
        assert oct(os.stat(tmp_path / "p.sock").st_mode & 0o777) == "0o600"
    finally:
        px.close()
    assert not (tmp_path / "p.sock").exists()


def _ask(sock, req: bytes) -> bytes:
    import socket
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(10)
    s.connect(str(sock))
    s.sendall(req)
    out = s.recv(128)
    s.close()
    return out.split(b"\r\n")[0]


def test_proxy_refuses_other_hosts_methods_and_private_peers(tmp_path):
    import socket
    from rrsi_evolve import netproxy
    sock = tmp_path / "p.sock"
    # the machine's own name resolves to a local address on most systems
    me = socket.gethostname()
    px = netproxy.Proxy(sock, ["allowed.invalid", me], ports=(443, 9))
    try:
        assert _ask(sock, b"CONNECT example.org:443 HTTP/1.1\r\n\r\n").startswith(b"HTTP/1.1 403")
        assert _ask(sock, b"GET http://example.org/ HTTP/1.1\r\n\r\n").startswith(b"HTTP/1.1 405")
        assert any(x.startswith("example.org:443") for x in px.denied), px.denied
        try:
            addr = ipaddress_of(me)
        except OSError:
            addr = None
        if addr is not None and (addr.is_private or addr.is_loopback):
            srv = socket.socket()
            srv.bind(("0.0.0.0" if addr.version == 4 else "::", 0))
            srv.listen(1)
            port = srv.getsockname()[1]
            px.ports.add(port)
            try:
                got = _ask(sock, f"CONNECT {me}:{port} HTTP/1.1\r\n\r\n".encode())
                assert got.startswith(b"HTTP/1.1 403"), got
            finally:
                srv.close()
    finally:
        px.close()


def ipaddress_of(name):
    import ipaddress
    import socket
    return ipaddress.ip_address(socket.getaddrinfo(name, None)[0][4][0])


def test_default_allow_follows_the_provider(monkeypatch):
    from rrsi_evolve import netproxy
    env = {"ANTHROPIC_BASE_URL": "https://gw.corp.test:8443/v1"}
    assert "gw.corp.test" in netproxy.default_allow(env)
    assert "*.amazonaws.com" in netproxy.default_allow({"CLAUDE_CODE_USE_BEDROCK": "1"})
    assert "*.amazonaws.com" not in netproxy.default_allow({})


# ------------------------------------------- round 4: signals ------------
def test_nohup_ignored_sigterm_stays_ignored():
    code = ("import signal, sys; sys.path.insert(0, %r); "
            "signal.signal(signal.SIGHUP, signal.SIG_IGN); "
            "from rrsi_evolve import cli; sys.argv=['x','--help']\n"
            "try:\n    cli.main()\nexcept SystemExit:\n    pass\n"
            "print(signal.getsignal(signal.SIGHUP) is signal.SIG_IGN, "
            "signal.getsignal(signal.SIGTERM) is cli._exit_on_signal)") % str(PLUGIN / "lib")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                         timeout=60)
    assert out.stdout.split()[-2:] == ["True", "True"], out


def test_driver_passes_sigterm_to_its_child(tmp_path):
    pidf = tmp_path / "child.pid"
    child = f"import os, time; open({str(pidf)!r}, 'w').write(str(os.getpid())); time.sleep(60)"
    code = ("import signal, sys; sys.path.insert(0, %r)\n"
            "from rrsi_evolve import cli, driver\n"
            "signal.signal(signal.SIGTERM, cli._exit_on_signal)\n"
            "driver.CHILD_GRACE_S = 5\n"
            "driver._run([sys.executable, '-c', %r], open(%r, 'w'))\n"
            ) % (str(PLUGIN / "lib"), child, str(tmp_path / "log"))
    parent = subprocess.Popen([sys.executable, "-c", code])
    try:
        for _ in range(200):
            if pidf.exists() and pidf.read_text():
                break
            time.sleep(0.05)
        cpid = int(pidf.read_text())
        parent.send_signal(15)
        assert parent.wait(timeout=30) == 128 + 15
        assert _gone(cpid)
    finally:
        if parent.poll() is None:
            parent.kill()


# ------------------------------------------- round 5: sandbox review -----
def test_bwrap_trials_get_their_own_config_dir(monkeypatch):
    if not shutil.which("bwrap"):
        pytest.skip("no bwrap")
    top = Path(tempfile.mkdtemp(dir=Path.home(), prefix=".rrsi-test-"))
    try:
        dom = _dom(top, monkeypatch, {"sandbox": "bwrap", "keep_workspaces": True,
                                      "concurrency": 1})
        if dom._sandbox_mode() != "bwrap":
            pytest.skip("bwrap does not work here")
        monkeypatch.setenv("FAKE_PROBE", "probe.json")
        monkeypatch.setenv("FAKE_PROBE_LEAK", "1")
        runs = top / "runs"
        dom.run(dom.repo, runs, "j", ["hello-file"], 2)
        seen = []
        for j in (0, 1):
            res = json.loads((runs / "jobs" / "j" / f"hello-file__{j}" / "result.json")
                             .read_text())
            seen.append(json.loads((Path(res["workspace"]) / "probe.json").read_text()))
            shutil.rmtree(res["workspace"], ignore_errors=True)
        # the same path, a fresh dir: trial 1 does not see trial 0's file
        assert seen[0]["config_dir"] == seen[1]["config_dir"]
        assert not any(x.startswith("trial-mark-") for x in seen[1]["config_entries"])
    finally:
        shutil.rmtree(top, ignore_errors=True)


class _Net:
    def __init__(self, path):
        self.path = path
        self.forwards = {4000: str(Path(path).parent / "fwd-4000.sock")}


def test_net_dir_is_read_only_and_forwards_are_passed(tmp_path, monkeypatch):
    dom = _dom(tmp_path, monkeypatch)
    net = tmp_path / "net"
    lay = {"net": _Net(str(net / "proxy.sock")), "python": "/usr/bin/python3"}
    argv = dom._bwrap_argv("/w", [], [], {"hidden": [], "layout": lay})
    pairs = list(zip(argv, argv[1:], argv[2:]))
    assert ("--ro-bind", str(net), str(net)) in pairs
    assert ("--bind", str(net), str(net)) not in pairs
    assert "--unshare-net" in argv
    tail = argv[argv.index("-c") + 2:]
    assert tail == [f"3128={net / 'proxy.sock'}", f"4000={net / 'fwd-4000.sock'}", "--"]


def test_home_at_root_refuses_to_sandbox(tmp_path, monkeypatch):
    dom = _dom(tmp_path, monkeypatch)
    monkeypatch.setenv("HOME", "/")
    with pytest.raises(SystemExit):
        dom._bwrap_argv("/w", [], [], {"hidden": [], "layout": {}})


def test_prepare_keeps_api_key_binds_ca_and_cloud_files(tmp_path, monkeypatch):
    dom = _dom(tmp_path, monkeypatch, {"sandbox_network": "host"})
    cfgd = tmp_path / "cfgd"
    cfgd.mkdir()
    (cfgd / ".claude.json").write_text(json.dumps(
        {"primaryApiKey": "sk-test-not-real", "projects": {"/x": {}}, "userID": "u"}))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(cfgd))
    ca = tmp_path / "ca.pem"
    ca.write_text("x")
    aws = tmp_path / "aws-creds"
    aws.write_text("x")
    monkeypatch.setenv("NODE_EXTRA_CA_CERTS", str(ca))
    monkeypatch.setenv("CLAUDE_CODE_USE_BEDROCK", "1")
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", str(aws))
    base = tmp_path / "base"
    base.mkdir()
    lay = dom._prepare_sandbox(base, base / "bin")
    tmpl = json.loads((lay["template"] / ".claude.json").read_text())
    assert tmpl == {"primaryApiKey": "sk-test-not-real", "userID": "u"}
    assert str(ca) in lay["binds"] and str(aws) in lay["binds"]
    home = Path.home().resolve()
    for b in lay["binds"]:
        assert Path(b) != home and home not in Path(b).parents or \
            ".claude" not in Path(b).relative_to(home).parts[:1], b
    t = dom._trial_config(lay)
    assert (t / ".claude.json").read_text() == (lay["template"] / ".claude.json").read_text()
    assert t.parent == base and t != lay["cfg"]


def test_long_tmpdir_puts_the_run_dir_under_tmp(tmp_path, monkeypatch):
    A = _mod(_dom(tmp_path, monkeypatch))
    long = tmp_path / ("x" * 60)
    long.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(long))
    base = A._run_base()
    try:
        assert str(base).startswith("/tmp/rrsi-run-")
        assert len(os.fsencode(str(base / "net" / "fwd-65535.sock"))) < 100
    finally:
        shutil.rmtree(base, ignore_errors=True)


def test_proxy_chains_to_the_hosts_own_proxy(tmp_path):
    import socket
    import threading
    from rrsi_evolve import netproxy
    up = socket.socket()
    up.bind(("127.0.0.1", 0))
    up.listen(1)
    got = {}

    def serve():
        c, _ = up.accept()
        head = b""
        while b"\r\n\r\n" not in head:
            head += c.recv(4096)
        got["head"] = head.decode()
        c.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
        c.sendall(c.recv(100))          # echo one message
        c.close()
    threading.Thread(target=serve, daemon=True).start()
    via = netproxy.upstream({"HTTPS_PROXY": f"http://u:p%40w@127.0.0.1:{up.getsockname()[1]}"})
    px = netproxy.Proxy(tmp_path / "p.sock", ["api.example.com"], via=via)
    try:
        s = socket.socket(socket.AF_UNIX)
        s.settimeout(10)
        s.connect(str(tmp_path / "p.sock"))
        s.sendall(b"CONNECT api.example.com:443 HTTP/1.1\r\n\r\n")
        assert s.recv(64).startswith(b"HTTP/1.1 200")
        s.sendall(b"ping")
        assert s.recv(64) == b"ping"
        s.close()
        assert got["head"].startswith("CONNECT api.example.com:443 HTTP/1.1")
        assert "Proxy-Authorization: Basic dTpwQHc=" in got["head"]
    finally:
        px.close()
        up.close()
    assert netproxy._no_proxy("a.corp.test", 443, ".corp.test,localhost")
    assert not netproxy._no_proxy("corp.test.evil", 443, ".corp.test")


def test_gateways_are_trusted_or_forwarded(tmp_path):
    import socket
    import threading
    from rrsi_evolve import netproxy
    trusted, fwd, env = netproxy.gateways({"ANTHROPIC_BASE_URL": "https://10.1.2.3:8443/v1",
                                           "ANTHROPIC_BEDROCK_BASE_URL": "http://localhost:4000"})
    assert trusted == {("10.1.2.3", 8443)} and fwd == {4000: ("localhost", 4000)} and env == {}
    # a sandbox cannot listen below 1024 (or on the proxy's port): another
    # port inside, and the trial's base URL says so
    _, fwd, env = netproxy.gateways({"ANTHROPIC_BASE_URL": "http://localhost/v1"})
    assert fwd == {10080: ("localhost", 80)}
    assert env == {"ANTHROPIC_BASE_URL": "http://localhost:10080/v1"}
    _, fwd, env = netproxy.gateways({"ANTHROPIC_BASE_URL": "https://[::1]:3128"})
    assert fwd == {13128: ("::1", 3128)} and env["ANTHROPIC_BASE_URL"] == "https://[::1]:13128"
    echo = socket.socket()
    echo.bind(("127.0.0.1", 0))
    echo.listen(1)

    def serve():
        c, _ = echo.accept()
        c.sendall(c.recv(100))
        c.close()
    threading.Thread(target=serve, daemon=True).start()
    port = echo.getsockname()[1]
    px = netproxy.Proxy(tmp_path / "p.sock", [], trusted=trusted,
                        forwards={port: ("127.0.0.1", port)})
    try:
        assert px.allowed("10.1.2.3", 8443) and not px.allowed("10.1.2.3", 443)
        s = socket.socket(socket.AF_UNIX)
        s.settimeout(10)
        s.connect(px.forwards[port])
        s.sendall(b"hello")
        assert s.recv(64) == b"hello"
        s.close()
    finally:
        px.close()
        echo.close()


def test_a_stronger_model_in_the_session_scores_nothing(tmp_path, monkeypatch):
    dom = _dom(tmp_path, monkeypatch)
    assert dom._foreign_models({"modelUsage": {"claude-haiku-4-5": {}}}) == []
    assert dom._foreign_models({"modelUsage": {"claude-opus-4-1": {}}}) == ["claude-opus-4-1"]
    monkeypatch.setenv("FAKE_RESULT_JSON", json.dumps(
        {"modelUsage": {"claude-haiku-4-5-fake": {}, "claude-opus-4-1": {"costUSD": 1}}}))
    runs = tmp_path / "runs"
    dom.run(dom.repo, runs, "j", ["hello-file"], 1)
    res = _res(runs, "j", "hello-file")
    assert res["reward"] == 0.0 and "claude-opus-4-1" in res["policy_violation"]


def test_smoke_fails_a_harness_mcp_server_that_does_not_start(tmp_path, monkeypatch):
    dom = _dom(tmp_path, monkeypatch)
    _write(dom.repo / "harness" / ".mcp.json",
           json.dumps({"mcpServers": {"docs": {"command": "npx", "args": ["docs-mcp"]}}}))
    monkeypatch.setenv("FAKE_INIT_MCP", json.dumps([{"name": "docs", "status": "failed"}]))
    ok, info = dom.smoke(dom.repo, tmp_path / "runs", "smoke", ["hello-file"])
    assert ok is False and "MCP server docs (failed)" in json.dumps(info)
    monkeypatch.setenv("FAKE_INIT_MCP", json.dumps([{"name": "docs", "status": "connected"}]))
    ok, info = dom.smoke(dom.repo, tmp_path / "runs", "smoke2", ["hello-file"])
    assert ok is True, info


@pytest.mark.parametrize("text", [
    "---﻿\ndescription: optimize\nmodel: opus\neffort: max\n---\nbody\n",
    "--- \ndescription: d\nmodel: opus\n---\n",
    '---\nname: rev\ndescription: Reviews diffs\n? "mod\\\n  el"\n: opus\nnote: x --- y\n'
    'bad: [\n---\nReview.\n',
])
def test_frontmatter_as_claude_code_reads_it(tmp_path, monkeypatch, text):
    dom = _dom(tmp_path, monkeypatch)
    _write(dom.repo / "harness" / ".claude" / "commands" / "optimize.md", text)
    ok, info = dom._smoke_static(dom.repo)
    assert ok is False and "frontmatter" in info["detail"], info


def test_precheck_sees_a_bom_after_the_dashes(tmp_path, monkeypatch):
    dom = _dom(tmp_path, monkeypatch)
    cmd = "diff --git a/harness/.claude/commands/o.md b/harness/.claude/commands/o.md\n" \
          "+++ b/harness/.claude/commands/o.md\n@@ -0,0 +1,4 @@\n"
    hits = precheck(cmd + "+---﻿\n+model: opus\n+---\n", dom.critic_patterns)
    assert hits, hits


def test_harness_symlink_fan_out_is_bounded(tmp_path, monkeypatch):
    dom = _dom(tmp_path, monkeypatch)
    h = dom.repo / "harness"
    depth = 16
    for i in range(depth + 1):
        (h / f"d{i}").mkdir(parents=True)
    _write(h / f"d{depth}" / "x.md", "x\n")
    for i in range(depth):
        (h / f"d{i}" / "l1").symlink_to(f"../d{i + 1}")
        (h / f"d{i}" / "l2").symlink_to(f"../d{i + 1}")
    t = time.monotonic()
    entries, problems = dom._harness_entries(dom.repo)
    assert time.monotonic() - t < 20
    assert any("more than" in p for p in problems)


# ------------------------------------------- round 6: re-check fixes -----
def test_a_socket_bound_through_a_symlink_is_masked_where_it_is(tmp_path, monkeypatch):
    import socket
    dom = _dom(tmp_path, monkeypatch)
    real = tmp_path / "real"
    real.mkdir()
    (tmp_path / "link").symlink_to(real)
    bound = tmp_path / "link" / "s.sock"
    if len(os.fsencode(str(bound))) > 100 or not os.path.isfile("/usr/bin/python3"):
        pytest.skip("temp path too long for a unix socket / no system python")
    srv = socket.socket(socket.AF_UNIX)
    srv.bind(str(bound))
    srv.listen(1)
    try:
        dom.sandbox_rw = [str(real)]
        argv = dom._bwrap_argv("/", [], [], {"hidden": [], "layout": {}})
        assert str(real / "s.sock") in argv and str(bound) not in argv
        r = subprocess.run(argv + ["/usr/bin/python3", "-c",
                                   "import socket,sys; s=socket.socket(socket.AF_UNIX)\n"
                                   "try:\n s.connect(sys.argv[1]); print('open')\n"
                                   "except OSError: print('closed')", str(real / "s.sock")],
                           capture_output=True, text=True, timeout=60)
        assert r.returncode == 0 and r.stdout.strip() == "closed", r
    finally:
        srv.close()


def test_model_check_follows_the_policy_tier_and_fallbacks(tmp_path, monkeypatch):
    dom = _dom(tmp_path, monkeypatch, {"policy_model": "sonnet"})
    arn = "arn:aws:bedrock:us-east-1:1:application-inference-profile/a1b2"
    mu = {"modelUsage": {arn: {}, "claude-haiku-4-5-20251001": {}}}
    assert dom._foreign_models(mu) == [arn]
    monkeypatch.setenv("ANTHROPIC_DEFAULT_SONNET_MODEL", arn)
    assert dom._foreign_models(mu) == []
    assert dom._foreign_models({"modelUsage": {"claude-opus-4-1": {}}}) == ["claude-opus-4-1"]
    ev = [{"type": "system", "subtype": "model_refusal_fallback",
           "originalModel": "x", "fallbackModel": "claude-opus-4-1"}]
    assert dom._foreign_models({"modelUsage": {"claude-opus-4-1": {}}}, ev) == []
    dom.policy_model = "opusplan"
    assert dom._foreign_models({"modelUsage": {"claude-sonnet-4-5": {},
                                                "claude-opus-4-1": {}}}) == []
    dom.policy_model = "sonnet"
    assert dom._foreign_models({"modelUsage": {"gw-model": {"canonicalModel":
                                                            "claude-sonnet-4-5"}}}) == []


def test_many_plain_harness_files_are_not_capped(tmp_path, monkeypatch):
    dom = _dom(tmp_path, monkeypatch)
    A = _mod(dom)
    d = dom.repo / "harness" / "docs"
    d.mkdir(parents=True)
    for i in range(A.MAX_HARNESS_ENTRIES + 50):
        (d / f"{i}.md").write_text("x")
    entries, problems = dom._harness_entries(dom.repo)
    assert not problems and len(entries) > A.MAX_HARNESS_ENTRIES


def test_mcp_server_names_are_normalized_for_the_allow_rules(tmp_path, monkeypatch):
    dom = _dom(tmp_path, monkeypatch)
    ws = tmp_path / "ws"
    _write(ws / ".mcp.json", json.dumps({"mcpServers": {"my.docs server": {"command": "x"}}}))
    cmd = dom._policy_cmd(str(ws))
    allowed = cmd[cmd.index("--allowedTools") + 1].split(",")
    assert "mcp__my_docs_server" in allowed


def test_proxy_host_names_and_upstream_urls():
    from rrsi_evolve import netproxy
    assert netproxy._HOSTNAME.fullmatch("api.anthropic.com")
    for bad in ("evil.com@api.anthropic.com", "*.anthropic.com", "a..anthropic.com"):
        assert not netproxy._HOSTNAME.fullmatch(bad), bad
    assert netproxy.upstream({"HTTPS_PROXY": "http://proxy.corp"})[1] == 80
    up = netproxy.upstream({"HTTPS_PROXY": "https://proxy.corp:8443"})
    assert up[:2] == ("proxy.corp", 8443) and up[4] is True
    with pytest.raises(SystemExit):
        netproxy.upstream({"HTTPS_PROXY": "http://proxy.corp:abc"})


# ------------------------------------------- round 3 leftovers -----------
def _git(cwd, *a):
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *a], cwd=cwd,
                   check=True, capture_output=True)


def test_candidate_worktree_is_evaluated_as_committed(tmp_path, monkeypatch):
    dom = _dom(tmp_path, monkeypatch)
    repo = dom.repo
    _git(repo, "init", "-q")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "base")
    wt = tmp_path / "wt"
    _git(repo, "worktree", "add", "-q", "--detach", str(wt))
    dom._check_committed(wt)                         # clean: fine
    dom._check_committed(repo)                       # main checkout: its working copy
    (wt / "harness" / "CLAUDE.md").write_text("# changed after the commit\n")
    with pytest.raises(RuntimeError, match="changed after its commit"):
        dom._check_committed(wt)
    (repo / "harness" / "CLAUDE.md").write_text("# a working-copy edit\n")
    dom._check_committed(repo)


@pytest.mark.parametrize("rw", ["~", "/", "/tmp", "~/.claude", "HOME_PARENT"])
def test_sandbox_rw_may_not_reopen_the_layout(tmp_path, monkeypatch, rw):
    dom = _dom(tmp_path, monkeypatch)
    home = Path.home().resolve()
    path = str(home.parent) if rw == "HOME_PARENT" else os.path.expanduser(rw)
    dom.sandbox_rw = [path]
    with pytest.raises(SystemExit, match="sandbox_rw"):
        dom._bwrap_argv("/w", [], [], {"hidden": [], "layout": {}})


def test_a_policy_the_layout_hides_stops_the_run(monkeypatch):
    if not shutil.which("bwrap"):
        pytest.skip("no bwrap")
    top = Path(tempfile.mkdtemp(dir=Path.home(), prefix=".rrsi-test-"))
    try:
        dom = _dom(top, monkeypatch, {"sandbox": "bwrap"})
        if dom._sandbox_mode() != "bwrap":
            pytest.skip("bwrap does not work here")
        # a policy inside the (hidden) repo cannot start in a trial
        inside = dom.repo / "bin" / "claude"
        inside.parent.mkdir()
        shutil.copy(FAKE, inside)
        inside.chmod(0o755)
        monkeypatch.setenv("RRSI_EVOLVE_POLICY_BIN", str(inside))
        with pytest.raises(SystemExit, match="does not start in the sandbox"):
            dom.run(dom.repo, top / "runs", "j", ["hello-file"], 1)
    finally:
        shutil.rmtree(top, ignore_errors=True)
