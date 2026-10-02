# Part of rrsi-policy. Ports google-research/rrsi (Apache-2.0).
"""Domain paragraphs for the three search roles on Claude Code harnesses.

`{policy}` is replaced by the run's `policy_label` before use. The trace
format referenced here is the one render.py emits.
"""

ANALYST = """The agent is Claude Code headless (`claude -p`): a frozen policy LLM
({policy}) runs in a fresh workspace per trial under a candidate project
configuration (CLAUDE.md, .claude/skills, .claude/agents, .claude/settings.json
with hooks, .mcp.json) installed from the candidate harness. It solves small
coding tasks by reading and editing files and running commands; a task counts
as solved only if its hidden checker (check.sh) passes after the agent stops.
There is one rendered trace per task (a failing trial where one exists); the
VERIFIER (ground truth) section at the end of each trace shows what the
checker demanded and what it reported. Near-miss tasks that failed the
checker are high-leverage. Where a failure digest hints the agent was blocked
by harness mechanics (no skill for the recurring situation, a missing or
over-broad permission, context lost before it mattered, no subagent for a
slow side quest, an unbounded Stop hook burning turns) rather than by
judgment, use the capability_gap lens."""

DIGESTER = """The trajectory comes from Claude Code headless: a frozen policy LLM
({policy}) solving a small coding task in a fresh workspace under a candidate
project configuration. Each <task_id>.txt holds the task prompt, every step
(ASSISTANT analysis, TOOL_USE with its JSON input, TOOL_RESULT with the tool's
output), run metadata (reward, turns, tokens, cost, timed_out, subtype) and
at the end the VERIFIER (ground truth): what the hidden checker demanded and
what it printed. Read the VERIFIER section first (what actually failed), then
grep for anchors (step numbers, tool names, error strings)."""

PROPOSER = """The harness is a Claude Code project configuration: CLAUDE.md,
.claude/skills/<name>/SKILL.md files, .claude/agents/<name>.md subagents,
.claude/commands, .claude/settings.json (permissions, env, hooks) and
.mcp.json servers, installed into a fresh workspace per trial before the
policy starts. The frozen policy is {policy}, run headless as
`claude -p` with tasks' prompts on stdin; it reads and edits files and runs
commands until it stops, and the task counts as solved only when its hidden
checker (check.sh, never shown to the agent) then passes. The model,
permission mode, tool set, pre-approved tools and timeouts are injected
externally by the evolution loop; every subagent is pinned to the frozen
policy model. What you can shape is the project configuration the policy
starts with. Do not assume the
policy shares your habits or judgment; read the trajectories for how it
actually behaves. The project configuration may compute and transform text
freely (skill bodies, hook commands), but the policy only ever sees what the
configuration injects into its context.

Out of bounds, enforced before measurement: reading, detecting or referencing
check.sh (do not name a harness file check.sh either), the tasks directory or
evaluation paths (rrsi-ws-, rrsi-run-, RRSI_WORKSPACE, RRSI_TRIAL_TOKEN) at
runtime, or any trial artifact (result.json, stream.jsonl, verifier.txt);
benchmark task names or task-specific file names, values or triggers anywhere
in code, prompts or state; changing the frozen policy by any route: `model:`
or `effort:` frontmatter other than `model: inherit`, settings keys for the
model, effort, thinking, advisor, fallback models, fast mode, plugins or
auto-memory (model, effortLevel, alwaysThinkingEnabled, advisorModel,
fallbackModel, enabledPlugins, autoMemoryEnabled, ...), a hook's "model"
field, env variables for the model, provider, effort or thinking
(ANTHROPIC_*, CLAUDE_CODE_SUBAGENT_MODEL, CLAUDE_CODE_EFFORT_LEVEL,
MAX_THINKING_TOKENS, CLAUDE_CODE_DISABLE_THINKING, PATH, ...), or calling a
model outside the policy (the claude command, any model API or SDK:
Anthropic, OpenAI, Gemini, OpenRouter, Bedrock) from a script, hook or MCP
server; keeping state anywhere but the workspace and $RRSI_HARNESS_STATE_DIR
(never reassign it; no paths under $HOME or ~ in scripts); reading Claude
Code's own data (~/.claude, transcripts, history, credentials); leaving the
trial's sandbox (systemd-run, D-Bus, /proc/<pid>/root); widening permissions
(defaultMode, bypassPermissions, additionalDirectories, dangerously*
settings); binary files and .gitattributes. The run's real outcome is judged on heldout
tasks the search never sees, so memorized task knowledge is worthless. Litmus
test for every edit: would this help a competent operator working on MANY
unfamiliar coding tasks with this project configuration?"""

CRITIC = """The harness is a Claude Code project configuration (CLAUDE.md,
.claude/skills, .claude/agents, .claude/settings.json with hooks, .mcp.json)
installed into a fresh workspace per trial; the frozen policy runs headless
({policy}) and solves coding tasks; a task is solved when its hidden checker
passes. General harness craft is fine ("add a skill that documents the
output-checking routine" = OK; "a PreToolUse hook that keeps Edit calls
inside the workspace" = OK); task knowledge is not ("if the prompt mentions
hello-file, write hello.txt with content hi" = REJECT). Out of bounds, REJECT
on sight: reading, detecting or referencing check.sh, the tasks directory,
evaluation paths (rrsi-ws-, rrsi-run-, RRSI_WORKSPACE, RRSI_TRIAL_TOKEN) or
trial artifacts (result.json, stream.jsonl, verifier.txt), including indirect
routes such as walking up from $RRSI_HARNESS_STATE_DIR, reading parent
processes (/proc) or globbing the filesystem for them; changing the frozen
policy by any route (`model:` or `effort:` frontmatter on agents, skills or
commands other than `model: inherit`; settings keys for the model, effort,
thinking, advisor, fallback models, fast mode, plugins or auto-memory; a
hook's "model" field; env variables for the model, provider, effort or
thinking such as ANTHROPIC_*, CLAUDE_CODE_SUBAGENT_MODEL,
CLAUDE_CODE_EFFORT_LEVEL, MAX_THINKING_TOKENS; a script, hook or MCP server
that calls the claude command or any model API or SDK, directly, through a
variable, an absolute path or npx): a stronger or longer-thinking model is
not a harness gain; state kept outside the workspace and
$RRSI_HARNESS_STATE_DIR (fixed paths in $HOME or /tmp, CLAUDE.md or AGENTS.md
files above the workspace), which leaks across candidates; reading Claude
Code's own data (~/.claude, session transcripts, history, credentials);
ways out of the trial's sandbox (systemd-run, busctl, dbus-send, nsenter,
another process's /proc/<pid>/root or environ); widening permissions
(defaultMode, bypassPermissions, additionalDirectories, dangerously*
settings, broad allow rules). The permission mode, tool set
and timeouts themselves are injected by the evaluator. Existing safety
mechanisms that must not be
disabled without a working replacement: permissions that keep edits inside
the workspace, hooks that bound the agent's stopping behavior, and output or
error handling that keeps the policy's context readable. A Stop hook may
block stopping ({"decision": "block"}) but must be bounded (it receives
stop_hook_active and must give up on the second stop), never unbounded."""
