"""Routing, subject extraction and policy evaluation for one tool call.

Policy SELECTION is deterministic (argv override, else the first matching
route on tool name / path glob / command regex). The LLM only judges inside
the selected policy, so text in the diff cannot pick a lenient policy.
"""

from __future__ import annotations

import difflib
import fnmatch
import hashlib
import json
import os
import re
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from . import models
from .ledger import Ledger

PLUGIN_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG = PLUGIN_ROOT / "policies" / "policies.json"
USER_CONFIG = Path("~/.config/rrsi-policy/policies.json").expanduser()
GUARD = "RRSI_POLICY_ACTIVE"


# ---------------------------------------------------------------- config --
def load_config(path: str | None = None) -> dict:
    p = Path(path or os.environ.get("RRSI_POLICY_CONFIG") or
             (USER_CONFIG if USER_CONFIG.exists() else DEFAULT_CONFIG)).expanduser()
    cfg = json.loads(p.read_text())
    cfg["_dir"] = str(p.parent)
    base = json.loads(DEFAULT_CONFIG.read_text())
    cfg["defaults"] = {**base["defaults"], **cfg.get("defaults", {})}
    cfg["selection"] = {**base.get("selection", {}), **cfg.get("selection", {})}
    cfg.setdefault("components", base["components"])
    return cfg


def resolve_asset(cfg: dict, rel: str) -> Path:
    for d in (Path(cfg["_dir"]), DEFAULT_CONFIG.parent):
        if (d / rel).exists():
            return d / rel
    raise FileNotFoundError(rel)


def ledger_for(cfg: dict) -> Ledger:
    return Ledger(os.environ.get("RRSI_POLICY_LEDGER") or cfg["ledger_path"])


def project_of(cwd: str) -> str:
    try:
        out = subprocess.run(["git", "-C", cwd, "rev-parse", "--show-toplevel"],
                             capture_output=True, text=True, timeout=5)
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    return str(Path(cwd).resolve())


# ----------------------------------------------------------- tool calls --
@dataclass
class ToolCall:
    tool: str
    cwd: str
    project: str
    session_id: str = ""
    call_key: str = ""
    file: str | None = None          # absolute path for file tools
    rel: str | None = None
    command: str | None = None       # Bash
    diff: str = ""
    added: str = ""                  # only the text this call introduces
    context: str = ""
    tool_input: dict = field(default_factory=dict)
    transcript_path: str = ""        # the live session's, to inherit its model

    @property
    def target(self) -> str:
        return self.file or ("bash:" + hashlib.sha1((self.command or "").encode()).hexdigest()[:10])


def call_key(ev: dict) -> str:
    if ev.get("tool_use_id"):
        return re.sub(r"[^A-Za-z0-9_-]", "_", ev["tool_use_id"])
    blob = json.dumps([ev.get("session_id"), ev.get("tool_name"), ev.get("tool_input")],
                      sort_keys=True, ensure_ascii=False)
    return hashlib.sha1(blob.encode()).hexdigest()[:20]


def _udiff(old: str, new: str, name: str) -> tuple[str, str]:
    lines = list(difflib.unified_diff(old.splitlines(), new.splitlines(),
                                      fromfile=f"a/{name}", tofile=f"b/{name}", lineterm=""))
    added = "\n".join(l[1:] for l in lines if l.startswith("+") and not l.startswith("+++"))
    return "\n".join(lines), added


def _read(p: Path, limit: int = 2_000_000) -> str:
    try:
        if p.is_file() and p.stat().st_size <= limit:
            return p.read_text(errors="replace")
    except OSError:
        pass
    return ""


def from_hook(ev: dict, max_context: int = 6000) -> ToolCall:
    cwd = ev.get("cwd") or os.getcwd()
    ti = ev.get("tool_input") or {}
    call = ToolCall(tool=ev.get("tool_name", ""), cwd=cwd, project=project_of(cwd),
                    session_id=ev.get("session_id", ""), call_key=call_key(ev), tool_input=ti,
                    transcript_path=ev.get("transcript_path") or "")
    fp = ti.get("file_path") or ti.get("notebook_path")
    if fp:
        p = Path(fp) if os.path.isabs(fp) else Path(cwd) / fp
        call.file = str(p)
        try:
            call.rel = str(p.relative_to(call.project))
        except ValueError:
            call.rel = str(p)
        current = _read(p)
        name = call.rel
        if call.tool == "Write":
            call.diff, call.added = _udiff(current, ti.get("content", ""), name)
        elif call.tool == "Edit":
            call.diff, call.added = _udiff(ti.get("old_string", ""), ti.get("new_string", ""), name)
            call.context = current[:max_context]
        elif call.tool == "MultiEdit":
            parts = [_udiff(e.get("old_string", ""), e.get("new_string", ""), name)
                     for e in ti.get("edits", [])]
            call.diff = "\n".join(d for d, _ in parts)
            call.added = "\n".join(a for _, a in parts)
            call.context = current[:max_context]
        elif call.tool == "NotebookEdit":
            call.diff, call.added = _udiff("", ti.get("new_source", ""), name)
    elif call.tool == "Bash":
        call.command = ti.get("command", "")
        call.diff = call.added = call.command
    return call


# ---------------------------------------------------------------- routing --
def _path_match(call: ToolCall, patterns: list[str]) -> bool:
    if not call.file:
        return False
    cands = [call.rel or "", call.file, os.path.basename(call.file)]
    return any(fnmatch.fnmatch(c, pat) for pat in patterns for c in cands)


def route(cfg: dict, call: ToolCall) -> tuple[str | None, str | None]:
    for r in cfg.get("routes", []):
        if r.get("tools") and call.tool not in r["tools"]:
            continue
        if r.get("paths") and not _path_match(call, r["paths"]):
            continue
        if r.get("command_all"):
            if call.command is None or not all(re.search(p, call.command) for p in r["command_all"]):
                continue
        return r["name"], r["policy"]
    return None, None


def component_hint(cfg: dict, call: ToolCall) -> str:
    for comp, pats in cfg["components"]:
        if _path_match(call, pats):
            return comp
    return "other"


# --------------------------------------------------------------- policies --
@dataclass
class Decision:
    decision: str                    # pass | deny | ask
    policy: str
    verdict: str = "accept"          # accept | reject | uncertain | error
    reasons: list = field(default_factory=list)
    component: str | None = None
    intent: str | None = None
    rule: str | None = None
    meta: dict = field(default_factory=dict)


def regex_hits(patterns: list, text: str) -> list[str]:
    return [why for pat, why in patterns if re.search(pat, text)]


def call_claude(system: str, payload: str, schema: dict, model: str, cfg: dict) -> tuple[dict, dict]:
    d = cfg["defaults"]
    cmd = [os.environ.get("RRSI_CLAUDE_BIN", "claude"), "-p", "--model", model,
           "--tools", "", "--no-session-persistence", "--output-format", "json",
           "--json-schema", json.dumps(schema), "--system-prompt", system]
    # Without --strict-mcp-config the child loads every MCP server the user has
    # and their tool schemas (measured: ~80k input tokens, $0.18 vs $0.004 on haiku).
    if d.get("strict_mcp", True):
        cmd.append("--strict-mcp-config")
    if d.get("setting_sources"):
        cmd += ["--setting-sources", d["setting_sources"]]
    if d.get("bare"):
        cmd.append("--bare")
    # Neutral cwd: the child must not auto-load the project's CLAUDE.md, which
    # may be the very file under review.
    p = subprocess.run(cmd, input=payload, capture_output=True, text=True,
                       timeout=d["timeout_s"], cwd=tempfile.gettempdir(),
                       env={**os.environ, GUARD: "1"})
    if p.returncode != 0 and not p.stdout.strip():
        raise RuntimeError(f"claude -p exited {p.returncode}: {p.stderr.strip()[:300]}")
    out = json.loads(p.stdout)
    if out.get("is_error"):
        raise RuntimeError(f"claude -p error: {str(out.get('result'))[:300]}")
    so = out.get("structured_output")
    if not isinstance(so, dict):
        so = json.loads(out.get("result") or "{}")
    if so.get("verdict") not in ("accept", "reject", "uncertain"):
        raise RuntimeError(f"critic returned no verdict: {str(so)[:200]}")
    return so, {"cost_usd": out.get("total_cost_usd"), "ms": out.get("duration_ms"), "model": model}


def build_payload(cfg: dict, call: ToolCall, route_name: str, hint: str, led: Ledger) -> str:
    d = cfg["defaults"]
    s = cfg["selection"]
    summ = led.summary(call.project, s["n_prune"], s["w"])
    measurement = {k: summ[k] for k in ("incumbent_S", "delta", "pending_edits", "stall", "untried")}
    measurement["prune_set"] = [p["component"] for p in summ["prune_set"]]
    parts = [f"TOOL: {call.tool}", f"TARGET: {call.rel or 'bash command'}",
             f"ROUTE: {route_name}", f"PATH HINT component: {hint}", "",
             "=== DIFF ===" if call.file else "=== COMMAND ===",
             call.diff[:d["max_diff_chars"]] or "(empty)"]
    if call.context:
        parts += ["", "=== FILE CONTEXT (current content, truncated) ===", call.context]
    parts += ["", "=== RECENT REJECTIONS (this project; critic and measured) ===",
              json.dumps(led.recent_rejections(call.project, d["history_n"]),
                         ensure_ascii=False, indent=1),
              "", "=== MEASUREMENT STATE ===",
              "Components in prune_set produced no measured gain recently: adding more "
              "machinery there needs a reason the earlier attempts lacked; removing it is fine.",
              json.dumps(measurement, ensure_ascii=False)]
    return "\n".join(parts)


def evaluate(cfg: dict, policy_name: str, call: ToolCall, route_name: str,
             led: Ledger, model: str | None = None, dry_run: bool = False) -> Decision:
    pol = cfg["policies"][policy_name]
    d = cfg["defaults"]
    kind = pol["kind"]
    if kind == "fixed":
        return Decision(pol["decision"], policy_name, "uncertain" if pol["decision"] == "ask" else "accept",
                        [pol.get("message", "")])
    if kind == "regex":
        hits = regex_hits(pol["patterns"], call.added)
        if hits:
            return Decision(pol.get("decision", "deny"), policy_name, "reject", hits, rule="regex")
        return Decision("pass", policy_name)
    if kind != "llm":
        raise ValueError(f"unknown policy kind {kind}")

    hint = component_hint(cfg, call)
    if pol.get("precheck"):
        hits = regex_hits(cfg["policies"][pol["precheck"]]["patterns"], call.added)
        if hits:
            return Decision("deny", policy_name, "reject", [f"precheck: {h}" for h in hits],
                            component=hint, rule="regex")
    if not call.diff.strip():
        return Decision("pass", policy_name, component=hint)

    # Edit budget: with measurement in use (a baseline exists), stop piling up
    # unmeasured edits so the next measurement stays attributable.
    max_pending = d.get("max_pending") or 0
    if max_pending and call.file and led.incumbent(call.project):
        n = len(led.pending(call.project))
        if n >= max_pending:
            return Decision(d.get("on_budget", "ask"), policy_name, "uncertain",
                            [f"{n} harness edits are landed but unmeasured (budget {max_pending}). "
                             f"Run `rrsi-policy measure` before adding more."], component=hint,
                            rule="edit_budget")
    if dry_run:
        return Decision("pass", policy_name, component=hint, reasons=["dry run: critic skipped"])

    system = resolve_asset(cfg, pol["prompt"]).read_text()
    schema = json.loads(resolve_asset(cfg, pol["schema"]).read_text())
    model = model or os.environ.get("RRSI_POLICY_MODEL") or pol.get("model") or d.get("model")
    # "inherit" (the default): judge with the model the session itself runs on
    model, _src = models.resolve(model, call.cwd, call.transcript_path)
    payload = build_payload(cfg, call, route_name, hint, led)
    out, meta = call_claude(system, payload, schema, model, cfg)
    verdict = out["verdict"]
    dec = Decision("pass", policy_name, verdict, out.get("reasons") or [],
                   component=out.get("component") or hint, intent=out.get("intent"),
                   rule=out.get("rule"), meta={**meta, "risk_notes": out.get("risk_notes")})
    if verdict == "reject":
        dec.decision = pol.get("on_reject", d["on_reject"])
        repairs = led.consecutive_denies(call.project, call.session_id, call.target)
        if dec.decision == "deny" and repairs + 1 >= d["max_repairs"]:
            dec.decision = "ask"
            dec.reasons.append(f"rejected {repairs + 1} times in a row; handing the decision to the user")
    elif verdict == "uncertain":
        dec.decision = pol.get("on_uncertain", d["on_uncertain"])
    return dec


# ------------------------------------------------------------ hook output --
def hook_output(dec: Decision) -> dict | None:
    """Never emit `allow`: that would skip the user's own permission prompts.
    A passing call prints nothing and the normal permission flow applies."""
    if dec.decision == "pass":
        return None
    head = f"rrsi-policy[{dec.policy}]"
    body = "; ".join(r for r in dec.reasons if r) or "no reason given"
    if dec.decision == "deny":
        reason = (f"{head} rejected this change ({dec.rule or 'rule'}): {body}. "
                  f"Revise it to address these points, or ask the user if the change is intended.")
    else:
        reason = f"{head} needs your confirmation ({dec.rule or dec.verdict}): {body}"
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                   "permissionDecision": dec.decision,
                                   "permissionDecisionReason": reason}}
