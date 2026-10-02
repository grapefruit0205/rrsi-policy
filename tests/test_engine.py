"""End-to-end tests through the CLI, with tests/fake_claude.py standing in for
`claude -p`. Run: python3 -m pytest tests"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
BIN = [sys.executable, str(ROOT / "bin" / "rrsi-policy")]
FAKE = str(ROOT / "tests" / "fake_claude.py")
sys.path.insert(0, str(ROOT / "lib"))

from rrsi_policy import selection  # noqa: E402


@pytest.fixture
def proj(tmp_path, monkeypatch):
    p = tmp_path / "proj"
    p.mkdir()
    subprocess.run(["git", "init", "-q", str(p)], check=True)
    (p / "CLAUDE.md").write_text("# rules\n")
    monkeypatch.setenv("RRSI_CLAUDE_BIN", FAKE)
    monkeypatch.setenv("RRSI_POLICY_LEDGER", str(tmp_path / "state" / "ledger.jsonl"))
    monkeypatch.setenv("FAKE_LOG", str(tmp_path / "fake.log"))
    monkeypatch.delenv("RRSI_POLICY_ACTIVE", raising=False)
    monkeypatch.delenv("RRSI_POLICY_CONFIG", raising=False)
    return p


def run(args, stdin="", env=None, cwd=None):
    return subprocess.run(BIN + args, input=stdin, capture_output=True, text=True,
                          env={**os.environ, **(env or {})}, cwd=cwd)


def event(proj, name, tid, tool, ti):
    return json.dumps({"hook_event_name": name, "session_id": "s1", "tool_use_id": tid,
                       "cwd": str(proj), "tool_name": tool, "tool_input": ti})


def hook(proj, tid, tool, ti, env=None):
    out = run(["hook"], event(proj, "PreToolUse", tid, tool, ti), env).stdout.strip()
    return json.loads(out)["hookSpecificOutput"] if out else None


def edit(proj, tid, old, new, env=None):
    """Pre-hook, apply the edit if not denied, post-hook: what Claude Code does."""
    ti = {"file_path": str(proj / "CLAUDE.md"), "old_string": old, "new_string": new}
    res = hook(proj, tid, "Edit", ti, env)
    if res and res["permissionDecision"] == "deny":
        return res
    p = proj / "CLAUDE.md"
    p.write_text(p.read_text().replace(old, new, 1))
    run(["hook"], event(proj, "PostToolUse", tid, "Edit", ti))
    return res


# ---------------------------------------------------------------- routing --
def test_routes(proj):
    def route(tool, ti):
        return json.loads(run(["route"], event(proj, "PreToolUse", "t", tool, ti)).stdout)["policy"]
    assert route("Edit", {"file_path": str(proj / "CLAUDE.md"), "old_string": "a", "new_string": "b"}) == "harness-critic"
    assert route("Write", {"file_path": str(proj / ".claude/skills/x/SKILL.md"), "content": "x"}) == "harness-critic"
    assert route("Edit", {"file_path": str(proj / "src/app.py"), "old_string": "a", "new_string": "b"}) == "secrets"
    assert route("Bash", {"command": "echo '- rule' >> CLAUDE.md"}) == "harness-critic"
    assert route("Bash", {"command": "cat CLAUDE.md"}) is None
    assert route("Bash", {"command": "ls"}) is None


# ------------------------------------------------------------- decisions --
def test_accept_prints_nothing_never_allow(proj):
    assert edit(proj, "t1", "# rules\n", "# rules\n- run tests\n") is None


def test_reject_denies_with_reason(proj):
    res = hook(proj, "t1", "Edit", {"file_path": str(proj / "CLAUDE.md"), "old_string": "# rules",
                                    "new_string": "# rules\n- if task is fix-git run fsck"},
               {"FAKE_VERDICT": "reject"})
    assert res["permissionDecision"] == "deny"
    assert "task_specific" in res["permissionDecisionReason"]


def test_uncertain_asks(proj):
    res = hook(proj, "t1", "Edit", {"file_path": str(proj / "CLAUDE.md"), "old_string": "a", "new_string": "b"},
               {"FAKE_VERDICT": "uncertain"})
    assert res["permissionDecision"] == "ask"


def test_secret_denied_without_llm(proj, tmp_path):
    res = hook(proj, "t1", "Write", {"file_path": str(proj / "cfg.py"), "content": 'K = "AKIAABCDEFGHIJKLMNOP"'})
    assert res["permissionDecision"] == "deny"
    assert not (tmp_path / "fake.log").exists()


def test_removing_a_secret_is_fine(proj):
    (proj / "cfg.py").write_text('K = "AKIAABCDEFGHIJKLMNOP"\n')
    assert hook(proj, "t1", "Write", {"file_path": str(proj / "cfg.py"), "content": "K = None\n"}) is None


def test_llm_failure_asks(proj):
    res = hook(proj, "t1", "Edit", {"file_path": str(proj / "CLAUDE.md"), "old_string": "a", "new_string": "b"},
               {"FAKE_FAIL": "1"})
    assert res["permissionDecision"] == "ask"


def test_guard_disables_engine(proj, tmp_path):
    out = run(["hook"], event(proj, "PreToolUse", "t", "Edit",
                              {"file_path": str(proj / "CLAUDE.md"), "old_string": "a", "new_string": "b"}),
              {"RRSI_POLICY_ACTIVE": "1", "FAKE_VERDICT": "reject"})
    assert out.stdout == "" and not (tmp_path / "fake.log").exists()


def test_child_call_is_isolated(proj, tmp_path):
    hook(proj, "t1", "Edit", {"file_path": str(proj / "CLAUDE.md"), "old_string": "a", "new_string": "b"})
    call = json.loads((tmp_path / "fake.log").read_text().splitlines()[-1])
    argv = call["argv"]
    assert call["guard"] == "1"
    assert argv[argv.index("--tools") + 1] == ""
    assert "--json-schema" in argv and "--no-session-persistence" in argv
    assert not call["cwd"].startswith(str(proj))


def test_repeated_rejections_escalate_to_user(proj):
    ti = {"file_path": str(proj / "CLAUDE.md"), "old_string": "a", "new_string": "b"}
    seen = [hook(proj, f"t{i}", "Edit", ti, {"FAKE_VERDICT": "reject"})["permissionDecision"]
            for i in range(3)]
    assert seen == ["deny", "deny", "ask"]       # max_repairs = 3


def test_argv_policy_override(proj):
    out = run(["hook", "--policy", "ask-user"],
              event(proj, "PreToolUse", "t", "Bash", {"command": "ls"}))
    assert json.loads(out.stdout)["hookSpecificOutput"]["permissionDecision"] == "ask"


def test_check_exit_codes(proj):
    diff = "--- a/CLAUDE.md\n+++ b/CLAUDE.md\n@@ -1 +1,2 @@\n # rules\n+- run tests\n"
    assert run(["check", "--policy", "harness-critic", "--file", "CLAUDE.md"], diff, cwd=proj).returncode == 0
    assert run(["check", "--policy", "harness-critic", "--file", "CLAUDE.md"], diff,
               {"FAKE_VERDICT": "reject"}, cwd=proj).returncode == 2


# ----------------------------------------------------------- measurement --
EVAL = ("n=$(grep -c '^- ' CLAUDE.md); python3 -c \"print('{\\\"S\\\": %.3f, \\\"C\\\": %d}' "
        "% (0.5 + 0.1*$n, 1000 + 100*$n))\"")


def test_measure_accept_reject_revert(proj):
    assert run(["baseline", "--cmd", EVAL, "--k", "2", "--delta", "0.02"], cwd=proj).returncode == 0
    edit(proj, "t1", "# rules\n", "# rules\n- run tests before finishing\n",
         {"FAKE_INTENT": "verify before finishing"})
    m = run(["measure", "--cmd", EVAL, "--k", "2"], cwd=proj)
    assert m.returncode == 0 and json.loads(m.stdout)["outcome"] == "ACCEPTED"

    edit(proj, "t2", "- run tests before finishing\n", "Be careful.\n")
    m = run(["measure", "--cmd", EVAL, "--k", "2"], cwd=proj)
    assert m.returncode == 1 and json.loads(m.stdout)["outcome"] == "REJECTED"
    assert run(["revert"], cwd=proj).returncode == 0
    assert (proj / "CLAUDE.md").read_text() == "# rules\n- run tests before finishing\n"
    assert run(["revert"], cwd=proj).returncode != 0          # only once

    st = json.loads(run(["status", "--json"], cwd=proj).stdout)
    assert st["incumbent_S"] == pytest.approx(0.6) and st["pending_edits"] == 0
    assert st["prune_set"] == []                               # claude_md had a +0.1 gain


def test_measured_rejection_reaches_the_critic(proj, tmp_path):
    run(["baseline", "--S", "0.6", "--delta", "0.02"], cwd=proj)
    edit(proj, "t1", "# rules\n", "# rules\nBe careful.\n", {"FAKE_INTENT": "add caution"})
    run(["measure", "--S", "0.5"], cwd=proj)
    hook(proj, "t2", "Edit", {"file_path": str(proj / "CLAUDE.md"), "old_string": "a", "new_string": "b"})
    payload = json.loads((tmp_path / "fake.log").read_text().splitlines()[-1])["payload"]
    assert '"source": "measurement"' in payload and "add caution" in payload
    assert '"prune_set": ["claude_md"]' in payload


def test_edit_budget_asks_after_max_pending(proj):
    run(["baseline", "--S", "0.5", "--delta", "0.02"], cwd=proj)
    for i in range(3):
        edit(proj, f"t{i}", "# rules\n", f"# rules\n- rule {i}\n")
    res = hook(proj, "t9", "Edit", {"file_path": str(proj / "CLAUDE.md"), "old_string": "a", "new_string": "b"})
    assert res["permissionDecision"] == "ask" and "measure" in res["permissionDecisionReason"]


def test_no_budget_without_baseline(proj):
    for i in range(4):
        assert edit(proj, f"t{i}", "# rules\n", f"# rules\n- rule {i}\n") is None


# -------------------------------------------------------------- selection --
CFG = {"beta0": 0.1, "beta1": 10.0, "w_s": 0.0, "w_c": 15.0, "w_n": 0.5}


def test_cost_rule():
    # real gain buys tokens up to beta0 + beta1 * dS
    assert selection.judge(0.6, 1500, 0.5, 1000, 0.5, 0.02, [], {}, CFG)["outcome"] == "ACCEPTED"
    assert selection.judge(0.53, 2000, 0.5, 1000, 0.5, 0.02, [], {}, CFG)["outcome"] == "REJECTED"
    # inside the band: only a token saving or a new structural component keeps it
    assert selection.judge(0.505, 900, 0.5, 1000, 0.5, 0.02, [], {}, CFG)["outcome"] == "ACCEPTED"
    assert selection.judge(0.505, 1000, 0.5, 1000, 0.5, 0.02, ["claude_md"], {}, CFG)["outcome"] == "REJECTED"
    assert selection.judge(0.505, 1000, 0.5, 1000, 0.5, 0.02, ["skill"], {}, CFG)["outcome"] == "ACCEPTED"
    # floor is against the best ever seen, not the incumbent
    assert selection.judge(0.55, 1000, 0.55, 1000, 0.6, 0.02, ["skill"], {}, CFG)["outcome"] == "REJECTED"


def test_noise_band():
    assert selection.noise_band(0.01, 2, 2.0) == pytest.approx(0.02)
