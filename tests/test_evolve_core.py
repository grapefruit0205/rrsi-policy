# Adapted from google-research/rrsi tests/test_core.py (Copyright 2026 Google LLC, Apache-2.0).
"""Unit tests for the method core of rrsi-evolve: no API, no benchmark, no LLM.

Every test of RRSI's tests/test_core.py is ported with imports switched to
rrsi_evolve, plus additions for the rrsi-evolve deviations:

  - schedule.endpoint=True (google-research/rrsi#2)
  - gitops.exclude_path (.git/info/exclude, idempotent, worktree-safe)
  - propose.Workspace skipping anything under a `.git` path part

The role modules (critic / digester / analyst / proposer) call llm.generate;
these tests never trigger a generate call. Run:

    uvx --quiet pytest tests/test_evolve_core.py -q
    or: python3 tests/test_evolve_core.py
"""

import json
import math
import sys
import tempfile
from pathlib import Path

PLUGIN = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLUGIN / "lib"))

from rrsi_evolve import gitops as G                          # noqa: E402
from rrsi_evolve.calibrate import calibrate                  # noqa: E402
from rrsi_evolve.components import normalize, novelty        # noqa: E402
from rrsi_evolve.config import RRSIConfig                    # noqa: E402
from rrsi_evolve.evaluate import EvalResult, TaskResult, aggregate  # noqa: E402
from rrsi_evolve.history import History, exploration, stall_flag  # noqa: E402
from rrsi_evolve.schedule import budget_table, edit_budget   # noqa: E402
from rrsi_evolve.selection import Candidate, cost_rule, select_round  # noqa: E402

# propose.py imports llm.generate; another engineer writes llm.py in parallel.
# If it is not there yet, inject a stub that fails loudly if called, so these
# file-operation tests do not depend on release timing.
try:  # noqa: SIM105
    import rrsi_evolve.llm  # noqa: F401
except ImportError:
    import types
    _fake_llm = types.ModuleType("rrsi_evolve.llm")

    def _generate(*a, **k):  # pragma: no cover - tests must not call generate
        raise AssertionError("tests must not call llm.generate")
    _fake_llm.generate = _generate
    sys.modules["rrsi_evolve.llm"] = _fake_llm

from rrsi_evolve.propose import Workspace                    # noqa: E402


def _ev(job, rewards_by_task, k, tokens=1000.0):
    per = {t: TaskResult(rewards=list(r), tokens=[tokens] * len(r))
           for t, r in rewards_by_task.items()}
    return aggregate(job, k, per)


def test_schedule_matches_eq_anneal():
    T, bmin, bmax = 20, 1, 4
    for t in range(T):
        expect = math.ceil(bmin + (bmax - bmin) * 0.5 * (1 + math.cos(math.pi * t / T)))
        assert edit_budget(t, T, bmin, bmax) == expect
    tab = budget_table(T, bmin, bmax)
    assert tab[0] == bmax and tab == sorted(tab, reverse=True)
    assert edit_budget(T, T, bmin, bmax) == bmin


def test_schedule_endpoint_opt_in():
    T, bmin, bmax = 8, 1, 4
    assert edit_budget(0, T, bmin, bmax, endpoint=True) == bmax
    assert edit_budget(T - 1, T, bmin, bmax, endpoint=True) == bmin
    tab = budget_table(T, bmin, bmax, endpoint=True)
    assert tab[0] == bmax and tab[-1] == bmin and tab == sorted(tab, reverse=True)
    # the default is unchanged from RRSI: endpoint=False == no argument
    for t in range(T):
        expect = math.ceil(bmin + (bmax - bmin) * 0.5 * (1 + math.cos(math.pi * t / T)))
        assert edit_budget(t, T, bmin, bmax) == expect
        assert edit_budget(t, T, bmin, bmax, endpoint=False) == expect
    assert budget_table(T, bmin, bmax, endpoint=False) == budget_table(T, bmin, bmax)
    # the opt-in changes the schedule only where the cosines differ
    assert tab != budget_table(T, bmin, bmax)


def test_estimator_and_weights():
    ev = _ev("j", {"a": [1, 0], "b": [1, 1]}, 2)
    assert abs(ev.S - 0.75) < 1e-9 and ev.n_expected == 4 and ev.missing == 0
    per = {"a": TaskResult(rewards=[0.5, 1.0], weights=[10, 10]),
           "b": TaskResult(rewards=[0.0, 0.0], weights=[90, 90])}
    ev = aggregate("w", 2, per)
    assert abs(ev.S - 15 / 200) < 1e-9          # criteria-weighted fraction


def test_calibration_bootstrap_and_repeats():
    rng_rewards = {f"t{i}": [i % 2, (i + 1) % 2] for i in range(40)}
    ev = _ev("base", rng_rewards, 2)
    cal = calibrate([ev], z=2.0, reps=300)
    assert cal["delta"] > 0 and cal["method"].startswith("bootstrap")
    ev2 = _ev("base2", {f"t{i}": [1, 1 if i % 3 else 0] for i in range(40)}, 2)
    cal2 = calibrate([ev, ev2], z=2.0, reps=100)
    assert cal2["n_evals"] == 2 and cal2["delta"] >= 0


def test_cost_rule_both_branches():
    cfg = RRSIConfig(beta0=0.1, beta1=40.0, w_s=100.0, w_c=15.0, w_n=0.5)
    ok, _ = cost_rule(0.05, 0.10 + 40 * 0.05 - 0.01, 0, 0.02, cfg)      # within budget
    assert ok
    ok, _ = cost_rule(0.05, 0.10 + 40 * 0.05 + 0.01, 0, 0.02, cfg)      # over budget
    assert not ok
    ok, _ = cost_rule(0.0, -0.10, 0, 0.02, cfg)                          # neutral, cheaper
    assert ok
    ok, _ = cost_rule(0.0, 0.10, 0, 0.02, cfg)                           # neutral, costlier
    assert not ok
    ok, _ = cost_rule(0.0, 0.0, 1, 0.02, cfg)                            # neutral, new structural
    assert ok


def test_selection_floor_argmax_and_sstar():
    cfg = RRSIConfig(beta0=0.1, beta1=40.0, w_s=100.0, w_c=15.0, w_n=0.5)
    inc = _ev("inc", {f"t{i}": [1, 1] if i < 5 else [0, 0] for i in range(10)}, 2)   # S=0.5
    a = Candidate("A", [{"id": "C1", "component": "prompt"}],
                  ev=_ev("A", {f"t{i}": [1, 1] if i < 7 else [0, 0] for i in range(10)}, 2))  # 0.7
    b = Candidate("B", [{"id": "C1", "component": "skill"}],
                  ev=_ev("B", {f"t{i}": [1, 1] if i < 6 else [0, 0] for i in range(10)}, 2))  # 0.6
    c = Candidate("C", [{"id": "C1", "component": "config"}],
                  ev=_ev("C", {f"t{i}": [1, 1] if i < 3 else [0, 0] for i in range(10)}, 2))  # 0.3 floor
    d = Candidate("D", [], gate_failure="critic_reject")
    S_star, delta = 0.55, 0.05                      # S* above the incumbent (earlier peak)
    win, decs = select_round([a, b, c, d], inc, S_star, delta, cfg, {}, guard_fn=None)
    assert win is a
    by = {x.variant: x for x in decs}
    assert by["A"].admissible and by["B"].admissible
    assert not by["C"].admissible and "floor" in by["C"].reason
    assert not by["D"].admissible and by["D"].reason == "critic_reject"
    # a gaining candidate that is too expensive is blocked by the L1 rule
    exp = Candidate("E", [{"id": "C1", "component": "prompt"}],
                    ev=_ev("E", {f"t{i}": [1, 1] if i < 7 else [0, 0] for i in range(10)}, 2,
                           tokens=1000.0 * (1 + 0.1 + 40 * 0.2 + 0.5)))
    win2, decs2 = select_round([exp], inc, S_star, delta, cfg, {})
    assert win2 is None and "cost rule" in decs2[0].reason
    # a domain guard is non-compensatory
    win3, decs3 = select_round([a], inc, S_star, delta, cfg, {},
                               guard_fn=lambda i, c: ["valid rate fell"])
    assert win3 is None and "guard" in decs3[0].reason


def test_history_summaries_prune_and_explore():
    with tempfile.TemporaryDirectory() as td:
        h = History(Path(td) / "history.jsonl")
        h.append_candidate(0, "A", [{"id": "C1", "component": "prompt", "hypothesis": "h1"}],
                           "ACCEPTED", 0.03, 0.05, True, 0.53, 1000, None)
        h.append_candidate(1, "A", [{"id": "C1", "component": "prompt", "hypothesis": "h2"}],
                           "REJECTED", -0.01, 0.0, False, 0.52, 1000, None)
        h.append_candidate(1, "B", [{"id": "C1", "component": "skill", "hypothesis": "h3"},
                                    {"id": "C2", "component": "memory", "hypothesis": "h4"}],
                           "REJECTED", -0.02, 0.3, False, 0.51, 1300, None)
        h.append_candidate(2, "A", [{"id": "C1", "component": "config", "hypothesis": "h5"}],
                           "critic_reject", None, None, False, None, None, None, "leak")
        assert h.tried() == {"prompt", "skill", "memory"}       # critic-rejected config not tried
        g = h.yield_g(t=3, n_prune=4)
        assert g["prompt"] == 0.03 and g["skill"] == -0.02
        # window: at t=5 with n_prune=4 the accepted prompt edit (t=0) falls out
        g5 = h.yield_g(t=5, n_prune=4)
        assert g5["prompt"] == -0.01
        assert h.yield_g(t=6, n_prune=4)["prompt"] == -math.inf    # nothing recent at all
        prune = {p["component"]: p for p in h.prune_set(t=5, n_prune=4)}
        assert set(prune) == {"prompt", "skill", "memory"}
        assert prune["prompt"]["accepted_edits_in_incumbent"][0]["hypothesis"] == "h1"
        assert h.incumbent_component_counts()["prompt"] == 1
        assert novelty(["client_tool", "prompt"], h.incumbent_component_counts()) == 1
        assert novelty(["skill"], {"skill": 2}) == 0
        assert h.has(1, "B") and not h.has(5, "A")
    traj = [0.50, 0.53, 0.53, 0.535, 0.60]
    assert stall_flag(traj, 3, 3, 0.02) == 0       # 0.535 - 0.50 = 0.035 > 0.02
    assert stall_flag(traj, 3, 2, 0.02) == 1       # 0.535 - 0.53 = 0.005 <= 0.02
    assert stall_flag(traj, 4, 2, 0.02) == 0       # 0.60 - 0.53 = 0.07
    assert stall_flag(traj, 1, 3, 0.02) == 0       # not enough history
    e = exploration(3, 1, {"prompt"}, 1)
    assert e["sigma"] == 1 and "prompt" not in e["untried"] and "RESERVED" in e["text"]


def test_component_normalization():
    assert normalize("Skill", "", []) == "prompt"          # no evidence in an empty diff
    assert normalize("Skill", "+++ b/x/skills/y/SKILL.md", []) == "skill"
    assert normalize("bogus", "+++ b/harness/skills/x/SKILL.md", []) == "skill"
    assert normalize(None, "+++ b/harness/resum.py\n+ keep_last = 5",
                     [("context_mgmt", [r"resum\.py"])]) == "context_mgmt"
    assert normalize(None, "nothing structural", []) == "prompt"


def test_config_roundtrip():
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "rrsi.json"
        p.write_text(json.dumps({"T": 7, "k": 3, "beta1": 12.5, "custom": "x"}))
        cfg = RRSIConfig.load(p, T=None, k=5)
        assert cfg.T == 7 and cfg.k == 5 and cfg.beta1 == 12.5 and cfg.notes["custom"] == "x"
        ev = EvalResult.from_json(aggregate("j", 1, {"a": TaskResult(rewards=[1.0])}).to_json())
        assert ev.S == 1.0


# ---------------------------------------------------- rrsi-evolve additions --

def test_config_engineering_fields():
    cfg = RRSIConfig()
    assert cfg.llm_backend == "claude-cli"
    assert cfg.llm_timeout_s == 900
    assert cfg.b_anneal_endpoint is False
    # deviation 1: the roles default to the model you use (resolved by the CLI)
    assert cfg.proposer_model == cfg.analyst_model == cfg.critic_model == "inherit"
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "rrsi.json"
        p.write_text(json.dumps({"llm_backend": "fake:/x.py", "unknown_key": 1}))
        cfg2 = RRSIConfig.load(p)
        assert cfg2.llm_backend == "fake:/x.py" and cfg2.notes["unknown_key"] == 1


def _init_repo(bd: Path) -> Path:
    repo = bd / "repo"
    repo.mkdir(parents=True)
    G.git(repo, "init", "-q", check=True)
    (repo / "f.txt").write_text("x\n")
    G.git(repo, "add", "-A", check=True)
    G.git(repo, "commit", "-q", "-m", "init", check=True)
    return repo


def test_gitops_exclude_path_idempotent():
    with tempfile.TemporaryDirectory() as td:
        repo = _init_repo(Path(td))
        G.exclude_path(repo, ".rrsi/runs/")
        excl = repo / ".git" / "info" / "exclude"
        text = excl.read_text()
        assert ".rrsi/runs" in text.splitlines()
        G.exclude_path(repo, ".rrsi/runs")     # same entry, no trailing slash
        G.exclude_path(repo, ".rrsi/runs/")
        lines = [l for l in excl.read_text().splitlines() if l.strip()]
        assert lines.count(".rrsi/runs") == 1  # idempotent
        assert (repo / ".git" / "info" / "exclude").exists()


def test_gitops_exclude_path_from_worktree():
    with tempfile.TemporaryDirectory() as td:
        repo = _init_repo(Path(td))
        wt = G.worktree_add(repo, Path(td) / "wt", "wtbranch", "HEAD")
        assert (wt / "f.txt").exists()
        G.exclude_path(wt, ".claude/sandbox/")  # cwd = the worktree, not the repo
        text = (repo / ".git" / "info" / "exclude").read_text()
        assert ".claude/sandbox" in text.splitlines()
        # the entry is shared: visible from the main checkout too
        G.exclude_path(repo, ".claude/sandbox/")
        lines = (repo / ".git" / "info" / "exclude").read_text().splitlines()
        assert lines.count(".claude/sandbox") == 1


def test_workspace_skips_git_paths():
    with tempfile.TemporaryDirectory() as td:
        hdir = Path(td) / "harness"
        (hdir / "skills" / "s1").mkdir(parents=True)
        (hdir / "CLAUDE.md").write_text("# harness\n")
        (hdir / "skills" / "s1" / "SKILL.md").write_text("---\nname: s1\n---\n")
        # worktree-style .git pointer FILE at the harness root
        (hdir / ".git").write_text("gitdir: /somewhere/main/.git/worktrees/wt\n")
        # a nested repository's .git DIRECTORY with internals
        nested = hdir / "vendor" / "lib"
        (nested / ".git" / "objects").mkdir(parents=True)
        (nested / ".git" / "config").write_text("[core]\n")
        (nested / "code.py").write_text("x = 1\n")
        (nested / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
        ws = Workspace(hdir, {".md", ".py"})
        names = [l.split(" (")[0] for l in ws.list_files().splitlines()]
        assert "CLAUDE.md" in names and str(Path("vendor/lib/code.py")) in names
        assert all(".git" not in Path(n).parts for n in names)
        dumped = ws.dump()
        assert "gitdir:" not in dumped and "[core]" not in dumped
        assert "ref: refs/heads/main" not in dumped
        assert "===== FILE: CLAUDE.md =====" in dumped
        assert "===== FILE: vendor/lib/code.py =====" in dumped


if __name__ == "__main__":
    import inspect
    fails = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_") and inspect.isfunction(fn):
            try:
                fn()
                print(f"ok   {name}")
            except Exception as e:  # noqa: BLE001
                fails += 1
                print(f"FAIL {name}: {e!r}")
    sys.exit(1 if fails else 0)


def test_evaluate_reruns_missing_trials_once(tmp_path):
    """A missing trial is infrastructure, not the policy: evaluate() runs the
    empty slots once more before they count as 0."""
    from rrsi_evolve.evaluate import TaskResult, evaluate

    class Flaky:
        def __init__(self, fail_runs):
            self.runs, self.fail_runs = 0, fail_runs

        def run(self, root, runs_dir, job, ids, k, log_prefix=""):
            self.runs += 1

        def score(self, runs_dir, job, ids, k):
            miss = 1 if self.runs <= self.fail_runs else 0
            return {"t": TaskResult(rewards=[1.0] * (k - miss) + [0.0] * miss,
                                    missing=miss)}, {}

    d = Flaky(fail_runs=1)
    ev = evaluate(d, tmp_path, tmp_path, "j", ["t"], 3)
    assert d.runs == 2 and ev.missing == 0 and ev.S == 1.0
    d = Flaky(fail_runs=5)              # still missing after the retry: counted, no loop
    ev = evaluate(d, tmp_path, tmp_path, "j", ["t"], 3)
    assert d.runs == 2 and ev.missing == 1
    d = Flaky(fail_runs=0)              # nothing missing: one run
    evaluate(d, tmp_path, tmp_path, "j", ["t"], 3)
    assert d.runs == 1


# ------------------------------------------------------------ harness scope --
def test_harness_scope_config_and_amendments(tmp_path):
    from rrsi_evolve import critic, scope
    p = tmp_path / "rrsi.json"
    p.write_text(json.dumps({"harness_scope": "repo"}))
    assert RRSIConfig.load(p).harness_scope == "repo"
    p.write_text(json.dumps({}))
    assert RRSIConfig.load(p).harness_scope == "general"
    p.write_text(json.dumps({"harness_scope": "team"}))
    try:
        RRSIConfig.load(p)
    except ValueError as e:
        assert "harness_scope" in str(e)
    else:
        raise AssertionError("unknown scope accepted")
    # general leaves the constitution and the critic as they are
    assert scope.skill("SKILL", "general") == "SKILL"
    assert scope.critic("SYS", "general") == "SYS"
    # repo amends both, and still bans task-specific answers
    s = scope.skill("SKILL", "repo")
    assert s.startswith("SKILL") and "Harness scope: repo" in s and "ticket numbers" in s
    c = scope.critic("SYS", "repo")
    assert c.startswith("SYS") and "HARNESS SCOPE: repo" in c and "Still REJECT" in c


def test_critic_review_passes_scope_to_the_reviewer(monkeypatch):
    from rrsi_evolve import critic

    class D:
        critic_patterns = []
        briefs = {"critic": "brief"}

    seen = []

    def fake_generate(payload, system=None, **kw):
        seen.append(system)
        return json.dumps({"verdict": "accept", "reasons": [], "risk_notes": []})

    monkeypatch.setattr(critic, "generate", fake_generate)
    critic.review(D(), "+ a line\n", "m", "mode")
    critic.review(D(), "+ a line\n", "m", "mode", scope="repo")
    assert "HARNESS SCOPE: repo" not in seen[0]
    assert "HARNESS SCOPE: repo" in seen[1]
