# Part of rrsi-policy. Ports google-research/rrsi (Apache-2.0).
"""`rrsi-evolve init [--force]`: scaffold a fresh claudecode domain in a repo.

Writes, without ever overwriting an existing file (unless --force):

  rrsi.json                      claudecode defaults for a first run
  harness/CLAUDE.md              a short neutral starting harness
  tasks/hello-file/task.json     two tiny WORKING examples:
  tasks/hello-file/check.sh        hello-file     write one file with exact content
  tasks/fix-off-by-one/...        fix-off-by-one  fix a seeded off-by-one bug
                                 (checkers are strict: correct solutions pass,
                                  wrong or careless ones fail)

and prints the next steps. `.rrsi/` (the default runs directory) is excluded
locally via .git/info/exclude by the loop itself.
"""

from __future__ import annotations

import json
from pathlib import Path

RRSI_JSON = {
    "T": 4,
    "k": 2,
    "m": 2,
    "b_min": 1,
    "b_max": 3,
    "w": 2,
    "m_draft": 1,
    "delta": None,
    "delta_z": 2.0,
    "beta0": 0.1,
    "beta1": 40,
    "w_s": 100,
    "w_c": 15,
    "w_n": 0.5,
    "n_prune": 3,
    "repair_rounds": 3,
    "n_fail_traces": 6,
    "n_success_traces": 3,
    "eval_parallel": 1,
    "domain": "claudecode",
    "policy_model": "inherit",
    "proposer_model": "inherit",
    "analyst_model": "inherit",
    "critic_model": "inherit",
    "concurrency": 4,
    "task_timeout_s": 300,
}

HARNESS_CLAUDE_MD = """# Working style

- Read the task prompt, make the smallest change that satisfies it, verify
  your work, then stop.
- Do not create extra files beyond what the task asks for.
"""

HELLO_TASK_JSON = {
    "prompt": (
        "Create a file named greeting.txt in the working directory whose "
        "entire content is exactly the single line:\n\n"
        "hello world\n\n"
        "(no extra text, no extra lines, no leading or trailing spaces)."
    ),
    "split": "evolve",
    "timeout_s": 120,
    "weight": 1,
}

HELLO_CHECK_SH = """#!/usr/bin/env bash
# Hidden checker for hello-file: exact single-line content.
set -u
cd "$RRSI_WORKSPACE"
if [ ! -f greeting.txt ]; then
  echo "missing greeting.txt"
  exit 1
fi
content="$(cat greeting.txt)"
expected="hello world"
if [ "$content" != "$expected" ]; then
  echo "greeting.txt content mismatch: got '$content', want '$expected'"
  exit 1
fi
if [ "$(wc -l < greeting.txt)" -gt 1 ]; then
  echo "greeting.txt must contain exactly one line"
  exit 1
fi
echo "1.0"
exit 0
"""

OFF_BY_ONE_TASK_JSON = {
    "prompt": (
        "The file sum.py contains a function total(nums) that sums a list of "
        "numbers but returns the wrong result because of an off-by-one bug in "
        "its loop bound. Fix the bug so total() returns the correct sum. Do "
        "not change the function signature or anything else in the file. "
        "When you are done, sum.py must pass: total([1, 2, 3]) == 6, "
        "total([]) == 0 and total([5]) == 5."
    ),
    "split": "evolve",
    "timeout_s": 180,
    "weight": 1,
}

OFF_BY_ONE_SUM_PY = '''\"\"\"Sum a list of numbers.\"\"\"


def total(nums):
    result = 0
    for i in range(len(nums) - 1):
        result += nums[i]
    return result
'''

OFF_BY_ONE_CHECK_SH = """#!/usr/bin/env bash
# Hidden checker for fix-off-by-one: hidden tests the agent cannot see,
# including an even-length list and a singleton (the classic off-by-one traps).
set -u
cd "$RRSI_WORKSPACE"
if [ ! -f sum.py ]; then
  echo "missing sum.py"
  exit 1
fi
python3 - <<'PY'
import sys
sys.path.insert(0, ".")
from sum import total

cases = [
    ([1, 2, 3], 6),
    ([], 0),
    ([5], 5),
    ([1, 2, 3, 4], 10),
    ([-1, 1], 0),
    ([0.5, 0.5], 1.0),
]
for nums, want in cases:
    got = total(nums)
    if got != want:
        print(f"total({nums!r}) = {got!r}, want {want!r}")
        sys.exit(1)
print("1.0")
PY
exit $?
"""

NEXT_STEPS = """Scaffolded rrsi-evolve files in {repo}:
  rrsi.json            claudecode defaults (T=4, k=2, m=2)
  harness/CLAUDE.md    the starting harness
  tasks/hello-file/    example task: write one exact file
  tasks/fix-off-by-one/  example task: fix a seeded off-by-one bug

Next steps:
  git add rrsi.json harness tasks && git commit -m "rrsi: initial harness"
  rrsi-evolve smoke
  rrsi-evolve baseline
  rrsi-evolve run

Note: the runs directory .rrsi/ is excluded locally (via .git/info/exclude),
so runs, worktrees and traces are not committed.
"""


def init(repo: Path, force: bool = False) -> None:
    repo = Path(repo)
    files = {
        "rrsi.json": json.dumps(RRSI_JSON, indent=1) + "\n",
        "harness/CLAUDE.md": HARNESS_CLAUDE_MD,
        "tasks/hello-file/task.json": json.dumps(HELLO_TASK_JSON, indent=1) + "\n",
        "tasks/hello-file/check.sh": HELLO_CHECK_SH,
        "tasks/fix-off-by-one/task.json": json.dumps(OFF_BY_ONE_TASK_JSON, indent=1) + "\n",
        "tasks/fix-off-by-one/workspace/sum.py": OFF_BY_ONE_SUM_PY,
        "tasks/fix-off-by-one/check.sh": OFF_BY_ONE_CHECK_SH,
    }
    created, skipped = [], []
    for rel, content in files.items():
        p = repo / rel
        if p.exists() and not force:
            skipped.append(rel)
            continue
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
        created.append(rel)
    for rel in ("tasks/hello-file/check.sh", "tasks/fix-off-by-one/check.sh"):
        p = repo / rel
        if p.exists():
            p.chmod(p.stat().st_mode | 0o111)
    if skipped:
        kept = ", ".join(skipped)
        print(f"kept {len(skipped)} existing file(s) (use --force to overwrite): {kept}")
    print(NEXT_STEPS.format(repo=repo).rstrip())
