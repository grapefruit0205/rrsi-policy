# rrsi-policy

**English** · [한국어](README.ko.md)

**Stop guessing whether your CLAUDE.md edit helped. Measure it.**

You add a rule to CLAUDE.md, a skill or a hook, and you can't tell whether
things got better. A rule that helps one task can quietly break another.

`rrsi-evolve` runs a task suite built from your own work through Claude Code
and scores it. It keeps a harness change only when the score rises clearly
above noise and the added cost is worth it, and it drafts the changes itself.
It is an unofficial port of the harness-improvement loop from Google
Research's [RRSI](https://github.com/google-research/rrsi), not a Google
product.

- **Chosen by measurement.** Scores before and after a change are compared,
  and a gain inside the noise band is not accepted.
- **Hard to game.** Trials run sandboxed and can't see the grader, and a
  trial that switched to a stronger model scores 0.
- **Everything is kept.** Each candidate leaves a git commit, a diff and its
  trial transcripts, so you can see why it won or lost.

**Don't know what to test?** [`rrsi-evolve mine`](#find-tasks-in-your-transcripts-mine)
reads your local Claude Code transcripts and lists what you corrected most
often ("I said only check it", "don't guess, verify", "answer in Korean"),
with a task idea for each. No model is called and nothing leaves your machine.

**Good fit:** a person or team that reuses one harness for similar work, on
Linux.
**Not yet:** if you edit your harness only now and then. On macOS trials run
without the sandbox, so scores are easier to game. Native Windows does not
work; use WSL2.

**Models:** every model setting defaults to `inherit`, the model you use.
Measurements and verdicts are only about the model they ran on, so by default
trials, search roles and the critic all run on it (see
[Which model](#which-model)).

**Cost:** measured on haiku, the demo's baseline (8 trials) was $0.17 and one
round $1.74. On your own model (opus, for most people) expect several times
more ([cost warning](examples/evolve-demo/README.md#cost-warning)).

---

Two tools that bring [google-research/rrsi](https://github.com/google-research/rrsi)
to Claude Code harnesses (CLAUDE.md, skills, agents, commands, hooks, settings,
MCP config, memory):

- **`rrsi-policy`**, a runtime policy engine. Right **before** a harness file
  changes, a separate `claude -p` critic screens the change for overfitting,
  no-op edits, unbounded loops and attempts to bypass the policy. Verdicts and
  measurements go into a ledger and feed the next verdict.
- **`rrsi-evolve`**, a port of RRSI's whole search loop (Algorithm 1 and 2).
  It evolves a Claude Code project configuration against your own task suite,
  with headless `claude -p` as the frozen policy and as the search roles. See
  [rrsi-evolve](#rrsi-evolve-the-full-rrsi-loop).

[Relation to RRSI](#relation-to-rrsi) lists what matches and what doesn't.

Requires Python 3.10+ (standard library only) and the `claude` CLI.

## Why use rrsi-policy

- **It is enforced.** A rule written in a skill or CLAUDE.md is advice the model
  can ignore. A PreToolUse hook runs before the edit reaches the disk, so it
  cannot be skipped.
- **The critic is independent.** It runs in its own process with a fresh
  context. By default it judges with the model the session runs on (read
  from the session transcript; set `"model"` in the policy config to pin
  one). It sees only the diff, not the editing agent's reasoning. Its output is constrained by `--json-schema`.
- **The LLM does not pick the policy.** The policy is selected deterministically
  from argv, the tool name, path globs or command regexes, so text inside a diff
  cannot route itself to a lenient policy.
- **It is cheap.** Only harness-file edits reach the LLM; other writes get a
  secret-regex check. With MCP isolated, a verdict costs about $0.01 on haiku
  (measured; $0.18 before isolation).
- **It decides from measurements, not impressions.** An eval command measures
  ΔS. Gains inside the noise band are not accepted, added tokens must be paid
  for by measured gain, and a rejected bundle can be restored from snapshots.
- **It remembers failures.** Critic rejections and the intents of edits that
  failed measurement are passed to the next critic call, so the same attempt is
  caught again.
- **Its defaults are safe.**
  - It never emits `allow`, so it cannot bypass the user's permission prompts.
  - Errors and uncertain verdicts become `ask`. Repeated rejections of the same
    file are handed to the user.
  - Edits to the engine's own config and ledger always `ask`.
  - A recursion guard is set, and the engine is disabled inside eval runs.
- **It works outside hooks.** `check` returns exit codes (0/2/3) for git hooks
  and CI.

## How it works

```
PreToolUse ─▶ route (deterministic: argv --policy > tool / path glob / command regex)
               │
               ├─ fixed   always ask (protects the engine's own config and ledger)
               ├─ regex   secret patterns → deny (no LLM, added lines only)
               └─ llm     precheck → edit budget → claude -p critic (--json-schema)
                            accept → no output (normal permission flow applies)
                            reject → deny + reasons (the agent revises and retries)
                            uncertain / error / max_repairs consecutive rejects → ask
PostToolUse ─▶ record only edits that actually landed (+ pre-edit snapshot)
```

The child `claude -p` runs with:

| Setting | Why |
|---|---|
| `RRSI_POLICY_ACTIVE=1` | recursion guard |
| `--tools ""` | no tool use |
| neutral cwd | does not load the CLAUDE.md under review |
| `--strict-mcp-config`, `--setting-sources project` | isolates MCP servers and user plugins |

## Install

In Claude Code:

```
/plugin marketplace add grapefruit0205/rrsi-policy
/plugin install rrsi-policy@rrsi-policy
```

Or clone it and load it for one session:

```bash
git clone https://github.com/grapefruit0205/rrsi-policy
claude --plugin-dir ./rrsi-policy
```

`rrsi-evolve` is a plain command: run `rrsi-policy/bin/rrsi-evolve` from the
clone (or put `bin/` on your PATH). To customize, copy
`policies/policies.json` to `~/.config/rrsi-policy/policies.json` and edit it.
Config inside the project is not read, because the agent can edit it.

## Measurement loop (ΔS)

The critic can only judge whether a change looks right. Whether it helped is
measured with an eval command, whose last stdout line is a number or
`{"S": 0.82, "C": 15300}` (C, tokens per run, is optional).

```bash
rrsi-policy baseline --cmd "./evals/run.sh" --k 3     # current score + noise band δ = 2·sd·√(2/k)
# ... edit the harness in Claude Code (critic-approved edits form a pending bundle)
rrsi-policy status                                    # incumbent, S*, δ, pending edits, yield, prune set, stall
rrsi-policy measure --cmd "./evals/run.sh" --k 3      # judge the bundle: ACCEPTED(0) / REJECTED(1)
rrsi-policy revert                                    # restore the files of a rejected bundle
```

The rule (RRSI Algorithm 2):
- Reject if S' is below S* − δ (S* is the best score seen so far).
- If ΔS > δ, accept only if token growth ≤ β0 + β1·ΔS.
- If ΔS is within δ, accept only for a token saving or a structural component
  (skill, agent, hook, mcp, memory) the incumbent has never had.

Measurements flow back into runtime verdicts:
- Intents of edits rejected by measurement appear under RECENT REJECTIONS in the
  critic's input.
- The prune set (components with no recent gain), the stall flag and the untried
  components are passed to the critic.
- With a baseline in place, `ask` is raised once `max_pending` (default 3)
  unmeasured edits have piled up.
- Eval runs get `RRSI_POLICY_ACTIVE=1`, so Claude sessions inside the eval do
  not trigger the engine.

Without a baseline, measurement is off and only the critic runs.

## rrsi-evolve: the full RRSI loop

`rrsi-policy` gates edits that someone else proposes. `bin/rrsi-evolve` runs
the search itself. One round:

1. Evaluate the incumbent (the tip of the `evolve/claudecode` branch) on the
   task suite, k trials per task. Each trial is one `claude -p` session in a
   fresh temp workspace with the harness installed as its project config; a
   hidden `check.sh` scores it afterwards.
2. Digesters and the analyst read failing and passing traces and write the
   three-lens report.
3. m proposers draft candidates, each in its own git worktree, under the
   annealed edit budget b_t. Their prompt carries the history ledger, the
   prune set, the stall flag and reserved exploration slots, and each must
   finish with `done()`: declared edits, component tags, predictions and a
   retroactive check.
4. The critic screens each draft (deterministic precheck, then LLM review,
   with bounded repair rounds), and a smoke run checks it loads.
5. Survivors are evaluated. Algorithm 2 keeps the best admissible one and
   fast-forwards the branch; everything is recorded for the next round.

```bash
cd your-repo                                   # a git repository
/path/to/rrsi-policy/bin/rrsi-evolve init      # rrsi.json, harness/CLAUDE.md, two example tasks
git add -A && git commit -m "rrsi: initial harness"
rrsi-evolve smoke                              # does the harness load and run
rrsi-evolve baseline                           # evaluate H_0, seed the frontier
rrsi-evolve round --t 0 --dry-run              # analysis only
rrsi-evolve run                                # rounds until T (a STOP file stops it)
rrsi-evolve heldout --label champ              # the incumbent on the heldout split
rrsi-evolve mine                               # task ideas from your transcripts (local only)
```

[examples/evolve-demo](examples/evolve-demo/) is a five-task suite with the
task format, the hidden-checker conventions and a cost breakdown.
`readjudicate`, `reevaluate`, `calibrate` and `status` work as in RRSI.

**What is RRSI's code.** The selection, history, components, evaluation,
calibration, critic, digester and analyst modules are RRSI's, unchanged apart
from comments. Schedule, git plumbing, proposer, loop, driver and CLI carry
small, listed changes (paths relative to your repo, a model-agnostic LLM
transport, the endpoint of the b_t schedule).

**What is new for Claude Code.**
- The LLM transport runs the roles as `claude -p` with no tools (or the
  Anthropic API) instead of Vertex.
- The `claudecode` domain installs the harness into each trial workspace,
  runs the policy, reads its stream-json trace, and scores it with the
  task's checker. C is the session's total tokens, subagents included.
- The proposer constitution (SKILL.md, PATTERNS.md) keeps RRSI's structure
  and rules, rewritten for Claude Code mechanics (hooks, skills, subagents,
  settings, MCP, and what does and doesn't apply in a headless trial).
- The policy is frozen for real. The child environment is scrubbed, so a
  parent Claude Code session's settings don't leak in. The tool set is
  fixed and subagents are pinned to the policy model. The pins go in through
  `--settings`, which outranks the harness's own settings files (measured on
  2.1.280). The precheck and the smoke gate reject harness edits that change
  the model, effort, thinking, advisor, fallback models, plugins,
  auto-memory, provider or permissions, by settings key, env variable or
  frontmatter, and harness scripts that call `claude`, a model API or SDK,
  or write under `$HOME`. A nested `claude` call from a trial fails. Set
  `policy_effort` to pin the policy's reasoning effort as well.

**Limits.**
- RRSI ran trials in containers. Here, with `sandbox: "auto"` (the default;
  also `RRSI_EVOLVE_SANDBOX`) on Linux with bubblewrap, each trial runs in
  its own bwrap PID, IPC, mount and network namespace. The repo, the runs
  dir, the shared temp dir and other trials are hidden, and every process
  the agent started dies with the trial. The rest of the host is read-only.
  `/run` and the user runtime dir are empty, and every other unix socket of
  the host is masked, so no host daemon (docker, the system and session
  buses, systemd) is reachable. `$HOME` is a fresh, empty, writable tmpfs
  per trial. Bound back read-only: the toolchain dirs in `sandbox_home`
  (`~/.local/bin`, `~/.nvm`, `~/.cargo/bin`, mise, asdf, pnpm, conda,
  `~/.gitconfig` and the like; `sandbox_home` adds to that list,
  `sandbox_home_only: true` replaces it), every `PATH` dir under `$HOME`,
  the policy's own interpreter, CA bundles named by `NODE_EXTRA_CA_CERTS`,
  `SSL_CERT_FILE` and friends, and with Bedrock, Vertex or Foundry their
  credentials (`~/.aws`, `~/.config/gcloud`, `~/.azure`; refresh an SSO
  login before a run). User-level installs (`pip --user`, `npm -g`) are
  read-only: install into the workspace. `sandbox_rw` lists host paths to
  bind writable (never `/`, `$HOME` or above it, a temp or run dir, or
  Claude Code's own data). Before the first trial the run starts the policy
  (`--version`) in the real layout and stops with the reason if it cannot,
  and a candidate whose harness changed on disk after its commit fails its
  evaluation. The network namespace has no route out: the only way out
  is an allowlist HTTPS proxy that opens CONNECT tunnels to Claude Code's
  API and sign-in hosts on port 443, plus your model gateway
  (`ANTHROPIC_BASE_URL` and the Bedrock, Vertex and Foundry hosts; a gateway
  on localhost is forwarded). If the host itself uses an HTTPS proxy, the
  tunnels go through it. Plain HTTP is not proxied, and git works only over
  HTTPS remotes you allow (no SSH keys or signing keys in a trial). A task
  that needs PyPI, npm or a git host adds them to `sandbox_net_allow`
  (`sandbox_net_ports` for other ports); `sandbox_network: "host"` shares
  the host's network instead, which also shares its abstract sockets (X11).
  The run lists the hosts it refused and why, and the smoke gate fails a
  harness MCP server that does not start in a trial. Claude Code gets a
  fresh private config dir per trial: your `~/.claude` (every session's
  transcripts, your own skills, agents, settings and CLAUDE.md) and
  `~/.claude.json` are not there, and only the credentials file is bound
  in (writable, so a token refresh still reaches it; a trial could also
  overwrite it). A Console API-key login keeps its key. The installed
  `claude` binaries are covered by a stub that refuses to run, and a trial
  whose session used a model other than the policy's (or Claude Code's
  small haiku helper) scores 0.
  A determined agent can still reach a model: the policy binary and the
  credentials must stay usable for the policy to run, and the API host is
  allowed. The precheck and the critic reject harness text that tries.
  Host sockets are masked as they are when a trial starts: one a daemon
  creates later outside `/run`, `/tmp` and `$HOME`, or one bound from
  another network namespace (a container runtime), is not.
- Without bubblewrap (macOS, a kernel without unprivileged user namespaces,
  or `sandbox: "none"`) the policy runs on the host with your permissions,
  your network and your real `~/.claude`. It gets no pointer to the repo,
  instruction files above the workspace are excluded, and stray processes
  are killed by a per-trial environment token, but an agent that searched
  could find the checkers (its process ancestry leads to the repo, and a
  process that cleared its environment escapes the token), and the run
  prints a warning. `policy_wrapper` takes any other sandbox (firejail, a
  container); `sandbox: "bwrap"` makes the sandbox required.
- <a id="which-model"></a>**Which model.** `policy_model`, `proposer_model`,
  `analyst_model`, `critic_model` (rrsi-evolve) and the critic's `"model"`
  (rrsi-policy) default to `"inherit"`: the session's own model (critic
  only), else `RRSI_MODEL`, `ANTHROPIC_MODEL`, the project's
  `.claude/settings.local.json` / `.claude/settings.json`, then
  `~/.claude/settings.json` (`$CLAUDE_CONFIG_DIR`); with none set, `opus`.
  Each evaluating command prints the model and where it came from. A run
  directory remembers the policy model of its first evaluation
  (`.rrsi/runs/<domain>/policy_model.json`) and stops if the model later
  resolves to something else, because scores from two models are not
  comparable: pin `"policy_model"` to the recorded one to continue, or start
  a new `--runs` directory. Set a name (`haiku`, `sonnet`, `opus`, a full id)
  to choose a cheaper model on purpose.
- Trials of one job run in parallel (`concurrency`), and so can several
  jobs (`eval_parallel`); each run has its own private dir, proxy and
  config dir, and the per-job state is saved after every trial.
- It costs real money. Every evaluation is |tasks| × k sessions, and every
  role turn is a `claude -p` call. Measured on the demo: a baseline (8 haiku
  sessions) cost $0.16, and one round with haiku roles made 116 role calls
  for $1.74. Start with `smoke`, `baseline` and `--dry-run`.
- A suite the policy already solves leaves nothing to gain. The demo
  saturates at baseline on haiku (S = 1.0), so a round can only accept a
  token saving or a new structural component. Bring tasks your harness fails.
- With k = 2 and trials that agree, the bootstrap estimate of δ is 0. Fix
  `delta` (the demo uses one trial in eight) or calibrate over several base
  evaluations.

## Find tasks in your transcripts (mine)

A task suite needs tasks your harness fails. If you don't know which, ask
your transcripts:

```bash
rrsi-evolve mine                         # Markdown report on stdout
rrsi-evolve mine --lang ko --out report.md
rrsi-evolve mine --project myapp --days 30 --json
```

It reads `$CLAUDE_CONFIG_DIR/projects` (default `~/.claude/projects`),
finds the messages where you corrected the answer before them, and sorts
them into failure types: did more than asked, guessed instead of checking,
answered in another language, wrong form of explanation, misread the
request, repeated the same failure, the result didn't work. For each type it
prints a count, a few excerpts and a task idea (what to put in the workspace,
what `check.sh` should test). It also counts interrupts and replies written
in another script than the question.

- **Local only.** No model is called and nothing is sent anywhere. It needs
  no repo or `rrsi.json`.
- **Redacted, still private.** Common key and token formats, passwords in
  URLs and `key=value` pairs, emails and your home path are masked, and
  excerpts are cut short. A secret written in an unusual way can still slip
  through, and the excerpts are still your conversations: `--out` writes the
  file with mode 0600 (and refuses a symlink); read it before sharing.
- **Approximate.** Matching is by English and Korean keywords. Messages
  repeated by resumed sessions are counted once, and headless `claude -p`
  sessions (critics, trials, scripts) are skipped unless you pass
  `--include-headless`. Use it to pick candidates, then write each task by
  hand (see [examples/evolve-demo](examples/evolve-demo/)) and set one or two
  aside as `"split": "heldout"`.

## Relation to RRSI

RRSI is an **automatic search loop**: a proposer drafts candidates, they are
evaluated in parallel, and the best one is selected. `rrsi-evolve` is that loop.
`rrsi-policy` is a **gate on edits made by a person or an agent**: the selection
math is ported as is, and the parts that belong to the search loop are missing
or only advisory. The table compares the gate with RRSI; the last column says
where `rrsi-evolve` stands.

| RRSI feature | rrsi-policy | Status | rrsi-evolve |
|---|---|---|---|
| Noise floor S' ≥ S* − δ | `selection.judge` | same formula | RRSI's code |
| Cost rule ΔC ≤ β0 + β1·ΔS, shaped score inside the band | `selection.cost_rule` | same formula; different defaults (β1 = 10, scale-free) | RRSI's code and defaults |
| Novelty ν over structural components | `selection.novelty` | same; K_str = skill, agent, hook, mcp, memory | RRSI's code; Claude Code component tags |
| Per-edit records, a bundle shares one ΔS | `Ledger.measured_edits` | same | RRSI's code |
| Tried T, yield g, prune set B, stall σ, untried U | `Ledger.summary` | same computation; used differently (see below) | RRSI's code, in the proposer prompt |
| Critic (deterministic precheck + LLM review before evaluation) | `harness-critic` policy | rules adapted for Claude Code; adds `policy_tampering` and `redrawn_rejection` | RRSI's code; Claude Code patterns and brief |
| Bounded repair after rejection | deny reasons → agent revises, `max_repairs` | similar; past the limit it goes to the user instead of being dropped | RRSI's code (`repair_rounds`) |
| Noise band δ | `baseline --k` → z·sd·√(2/k) | similar to RRSI's repeated base evaluations; no bootstrap | RRSI's code (bootstrap or repeated evaluations) |
| Prune set and exploration **enforced** on the proposer | passed to the critic as context only | **weaker**; no reserved slots | RRSI's code (reserved slots) |
| Cosine-annealed edit budget b_t | fixed `max_pending` | **different**; counts tool calls, not independent edits | RRSI's code |
| Declared component checked against the diff (`has_evidence`) | path hint + LLM classification | **partial** | RRSI's code |
| m candidates per round, evaluated in parallel, argmax | none (one bundle at a time) | **missing** | RRSI's code |
| Proposer, analyst, digester (failure-trace analysis) | none (Claude Code or the user proposes) | **missing** | RRSI's code; roles run as `claude -p` |
| Worktree isolation per candidate | none (real files + snapshots) | **missing** | RRSI's code |

In short, `rrsi-policy` does not run RRSI as is: RRSI's **selection-side
regularization** (noise floor, cost rule, novelty) and its **critic** work
there, while its **proposal-side regularization** (annealed budget, enforced
exploration, history-conditioned proposals) only reaches the critic as
context. `rrsi-evolve` runs both sides. What differs there is the
environment, not the algorithm: Claude Code sessions in a bwrap sandbox
(or on the host) instead of containers, and a constitution written for
Claude Code.

## Other uses

```bash
git diff --cached -- CLAUDE.md | rrsi-policy check --policy harness-critic --file CLAUDE.md   # 0 pass / 2 deny / 3 ask
rrsi-policy check --policy secrets --command "export TOKEN=..."
rrsi-policy hook --policy harness-critic --dry-run < event.json                            # routing and precheck only, no LLM
```

## rrsi-policy limitations

- Bash edits are caught by regex and can be missed (for example a script that
  writes the file internally). Treat this as a guardrail, not a security boundary.
- Files changed through Bash have no snapshot and cannot be restored by `revert`.
- Each harness edit waits 15–20 seconds for the critic.
- Measurement is only as good as the eval command and k. With k = 1, δ cannot
  be estimated; pass `--delta`.
- The critic is an LLM and can be wrong. A wrong rejection reaches the user after
  `max_repairs`.
- Real `claude -p` verdicts have been checked on only a few cases. Whether the
  plugin improves results in practice has not been measured yet.

## Tests

```bash
python3 -m pytest tests     # fakes stand in for claude -p (tests/fake_*.py)
```

## License

Apache-2.0 ([LICENSE](LICENSE)). Parts are adapted from
[google-research/rrsi](https://github.com/google-research/rrsi) (Copyright
2026 Google LLC, Apache-2.0); [NOTICE](NOTICE) lists them. This is an
independent project, not affiliated with or endorsed by Google.
