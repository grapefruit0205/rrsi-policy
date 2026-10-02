# Pattern Library (reference, not an allowlist)

Known mechanism families for Claude Code harnesses, with concrete format
examples and generic engineering traps. You may adopt, adapt, ignore, or
invent formats not listed here.

## 1. Stop gates (bounded verification before finishing)

`Stop` fires when Claude finishes responding; a hook there can force one
more pass. Format (in `.claude/settings.json`):

```json
{
  "hooks": {
    "Stop": [{
      "hooks": [{
        "type": "command",
        "command": "bash \"${CLAUDE_PROJECT_DIR}\"/.claude/hooks/stop-gate.sh",
        "timeout": 15
      }]
    }]
  }
}
```

The hook reads one JSON line on stdin (`stop_hook_active`, cwd,
`last_assistant_message`); it returns
`{"decision": "block", "reason": "..."}` to force a continuation (exit
code 2 does the same) and exits 0 to let the run finish. SessionStart
hooks can also inject state at turn start via
`hookSpecificOutput.additionalContext`. Call the script through `bash`
(files the harness writes are not executable unless they start with `#!`);
a hook whose script cannot run is a non-blocking error and the gate
silently never fires. Trials do not persist the session, so
`transcript_path` points at a missing file: decide from the stdin fields.

Generic trap: an unbounded Stop gate loop-kills the run — the hook receives
`stop_hook_active` (true once a Stop hook has already blocked this turn)
and MUST stand down when it is set; count your own fires as a second
guard, in a per-session file under the workspace
(`.claude/state/<session_id>`), not in the job-wide state dir. "Check your work carefully" is a wish and washes out; a bounded,
mechanical gate ("at most one targeted verification pass, and only when X")
is a mechanism.

## 2. Permission plumbing (what the evaluator already grants)

`--permission-mode`, the tool set and the pre-approved tools are injected
externally: the file tools and Bash, `Skill`, `Task`, and the tools of
every MCP server in the harness `.mcp.json` are pre-approved. In a trial a
permission request is denied at once and the agent gets an error, so an
unapproved call costs a turn, not the trial. What the harness can usefully
add is a NARROWING rule:

```json
{
  "permissions": {
    "deny": [
      "Edit(./.claude/settings.json)"
    ]
  }
}
```

Generic trap: `permissions.allow` rules and `additionalDirectories` in
`.claude/settings.json` are not applied in a trial (the fresh workspace is
untrusted; Claude Code only prints a warning), and `defaultMode` from
project settings never overrides the injected `--permission-mode`. An
allow-rule edit is a no-op, and widening edits (`defaultMode`,
`bypassPermissions`, `additionalDirectories`) are auto-rejected.

## 3. Executable skills (backed, not advisory)

Format: `.claude/skills/<name>/SKILL.md` with frontmatter `name` (must
match the directory) and `description` (when to invoke); body = the
procedure; plus a helper script the skill tells the model to run.

```
---
name: verify-before-finish
description: Run the mechanical self-check before declaring a change done
---
Run `bash .claude/skills/verify-before-finish/verify.sh` and read its
verdict before finishing. If it reports anything, fix it first.
```

Skills are progressive disclosure by construction: only the `name` +
`description` load every session; the body loads on invocation; supporting
files load only when read. Generic trap: a purely advisory skill is dead
weight — if the body only says "remember to X", it tends to wash out; a
skill whose procedure ends in a runnable check does the work itself. The
helper script and the skill are ONE edit (dependent parts). Reference
the helper by workspace-relative path (the policy's cwd is the workspace
root; its Bash tool has no `CLAUDE_PROJECT_DIR`), and leave `allowed-tools`
out: Skill calls and Bash are already pre-approved.

## 4. Per-job memory (append-only lessons in $RRSI_HARNESS_STATE_DIR)

The evaluator exports `RRSI_HARNESS_STATE_DIR` (shared by all trials of a
job). Format: one JSON record per lesson, filtered on read.

```json
{"lesson": "when a build fails, run the failing target alone before re-running the suite", "evidence": "full-suite re-runs burned the budget"}
```

Iron rule: a write path without a read path that changes behavior is dead
storage. Reads happen through a SessionStart hook (or an explicit step the
CLAUDE.md mandates); filter by trigger, never dump the whole file into
context. Keep records entity-free: task-specific runtime data (contents,
outputs, answers, names) is memorization and the critic rejects it.

## 5. Context-budget mechanisms (what survives a long run)

Examples: a `PreCompact` hook that records an explicit state block before
context is summarized; a `SessionStart` hook that re-injects the state
block; a `PostToolUse` hook that replaces a noisy tool result with a
trimmed one (`hookSpecificOutput.updatedToolOutput`, in the tool's output
shape; `additionalContext` only adds text next to it); CLAUDE.md rules
that keep a discovered-facts list updated. Note: compression-path changes
affect every long task, so test their effect broadly.

Generic trap: "context collapse" — a summary of a summary loses the task;
always regenerate the state block from pinned originals, not from the
previous summary. And check the traces for what actually entered context:
truncated-away numbers look identical to never-computed ones.

## 6. Bounded extra model calls (subagents and forks)

Format: `.claude/agents/<name>.md` with frontmatter `name`,
`description`, optional `tools` (no `model`: the evaluator pins every
subagent to the frozen policy model, and a model override is rejected);
body = that worker's system prompt. The worker
runs in its own fresh context window, invoked when the description matches
or explicitly by name; `context: fork` on a skill runs its body as an
isolated subagent.

Cost note: every subagent adds a fresh context and separate tokens on the
same frozen policy; keep briefs tight and outputs short ("return ONLY the
verdict per requirement"). Never delegate the whole task or open-ended
reasoning. PRIOR: sub-calls tend to hurt on small policies (early exit,
added latency without payoff); this policy is strong, which makes this less
certain, but it is still a cost the gain has to cover.
