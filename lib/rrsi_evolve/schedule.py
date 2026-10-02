# Adapted from google-research/rrsi rrsi/schedule.py (Copyright 2026 Google LLC, Apache-2.0).
"""Annealed L0 update budget, Eq. (anneal) of the paper:

    b_t = ceil( b_min + (b_max - b_min) * 1/2 (1 + cos(pi t / T)) ),  t = 0..T-1

Early rounds may bundle several coordinated edits in one candidate; late rounds
become sparse and attributable. The constraint bounds ||z_t||_0, the number of
independent edits active in one proposal, and nothing else: the set of
mechanisms the harness may eventually contain is not restricted.

`endpoint=True` (opt-in, google-research/rrsi#2) evaluates the same cosine at
t/(T-1) when T > 1, so the last round reaches b_min exactly; the default keeps
RRSI's behaviour byte-for-byte.
"""

from __future__ import annotations

import math


def edit_budget(t: int, T: int, b_min: int, b_max: int, endpoint: bool = False) -> int:
    """b_t for round t (0-indexed) of a T-round run."""
    if T <= 0:
        return int(b_max)
    t = max(0, min(int(t), int(T)))
    if endpoint and T > 1:
        v = b_min + (b_max - b_min) * 0.5 * (1.0 + math.cos(math.pi * t / (T - 1)))
    else:
        v = b_min + (b_max - b_min) * 0.5 * (1.0 + math.cos(math.pi * t / T))
    # Guard against 1.0000000002 -> 2 from floating error at t = T.
    return int(math.ceil(round(v, 9)))


def budget_table(T: int, b_min: int, b_max: int, endpoint: bool = False) -> list[int]:
    return [edit_budget(t, T, b_min, b_max, endpoint=endpoint) for t in range(T)]
