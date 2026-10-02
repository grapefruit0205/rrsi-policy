# evolve-demo

A tiny end-to-end task suite for the `claudecode` domain of
[rrsi-evolve](../../lib/rrsi_evolve/) — a faithful port of
[google-research/rrsi](https://github.com/google-research/rrsi)
(the paper's harness-evolution loop) to Claude Code harnesses. This directory
is both an example and a working starting point: copy it into any git
repository, adjust the tasks, and run the loop.

The evolved "harness" is a Claude Code project configuration (`harness/`:
CLAUDE.md plus anything else you add — `.claude/skills`,
`.claude/agents`, `.claude/commands`, `.claude/settings.json` with hooks,
`.mcp.json`, helper scripts). The frozen policy is **Claude Code headless
(`claude -p`)**: every trial is one headless session in a fresh workspace
with the candidate harness installed as its project config.

## Purpose

- Show the task format the `claudecode` domain reads (`tasks/<task_id>/...`)
  with 5 realistic, haiku-sized tasks: 4 in the `evolve` split, 1 in the
  `heldout` split.
- Give a neutral starting harness (`harness/CLAUDE.md`) that the loop can
  improve from nothing.
- Let you run `smoke`, `baseline`, `round`, `run`, and `status` on a suite
  that is cheap enough to watch end-to-end.

## Layout

```
evolve-demo/
  rrsi.json               run configuration (T=4, k=2, m=2, delta=0.125, ...)
  harness/CLAUDE.md       the initial (neutral) evolved harness
  tasks/
    hello-file/           evolve  — create notes/todo.txt with exact contents
    fix-off-by-one/       evolve  — fix a buggy sum.py (hidden tests), write FIXED in a strict format
    csv-filter/           evolve  — filter.sh must match an exact row set (edge cases)
    median-bug/           evolve  — fix median() for even lists; hidden property tests
    reverse-words/        heldout — reverse words per line (whitespace collapse, exec bit)
  README.md               this file
```

## Task format

Each task lives in its own directory under `tasks/`:

```
tasks/<task_id>/
  task.json     required: {"prompt": "...", "split": "evolve"|"heldout",
                "timeout_s": 300, "weight": 1}
                ("prompt_file": "prompt.md" may replace "prompt";
                split defaults to "evolve")
  workspace/    optional fixture, copied into each trial workspace
  setup.sh      optional, run in the workspace before the agent
  check.sh      required hidden checker, run in the workspace after the agent
```

A checker (`check.sh`) runs with `cwd` = the trial workspace and
`RRSI_WORKSPACE` set to it, and must print its reward on the **last
non-empty stdout line**: a float, or a JSON object with `"reward"` (and
optional `"weight"`, which then replaces the task weight for that trial; a
trial lost to an infrastructure failure counts 0 at the weight the task's
measured trials carry);
otherwise the exit code is used (`0` -> 1.0). A reward line that starts with
`{` but does not parse, or a non-finite number, scores 0. The runner clamps
the reward to `[0, 1]`. Build the line with `json.dumps` (as these checkers
do) so agent output quoted in a failure detail cannot break it. Checkers
must use only bash + python3 stdlib, finish well under their timeout, and
never depend on files outside the workspace; whatever a checker starts is
killed with it.

A fixture may carry its own `CLAUDE.md`, `AGENTS.md`, `CLAUDE.local.md`,
`.gitignore`, `.claude/settings*.json` or `.mcp.json`: the harness's copy is
appended (text) or deep-merged (JSON) into it, so both apply. Any other file
the harness and a fixture both have fails the smoke gate (a harness may not
replace task files). The weight a checker reports is remembered per task in
`<runs>/checker_weights.json`, so a task whose trials are all lost still
counts at its measured weight.

Isolation: on Linux with bubblewrap (`sandbox: "auto"`, the default) each
trial runs in a bwrap namespace that hides this repo, the runs dir, `/tmp`,
`/run` (and every other host unix socket) and other trials, and kills every
process the agent started. The host is read-only there. `$HOME` is an
empty, writable tmpfs per trial with the toolchain dirs (`"sandbox_home"`
adds to the defaults) and `PATH` dirs bound back read-only, and
`"sandbox_rw": ["/path", ...]` binds host paths writable. The trial has no
network but an allowlist proxy to Claude Code's API hosts (and your model
gateway): a task that installs packages needs `"sandbox_net_allow":
["pypi.org", "files.pythonhosted.org"]` (or `"sandbox_network": "host"`).
Claude Code runs on a fresh private config dir per trial: your `~/.claude`
and `~/.claude.json` are not there and only the credentials file is bound
in.
Without bubblewrap the policy's tools run on the host with your permissions
and your real `~/.claude`: it gets no pointer to the repo, but an agent that
goes looking can still find the task suite (the run warns). RRSI ran trials
in containers. Set `"sandbox": "bwrap"` to require the sandbox,
`"sandbox": "none"` to run without it, or `policy_wrapper` to use another
one (firejail, a container; `{ws}` is replaced by the workspace path).
`"policy_effort": "low"|"medium"|"high"|"xhigh"|"max"` pins the policy's
reasoning effort (default: the model's own). Checkers also get
`RRSI_PRE_MANIFEST`, a file listing what the workspace held before the
agent ran (`hello-file` uses it to fail stray files).

Each task here has at least one strict detail that a careless run fails on:
exact output format (`sum=31`), an edge case baked into the fixture
(`westcoast`/`northwest` rows, a zero-units row that must be kept), a file
that must exist (`FIXED`, `notes/todo.txt`), hidden property tests in
`median-bug`, and required file permissions (executable scripts).

## Set up a fresh git repo

The loop evolves git commits: the incumbent harness is the tip of an
`evolve/<domain>` branch and every candidate is a commit. Start from this
directory inside a fresh repository:

```bash
mkdir my-harness-run && cd my-harness-run
git init
# copy the demo suite in (rrsi.json, harness/, tasks/, README.md)
cp -r /path/to/rrsi-policy/examples/evolve-demo/{rrsi.json,harness,tasks,README.md} .
git add rrsi.json harness tasks README.md
git commit -m "rrsi: initial harness"
```

`rrsi-evolve` reads `rrsi.json` from the repo root (override with
`--config`), finds `harness/` and `tasks/` relative to it, and writes all
runs state under `.rrsi/` (excluded locally via `.git/info/exclude`; it is
never committed).

## Commands

`rrsi-evolve` is the shim at `rrsi-policy/bin/rrsi-evolve`
(`PLUGIN/lib/rrsi_evolve/cli.py`). Run it from inside the repo:

```bash
rrsi-evolve smoke                 # liveness: parse every *.json under harness/,
                                  # check frontmatter, then run smoke tasks once
rrsi-evolve baseline              # evaluate H_0 (4 evolve tasks x k=2) and seed
                                  # the frontier; estimates delta (delta_z x sd
                                  # of the null score difference) when delta is null
                                  # (this suite fixes delta=0.125, one trial of 8)
rrsi-evolve round --t 0           # one full round (Algorithm 1 + Algorithm 2):
                                  # analyst, m=2 worktree candidates, critic screen,
                                  # evaluate survivors, keep the best admissible one
rrsi-evolve round --t 0 --dry-run # analysis only: stops before proposing
rrsi-evolve run                   # rounds 0..T-1 in sequence (driver)
rrsi-evolve status                # frontier, b_t schedule, delta, history
rrsi-evolve heldout --label champ # evaluate the incumbent on the heldout task
                                  # (reverse-words) after evolution finishes
```

Useful options: `--repo <path>` and `--config <path>` point at another
repository or config; `--runs <dir>` relocates the state directory; any
hyperparameter in `rrsi.json` (`--T`, `--k`, `--m`, `--delta`, ...) can be
overridden on the command line. Model names are the aliases the LLM layer
understands: `haiku`, `sonnet`, `opus`.

Environment overrides for testing without the real CLI:
`RRSI_EVOLVE_POLICY_BIN` replaces the `claude` binary used by the trial
policy, and `RRSI_EVOLVE_LLM=fake:<path.py>` replaces the search-role LLM.

## Cost warning

Every evaluation is `|evolve tasks| x k` headless Claude Code sessions. With
this suite's defaults that is **4 x 2 = 8 `claude -p` policy sessions per
evaluation** (measured on haiku: about $0.17 and 35 s for the baseline).

The search roles are multi-turn loops, and with the default `claude-cli`
backend every turn is a separate `claude -p` call that re-sends the
constitution and the harness. Per round, up to:

- 1 analyst session (up to 30 calls), plus up to 8 parallel digesters per
  analyst request (up to 15 calls each);
- m x (1 + repair_rounds) proposer sessions (up to 40 calls each);
- up to m x (1 + repair_rounds) critic calls;
- m smoke runs (`|smoke tasks|` sessions each) and m evaluations
  (8 sessions each).

Measured: one round on this suite with haiku in every role (m=1,
repair_rounds=1) made 116 role calls (105 of them digesters) for $1.74 in
about 20 minutes. All tasks passed at baseline, so the proposer read the
traces and stopped without a proposal and no candidate was evaluated. Sonnet
roles cost several times more, and a round with candidates adds their smoke
runs and evaluations (8 policy sessions each). A full `run` (T=4) is several
hundred role calls. Start with `smoke` and `baseline`, run `round --t 0
--dry-run` before spending on a proposal, set `max_budget_usd_per_trial` to
cap each trial, and set `RRSI_EVOLVE_LLM_LOG=<file>` to log every role call
with its cost (each round also logs the accumulated role spend). Do not
point this at a repository you care about without reading what the loop
commits.
