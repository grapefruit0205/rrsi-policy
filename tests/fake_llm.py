#!/usr/bin/env python3
# Part of rrsi-policy. Ports google-research/rrsi (Apache-2.0).
"""Scripted LLM backend for the rrsi-evolve loop tests.

Point RRSI_EVOLVE_LLM at this file as fake:<path> and the four search roles
(analyst, digester, proposer, critic) run their REAL RRSI control flow against
a scripted model with RRSI's generate() signature. Routing is deterministic
and (except the critic) stateless: every call re-reads the interaction log
embedded in its own prompt, exactly as RRSI's role loops do, so no per-call
bookkeeping is needed across the parallel digesters or the two candidate
variants of a round.

  digester  one valid "return" digest matching the assigned lens
  analyst   first digest_many over the task ids found in the TASK TABLE, then
            the three-lens "report"
  proposer  one harness change, then a valid done(): edit_file appends the
            next generic standing rule to the harness CLAUDE.md; when the
            prompt says a RESERVED EXPLORATION SLOT applies, the change is a
            write_file on an untried component instead (a
            skills/general-truth-rules/SKILL.md). The round's edit budget is
            respected (exactly one change per candidate) and done() fills
            every contract field.
  critic    accept, unless FAKE_EVOLVE_CRITIC_REJECTS forces the first N
            reviews of the process to reject (FAKE_EVOLVE_REJECT_REASON
            overrides the reason), which drives the repair path.

All produced text is entity-free, deterministic and offline: no network, no
real `claude` CLI, no paid API.

Env knobs: FAKE_EVOLVE_CRITIC_REJECTS (int, default 0),
FAKE_EVOLVE_REJECT_REASON, FAKE_EVOLVE_TRACE_LOG (optional JSONL of calls).
"""

from __future__ import annotations

import json
import os


def _trace(system: str, prompt: str, out: str) -> None:
    path = os.environ.get("FAKE_EVOLVE_TRACE_LOG")
    if not path:
        return
    try:
        with open(path, "a") as f:
            f.write(json.dumps({"system_head": (system or "")[:60],
                                "prompt_chars": len(prompt),
                                "out_head": out[:80]}) + "\n")
    except OSError:
        pass


def _out(obj) -> str:
    return json.dumps(obj)


# ---------------------------------------------------------------- digester --
# Lens -> digest body, matching rrsi_evolve.digester.SCHEMAS field names.
_DIGESTS = {
    "failure": {
        "blocker": "The instructions never said when to stop, so the run kept "
                   "polishing past the point the checker rewards.",
        "narrative": "The run finished the requested change, then continued to "
                     "re-read and re-edit the same file. Nothing wrong was "
                     "found; time ran out before the final turn returned.",
        "evidence": [{"where": "step 3", "quote": "verify your work, then stop"}],
        "verifier_evidence": "",
        "capability_note": "",
        "needed_instead": "A bounded stop: one check, then finish.",
    },
    "capability_gap": {
        "wanted": "Re-check the finished change once before stopping.",
        "why_couldnt": "The standing instructions do not schedule a check, so "
                       "the run had no trigger to use one.",
        "evidence": [{"where": "step 2", "quote": "no such instruction"}],
        "workaround_seen": "",
    },
    "success": {
        "habits": [{"habit": "Read the task, make the smallest change that "
                             "satisfies it, then stop",
                    "where_shown": "step 1"}],
        "risk_if_removed": "Removal would slow every run that currently "
                           "finishes in one pass.",
    },
}


def _digest_for(system: str, prompt: str) -> str:
    lens = "failure"
    for cand in ("failure", "capability_gap", "success"):
        if f"Your lens for this assignment: {cand}" in (system or ""):
            lens = cand
    task_id = "task"
    head = prompt.split("\n", 1)[0] if prompt else ""
    if head.startswith("Assigned trace: "):
        task_id = head[len("Assigned trace: "):].split(".", 1)[0]
    d = dict(_DIGESTS[lens])
    d["task_id"] = task_id
    d["lens"] = lens
    return _out({"action": "return", "digest": d})


# ---------------------------------------------------------------- analyst --
_REPORT = {
    "n_digests": 2,
    "failure_modes": [{
        "mode": "no_bounded_stop",
        "n_tasks": 2,
        "affected_tasks": [],
        "description": "Runs keep working past the rewarded change because the "
                       "standing instructions never schedule a stop.",
        "needed_instead": "One bounded check after the change, then finish.",
        "representative_evidence": [],
    }],
    "capability_gaps": [],
    "success_habits": [{
        "habit": "Make the smallest change that satisfies the prompt",
        "n_tasks": 3,
        "description": "Runs pass by changing only what the prompt asked for.",
        "risk_if_broken": "Extra edits could break currently passing tasks.",
    }],
}


def _analyst_action(prompt: str) -> str:
    # A fresh interaction log (no digest_many answered yet) -> digest_many
    # over every task id in the TASK TABLE; afterwards -> the report.
    if "digest_many" in prompt:
        return _out({"action": "report", **_REPORT})
    table = prompt.split("=== TASK TABLE (one rendered trace per task; lowest "
                         "scores first) ===", 1)[-1]
    table = table.split("=== PRIOR", 1)[0]
    ids = []
    for line in table.splitlines():
        line = line.strip()
        if not line or line.startswith("==="):
            continue
        tok = line.split()[0]
        if tok not in ids:
            ids.append(tok)
    reqs = [{"task_id": tid, "lens": "failure",
             "questions": ["How did the run end?"]} for tid in ids]
    return _out({"action": "digest_many", "requests": reqs})


# --------------------------------------------------------------- proposer --
# General (entity-free) standing rules the fake proposer appends, one per
# edit_file, in order. Each mention of a general practice flips the toy
# domain's keyword scoring without naming any task.
_RULES = [
    "Finish the change, run one bounded verification pass, then stop; never "
    "re-open work you already finished.",
    "Answer every verification question with the general rule that decides it, "
    "then stop.",
    "Prefer the smallest change that satisfies the prompt; never add files the "
    "prompt did not ask for.",
    "Before finishing, re-read the prompt aloud and confirm the deliverable "
    "matches every clause of it.",
]

_NEXT_SKILL = ("skills/general-truth-rules", "SKILL.md")


def _harness_rule_count(prompt: str) -> int:
    """How many of _RULES the harness dump in the prompt already contains."""
    n = 0
    for r in _RULES:
        if r[:40] in prompt:
            n += 1
    return n


def _edit(edit_id: str, n_rule: int) -> dict:
    return {
        "id": edit_id,
        "component": "prompt",
        "hypothesis": "A standing bounded-stop rule in the harness guidance "
                      "turns run-away polishing into a single check, which "
                      "should lift the affected trials.",
        "targets_mode": "no_bounded_stop",
        "why_not_lower_lever": "The policy already knows how to check; what is "
                               "missing is WHEN to stop.",
        "trigger_condition": "always (a standing rule read every session)",
        "predicted_affected": [f"t{n_rule + 1}"],
        "retroactive_check": "corrective: the failing runs kept polishing and "
                             "would have stopped; preservative: the passing "
                             "runs already finish cleanly and are not slowed; "
                             "transfer: any task benefits from a bounded stop.",
        "regression_risk": "A stop made too eager could end runs early; the "
                           "rule asks for one check first.",
        "rep_rule_index": n_rule,
    }


def _skill_edit(edit_id: str) -> dict:
    return {
        "id": edit_id,
        "component": "skill",
        "hypothesis": "A skill that schedules one bounded check before "
                      "finishing gives the run an explicit trigger.",
        "targets_mode": "no_bounded_stop",
        "why_not_lower_lever": "The skill body runs a procedure, not advice.",
        "trigger_condition": "when a change is finished",
        "predicted_affected": ["t1"],
        "retroactive_check": "corrective: failing runs lacked the trigger; "
                             "preservative: passing runs keep their one pass; "
                             "transfer: generalizes to any task.",
        "regression_risk": "None expected; the skill is read on demand.",
    }


def _proposer_action(prompt: str) -> str:
    """Ship exactly one change, then done(). The interaction log tells which
    turn this is: edit/write first, done second. A fresh log means the
    candidate just started (or a repair round began with the edits already in
    the tree, which the dump then already contains)."""
    in_repair = "REPAIR ROUND:" in prompt
    reserved = "RESERVED EXPLORATION SLOT" in prompt
    n_rules = _harness_rule_count(prompt)
    if in_repair:
        # The previous edits are already in the working tree and done() needs
        # at least one file change this candidate: make the minimal repair the
        # objections require (re-affirm the rule the harness already carries),
        # then done() on the next turn.
        if '"action": "edit_file"' not in prompt:
            return _out({"action": "edit_file", "path": "CLAUDE.md",
                         "old": "Working style:",
                         "new": "Working style:\n\n- Finish the change, verify "
                                "once, then stop; fix only what review asked."})
        return _out({"action": "done", "summary": "bounded-stop standing rule",
                     "edits": [_repair_edit()]})
    # First turn of a candidate. Did an earlier variant of the run already use
    # the skill slot? Only propose a skill when the slot text asks for one.
    if reserved and "skill" in prompt.split("RESERVED EXPLORATION SLOT", 1)[1] \
            .split("never-exercised", 1)[1].split("component from:", 1)[-1] \
            .split("]", 1)[0]:
        rel, name = _NEXT_SKILL
        return _out({"action": "write_file", "path": f"{rel}/{name}", "content":
                     "---\nname: general-truth-rules\n"
                     "description: Run the bounded truth check before finishing\n---\n"
                     "Check the deliverable against the general rules once, then "
                     "stop.\n"})
    if "edit_file" in prompt or True:
        idx = min(n_rules, len(_RULES) - 1)
        rule = _RULES[idx]
        return _out({"action": "edit_file", "path": "CLAUDE.md",
                     "old": "All code and comments in English.",
                     "new": "All code and comments in English.\n\nGeneral rule:\n"
                            "- " + rule})
    return ""


def _repair_edit() -> dict:
    return {
        "id": "C1",
        "component": "prompt",
        "hypothesis": "A standing bounded-stop rule in the harness guidance "
                      "turns run-away polishing into a single check, which "
                      "should lift the affected trials.",
        "targets_mode": "no_bounded_stop",
        "why_not_lower_lever": "The policy already knows how to check; what is "
                               "missing is WHEN to stop.",
        "trigger_condition": "always (a standing rule read every session)",
        "predicted_affected": ["t1"],
        "retroactive_check": "corrective: the failing runs kept polishing and "
                             "would have stopped; preservative: the passing "
                             "runs already finish cleanly and are not slowed; "
                             "transfer: any task benefits from a bounded stop.",
        "regression_risk": "A stop made too eager could end runs early; the "
                           "rule asks for one check first.",
    }


def _done_edits(prompt: str) -> list:
    """The done() edits array: what the fake shipped this candidate."""
    if "REPAIR ROUND:" in prompt:
        return [_repair_edit()]
    if _wrote_skill(prompt):
        return [_skill_edit("C1")]
    limits = []
    for line in prompt.splitlines():
        if "You may ship AT MOST" in line:
            tok = line.split("AT MOST", 1)[1].strip().split()[0]
            try:
                limits.append(int(tok))
            except ValueError:
                pass
    n_rules = _harness_rule_count(prompt)
    # A reserved candidate ships the skill write (see _proposer_action): its
    # single change is the skill file, not a CLAUDE.md rule.
    if _wrote_skill(prompt):
        return [_skill_edit("C1")]
    budget = min(limits) if limits else 1
    if budget < 1:
        return []
    # The edit_file that appended rule n_rules already happened this candidate;
    # declare exactly it (one change per candidate, within the budget).
    return [_edit("C1", n_rules)]


def _wrote_skill(prompt: str) -> bool:
    return '"action": "write_file"' in prompt.split(
        "=== INTERACTION LOG ===", 1)[-1]


def _proposer_done(prompt: str) -> str:
    return _out({"action": "done", "summary": "bounded-stop standing rule",
                 "edits": _done_edits(prompt)})


# ----------------------------------------------------------------- critic --
_ACCEPT = {"verdict": "accept", "reasons": [], "risk_notes": []}


def _critic_action() -> str:
    n = 0
    p = os.environ.get("FAKE_EVOLVE_CRITIC_REJECTS")
    try:
        n = int(p) if p else 0
    except ValueError:
        n = 0
    # "the first N reviews of the process reject": count review payload calls.
    # Each reviewer turn embeds the payload once; use a process-wide counter.
    global _CRITIC_CALLS
    _CRITIC_CALLS += 1
    if _CRITIC_CALLS <= n:
        reason = os.environ.get("FAKE_EVOLVE_REJECT_REASON",
                                "fake: rejecting the first reviews")
        return _out({"verdict": "reject", "reasons": [reason],
                     "risk_notes": ["fake reject steering"]})
    return _out(_ACCEPT)


_CRITIC_CALLS = 0


# ------------------------------------------------------------------ route --
def generate(prompt: str, system: str | None = None, model=None,
             json_only: bool = False, cache_prefix=None) -> str:
    s = system or ""
    out = ""
    if s.startswith("You are a trajectory digester:"):
        out = _digest_for(s, prompt)
    elif s.startswith("You are the batch analyst"):
        out = _analyst_action(prompt)
    elif s.startswith("You are a harness engineer agent."):
        if "REPAIR ROUND:" in prompt:
            out = _proposer_action(prompt)        # repair edit first, then done
        elif "\n[result] " in prompt and ('"action": "edit_file"' in prompt
                                          or '"action": "write_file"' in prompt):
            out = _proposer_done(prompt)          # second turn: done()
        elif '"action": "done"' in prompt:
            out = _proposer_done(prompt)          # bounced done: retry fixed
        elif "\n[result] " in prompt:
            out = _proposer_done(prompt)          # some other tool result: done
        else:
            out = _proposer_action(prompt)
    elif s.startswith("You are a strict reviewer"):
        out = _critic_action()
    elif s.startswith("You are the policy agent driving an LLM-agent harness") \
            or "FAKE-POLICY-ROLE" in s:
        out = "Done."
    else:
        out = _out({"action": "done", "edits": []})
    _trace(s, prompt, out)
    return out
