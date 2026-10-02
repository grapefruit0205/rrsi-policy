"""Command line: the hook entry point plus the measurement commands.

    rrsi-policy hook [--policy P] [--model M] [--dry-run]   PreToolUse / PostToolUse
    rrsi-policy route                                       which policy a hook event selects
    rrsi-policy check --policy P (--diff-file D | --command C) [--file F]
    rrsi-policy baseline (--cmd CMD [--k K] | --S S [--C C]) [--delta X]
    rrsi-policy measure  (--cmd CMD [--k K] | --S S [--C C]) [--force]
    rrsi-policy revert [--measure ID]
    rrsi-policy status [--json]
    rrsi-policy ledger [-n N] [--all]

check exits 0 pass, 2 deny, 3 ask; usable from git hooks and CI.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys

from . import selection
from .engine import (GUARD, Decision, ToolCall, call_key, evaluate, from_hook, hook_output,
                     ledger_for, load_config, project_of, route, component_hint)

EXIT = {"pass": 0, "deny": 2, "ask": 3}


def _record(led, call: ToolCall, route_name: str, dec: Decision) -> dict:
    return led.append({
        "kind": "decision", "project": call.project, "session_id": call.session_id,
        "tool": call.tool, "target": call.target, "file": call.rel, "route": route_name,
        "policy": dec.policy, "component": dec.component, "intent": dec.intent,
        "rule": dec.rule, "verdict": dec.verdict, "decision": dec.decision,
        "reasons": dec.reasons, **{k: v for k, v in dec.meta.items() if v is not None}})


# ------------------------------------------------------------------ hook --
def cmd_hook(a) -> int:
    if os.environ.get(GUARD):
        return 0                      # inside our own critic or an eval run
    try:
        ev = json.loads(sys.stdin.read() or "{}")
    except json.JSONDecodeError:
        return 0
    cfg = load_config(a.config)
    led = ledger_for(cfg)

    if ev.get("hook_event_name") == "PostToolUse":
        did = led.pop_inflight(call_key(ev))
        if did:
            led.append({"kind": "landed", "decision_id": did,
                        "project": project_of(ev.get("cwd") or os.getcwd())})
        return 0

    call = from_hook(ev, cfg["defaults"]["max_context_chars"])
    route_name, pol = ("argv", a.policy) if a.policy else route(cfg, call)
    if not pol:
        return 0
    kind = cfg["policies"][pol]["kind"]
    try:
        dec = evaluate(cfg, pol, call, route_name, led, model=a.model, dry_run=a.dry_run)
    except Exception as e:  # never crash the user's tool call
        dec = Decision(cfg["defaults"]["on_error"], pol, "error",
                       [f"policy engine error: {type(e).__name__}: {e}"])
    # Harness (llm) decisions always enter the ledger; cheap regex policies
    # only when they fire, so ordinary writes do not flood it.
    if kind == "llm" or dec.decision != "pass":
        rec = _record(led, call, route_name, dec)
        if kind == "llm" and dec.decision != "deny":
            if call.file:
                led.snapshot(rec["id"], call.file)
            led.mark_inflight(call.call_key, rec["id"])
    out = hook_output(dec)
    if out:
        print(json.dumps(out, ensure_ascii=False))
    return 0


def cmd_route(a) -> int:
    cfg = load_config(a.config)
    call = from_hook(json.loads(sys.stdin.read() or "{}"))
    r, p = route(cfg, call)
    print(json.dumps({"route": r, "policy": p, "component_hint": component_hint(cfg, call),
                      "target": call.rel or call.command, "project": call.project}, ensure_ascii=False))
    return 0


def cmd_check(a) -> int:
    cfg = load_config(a.config)
    led = ledger_for(cfg)
    cwd = os.getcwd()
    proj = project_of(cwd)
    if a.command is not None:
        call = ToolCall(tool="Bash", cwd=cwd, project=proj, session_id="cli",
                        command=a.command, diff=a.command, added=a.command)
    else:
        diff = sys.stdin.read() if a.diff_file in (None, "-") else open(a.diff_file).read()
        added = "\n".join(l[1:] for l in diff.splitlines()
                          if l.startswith("+") and not l.startswith("+++")) or diff
        fp = os.path.abspath(a.file) if a.file else None
        call = ToolCall(tool="Edit", cwd=cwd, project=proj, session_id="cli", file=fp,
                        rel=(os.path.relpath(fp, proj) if fp else None), diff=diff, added=added)
    try:
        dec = evaluate(cfg, a.policy, call, "argv", led, model=a.model, dry_run=a.dry_run)
    except Exception as e:
        dec = Decision(cfg["defaults"]["on_error"], a.policy, "error", [f"{type(e).__name__}: {e}"])
    if not a.no_record:
        _record(led, call, "argv", dec)
    print(json.dumps(dec.__dict__, ensure_ascii=False, indent=1))
    return EXIT.get(dec.decision, 3)


# ----------------------------------------------------------- measurement --
def _parse_score(stdout: str) -> dict:
    lines = [l.strip() for l in stdout.splitlines() if l.strip()]
    if not lines:
        raise ValueError("eval command printed nothing")
    last = lines[-1]
    try:
        v = json.loads(last)
    except json.JSONDecodeError:
        raise ValueError(f"last stdout line is not a number or JSON: {last[:120]}")
    if isinstance(v, (int, float)):
        return {"S": float(v), "C": None}
    S = v.get("S", v.get("score"))
    C = v.get("C", v.get("tokens", v.get("cost")))
    if S is None:
        raise ValueError(f"no S/score field in {last[:120]}")
    return {"S": float(S), "C": None if C is None else float(C)}


def _runs(a, proj: str) -> list[dict]:
    if a.S is not None:
        return [{"S": a.S, "C": a.C}]
    if not a.cmd:
        raise SystemExit("give --cmd (an eval command) or --S")
    runs = []
    for i in range(a.k):
        # GUARD disables this engine inside the eval, whose own claude -p
        # sessions would otherwise run the critic on every harness edit.
        p = subprocess.run(a.cmd, shell=True, cwd=proj, capture_output=True, text=True,
                           timeout=a.timeout, env={**os.environ, GUARD: "1"})
        if p.returncode != 0:
            raise SystemExit(f"eval run {i + 1} exited {p.returncode}:\n{p.stderr[-800:]}")
        runs.append(_parse_score(p.stdout))
        print(f"run {i + 1}/{a.k}: S={runs[-1]['S']:.4f} C={runs[-1]['C']}", file=sys.stderr)
    return runs


def cmd_baseline(a) -> int:
    cfg = load_config(a.config)
    led = ledger_for(cfg)
    proj = project_of(os.getcwd())
    runs = _runs(a, proj)
    s = selection.summarize_runs(runs)
    if a.delta is not None:
        delta = a.delta
    elif s["sd"] is not None:
        delta = selection.noise_band(s["sd"], s["k"], cfg["selection"]["delta_z"])
    else:
        raise SystemExit("one run cannot estimate noise: pass --k >= 2 or --delta")
    rec = led.append({"kind": "baseline", "project": proj, "S": s["S"], "C": s["C"],
                      "sd": s["sd"], "k": s["k"], "delta": delta, "runs": runs, "cmd": a.cmd})
    print(json.dumps({k: rec[k] for k in ("id", "S", "C", "sd", "k", "delta")}, indent=1))
    if delta == 0:
        print("warning: delta = 0 (deterministic eval?) - every gain counts as real", file=sys.stderr)
    return 0


def cmd_measure(a) -> int:
    cfg = load_config(a.config)
    led = ledger_for(cfg)
    proj = project_of(os.getcwd())
    inc = led.incumbent(proj)
    if not inc:
        raise SystemExit("no baseline for this project: run `rrsi-policy baseline` first")
    pend = led.pending(proj)
    if not pend and not a.force:
        raise SystemExit("no landed harness edits since the last measurement (use --force to re-measure)")
    runs = _runs(a, proj)
    s = selection.summarize_runs(runs)
    comps = [p.get("component") for p in pend if p.get("component")]
    j = selection.judge(s["S"], s["C"], inc["S"], inc.get("C"), led.s_star(proj), inc["delta"],
                        comps, led.incumbent_component_counts(proj), cfg["selection"])
    rec = led.append({"kind": "measure", "project": proj, "S": s["S"], "C": s["C"], "k": s["k"],
                      "runs": runs, "edits": [p["id"] for p in pend], "components": comps,
                      "incumbent_id": inc["id"], **j})
    print(json.dumps({"id": rec["id"], "outcome": j["outcome"], "reason": j["reason"],
                      "S": s["S"], "incumbent_S": inc["S"], "delta_S": j["delta_S"],
                      "delta_C": j["delta_C"], "bundle": [
                          {"id": p["id"], "file": p.get("file"), "component": p.get("component"),
                           "intent": p.get("intent")} for p in pend]}, ensure_ascii=False, indent=1))
    if j["outcome"] == "REJECTED" and pend:
        print(f"\nThe files still contain this bundle but the incumbent does not. "
              f"Restore them with: rrsi-policy revert --measure {rec['id']}", file=sys.stderr)
    return 0 if j["outcome"] == "ACCEPTED" else 1


def cmd_revert(a) -> int:
    cfg = load_config(a.config)
    led = ledger_for(cfg)
    proj = project_of(os.getcwd())
    ms = [r for r in led.records(proj, "measure") if r.get("outcome") == "REJECTED"]
    m = next((r for r in ms if r["id"] == a.measure), None) if a.measure else (ms[-1] if ms else None)
    if not m:
        raise SystemExit("no rejected measurement to revert")
    if any(r.get("measure_id") == m["id"] for r in led.records(proj, "reverted")):
        raise SystemExit(f"measurement {m['id']} was already reverted")
    decisions = {r["id"]: r for r in led.records(proj, "decision")}
    first_per_target, skipped = {}, []
    for did in m["edits"]:        # earliest snapshot per file = state before the bundle
        d = decisions.get(did, {})
        if d.get("tool") == "Bash" or not d.get("file"):
            skipped.append(d.get("target"))
        else:
            first_per_target.setdefault(d["target"], did)
    done = [msg for did in first_per_target.values() if (msg := led.restore(did))]
    led.append({"kind": "reverted", "project": proj, "measure_id": m["id"], "files": done,
                "not_restorable": skipped})
    print("\n".join(done) or "nothing restored")
    if skipped:
        print(f"not restorable (edited through Bash, no snapshot): {skipped}", file=sys.stderr)
    return 0


def cmd_status(a) -> int:
    cfg = load_config(a.config)
    led = ledger_for(cfg)
    proj = project_of(os.getcwd())
    s = led.summary(proj, cfg["selection"]["n_prune"], cfg["selection"]["w"])
    s["project"] = proj
    s["pending"] = [{"id": p["id"], "file": p.get("file"), "component": p.get("component"),
                     "intent": p.get("intent")} for p in led.pending(proj)]
    if a.json:
        print(json.dumps(s, ensure_ascii=False, indent=1))
        return 0
    f = lambda v: "-" if v is None else (f"{v:.4f}" if isinstance(v, float) else str(v))
    print(f"project      {proj}")
    print(f"incumbent S  {f(s['incumbent_S'])}   S* {f(s['S_star'])}   delta {f(s['delta'])}   "
          f"measurements {s['measurements']}")
    print(f"stall        {'YES - steer toward untried components' if s['stall'] else 'no'}")
    print(f"untried      {', '.join(s['untried']) or '-'}")
    print(f"yield g(l)   {json.dumps(s['yield'])}")
    print(f"prune set    {', '.join(p['component'] for p in s['prune_set']) or '-'}")
    print(f"pending      {len(s['pending'])} landed edit(s) awaiting `rrsi-policy measure`")
    for p in s["pending"]:
        print(f"  - [{p['component']}] {p['file']}: {p['intent']}")
    return 0


def cmd_ledger(a) -> int:
    cfg = load_config(a.config)
    led = ledger_for(cfg)
    recs = led.records(None if a.all else project_of(os.getcwd()))
    for r in recs[-a.n:]:
        r = {k: v for k, v in r.items() if k not in ("runs", "reasons")} | (
            {"reasons": r["reasons"][:2]} if r.get("reasons") else {})
        print(json.dumps(r, ensure_ascii=False))
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="rrsi-policy", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", help="policies.json (default: ~/.config/rrsi-policy/, else the plugin's)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    h = sub.add_parser("hook", help="Claude Code hook entry point (reads the event on stdin)")
    h.add_argument("--policy", help="force this policy instead of routing")
    h.add_argument("--model")
    h.add_argument("--dry-run", action="store_true", help="route and precheck, skip the LLM")
    h.set_defaults(fn=cmd_hook)

    sub.add_parser("route", help="print the route a hook event would take").set_defaults(fn=cmd_route)

    c = sub.add_parser("check", help="run one policy on a diff or command")
    c.add_argument("--policy", required=True)
    c.add_argument("--file")
    c.add_argument("--diff-file", help="unified diff; '-' or omitted reads stdin")
    c.add_argument("--command")
    c.add_argument("--model")
    c.add_argument("--dry-run", action="store_true")
    c.add_argument("--no-record", action="store_true")
    c.set_defaults(fn=cmd_check)

    for name, fn in (("baseline", cmd_baseline), ("measure", cmd_measure)):
        m = sub.add_parser(name, help=f"{name}: run the eval command k times "
                                      f"(last stdout line: a number or {{\"S\":..,\"C\":..}})")
        m.add_argument("--cmd")
        m.add_argument("--k", type=int, default=3)
        m.add_argument("--S", type=float, help="record a score measured elsewhere")
        m.add_argument("--C", type=float, help="its mean tokens per run (optional)")
        m.add_argument("--timeout", type=int, default=3600)
        if name == "baseline":
            m.add_argument("--delta", type=float, help="noise band; default z*sd*sqrt(2/k)")
        else:
            m.add_argument("--force", action="store_true")
        m.set_defaults(fn=fn)

    r = sub.add_parser("revert", help="restore the files of a rejected measurement")
    r.add_argument("--measure")
    r.set_defaults(fn=cmd_revert)

    s = sub.add_parser("status", help="incumbent, prune set, stall, pending edits")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_status)

    l = sub.add_parser("ledger", help="recent ledger records")
    l.add_argument("-n", type=int, default=20)
    l.add_argument("--all", action="store_true")
    l.set_defaults(fn=cmd_ledger)

    a = ap.parse_args(argv)
    return a.fn(a)
