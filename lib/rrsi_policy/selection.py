"""Acceptance of a measured bundle of harness edits (RRSI Algorithm 2).

For a bundle measured at (S', C') against the incumbent (S_t, C_t):

    dS = S' - S_t,   dC = (C' - C_t) / C_t       (dC = 0 when no cost is reported)
    cost rule:
        dS >  delta:  dC <= beta0 + beta1 * dS
        dS <= delta:  w_s * dS - w_c * dC + w_n * novelty > 0
    admissible iff S' >= S* - delta and the cost rule holds

S is whatever the user's eval command reports (a pass rate in [0, 1] is the
intended scale); delta, beta1 and w_s are in the same units.
"""

from __future__ import annotations

import math
import statistics

STRUCTURAL = ("skill", "agent", "hook", "mcp", "memory")


def relative_cost_change(C_new, C_old) -> float:
    if C_new is None or C_old in (None, 0):
        return 0.0
    return (C_new - C_old) / C_old


def novelty(components: list[str], incumbent_counts: dict) -> int:
    """Structural components the bundle touches that the incumbent has never
    had a measured-and-accepted edit on. Only tie-breaks inside the band."""
    return sum(1 for c in set(components)
               if c in STRUCTURAL and incumbent_counts.get(c, 0) == 0)


def cost_rule(dS: float, dC: float, nov: int, delta: float, cfg: dict) -> tuple[bool, str]:
    if dS > delta:
        budget = cfg["beta0"] + cfg["beta1"] * dS
        ok = dC <= budget
        return ok, (f"gain {dS:+.4f} > delta {delta:.4f}; cost {dC:+.3f} "
                    f"{'<=' if ok else '>'} budget {budget:.3f}")
    shaped = cfg["w_s"] * dS - cfg["w_c"] * dC + cfg["w_n"] * nov
    ok = shaped > 0
    return ok, (f"gain {dS:+.4f} within delta {delta:.4f}; shaped "
                f"{cfg['w_s']}*dS - {cfg['w_c']}*dC + {cfg['w_n']}*nu = {shaped:+.4f} "
                f"{'>' if ok else '<='} 0 (nu={nov})")


def judge(S: float, C, inc_S: float, inc_C, S_star: float, delta: float,
          components: list[str], incumbent_counts: dict, cfg: dict) -> dict:
    dS = S - inc_S
    dC = relative_cost_change(C, inc_C)
    nov = novelty(components, incumbent_counts)
    out = {"delta_S": round(dS, 6), "delta_C": round(dC, 6), "novelty": nov}
    if S < S_star - delta:
        out.update(outcome="REJECTED",
                   reason=f"below noise-adjusted floor: S' {S:.4f} < S* {S_star:.4f} - delta {delta:.4f}")
        return out
    ok, why = cost_rule(dS, dC, nov, delta, cfg)
    out.update(outcome="ACCEPTED" if ok else "REJECTED",
               reason=("admissible: " if ok else "cost rule failed: ") + why)
    return out


def summarize_runs(runs: list[dict]) -> dict:
    """Mean score/cost over k repeated eval runs, plus the run-to-run sd."""
    S = [r["S"] for r in runs]
    C = [r["C"] for r in runs if r.get("C") is not None]
    return {"S": statistics.fmean(S),
            "C": statistics.fmean(C) if len(C) == len(runs) and C else None,
            "sd": statistics.stdev(S) if len(S) >= 2 else None,
            "k": len(runs)}


def noise_band(sd: float, k: int, z: float) -> float:
    """z standard deviations of the difference of two k-run means."""
    return z * sd * math.sqrt(2.0 / k)
