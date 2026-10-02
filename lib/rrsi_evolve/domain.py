# Adapted from google-research/rrsi rrsi/domain.py (Copyright 2026 Google LLC, Apache-2.0).
"""The Domain interface: everything rrsi-evolve needs from a benchmark env.

RRSI never runs an agent, grades a deliverable or reads a trajectory format.
A domain adapter (domains/<name>/adapter.py) supplies those, and the texts
that make the search roles speak the benchmark's language.

    run / score        Evaluate(H', D, k)              rrsi_evolve.evaluate
    load_trial         trajectories for Analyze        rrsi_evolve.loop.build_traces
    render_trace       what the analyst/digester/proposer read
    task_row           one-line summary per trace in the task tables
    smoke              liveness check before evaluation (not a selection rule)
    critic_patterns    deterministic leakage denylist   rrsi_evolve.critic
    component_signals  diff regexes -> component tag    rrsi_evolve.components
    guards             non-compensatory domain checks   rrsi_evolve.selection (Sec. 3.3)
    briefs             domain paragraphs for analyst / digester / proposer / critic

Deviations from RRSI's Domain (allowed deviations 2 and 6):

  - `harness_path` is relative to the user's REPOSITORY root (default
    "harness"), not to domains/<name>/; `harness_rel` is its normalized
    relative form and `repo` is the main checkout of the user's repository.
  - Domain modules live in this package's `domains/` directory, in a
    repository-level directory `rrsi_domains/<name>/adapter.py`, or (for
    tests) in a directory named by RRSI_EVOLVE_DOMAIN_PATH.
  - Adapters export `make_domain(repo, raw_cfg) -> Domain` and receive the
    flat rrsi.json config (`raw`) read by the CLI; `load_domain` sets
    `dom.root` (the adapter's own directory, where SKILL.md /
    PATTERNS.md / briefs live) and `dom.repo`.
  - `constitution(root)` honours a REPO-LEVEL override: when
    `<root>/rrsi/SKILL.md` and `<root>/rrsi/PATTERNS.md` exist they are read
    instead of the package copies.
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

# Package directory that holds domains/<name>/adapter.py (and the shipped
# claudecode domain's module files).
DOMAINS = Path(__file__).resolve().parent / "domains"


class Domain:
    name: str = "base"
    harness_path: str = "harness"     # relative to the REPOSITORY root
    root: Path                        # the domain package dir (SKILL.md, ...)
    repo: Path                        # MAIN checkout of the user's repository

    # ---- task sets ---------------------------------------------------------
    def evolve_ids(self) -> list[str]: raise NotImplementedError
    def heldout_ids(self) -> list[str]: return []
    def smoke_ids(self, incumbent_per_task: dict | None = None) -> list[str]:
        raise NotImplementedError

    # ---- Evaluate ----------------------------------------------------------
    def run(self, root: Path, runs_dir: Path, job: str, ids: list[str], k: int,
            log_prefix: str = "") -> None:
        """Run the harness checked out under `root` (a worktree) on `ids`
        with k trials, writing to runs_dir/jobs/<job>. Must be resume-safe."""
        raise NotImplementedError

    def score(self, runs_dir: Path, job: str, ids: list[str], k: int
              ) -> tuple[dict, dict]:
        """-> ({task_id: TaskResult}, extra aggregates)."""
        raise NotImplementedError

    def guards(self, incumbent, candidate) -> list[str]:
        """Violated non-compensatory domain criteria (empty = none)."""
        return []

    def regression_threshold(self, k: int) -> float:
        """Per-task mean drop that counts as a regression in attribution."""
        return 1.0 / max(1, k)

    # ---- evidence ----------------------------------------------------------
    def load_trial(self, runs_dir: Path, job: str, task_id: str, trial: int):
        raise NotImplementedError

    def render_trace(self, rec, detail: bool = False) -> str:
        raise NotImplementedError

    def task_row(self, task_id: str, rec, tr) -> str:
        raise NotImplementedError

    # ---- gates and texts ---------------------------------------------------
    def smoke(self, root: Path, runs_dir: Path, job: str, ids: list[str]
              ) -> tuple[bool, dict]:
        raise NotImplementedError

    critic_patterns: list = []
    component_signals: list = []
    briefs: dict = {}                    # analyst / digester / proposer / critic
    source_exts: set = {".py", ".txt", ".md", ".json"}

    @property
    def harness_rel(self) -> str:
        """The harness path, normalized, relative to the repository root."""
        return os.path.normpath(self.harness_path)

    def harness_dir(self, root: Path) -> Path:
        return (root / self.harness_path).resolve()

    def constitution(self, root: Path) -> tuple[str, str]:
        """(SKILL.md, PATTERNS.md): a repo-level override under <root>/rrsi/
        wins over the domain package's copies."""
        override = root / "rrsi"
        if (override / "SKILL.md").is_file() and (override / "PATTERNS.md").is_file():
            return ((override / "SKILL.md").read_text(),
                    (override / "PATTERNS.md").read_text())
        return ((self.root / "SKILL.md").read_text(),
                (self.root / "PATTERNS.md").read_text())


def load_domain(name: str, repo: Path, raw_cfg: dict | None = None) -> Domain:
    """Find <name>'s adapter, build the Domain from make_domain(repo, raw_cfg),
    and stamp `root` (the adapter's directory) and `repo` on it."""
    repo = Path(repo)
    raw_cfg = raw_cfg or {}
    candidates = [
        repo / "rrsi_domains" / name / "adapter.py",
        DOMAINS / name / "adapter.py",
    ]
    env_dir = os.environ.get("RRSI_EVOLVE_DOMAIN_PATH")
    if env_dir:
        candidates.append(Path(env_dir) / name / "adapter.py")
    p = next((c for c in candidates if c.is_file()), None)
    if p is None:
        looked = ", ".join(str(c) for c in candidates)
        raise SystemExit(f"unknown domain {name!r} (no adapter.py in {looked})")
    spec = importlib.util.spec_from_file_location(f"rrsi_evolve.domains.{name}.adapter", p)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    make = getattr(mod, "make_domain", None)
    if make is None:
        raise SystemExit(f"{p} does not export make_domain(repo, raw_cfg)")
    dom = make(repo, raw_cfg)
    dom.root = p.parent
    dom.repo = repo
    return dom
