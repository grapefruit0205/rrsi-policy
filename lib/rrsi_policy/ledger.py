"""The decision ledger: one JSONL file, every record tagged with its project.

Record kinds:
    decision  a policy verdict on one tool call (written by the PreToolUse hook)
    landed    that call actually ran (PostToolUse); only landed edits are measured
    baseline  the incumbent's score S, cost C and noise band delta
    measure   a bundle of landed edits measured against the incumbent and judged
    reverted  files restored from snapshots after a rejected measurement

The measurement views port rrsi/history.py: every edit in a measured bundle
carries the bundle's (dS, accepted); tried components, the recent yield g(l),
the prune set B and the stall flag sigma are computed from those records.
"""

from __future__ import annotations

import fcntl
import json
import math
import os
import time
import uuid
from pathlib import Path

COMPONENTS = ["claude_md", "skill", "agent", "command", "hook", "settings", "mcp", "memory"]


class Ledger:
    def __init__(self, path: str | Path):
        self.path = Path(os.path.expanduser(str(path)))
        self.state = self.path.parent
        self.snapshots = self.state / "snapshots"
        self.inflight = self.state / "inflight"

    # ------------------------------------------------------------ storage --
    def records(self, project: str | None = None, kind: str | None = None) -> list[dict]:
        if not self.path.exists():
            return []
        out = []
        with open(self.path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if project is not None and r.get("project") != project:
                    continue
                if kind is not None and r.get("kind") != kind:
                    continue
                out.append(r)
        return out

    def append(self, rec: dict) -> dict:
        rec = dict(rec)
        rec.setdefault("id", uuid.uuid4().hex[:12])
        rec.setdefault("ts", time.strftime("%Y-%m-%dT%H:%M:%S%z"))
        self.state.mkdir(parents=True, exist_ok=True)
        with open(self.path, "a") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            fcntl.flock(f, fcntl.LOCK_UN)
        return rec

    # ---------------------------------------------- snapshots and inflight --
    def snapshot(self, decision_id: str, file_path: str, max_bytes: int = 2_000_000) -> bool:
        """Store a file's content BEFORE an edit so a rejected bundle can be
        restored. A file that does not exist yet is recorded as absent."""
        self.snapshots.mkdir(parents=True, exist_ok=True)
        p = Path(file_path)
        meta = {"file": str(p), "absent": not p.exists()}
        if p.exists():
            if p.stat().st_size > max_bytes:
                return False
            (self.snapshots / f"{decision_id}.bin").write_bytes(p.read_bytes())
        (self.snapshots / f"{decision_id}.json").write_text(json.dumps(meta))
        return True

    def restore(self, decision_id: str) -> str | None:
        meta_p = self.snapshots / f"{decision_id}.json"
        if not meta_p.exists():
            return None
        meta = json.loads(meta_p.read_text())
        p = Path(meta["file"])
        if meta["absent"]:
            if p.exists():
                p.unlink()
            return f"removed {p}"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes((self.snapshots / f"{decision_id}.bin").read_bytes())
        return f"restored {p}"

    def mark_inflight(self, call_key: str, decision_id: str) -> None:
        self.inflight.mkdir(parents=True, exist_ok=True)
        (self.inflight / call_key).write_text(decision_id)

    def pop_inflight(self, call_key: str) -> str | None:
        p = self.inflight / call_key
        if not p.exists():
            return None
        did = p.read_text().strip()
        p.unlink(missing_ok=True)
        return did

    # ----------------------------------------------------- critic history --
    def recent_rejections(self, project: str, n: int) -> list[dict]:
        """Critic rejections and measured rejections for this project, newest
        last: the negative evidence a re-proposal has to answer."""
        recs = self.records(project)
        by_id = {r["id"]: r for r in recs if r.get("kind") == "decision"}
        out = []
        for r in recs:
            # only critic verdicts carry an intent; regex hits are not evidence about mechanisms
            if r.get("kind") == "decision" and r.get("verdict") == "reject" and r.get("intent"):
                out.append({"source": "critic", "component": r.get("component"),
                            "intent": r.get("intent"), "rule": r.get("rule"),
                            "reasons": (r.get("reasons") or [])[:3]})
            elif r.get("kind") == "measure" and r.get("outcome") == "REJECTED":
                for did in r.get("edits", []):
                    d = by_id.get(did, {})
                    out.append({"source": "measurement", "component": d.get("component"),
                                "intent": d.get("intent"), "delta_S": r.get("delta_S"),
                                "reason": r.get("reason")})
        return out[-n:]

    def consecutive_denies(self, project: str, session_id: str, target: str) -> int:
        n = 0
        for r in self.records(project, "decision"):
            if r.get("session_id") != session_id or r.get("target") != target:
                continue
            if r.get("verdict") == "reject":
                n += 1
            elif r.get("verdict") == "accept":
                n = 0
        return n

    # ------------------------------------------------------- measurement --
    def incumbent(self, project: str) -> dict | None:
        """Latest baseline, or the latest accepted measurement after it."""
        inc = None
        for r in self.records(project):
            if r.get("kind") == "baseline":
                inc = r
            elif r.get("kind") == "measure" and r.get("outcome") == "ACCEPTED" and inc:
                inc = dict(r, delta=inc["delta"])
        return inc

    def s_star(self, project: str) -> float | None:
        vals = [r["S"] for r in self.records(project)
                if r.get("kind") == "baseline"
                or (r.get("kind") == "measure" and r.get("outcome") == "ACCEPTED")]
        return max(vals) if vals else None

    def pending(self, project: str) -> list[dict]:
        """Landed edits not yet part of any measurement. A denied call never
        lands; an `ask` the user approved does, and is measured like the rest."""
        recs = self.records(project)
        landed = {r["decision_id"] for r in recs if r.get("kind") == "landed"}
        measured = {d for r in recs if r.get("kind") == "measure" for d in r.get("edits", [])}
        return [r for r in recs if r.get("kind") == "decision" and r["id"] in landed
                and r["id"] not in measured]

    def measured_edits(self, project: str) -> list[dict]:
        """One row per edit per measurement: (t, component, delta_S, accepted)."""
        recs = self.records(project)
        by_id = {r["id"]: r for r in recs if r.get("kind") == "decision"}
        rows, t = [], 0
        for r in recs:
            if r.get("kind") != "measure":
                continue
            for did in r.get("edits", []):
                c = by_id.get(did, {}).get("component")
                if c in COMPONENTS:
                    rows.append({"t": t, "component": c, "decision_id": did,
                                 "delta_S": r["delta_S"],
                                 "accepted": r["outcome"] == "ACCEPTED",
                                 "intent": by_id[did].get("intent")})
            t += 1
        return rows

    def incumbent_component_counts(self, project: str) -> dict:
        counts = {c: 0 for c in COMPONENTS}
        for row in self.measured_edits(project):
            if row["accepted"]:
                counts[row["component"]] += 1
        return counts

    def summary(self, project: str, n_prune: int, w: int) -> dict:
        rows = self.measured_edits(project)
        t_now = (max(r["t"] for r in rows) if rows else -1)
        tried = sorted({r["component"] for r in rows})
        g = {c: -math.inf for c in tried}
        for r in rows:
            if t_now - r["t"] < n_prune:
                g[r["component"]] = max(g[r["component"]], r["delta_S"])
        prune = []
        for c in tried:
            if g[c] <= 0:
                prune.append({"component": c,
                              "recent_best_gain": None if g[c] == -math.inf else round(g[c], 5),
                              "accepted_edits_in_incumbent": [
                                  {"decision_id": r["decision_id"], "intent": r["intent"]}
                                  for r in rows if r["component"] == c and r["accepted"]]})
        # incumbent score trajectory: baseline, then the incumbent after each measurement
        traj, cur = [], None
        for r in self.records(project):
            if r.get("kind") == "baseline":
                cur = r["S"]
                traj = [cur]
            elif r.get("kind") == "measure" and cur is not None:
                if r["outcome"] == "ACCEPTED":
                    cur = r["S"]
                traj.append(cur)
        inc = self.incumbent(project)
        delta = inc["delta"] if inc else None
        stall = int(len(traj) > w and delta is not None and traj[-1] - traj[-1 - w] <= delta)
        return {"incumbent_S": inc["S"] if inc else None,
                "incumbent_C": inc.get("C") if inc else None,
                "S_star": self.s_star(project), "delta": delta,
                "measurements": t_now + 1, "trajectory": traj,
                "pending_edits": len(self.pending(project)),
                "tried": tried, "untried": [c for c in COMPONENTS if c not in tried],
                "yield": {c: (None if v == -math.inf else round(v, 5)) for c, v in g.items()},
                "prune_set": prune, "stall": stall}
