# Adapted from google-research/rrsi rrsi/driver.py (Copyright 2026 Google LLC, Apache-2.0).
"""Sequential driver: baseline + calibration if missing, then rounds t..T-1.

A round is settled once the frontier trajectory has an entry for t+1.
`touch runs/<domain>/STOP` stops after the current round. Consecutive
infrastructure failures stop the driver so a broken environment cannot burn
the whole budget.

Deviation from RRSI: `drive` gains `repo` and `config`, passed to the entry
point as `--repo` / `--config` (before `--domain`), so a subprocess round
resolves the same repository and rrsi.json the parent invocation used.
A SIGTERM / Ctrl-C to the driver is passed on to the running child (SIGTERM,
then a bounded wait), so the child's own cleanups run.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

MAX_CONSECUTIVE_INFRA = 3
CHILD_GRACE_S = 120


def _run(args: list[str], lf) -> subprocess.CompletedProcess:
    proc = subprocess.Popen(args, stdout=lf, stderr=subprocess.STDOUT)
    try:
        return subprocess.CompletedProcess(args, proc.wait())
    except BaseException:
        proc.terminate()
        try:
            proc.wait(timeout=CHILD_GRACE_S)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        raise


def settled_rounds(frontier_path: Path) -> int:
    if not frontier_path.exists():
        return -1
    fr = json.loads(frontier_path.read_text())
    return len(fr["trajectory"]) - 1          # trajectory has entries 0..settled


def drive(entry: Path, domain: str, runs_dir: Path, T: int, start: int = 0,
          extra_args: list[str] | None = None, repo: Path | None = None,
          config: Path | None = None) -> None:
    run_dir = runs_dir / domain
    fr = run_dir / "frontier.json"
    stop = run_dir / "STOP"
    logs = run_dir / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    base_args = [sys.executable, str(entry)]
    if repo is not None:
        base_args += ["--repo", str(repo)]
    if config is not None:
        base_args += ["--config", str(config)]
    base_args += ["--domain", domain,
                  "--runs", str(runs_dir)] + list(extra_args or [])
    if not fr.exists():
        print(f"[driver:{domain}] baseline + calibrate", flush=True)
        with open(logs / "baseline.log", "a") as lf:
            r = _run(base_args + ["baseline"], lf)
        if r.returncode != 0 or not fr.exists():
            print(f"[driver:{domain}] baseline failed (rc={r.returncode})")
            sys.exit(1)
    if not (run_dir / "calibration.json").exists():
        with open(logs / "calibrate.log", "a") as lf:
            _run(base_args + ["calibrate"], lf)
    infra = 0
    for t in range(start, T):
        if stop.exists():
            print(f"[driver:{domain}] STOP present; exiting", flush=True)
            return
        if settled_rounds(fr) >= t + 1:
            print(f"[driver:{domain}] round {t} already settled", flush=True)
            continue
        log = logs / f"r{t}.log"
        print(f"[driver:{domain}] === round {t} ({time.strftime('%H:%M:%S')}) -> {log}",
              flush=True)
        with open(log, "a") as lf:
            r = _run(base_args + ["round", "--t", str(t)], lf)
        if r.returncode != 0 or settled_rounds(fr) < t + 1:
            infra += 1
            print(f"[driver:{domain}] round {t} did not settle (rc={r.returncode}, "
                  f"{infra}/{MAX_CONSECUTIVE_INFRA})", flush=True)
            if infra >= MAX_CONSECUTIVE_INFRA:
                print(f"[driver:{domain}] too many consecutive failures; stopping")
                sys.exit(1)
        else:
            infra = 0
            b = json.loads(fr.read_text())["incumbent"]
            print(f"[driver:{domain}]   incumbent t={b['t']} {b['commit']} S={b['S']:.4f}",
                  flush=True)
    print(f"[driver:{domain}] all rounds settled", flush=True)
