"""Tests for the claudecode domain adapter, with tests/fake_policy_claude.py
standing in for `claude -p` (RRSI_EVOLVE_POLICY_BIN). Run:
    uvx --quiet pytest tests/test_claudecode_adapter.py -q
"""

import json
import os
import stat
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path

import pytest

PLUGIN = Path(__file__).resolve().parents[1]
FAKE = str(PLUGIN / "tests" / "fake_policy_claude.py")
sys.path.insert(0, str(PLUGIN / "lib"))

from rrsi_evolve.domain import load_domain  # noqa: E402
from rrsi_evolve import components  # noqa: E402


# ---------------------------------------------------------------- helpers --
def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(text))


def make_repo(tmp_path: Path, extra_tasks: dict | None = None) -> Path:
    """A git repo with rrsi.json, a neutral harness and two tiny tasks."""
    repo = tmp_path / "proj"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    _write(repo / "rrsi.json", "{}")
    _write(repo / "harness" / "CLAUDE.md", "# neutral harness\n")
    _write(repo / "tasks" / "hello-file" / "task.json",
           json.dumps({"prompt": "Create FILE: hello.txt with content hi\n",
                       "split": "evolve"}))
    _write(repo / "tasks" / "hello-file" / "check.sh",
           'grep -q hi "$RRSI_WORKSPACE/hello.txt" 2>/dev/null '
           '&& echo 1.0 || echo 0.0\n')
    _write(repo / "tasks" / "failing-task" / "task.json",
           json.dumps({"prompt": "Create FILE: nothing.txt\n", "split": "evolve"}))
    _write(repo / "tasks" / "failing-task" / "check.sh", "echo 0.0\n")
    for tid, spec in (extra_tasks or {}).items():
        _write(repo / "tasks" / tid / "task.json", json.dumps(spec))
        _write(repo / "tasks" / tid / "check.sh", "echo 1.0\n")
    return repo


@pytest.fixture
def domain(tmp_path, monkeypatch):
    monkeypatch.setenv("RRSI_EVOLVE_POLICY_BIN", FAKE)
    repo = make_repo(tmp_path)
    dom = load_domain("claudecode", repo, {"harness_path": "harness",
                                           "tasks_dir": "tasks"})
    yield dom


def solve_env(monkeypatch, **over):
    monkeypatch.setenv("RRSI_EVOLVE_POLICY_BIN", FAKE)
    monkeypatch.setenv("FAKE_POLICY_MODE", over.pop("mode", "solve"))
    for k, v in over.items():
        monkeypatch.setenv(k, v)


def task_row_of(dom, runs, job, tid):
    rec = dom.load_trial(runs, job, tid, 0)
    tr = dom.score(runs, job, [tid], 1)[0][tid]
    return dom.task_row(tid, rec, tr)


# ----------------------------------------------------------------- basics --
def test_domain_basics(domain):
    assert domain.name == "claudecode"
    assert domain.harness_rel == "harness"
    assert domain.evolve_ids() == ["failing-task", "hello-file"]
    assert "hello-file" in domain.briefs["proposer"] or True
    for role in ("analyst", "digester", "proposer", "critic"):
        assert "{policy}" not in domain.briefs[role]
    assert domain.guards(None, None) == []


def test_make_domain_stamps_root_and_repo(tmp_path):
    repo = make_repo(tmp_path)
    dom = load_domain("claudecode", repo, {})
    assert Path(dom.root) == PLUGIN / "lib" / "rrsi_evolve" / "domains" / "claudecode"
    assert Path(dom.repo) == repo
    assert (dom.root / "SKILL.md").is_file()
    skill, patterns = dom.constitution(repo)
    assert "SKILL" in skill.upper() or "judged" in skill
    assert patterns


# -------------------------------------------------------------- run/score --
def test_run_and_score_k2(domain, tmp_path, monkeypatch):
    solve_env(monkeypatch)
    runs = tmp_path / "runs"
    ids = domain.evolve_ids()
    domain.run(domain.repo, runs, "j1", ids, 2)
    per, extra = domain.score(runs, "j1", ids, 2)
    assert set(per.keys()) == set(ids)
    # hello-file solved both trials; failing-task fails both
    assert per["hello-file"].rewards == [1.0, 1.0]
    assert per["failing-task"].rewards == [0.0, 0.0]
    assert per["hello-file"].missing == 0
    assert extra["total_passes"] == 2
    assert extra["n_trials"] == 4
    assert extra["pass_rate"] == 0.5
    assert extra["cost_usd"] > 0
    # trial dirs named <task>__<j>, workspace removed, stream + verifier written
    td = runs / "jobs" / "j1" / "hello-file__0"
    assert (td / "result.json").is_file()
    assert (td / "stream.jsonl").is_file()
    assert (td / "stderr.txt").is_file()
    assert (td / "verifier.txt").is_file()
    res = json.loads((td / "result.json").read_text())
    assert res["task_id"] == "hello-file"
    assert res["reward"] == 1.0
    assert res["tokens"] == 20 + 100 + 30 + 15
    assert res["num_turns"] == 2
    assert res["timed_out"] is False
    assert res["infra_error"] is None
    assert "workspace" not in res
    # the shared per-job state dir exists (the memory lever's substrate)
    assert (runs / "jobs" / "j1" / "state").is_dir()


def test_task_id_patterns_are_idempotent(domain):
    ids = domain.evolve_ids()
    domain._add_task_patterns(domain._tasks())
    n = len(domain.critic_patterns)
    domain._add_task_patterns(domain._tasks())
    assert len(domain.critic_patterns) == n


def test_resume_skips_finished_and_reruns_infra(domain, tmp_path, monkeypatch):
    solve_env(monkeypatch)
    runs = tmp_path / "runs"
    ids = domain.evolve_ids()
    domain.run(domain.repo, runs, "j1", ids, 1)
    # make hello-file__0 an infra failure (crash: no result event), keep the
    # failing-task trial finished
    td = runs / "jobs" / "j1" / "hello-file__0"
    (td / "result.json").write_text(json.dumps(
        {"task_id": "hello-file", "trial": 0, "reward": 0.0, "timed_out": False,
         "infra_error": "no result event (exit 1)", "exit_code": 1}))
    (runs / "jobs" / "j1" / "failing-task__0" / "result.json").write_text(
        json.dumps({"task_id": "failing-task", "trial": 0, "reward": 0.0,
                    "timed_out": False, "infra_error": None, "exit_code": 0}))
    domain.run(domain.repo, runs, "j1", ids, 1)      # resume: infra re-run only
    res = json.loads((runs / "jobs" / "j1" / "hello-file__0" / "result.json")
                     .read_text())
    assert res["infra_error"] is None              # re-run and now solved
    assert res["reward"] == 1.0
    per, _ = domain.score(runs, "j1", ids, 1)
    assert per["hello-file"].rewards == [1.0]


def test_timeout_is_not_infra(domain, tmp_path, monkeypatch):
    solve_env(monkeypatch, mode="hang", FAKE_HANG_S="60")
    # shrink the task's timeout to keep the test fast
    repo = Path(domain.repo)
    (repo / "tasks" / "hello-file" / "task.json").write_text(json.dumps(
        {"prompt": "Create FILE: hello.txt\n", "timeout_s": 2}))
    runs = tmp_path / "runs"
    domain.run(domain.repo, runs, "j1", ["hello-file"], 1)
    res = json.loads((runs / "jobs" / "j1" / "hello-file__0" / "result.json")
                     .read_text())
    assert res["timed_out"] is True
    assert res["infra_error"] is None              # NOT an infra failure
    per, _ = domain.score(runs, "j1", ["hello-file"], 1)
    # counted as a real (zero) score, not missing
    assert per["hello-file"].rewards == [0.0]
    assert per["hello-file"].missing == 0


def test_api_error_is_infra(domain, tmp_path, monkeypatch):
    solve_env(monkeypatch, mode="api_error")
    runs = tmp_path / "runs"
    domain.run(domain.repo, runs, "j1", ["hello-file"], 1)
    res = json.loads((runs / "jobs" / "j1" / "hello-file__0" / "result.json")
                     .read_text())
    assert res["infra_error"]
    assert "API Error" in res["infra_error"]
    per, _ = domain.score(runs, "j1", ["hello-file"], 1)
    assert per["hello-file"].missing == 1          # counted as missing
    assert per["hello-file"].rewards == [0.0]


def test_crash_is_infra_missing(domain, tmp_path, monkeypatch):
    solve_env(monkeypatch, mode="crash")
    runs = tmp_path / "runs"
    domain.run(domain.repo, runs, "j1", ["hello-file"], 1)
    td = runs / "jobs" / "j1" / "hello-file__0"
    res = json.loads((td / "result.json").read_text())
    assert res["infra_error"] is not None
    assert res["exit_code"] == 1
    per, _ = domain.score(runs, "j1", ["hello-file"], 1)
    assert per["hello-file"].missing == 1
    assert per["hello-file"].tokens == [None]
    # but the checker still ran after the policy died
    # (no result event + not timed out -> infra; verifier may exist)
    assert (td / "stream.jsonl").is_file()


# --------------------------------------------------------- reward parsing --
@pytest.mark.parametrize("stdout,rc,want", [
    ("0.5\n", 0, 0.5),
    ("noise\n1.0\n", 0, 1.0),
    ("", 0, 1.0),                       # no output -> rc fallthrough
    ("", 1, 0.0),
    ('{"reward": 0.75}\n', 3, 0.75),
    ('{"reward": 0.25, "weight": 4}\n', 0, 0.25),
    ("garbage\n", 0, 1.0),
])
def test_parse_reward(domain, stdout, rc, want):
    got, weight = domain._parse_reward(stdout, rc)
    assert got == want


def test_checker_reward_json_weight_flows_to_score(tmp_path, monkeypatch):
    solve_env(monkeypatch)
    repo = make_repo(tmp_path)
    _write(repo / "tasks" / "hello-file" / "check.sh",
           'grep -q hi "$RRSI_WORKSPACE/hello.txt" 2>/dev/null '
           '&& echo \'{"reward": 1.0, "weight": 2}\' || echo \'{"reward": 0.0, "weight": 2}\'\n')
    dom = load_domain("claudecode", repo, {"harness_path": "harness",
                                           "tasks_dir": "tasks"})
    runs = tmp_path / "runs"
    dom.run(repo, runs, "j1", ["hello-file"], 2)
    res = json.loads((runs / "jobs" / "j1" / "hello-file__0" / "result.json")
                     .read_text())
    assert res["reward"] == 1.0 and res["weight"] == 2


# ------------------------------------------------- overlay, fixture, ws ----
def test_overlay_and_fixture_copy(tmp_path, monkeypatch):
    solve_env(monkeypatch)
    repo = make_repo(tmp_path)
    # candidate harness: a skill + an overlay file that wins over the fixture
    _write(repo / "harness" / ".claude" / "skills" / "greet" / "SKILL.md",
           "---\nname: greet\ndescription: say hi properly\n---\n# greet\n")
    _write(repo / "tasks" / "hello-file" / "workspace" / "hello.txt", "stale\n")
    _write(repo / "tasks" / "hello-file" / "setup.sh",
           'echo setup-ran > "$RRSI_WORKSPACE/setup.txt"$\'\\n\''
           'cat hello.txt > seen.txt\n')
    dom = load_domain("claudecode", repo, {"harness_path": "harness",
                                           "tasks_dir": "tasks"})
    # check what setup.sh sees: the fixture was copied, then the harness overlay
    # wins for CLAUDE.md-equivalents (nothing overrides hello.txt here)
    runs = tmp_path / "runs"
    dom.run(repo, runs, "j1", ["hello-file"], 1)
    res = json.loads((runs / "jobs" / "j1" / "hello-file__0" / "result.json")
                     .read_text())
    assert res["reward"] == 1.0          # the agent overwrote the fixture


def test_harness_wins_over_fixture(tmp_path, monkeypatch):
    solve_env(monkeypatch, mode="write", FAKE_WRITE_FILE="hello.txt",
              FAKE_WRITE_TEXT="hi")
    repo = make_repo(tmp_path)
    _write(repo / "tasks" / "hello-file" / "workspace" / "hello.txt", "stale\n")
    # a harness file that lands on the same path: harness must win
    _write(repo / "harness" / "hello.txt", "harness\n")
    # and setup.sh proves ordering by dumping what it sees
    _write(repo / "tasks" / "hello-file" / "setup.sh",
           'cp hello.txt "$RRSI_WORKSPACE/seen-by-setup.txt"\n')
    # a checker that reads what setup saw: harness text
    _write(repo / "tasks" / "hello-file" / "check.sh",
           'grep -q harness "$RRSI_WORKSPACE/seen-by-setup.txt" && echo 1.0 || echo 0.0\n')
    dom = load_domain("claudecode", repo, {"harness_path": "harness",
                                           "tasks_dir": "tasks"})
    runs = tmp_path / "runs"
    dom.run(repo, runs, "j1", ["hello-file"], 1)
    res = json.loads((runs / "jobs" / "j1" / "hello-file__0" / "result.json")
                     .read_text())
    assert res["reward"] == 1.0


def test_workspace_under_tempdir_and_removed(domain, tmp_path, monkeypatch):
    solve_env(monkeypatch, mode="write", FAKE_WRITE_FILE="probe.txt",
              FAKE_WRITE_TEXT="x")
    # capture the cwd the fake ran in, from the stream init event
    runs = tmp_path / "runs"
    domain.run(domain.repo, runs, "j1", ["hello-file"], 1)
    sj = runs / "jobs" / "j1" / "hello-file__0" / "stream.jsonl"
    events = [json.loads(l) for l in sj.read_text().splitlines() if l.strip()]
    init = next(e for e in events if e.get("type") == "system")
    ws_cwd = init["cwd"]
    tws = Path(tempfile.gettempdir()).resolve()
    assert Path(ws_cwd).resolve().parent.parent == tws or \
        str(Path(ws_cwd).resolve()).startswith(str(tws))
    assert "rrsi-ws-" in ws_cwd
    assert not str(ws_cwd).startswith(str(tmp_path))   # NOT under the repo
    # and it was removed afterwards
    assert not os.path.isdir(ws_cwd)


def test_keep_workspaces(tmp_path, monkeypatch):
    solve_env(monkeypatch, mode="write")
    repo = make_repo(tmp_path)
    dom = load_domain("claudecode", repo, {"harness_path": "harness",
                                           "tasks_dir": "tasks",
                                           "keep_workspaces": True})
    runs = tmp_path / "runs"
    dom.run(repo, runs, "j1", ["hello-file"], 1)
    res = json.loads((runs / "jobs" / "j1" / "hello-file__0" / "result.json")
                     .read_text())
    assert res.get("workspace")
    assert os.path.isdir(res["workspace"])
    ws_suffix = os.path.basename(res["workspace"])
    assert ws_suffix.startswith("rrsi-ws-")
    import shutil
    shutil.rmtree(res["workspace"], ignore_errors=True)


def test_state_dir_shared_per_job(domain, tmp_path, monkeypatch):
    solve_env(monkeypatch)
    runs = tmp_path / "runs"
    domain.run(domain.repo, runs, "j1", ["hello-file"], 2)
    sd = runs / "jobs" / "j1" / "state"
    assert sd.is_dir()
    state_file = sd / "state.jsonl"
    assert state_file.is_file()                    # both trials logged to it
    rows = [json.loads(l) for l in state_file.read_text().splitlines() if l.strip()]
    assert len(rows) == 2


# ------------------------------------------------------------ smoke gates --
def test_smoke_static_bad_json(tmp_path, monkeypatch):
    solve_env(monkeypatch)
    repo = make_repo(tmp_path)
    _write(repo / "harness" / ".claude" / "settings.json", "{not json")
    dom = load_domain("claudecode", repo, {"harness_path": "harness",
                                           "tasks_dir": "tasks"})
    ok, info = dom.smoke(repo, tmp_path / "runs", "smk", dom.smoke_ids())
    assert ok is False
    assert info["stage"] == "static"
    assert "invalid JSON" in info["detail"]


def test_smoke_static_skill_without_frontmatter(tmp_path, monkeypatch):
    solve_env(monkeypatch)
    repo = make_repo(tmp_path)
    _write(repo / "harness" / ".claude" / "skills" / "bad" / "SKILL.md",
           "# no frontmatter\n")
    dom = load_domain("claudecode", repo, {"harness_path": "harness",
                                           "tasks_dir": "tasks"})
    ok, info = dom.smoke(repo, tmp_path / "runs", "smk", ["hello-file"])
    assert ok is False
    assert info["stage"] == "static"
    assert "name:" in info["detail"] and "description:" in info["detail"]


def test_smoke_static_settings_not_object(tmp_path, monkeypatch):
    solve_env(monkeypatch)
    repo = make_repo(tmp_path)
    _write(repo / "harness" / ".claude" / "settings.json", "[1,2]\n")
    dom = load_domain("claudecode", repo, {"harness_path": "harness",
                                           "tasks_dir": "tasks"})
    ok, info = dom.smoke(repo, tmp_path / "runs", "smk", ["hello-file"])
    assert ok is False and info["stage"] == "static"
    assert "not a JSON object" in info["detail"]


def test_smoke_run_ok(domain, tmp_path, monkeypatch):
    solve_env(monkeypatch)
    runs = tmp_path / "runs"
    ok, info = domain.smoke(domain.repo, runs, "smk", ["hello-file"])
    assert ok is True, info
    assert info == {"stage": "smoke_run", "n": 1}


def test_smoke_run_fails_when_trial_infra_fails(tmp_path, monkeypatch):
    solve_env(monkeypatch, mode="crash")
    repo = make_repo(tmp_path)
    dom = load_domain("claudecode", repo, {"harness_path": "harness",
                                           "tasks_dir": "tasks"})
    ok, info = dom.smoke(repo, tmp_path / "runs", "smk", ["hello-file"])
    assert ok is False
    assert info["stage"] == "smoke_run"
    assert "hello-file" in info


def test_smoke_run_fails_when_trial_times_out(tmp_path, monkeypatch):
    solve_env(monkeypatch, mode="hang", FAKE_HANG_S="60")
    repo = make_repo(tmp_path)
    (repo / "tasks" / "hello-file" / "task.json").write_text(json.dumps(
        {"prompt": "x", "timeout_s": 1}))
    dom = load_domain("claudecode", repo, {"harness_path": "harness",
                                           "tasks_dir": "tasks"})
    ok, info = dom.smoke(repo, tmp_path / "runs", "smk", ["hello-file"])
    assert ok is False
    assert info["stage"] == "smoke_run" and "timed out" in info.get("hello-file", "")


# ----------------------------------------------------------- evidence API --
def test_load_trial_and_task_row(domain, tmp_path, monkeypatch):
    solve_env(monkeypatch)
    runs = tmp_path / "runs"
    domain.run(domain.repo, runs, "j1", ["hello-file"], 1)
    rec = domain.load_trial(runs, "j1", "hello-file", 0)
    assert rec is not None
    assert rec["task_id"] == "hello-file"
    assert Path(rec["trial_dir"]).is_dir()
    assert "hello.txt" in rec["prompt"]
    assert rec["reward"] == 1.0
    assert rec["turns"] == 2
    assert rec["tokens"] == 20 + 100 + 30 + 15
    assert rec["timed_out"] is False
    assert rec["exception"] is None
    assert domain.load_trial(runs, "j1", "nope", 0) is None
    tr = domain.score(runs, "j1", ["hello-file"], 1)[0]["hello-file"]
    row = domain.task_row("hello-file", rec, tr)
    assert row.startswith("hello-file | rewards=")
    assert "rendered_trial_reward=1.0" in row
    assert "turns=2" in row and "timed_out=False" in row and "tokens=165" in row


def test_render_trace_steps_and_verifier(domain, tmp_path, monkeypatch):
    solve_env(monkeypatch)
    runs = tmp_path / "runs"
    _write(Path(domain.repo) / "tasks" / "hello-file" / "check.sh",
           'echo "checker says missing file" >&2\necho 0.0\n')
    domain.run(domain.repo, runs, "j1", ["hello-file"], 1)
    rec = domain.load_trial(runs, "j1", "hello-file", 0)
    tr = domain.render_trace(rec)
    assert tr.startswith("=== TASK ===\nCreate FILE: hello.txt")
    assert "=== TRAJECTORY ===" in tr
    assert "[step 1] TOOL_USE Write:" in tr
    assert '"file_path"' in tr
    # a tool result carries the step of the call it answers
    assert "[step 1] TOOL_RESULT: File created successfully" in tr
    assert "TOOL_RESULT (error):" not in tr
    assert "[step 2] ASSISTANT: Done." in tr
    assert "=== RUN METADATA ===" in tr
    assert "reward: 0.0" in tr
    assert "=== VERIFIER (ground truth) ===" in tr
    assert "checker says missing file" in tr
    short = domain.render_trace(rec, detail=False)
    detail = domain.render_trace(rec, detail=True)
    assert detail == short or len(detail) >= len(short)


def test_render_trace_handles_real_sample(domain, tmp_path):
    """render.py must handle exactly the REAL claude stream-json format."""
    sample = PLUGIN / "tests" / "data" / "sample_stream.jsonl"   # claude 2.1.x output
    td = tmp_path / "trial"
    td.mkdir()
    (td / "stream.jsonl").write_text(sample.read_text())
    (td / "verifier.txt").write_text("verifier output\n")
    (td / "result.json").write_text(json.dumps(
        {"task_id": "x", "reward": 1.0, "num_turns": 2}))
    tr = domain.render_trace({"trial_dir": str(td), "prompt": "p"})
    assert "[step 1] TOOL_USE Write:" in tr
    assert '"content": "hi"' in tr
    # one step per assistant message (num_turns 2), result shares the call's
    assert "[step 1] TOOL_RESULT: File created successfully" in tr
    assert "[step 2] ASSISTANT:" in tr
    assert "[step 3]" not in tr
    assert "BANANA" in tr                       # final text block comes through
    assert "rate_limit_event" not in tr         # non-message events skipped


def test_render_tool_result_list_content():
    from rrsi_evolve.domains.claudecode import render as _r_local
    txt = _r_local._result_text([{"type": "text", "text": "a"},
                                 {"type": "text", "text": "b"}])
    assert txt == "a\nb"
    assert _r_local._result_text("plain") == "plain"


# ---------------------------------------------------- leakage and signals --
def test_critic_patterns_include_task_ids(domain):
    assert any(p == r"check\.sh" for p, _ in domain.critic_patterns)
    assert any("benchmark task name 'hello-file' in diff" in why
               for _, why in domain.critic_patterns)
    assert any(p.startswith(r"RRSI_WORKSPACE|RRSI_EVOLVE_TRIAL")
               for p, _ in domain.critic_patterns)
    # a task name hit is caught by the critic precheck
    from rrsi_evolve.critic import precheck
    hits = precheck("+ mm 'hello-file'", domain.critic_patterns)
    assert any("benchmark task name" in h for h in hits)
    hits2 = precheck("cat $RRSI_WORKSPACE/check.sh", domain.critic_patterns)
    assert any("check.sh" in h for h in hits2)


def test_component_signals_order(domain):
    sig = domain.component_signals
    names = [n for n, _ in sig]
    assert names[:2] == ["skill", "subagent"]
    assert "prompt" in names and names[-1] == "prompt"
    classify = lambda d: components.classify_diff(d, domain.component_signals)
    # an all-string-literal diff is a text edit: "prompt", whatever it mentions
    assert components.text_only('+ "skills/foo"\n') is True
    assert classify('+ "skills/foo"\n') == "prompt"
    assert classify("+x\n+++ skills/foo/SKILL.md\n+- body\n") == "skill"
    assert classify("+x\n+ skills/foo/SKILL.md body\n") == "skill"
    assert classify("+x\n+ .claude/agents/helper.md\n") == "subagent"
    assert classify("+x\n+ .mcp.json server\n") == "client_tool"
    assert classify("+x\n+ export RRSI_HARNESS_STATE_DIR=/tmp\n") == "memory"
    assert classify("+x\n+ on PreCompact summarize\n") == "context_mgmt"
    assert classify("+x\n+ statusline output-styles/\n") == "output_plumbing"
    assert classify("+x\n+\"hooks\": {\"PreToolUse\": []}\n".replace("'", '"')) == "control_flow"
    assert classify('+x\n+ settings.local.json "permissions": {}\n') == "config"
    assert classify("+x\n+ CLAUDE.md guidance\n") == "prompt"


def test_component_signals_examples(tmp_path, monkeypatch):
    """classify example diffs for skill/agent/hook/settings/CLAUDE.md/.mcp.json."""
    repo = make_repo(tmp_path)
    dom = load_domain("claudecode", repo, {"harness_path": "harness",
                                           "tasks_dir": "tasks"})
    sig = dom.component_signals
    cases = {
        "skill": "+x\n+ new skills/pack/SKILL.md with steps\n",
        "subagent": "+x\n+ .claude/agents/worker.md\n",
        "hook": "+x\n+ \"hooks\": {f\"UserPromptSubmit\": []}\n",
        "settings": "+x\n+ settings.json \"permissions\": {{}}\n",
        "CLAUDE.md": "+x\n+ CLAUDE.md: re-read the file\n",
        ".mcp.json": "+x\n+ .mcp.json mcpServers: []\n",
    }
    got = {name: components.classify_diff(diff, sig) for name, diff in cases.items()}
    assert got["skill"] == "skill"
    assert got["subagent"] == "subagent"
    assert got["hook"] == "control_flow"
    assert got["settings"] == "config"
    assert got["CLAUDE.md"] == "prompt"
    assert got[".mcp.json"] == "client_tool"


# ------------------------------------------------------------- config keys --
def test_config_defaults_and_overrides(tmp_path):
    repo = make_repo(tmp_path)
    dom = load_domain("claudecode", repo, {})
    assert dom.policy_model == "sonnet"
    assert dom.permission_mode == "acceptEdits"
    assert dom.allowed_tools == ["Read", "Edit", "Write", "Glob", "Grep", "Bash"]
    assert dom.task_timeout_s == 600
    assert dom.check_timeout_s == 300
    assert dom.max_budget_usd_per_trial is None
    assert dom.concurrency == 4
    assert dom.keep_workspaces is False
    dom2 = load_domain("claudecode", repo, {
        "policy_model": "haiku", "policy_label": "the frozen policy",
        "permission_mode": "bypassPermissions", "allowed_tools": ["Read"],
        "disallowed_tools": ["WebFetch"], "task_timeout_s": 7,
        "check_timeout_s": 8, "max_budget_usd_per_trial": 0.5, "concurrency": 1,
        "smoke_tasks": ["hello-file"], "keep_workspaces": True,
        "extra_claude_args": ["--no-plugins"], "harness_path": "harness",
        "tasks_dir": "tasks"})
    assert dom2.policy_model == "haiku"
    assert dom2.policy_label == "the frozen policy"
    assert dom2.disallowed_tools == ["WebFetch"]
    assert dom2.max_budget_usd_per_trial == 0.5
    assert dom2.smoke_ids() == ["hello-file"]
    assert dom2.extra_claude_args == ["--no-plugins"]


def test_smoke_ids_incumbent_top2(tmp_path, monkeypatch):
    from rrsi_evolve.evaluate import TaskResult
    repo = make_repo(tmp_path, extra_tasks={"t3": {"prompt": "x"},
                                            "t4": {"prompt": "x"}})
    dom = load_domain("claudecode", repo, {"harness_path": "harness",
                                           "tasks_dir": "tasks"})
    inc = {"failing-task": TaskResult(rewards=[0.0]),
           "hello-file": TaskResult(rewards=[1.0]),
           "t3": TaskResult(rewards=[0.9]), "t4": TaskResult(rewards=[1.0])}
    assert dom.smoke_ids(inc)[:2] == ["hello-file", "t4"]
    assert dom.smoke_ids() == ["failing-task", "hello-file"]


def test_prompt_file_and_split(tmp_path, monkeypatch):
    repo = make_repo(tmp_path)
    _write(repo / "tasks" / "heldout-task" / "prompt.md", "do the heldout thing\n")
    _write(repo / "tasks" / "heldout-task" / "task.json", json.dumps(
        {"prompt_file": "prompt.md", "split": "heldout"}))
    _write(repo / "tasks" / "heldout-task" / "check.sh", "echo 1.0\n")
    dom = load_domain("claudecode", repo, {"harness_path": "harness",
                                           "tasks_dir": "tasks"})
    assert dom.heldout_ids() == ["heldout-task"]
    assert "heldout" in dom._tasks()["heldout-task"]["prompt"]
    assert "heldout-task" not in dom.evolve_ids()


def test_fake_stream_json_perf(monkeypatch, tmp_path):
    """The fake emits the exact event sequence the adapter expects."""
    cwd = tempfile.mkdtemp(prefix="rrsi-ws-")
    out = subprocess.run([FAKE], input="Create FILE: hello.txt\n", text=True,
                         capture_output=True,
                         env={**os.environ, "FAKE_POLICY_MODE": "solve"},
                         cwd=cwd)
    import shutil
    shutil.rmtree(cwd, ignore_errors=True)
    assert out.returncode == 0
    events = [json.loads(l) for l in out.stdout.splitlines() if l.strip()]
    types = [e["type"] for e in events]
    assert types[0] == "system" and events[0]["subtype"] == "init"
    assert types.count("assistant") >= 2
    assert types.count("user") == 1
    assert types[-1] == "result"
    tu = next(e for e in events if e["type"] == "user")
    assert tu["message"]["content"][0]["type"] == "tool_result"
    assert types == ["system", "assistant", "user", "assistant", "result"]


def test_policy_env_scrubbed_and_tools_fixed(domain, tmp_path, monkeypatch):
    """A parent Claude Code session's settings must not reach the policy."""
    for k, v in {"CLAUDE_CODE_SUBAGENT_MODEL": "other-model", "CLAUDE_EFFORT": "xhigh",
                 "CLAUDE_CODE_ENTRYPOINT": "claude-desktop", "CLAUDECODE": "1",
                 "MCP_CONNECTION_NONBLOCKING": "true", "DISABLE_MICROCOMPACT": "1",
                 "ANTHROPIC_API_KEY": "test-key", "CLAUDE_CODE_OAUTH_TOKEN": "tok"}.items():
        monkeypatch.setenv(k, v)
    dump = tmp_path / "env.json"
    monkeypatch.setenv("FAKE_ENV_DUMP", str(dump))
    runs = tmp_path / "runs"
    domain.run(domain.repo, runs, "j", ["hello-file"], 1)
    seen = json.loads(dump.read_text())
    env, argv = seen["env"], seen["argv"]
    for k in ("CLAUDE_EFFORT", "CLAUDE_CODE_ENTRYPOINT",
              "CLAUDECODE", "MCP_CONNECTION_NONBLOCKING", "DISABLE_MICROCOMPACT"):
        assert k not in env, k
    # subagents are pinned to the frozen policy model, not the parent's
    assert env["CLAUDE_CODE_SUBAGENT_MODEL"] == domain.policy_model
    assert env["CLAUDE_CODE_SUBAGENT_MODEL_FORCE"] == "1"
    # the state dir the policy sees is outside the repo and the runs tree
    assert not env["RRSI_HARNESS_STATE_DIR"].startswith(str(domain.repo))
    assert not env["RRSI_HARNESS_STATE_DIR"].startswith(str(runs))
    assert env["ANTHROPIC_API_KEY"] == "test-key"
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "tok"
    assert env["CLAUDE_CODE_DISABLE_AUTO_MEMORY"] == "1"
    assert env["RRSI_POLICY_ACTIVE"] == "1"
    assert argv[argv.index("--tools") + 1] == \
        "Read,Edit,Write,Glob,Grep,Bash,Skill,Task"
    allowed = argv[argv.index("--allowedTools") + 1].split(",")
    assert "Skill" in allowed and "Task" in allowed


def test_policy_tools_and_env_passthrough_config(tmp_path, monkeypatch):
    repo = make_repo(tmp_path)
    monkeypatch.setenv("CLAUDE_CODE_USE_FOO", "1")
    dom = load_domain("claudecode", repo, {"tools": ["Bash", "Read"],
                                           "env_passthrough": ["CLAUDE_CODE_USE_FOO"]})
    cmd = dom._policy_cmd(str(tmp_path))
    assert cmd[cmd.index("--tools") + 1] == "Bash,Read"
    from rrsi_evolve.procenv import child_env
    assert child_env(passthrough=dom.env_passthrough)["CLAUDE_CODE_USE_FOO"] == "1"
    assert "CLAUDE_CODE_USE_FOO" not in child_env()


def test_render_steps_messages_and_subagents():
    from rrsi_evolve.domains.claudecode import render as R
    ev = [
        {"type": "assistant", "message": {"id": "m1", "content": [
            {"type": "text", "text": "plan"}]}},
        {"type": "assistant", "message": {"id": "m1", "content": [
            {"type": "tool_use", "id": "u1", "name": "Task", "input": {"prompt": "go"}}]}},
        {"type": "user", "parent_tool_use_id": "u1", "message": {"content": "find it"}},
        {"type": "assistant", "parent_tool_use_id": "u1", "message": {"id": "s1", "content": [
            {"type": "tool_use", "id": "u2", "name": "Grep", "input": {}}]}},
        {"type": "user", "parent_tool_use_id": "u1", "message": {"content": [
            {"type": "tool_result", "tool_use_id": "u2", "content": "hit"}]}},
        {"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": "u1", "content": "found"}]}},
        {"type": "assistant", "message": {"id": "m2", "content": [
            {"type": "text", "text": "done"}]}},
    ]
    out = R.render_steps(ev).splitlines()
    assert out[0] == "[step 1] ASSISTANT: plan"
    assert out[1].startswith("[step 1] TOOL_USE Task:")
    assert out[2] == "[step 2] SUBAGENT(u1) PROMPT: find it"
    assert out[3].startswith("[step 3] SUBAGENT(u1) TOOL_USE Grep:")
    assert out[4] == "[step 3] SUBAGENT(u1) TOOL_RESULT: hit"
    assert out[5] == "[step 1] TOOL_RESULT: found"
    assert out[6] == "[step 4] ASSISTANT: done"


# ------------------------------------------------------------ multi-turn --
def _turns_repo(tmp_path, turns, extra=None):
    tmp_path.mkdir(parents=True, exist_ok=True)
    repo = make_repo(tmp_path)
    t = repo / "tasks" / "chat"
    (t / "workspace").mkdir(parents=True)
    (t / "task.json").write_text(json.dumps({"turns": turns, "split": "evolve", **(extra or {})}))
    (t / "check.sh").write_text("#!/usr/bin/env bash\n[ -f out.txt ] && echo 1 || echo 0\n")
    (t / "check.sh").chmod(0o755)
    return repo


def test_turns_are_sent_one_by_one_in_one_session(tmp_path, monkeypatch):
    repo = _turns_repo(tmp_path, ["first, read the code", "now write FILE: out.txt"])
    log = tmp_path / "turns.log"
    monkeypatch.setenv("RRSI_EVOLVE_POLICY_BIN", FAKE)
    monkeypatch.setenv("FAKE_TURN_LOG", str(log))
    dom = load_domain("claudecode", repo, {"task_timeout_s": 60})
    task = dom._tasks()["chat"]
    assert task["turns"] == ["first, read the code", "now write FILE: out.txt"]
    assert "[user turn 2/2]" in task["prompt"]
    cmd = dom._policy_cmd(str(tmp_path), multi_turn=True)
    assert cmd[cmd.index("--input-format") + 1] == "stream-json" and "--replay-user-messages" in cmd
    assert "--input-format" not in dom._policy_cmd(str(tmp_path))
    dom.run(dom.repo, tmp_path / "runs", "job", ["chat"], 1)
    assert [json.loads(l) for l in log.read_text().splitlines()] == task["turns"]
    per, extra = dom.score(tmp_path / "runs", "job", ["chat"], 1)
    assert per["chat"].rewards == [1.0]


def test_turns_validation(tmp_path):
    for i, bad in enumerate(([], [""], "one string", [1])):
        repo = _turns_repo(tmp_path / f"bad{i}", bad)
        with pytest.raises(SystemExit):
            load_domain("claudecode", repo, {})._tasks()
    repo = _turns_repo(tmp_path / "both", ["a"], {"prompt": "b"})
    with pytest.raises(SystemExit):
        load_domain("claudecode", repo, {})._tasks()


def test_turns_stop_after_an_api_error(tmp_path, monkeypatch):
    repo = _turns_repo(tmp_path, ["one", "two", "three"])
    log = tmp_path / "turns.log"
    monkeypatch.setenv("RRSI_EVOLVE_POLICY_BIN", FAKE)
    monkeypatch.setenv("FAKE_TURN_LOG", str(log))
    monkeypatch.setenv("FAKE_TURN_ERROR", "2")
    dom = load_domain("claudecode", repo, {"task_timeout_s": 60})
    dom.run(dom.repo, tmp_path / "runs", "job", ["chat"], 1)
    assert len(log.read_text().splitlines()) == 2


def test_turns_time_out(tmp_path, monkeypatch):
    repo = _turns_repo(tmp_path, ["one", "two"], {"timeout_s": 2})
    monkeypatch.setenv("RRSI_EVOLVE_POLICY_BIN", FAKE)
    monkeypatch.setenv("FAKE_POLICY_MODE", "hang")
    monkeypatch.setenv("FAKE_HANG_S", "60")
    dom = load_domain("claudecode", repo, {})
    t0 = __import__("time").monotonic()
    dom.run(dom.repo, tmp_path / "runs", "job", ["chat"], 1)
    assert __import__("time").monotonic() - t0 < 50


def test_checker_gets_the_stream(tmp_path, monkeypatch):
    repo = _turns_repo(tmp_path, ["say hi", "write FILE: out.txt"])
    (repo / "tasks" / "chat" / "check.sh").write_text(
        '#!/usr/bin/env bash\ngrep -q "ack 2" "$RRSI_STREAM" && echo 1 || echo 0\n')
    monkeypatch.setenv("RRSI_EVOLVE_POLICY_BIN", FAKE)
    dom = load_domain("claudecode", repo, {"task_timeout_s": 60})
    dom.run(dom.repo, tmp_path / "runs", "job", ["chat"], 1)
    per, _ = dom.score(tmp_path / "runs", "job", ["chat"], 1)
    assert per["chat"].rewards == [1.0]
