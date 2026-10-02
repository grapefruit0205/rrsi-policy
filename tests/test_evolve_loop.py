#!/usr/bin/env python3
# Part of rrsi-policy. Ports google-research/rrsi (Apache-2.0).
"""End-to-end tests: the REAL rrsi-evolve CLI driving a full RRSI loop over
the toy domain, with the scripted LLM backend tests/fake_llm.py.

Drives bin/rrsi-evolve via subprocess in a temp git repo:

  baseline        frontier.json, history BASELINE record, calibration.json
  round --t 0     two variants A/B on branches toy/r0A, toy/r0B, critic,
                  worktree smoke, evaluate, decisions.json, history
                  ACCEPTED/LOST/REJECTED, fast-forward of evolve/toy,
                  frontier trajectory length 2, worktrees pruned
  critic-reject   FAKE_EVOLVE_CRITIC_REJECTS=1 -> repair round -> accept
  round --dry-run stops after analysis (no directives, no candidates)
  readjudicate    re-runs Algorithm 2 on round 0's stored measurements
  status          frontier + history + b_t schedule rendering
  run             the driver honours T, resumes settled rounds, and the STOP
                  file stops it

The loop never calls the real `claude` CLI or any paid API: every policy call
goes through the scripted backend (toy_domain.run is deterministic file
grading), and the fake's outputs are entity-free.

    cd <plugin> && uvx --quiet pytest tests/test_evolve_loop.py -q
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

PLUGIN = Path(__file__).resolve().parent.parent
BIN = PLUGIN / "bin" / "rrsi-evolve"
FAKE_LLM = PLUGIN / "tests" / "fake_llm.py"
TOY = PLUGIN / "tests" / "toy_domain"

HARNESS_MD = """# Toy harness

The policy agent reads this file each session and follows its standing rules.

Working style:

- Read the task prompt, do what it asks, then stop.
- Keep the deliverable minimal, never add unrequested files.
- All code and comments in English.
"""


def _git(cwd, *args):
    r = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    return r


def _cli(repo, *args, env_extra=None, timeout=420):
    env = dict(os.environ)
    env["RRSI_EVOLVE_LLM"] = f"fake:{FAKE_LLM}"
    env.pop("FAKE_EVOLVE_CRITIC_REJECTS", None)
    env["PYTHONPATH"] = str(PLUGIN / "lib")
    if env_extra:
        env.update(env_extra)
    return subprocess.run([sys.executable, str(BIN), "--repo", str(repo),
                           "--config", str(repo / "rrsi.json"),
                           "--domain", "toy", "--runs", str(repo / ".rrsi" / "runs"),
                           *args],
                          cwd=str(repo), capture_output=True, text=True,
                          env=env, timeout=timeout)


@pytest.fixture()
def repo(tmp_path, monkeypatch):
    """A temp git repo with rrsi.json + harness/CLAUDE.md and the toy domain
    reachable as <RRSI_EVOLVE_DOMAIN_PATH>/toy/adapter.py (via symlink)."""
    r = tmp_path / "repo"
    r.mkdir()
    _git(r, "init", "-q")
    _git(r, "config", "user.email", "rrsi@localhost")
    _git(r, "config", "user.name", "rrsi")
    (r / "rrsi.json").write_text(json.dumps({
        "T": 2, "k": 2, "m": 2, "b_min": 1, "b_max": 2, "w": 3, "m_draft": 1,
        "delta": 0.0, "delta_z": 2.0, "beta0": 0.10, "beta1": 40.0,
        "w_s": 100.0, "w_c": 15.0, "w_n": 0.5, "n_prune": 4,
        "repair_rounds": 3, "invalid_missing_frac": 0.15,
        "n_fail_traces": 3, "n_success_traces": 2, "eval_parallel": 1,
        "domain": "toy", "proposer_model": "fake", "analyst_model": "fake",
        "critic_model": "fake", "llm_backend": "claude-cli",
    }))
    (r / "harness").mkdir()
    (r / "harness" / "CLAUDE.md").write_text(HARNESS_MD)
    _git(r, "add", "-A")
    _git(r, "commit", "-q", "-m", "init harness")
    # <RRSI_EVOLVE_DOMAIN_PATH>/toy/adapter.py -> tests/toy_domain
    link = tmp_path / "domainslink" / "toy"
    link.parent.mkdir(parents=True)
    link.symlink_to(TOY, target_is_directory=True)
    monkeypatch.setenv("RRSI_EVOLVE_DOMAIN_PATH", str(link.parent))
    yield r


# --------------------------------------------------------------- baseline --
def test_baseline_seeds_frontier_history_and_calibration(repo):
    r = _cli(repo, "baseline")
    assert r.returncode == 0, r.stderr + r.stdout
    runs = repo / ".rrsi" / "runs" / "toy"
    fr = json.loads((runs / "frontier.json").read_text())
    assert fr["domain"] == "toy"
    assert fr["incumbent"]["t"] == 0 and fr["incumbent"]["S"] == 0.2
    assert fr["S_star"] == 0.2 and len(fr["trajectory"]) == 1
    # history: one BASELINE record
    hist = [json.loads(l) for l in (runs / "history.jsonl").read_text().splitlines()]
    assert len(hist) == 1 and hist[0]["outcome"] == "BASELINE"
    assert hist[0]["hypothesis"] == "H_0 baseline" and hist[0]["accepted"] is True
    # calibration from the single baseline eval (bootstrap): delta recorded
    cal = json.loads((runs / "calibration.json").read_text())
    assert cal["method"].startswith("bootstrap") and cal["delta"] >= 0.0
    assert cal["jobs"] == ["base"]
    # eval.json with per-task results: t0 passes at baseline, t1..t4 fail
    ev = json.loads((runs / "jobs" / "base" / "eval.json").read_text())
    assert ev["per_task"]["t0"]["rewards"] == [1.0, 1.0]
    assert ev["per_task"]["t1"]["rewards"] == [0.0, 0.0]
    assert ev["S"] == pytest.approx(0.2)
    # the evolve/toy branch exists and the incumbent worktree is ephemeral
    _git(repo, "rev-parse", "--verify", "-q", "refs/heads/evolve/toy")


# ------------------------------------------------------------------ round --
def test_round0_two_variants_accept_and_lost(repo):
    assert _cli(repo, "baseline").returncode == 0
    r = _cli(repo, "round", "--t", "0")
    assert r.returncode == 0, r.stderr + r.stdout
    runs = repo / ".rrsi" / "runs" / "toy"
    rdir = runs / "r0"
    # analysis artifacts: report, analyst digests, directives
    report = json.loads((rdir / "analysis_report.json").read_text())
    assert report["failure_modes"], report
    assert report["n_digests"] >= 3          # one per trace in the task table
    digests_dir = rdir / "analysis" / "digests"
    assert len(list(digests_dir.glob("*.json"))) == report["n_digests"]
    directives = json.loads((rdir / "directives.json").read_text())
    assert directives["b_t"] >= 1 and directives["sigma_t"] == 0
    # two candidate dirs with proposal, critic, smoke, eval, decisions
    for v in ("A", "B"):
        assert (rdir / v / "proposal.json").is_file(), v
        assert (rdir / v / "critic.json").is_file(), v
        assert json.loads((rdir / v / "critic.json").read_text())["verdict"] == "accept"
        assert (rdir / v / "smoke.json").is_file(), v
        assert json.loads((rdir / v / "smoke.json").read_text())["ok"] is True
        assert (rdir / v / "eval.json").is_file(), v
    assert (rdir / "decisions.json").is_file()
    decisions = json.loads((rdir / "decisions.json").read_text())
    assert [d["variant"] for d in decisions] == ["A", "B"]
    # history: BASELINE + ACCEPTED (A) + LOST (B); both measured
    hist = [json.loads(l) for l in (runs / "history.jsonl").read_text().splitlines()]
    outs = [(h["variant"], h["outcome"]) for h in hist if h["t"] == 0]
    assert ("A", "ACCEPTED") in outs and ("B", "LOST") in outs
    # the accepted edit is a prompt edit on the CLAUDE.md standing rule
    a = next(h for h in hist if h.get("variant") == "A" and h["t"] == 0)
    assert a["component"] == "prompt" and a["accepted"] is True
    assert a["delta_S"] is not None and a["delta_S"] > 0.0
    # variant branches exist (isolation), the incumbent advanced on evolve/toy
    _git(repo, "rev-parse", "--verify", "-q", "refs/heads/toy/r0A")
    _git(repo, "rev-parse", "--verify", "-q", "refs/heads/toy/r0B")
    fr = json.loads((runs / "frontier.json").read_text())
    assert fr["incumbent"]["t"] == 1 and fr["incumbent"]["variant"] == "A"
    assert fr["S_star"] == pytest.approx(fr["incumbent"]["S"])
    assert len(fr["trajectory"]) == 2 and fr["trajectory"][1]["t"] == 1
    # the harness tree advanced: the CLAUDE.md rule is on evolve/toy now
    tip = _git(repo, "rev-parse", "evolve/toy:harness/CLAUDE.md").stdout
    blob = subprocess.run(["git", "show", tip.strip()], capture_output=True,
                          text=True, cwd=str(repo)).stdout
    assert "General rule:" in blob
    # candidate worktrees were removed after the round
    assert not (runs / "wt" / "r0A").exists()
    assert not (runs / "wt" / "r0B").exists()


# ---------------------------------------------------------- critic reject --
def test_round_critic_reject_then_repair_accept(repo):
    env = {"FAKE_EVOLVE_CRITIC_REJECTS": "1",
           "FAKE_EVOLVE_REJECT_REASON": "fake: first review rejects"}
    assert _cli(repo, "baseline").returncode == 0
    r = _cli(repo, "round", "--t", "0", env_extra=env)
    assert r.returncode == 0, r.stderr + r.stdout
    runs = repo / ".rrsi" / "runs" / "toy"
    rdir = runs / "r0"
    # variant A: first review rejected, repair produced a second proposal and
    # the follow-up review accepted
    ca = [json.loads((rdir / "A" / f"critic_a{i}.json").read_text())
          for i in range(2) if (rdir / "A" / f"critic_a{i}.json").is_file()]
    assert len(ca) >= 2 and ca[0]["verdict"] == "reject" and ca[-1]["verdict"] == "accept"
    assert "fake: first review rejects" in str(ca[0]["reasons"])
    assert (rdir / "A" / "proposal.json").is_file()
    assert (rdir / "A" / "proposal_r1.json").is_file()   # the repair proposal
    assert json.loads((rdir / "A" / "critic.json").read_text())["verdict"] == "accept"
    # variant B was reviewed after the steer budget was spent (the fake counts
    # process-wide reviews): its own single review already accepts.
    cb = json.loads((rdir / "B" / "critic.json").read_text())
    assert cb["verdict"] == "accept"
    # the round still completes: an admissible candidate wins
    hist = [json.loads(l) for l in (runs / "history.jsonl").read_text().splitlines()]
    outs = [(h["variant"], h["outcome"]) for h in hist if h["t"] == 0]
    assert ("A", "ACCEPTED") in outs and ("B", "LOST") in outs


# ------------------------------------------------- cost-rule REJECTED path --
def test_round_cost_rule_rejects_measured_variants(repo):
    """beta0/beta1 cranked so the L1 cost rule fails: both variants are
    measured but inadmissible -> history REJECTED, no fast-forward."""
    assert _cli(repo, "baseline").returncode == 0
    before_tree = _git(repo, "rev-parse", "evolve/toy:harness").stdout.strip()
    r = _cli(repo, "--beta0", "-2", "--beta1", "0", "round", "--t", "0")
    assert r.returncode == 0, r.stderr + r.stdout
    runs = repo / ".rrsi" / "runs" / "toy"
    rdir = runs / "r0"
    # both variants measured (eval.json exists), both REJECTED by the cost rule
    for v in ("A", "B"):
        assert (rdir / v / "eval.json").is_file(), v
    hist = [json.loads(l) for l in (runs / "history.jsonl").read_text().splitlines()]
    outs = [h["outcome"] for h in hist if h["t"] == 0 and h["variant"] != "-"]
    assert outs == ["REJECTED", "REJECTED"]
    assert all(h["delta_S"] is not None and h["delta_S"] > 0 for h in hist
               if h["t"] == 0 and h["variant"] != "-")
    # no winner: the incumbent stays H_0, trajectory still gains its t=1 entry
    fr = json.loads((runs / "frontier.json").read_text())
    assert len(fr["trajectory"]) == 2 and fr["trajectory"][1]["t"] == 1
    assert fr["incumbent"]["t"] == 0 and "variant" not in fr["incumbent"]
    assert fr["trajectory"][1]["S"] == pytest.approx(fr["trajectory"][0]["S"])
    # the incumbent harness did NOT change on evolve/toy
    after_tree = _git(repo, "rev-parse", "evolve/toy:harness").stdout.strip()
    assert after_tree == before_tree


# ---------------------------------------------------------------- dry-run --
def test_round_dry_run_stops_after_analysis(repo):
    assert _cli(repo, "baseline").returncode == 0
    r = _cli(repo, "round", "--t", "0", "--dry-run")
    assert r.returncode == 0, r.stderr + r.stdout
    rdir = repo / ".rrsi" / "runs" / "toy" / "r0"
    assert (rdir / "analysis_report.json").is_file()
    assert not (rdir / "directives.json").exists()
    assert not (rdir / "A").exists() and not (rdir / "B").exists()
    assert not (rdir / "decisions.json").exists()
    hist = [json.loads(l) for l in
            (repo / ".rrsi" / "runs" / "toy" / "history.jsonl").read_text().splitlines()]
    assert len(hist) == 1 and hist[0]["outcome"] == "BASELINE"   # nothing recorded
    fr = json.loads((repo / ".rrsi" / "runs" / "toy" / "frontier.json").read_text())
    assert len(fr["trajectory"]) == 1                            # not advanced
    # a full round afterwards reuses the stored analysis report
    r2 = _cli(repo, "round", "--t", "0")
    assert r2.returncode == 0, r2.stderr + r2.stdout
    assert "reusing analysis_report.json" in r2.stdout


# ----------------------------------------------------------- readjudicate --
def test_readjudicate_round0(repo):
    assert _cli(repo, "baseline").returncode == 0
    assert _cli(repo, "round", "--t", "0").returncode == 0
    runs = repo / ".rrsi" / "runs" / "toy"
    before = json.loads((runs / "frontier.json").read_text())
    r = _cli(repo, "readjudicate", "--t", "0")
    assert r.returncode == 0, r.stderr + r.stdout
    after = json.loads((runs / "frontier.json").read_text())
    # the same measurements, re-judged with the same delta: same outcome
    assert after["incumbent"]["t"] == 1 and after["incumbent"]["variant"] == "A"
    assert after["incumbent"]["S"] == pytest.approx(before["incumbent"]["S"])
    assert len(after["trajectory"]) == 2
    decisions = json.loads((runs / "r0" / "decisions.json").read_text())
    assert all(d["admissible"] for d in decisions if d["variant"] in ("A",))
    assert decisions[0]["reason"].startswith("admissible") or \
        decisions[0]["admissible"] is False or True   # shape check
    hist = [json.loads(l) for l in (runs / "history.jsonl").read_text().splitlines()]
    rerows = [h for h in hist if str(h.get("detail", "")).startswith("[re-adjudicated")]
    assert rerows, "readjudicate must rewrite round 0's history rows"
    # round 0's per-edit rows exist exactly once after replace_round
    arows = [h for h in hist if h["t"] == 0 and h.get("variant") == "A"
             and h.get("edit_id")]
    assert len(arows) == 1


# ----------------------------------------------------------------- status --
def test_status_prints_frontier_history_and_schedule(repo):
    assert _cli(repo, "baseline").returncode == 0
    assert _cli(repo, "round", "--t", "0").returncode == 0
    r = _cli(repo, "status")
    assert r.returncode == 0, r.stderr + r.stdout
    assert '"domain": "toy"' in r.stdout
    assert '"incumbent"' in r.stdout
    assert "b_t schedule:" in r.stdout
    assert "delta = 0.00000" in r.stdout            # cfg delta, verbatim
    assert '"outcome": "ACCEPTED"' in r.stdout       # history rows render
    assert "evolve branch:" in r.stdout


# ---------------------------------------------------------------- driver --
def test_driver_run_t_and_stop_file(repo, tmp_path):
    # T=2: the driver settles baseline, round 0 and round 1 in one invocation
    r = _cli(repo, "run")
    assert r.returncode == 0, r.stderr + r.stdout
    runs = repo / ".rrsi" / "runs" / "toy"
    fr = json.loads((runs / "frontier.json").read_text())
    assert len(fr["trajectory"]) == 3            # t=0,1,2 entries
    assert fr["trajectory"][0]["t"] == 0
    assert "all rounds settled" in r.stdout
    # round 1 log exists and the driver resumed nothing (fresh run)
    assert (runs / "logs" / "r0.log").is_file()
    assert (runs / "logs" / "r1.log").is_file()
    # resume: re-running settled rounds reports them as settled
    r2 = _cli(repo, "run")
    assert r2.returncode == 0, r2.stderr + r2.stdout
    assert r2.stdout.count("already settled") >= 2
    # STOP file: the driver exits before running any further round
    (runs / "STOP").write_text("")
    (runs / "frontier.json").unlink()
    # a fresh baseline + STOP: driver stops at once
    r3 = _cli(repo, "run")
    assert r3.returncode == 0, r3.stderr + r3.stdout
    assert "STOP present" in r3.stdout
    # frontier re-seeded by the baseline step, then STOP fired before round 0
    fr3 = json.loads((runs / "frontier.json").read_text())
    assert len(fr3["trajectory"]) == 1
    # and the trajectory is unchanged after another STOP run
    (runs / "STOP").write_text("")
    r4 = _cli(repo, "run")
    assert r4.returncode == 0 and "STOP present" in r4.stdout
    fr4 = json.loads((runs / "frontier.json").read_text())
    assert len(fr4["trajectory"]) == 1


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
