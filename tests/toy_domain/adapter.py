#!/usr/bin/env python3
# Part of rrsi-policy. Ports google-research/rrsi (Apache-2.0).
"""Toy domain adapter for the rrsi-evolve loop tests.

A tiny, fully deterministic benchmark built from the harness files of the
checked-out worktree (no agent runs, no network, no subprocesses):

  evolve set    tasks t0..t4; task i passes a trial (reward 1.0) when the
                harness CLAUDE.md mentions its keyword ("stop", "bounded",
                "decides", "smallest", "confirm"), a stand-in for "the
                harness teaches a general practice the task needs"
  score         mean reward over trials (Eq. estimate); EVERY trial of a task
                yields the same reward, so the bootstrap noise band
                delta is exactly 0.0 and acceptance decisions are exact
  cost          tokens per trial = the byte size of harness/CLAUDE.md, so an
    edit that grows the guidance grows C and the L1 cost rule sees it
  traces        render_trace produces a fixed pseudo transcript with
                [step N] lines and a === VERIFIER GRADES === section, the
                format the pipeline expects

Loaded through RRSI_EVOLVE_DOMAIN_PATH pointing at a directory that holds
this adapter as <dir>/toy/adapter.py (see tests/test_evolve_loop.py).
make_domain(repo, raw_cfg) exports the standard adapter entry point and
fills SKILL.md / PATTERNS.md / briefs from this directory.
"""

from __future__ import annotations

import json
from pathlib import Path

from rrsi_evolve.domain import Domain
from rrsi_evolve.evaluate import TaskResult

DEVNOTE = __doc__
# (task_id, keyword): t0 passes at baseline; t1..t4 are flipped one at a time
# by the successive standing rules the scripted proposer appends.
TASKS = [
    ("t0", "stop"),
    ("t1", "bounded"),
    ("t2", "decides"),
    ("t3", "smallest"),
    ("t4", "confirm"),
]


class ToyDomain(Domain):
    name = "toy"
    harness_path = "harness"

    briefs = {
        "analyst":
            "Benchmark: deterministic toy task suite over an LLM-agent harness "
            "of standing instructions (a CLAUDE.md). A task passes when the "
            "harness carries the general practice the task needs. Failure "
            "modes live in what the standing instructions omit.",
        "digester":
            "Benchmark: deterministic toy task suite over an LLM-agent harness "
            "of standing instructions. A trial passes (reward 1.0) when the "
            "harness mentions the general practice the task needs; traces show "
            "the steps a policy agent took and the verifier grade.",
        "proposer":
            "Benchmark: deterministic toy task suite over an LLM-agent harness "
            "of standing instructions (a single CLAUDE.md at the harness "
            "root). Standing rules that teach general practice are the main "
            "lever; keep them entity-free and procedural.",
        "critic":
            "Benchmark: deterministic toy task suite over an LLM-agent harness "
            "of standing instructions. Reject edits that hard-code task ids, "
            "keywords of a specific task, or expected outputs; general "
            "practices are fine.",
    }
    critic_patterns = []                  # generic denylist only
    component_signals = []                # generic diff signals only
    source_exts = {".py", ".txt", ".md", ".json"}

    # ---- task sets ---------------------------------------------------------
    def evolve_ids(self) -> list[str]:
        return [t for t, _ in TASKS]

    def smoke_ids(self, incumbent_per_task: dict | None = None) -> list[str]:
        return self.evolve_ids()[:2]

    # ---- Evaluate ----------------------------------------------------------
    def _claw(self, root: Path) -> Path:
        return Path(root) / self.harness_rel / "CLAUDE.md"

    def _reward(self, root: Path, task_id: str) -> float:
        kw = dict(TASKS).get(task_id, "")
        try:
            return 1.0 if kw in self._claw(root).read_text() else 0.0
        except OSError:
            return 0.0

    def _tokens(self, root: Path) -> int:
        try:
            return len(self._claw(root).read_text())
        except OSError:
            return 0

    def run(self, root: Path, runs_dir: Path, job: str, ids: list[str], k: int,
            log_prefix: str = "") -> None:
        """Deterministic trials: every trial of a task re-reads the harness."""
        out = Path(runs_dir) / "jobs" / job
        out.mkdir(parents=True, exist_ok=True)
        for tid in ids:
            tdir = out / tid
            tdir.mkdir(parents=True, exist_ok=True)
            reward = self._reward(root, tid)
            tokens = self._tokens(root)
            for j in range(k):
                f = tdir / f"trial_{j}.json"
                if f.exists():
                    continue                      # resume-safe
                f.write_text(json.dumps({
                    "task_id": tid, "trial": j, "reward": reward,
                    "tokens": tokens,
                    "status": "pass" if reward == 1.0 else "fail",
                    "steps": 3,
                }))

    def score(self, runs_dir: Path, job: str, ids: list[str], k: int
              ) -> tuple[dict, dict]:
        per = {}
        for tid in ids:
            rewards, tokens = [], []
            for j in range(k):
                f = Path(runs_dir) / "jobs" / job / tid / f"trial_{j}.json"
                rec = json.loads(f.read_text()) if f.exists() else None
                rewards.append(rec["reward"] if rec else 0.0)
                tokens.append(rec["tokens"] if rec else None)
                if rec is None:
                    pass
            missing = 0
            per[tid] = TaskResult(rewards=rewards, tokens=tokens, missing=missing)
        passed = sum(1 for tr in per.values() if tr.mean == 1.0)
        return per, {"task_passes": passed}

    # ---- evidence ----------------------------------------------------------
    def load_trial(self, runs_dir: Path, job: str, task_id: str, trial: int):
        f = Path(runs_dir) / "jobs" / job / str(task_id) / f"trial_{int(trial)}.json"
        if not f.exists():
            return None
        return json.loads(f.read_text())

    def render_trace(self, rec, detail: bool = False) -> str:
        reward = rec.get("reward", 0.0) if isinstance(rec, dict) else 0.0
        lines = [
            f"[step 1] read the task prompt for {rec.get('task_id', 'task')}",
            "[step 2] edit the harness deliverable",
            "[step 3] finish the turn",
            f"=== VERIFIER GRADES ===\nreward: {reward}",
        ]
        return "\n".join(lines) if not detail else (
            "=== TASK ===\n" + rec.get("task_id", "task")
            + "\n" + render_detail(rec))

    def task_row(self, task_id: str, rec, tr) -> str:
        mean = tr.mean if tr is not None else 0.0
        status = "pass" if mean == 1.0 else "fail"
        return f"{task_id} | {status} | mean={mean:.2f} | steps=3"

    # ---- gates -------------------------------------------------------------
    def smoke(self, root: Path, runs_dir: Path, job: str, ids: list[str]
              ) -> tuple[bool, dict]:
        """Liveness: the harness CLAUDE.md must exist and be readable."""
        claw = self._claw(root)
        ok = claw.exists()
        return ok, {"checked": [str(claw)] if not ok else [],
                    "detail": "ok" if ok else f"missing {claw}"}


def render_detail(rec) -> str:
    reward = rec.get("reward", 0.0)
    return "\n".join([
        "[step 1] user: solve the task",
        "[step 2] assistant: edited the harness deliverable",
        "[step 3] assistant: finished",
        f"=== VERIFIER GRADES ===\nreward: {reward}",
    ])


def make_domain(repo, raw_cfg) -> ToyDomain:
    return ToyDomain()
