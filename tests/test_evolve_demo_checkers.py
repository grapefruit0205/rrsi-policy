"""The demo suite's hidden checkers accept correct solutions and reject wrong
ones, including outputs that used to break the reward line. Run:
    uvx --quiet pytest tests/test_evolve_demo_checkers.py -q
"""

import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

PLUGIN = Path(__file__).resolve().parents[1]
DEMO = PLUGIN / "examples" / "evolve-demo"
sys.path.insert(0, str(PLUGIN / "lib"))

from rrsi_evolve.domains.claudecode.adapter import ClaudeCodeDomain  # noqa: E402


def _exe(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def reward(tmp_path: Path, task: str, solve, harness_files=()) -> float:
    ws = tmp_path / "ws"
    shutil.rmtree(ws, ignore_errors=True)
    fixture = DEMO / "tasks" / task / "workspace"
    if fixture.is_dir():
        shutil.copytree(fixture, ws)
    ws.mkdir(exist_ok=True)
    shutil.copy(DEMO / "harness" / "CLAUDE.md", ws / "CLAUDE.md")
    for rel in harness_files:
        (ws / rel).parent.mkdir(parents=True, exist_ok=True)
        (ws / rel).write_text("x\n")
    solve(ws)
    proc = subprocess.run(["bash", str(DEMO / "tasks" / task / "check.sh")], cwd=ws,
                          capture_output=True, text=True, errors="replace",
                          env={**os.environ, "RRSI_WORKSPACE": str(ws)})
    r, _ = ClaudeCodeDomain._parse_reward(proc.stdout, proc.returncode)
    return r


# ---------------------------------------------------------------- csv-filter --
CSV_OK = "#!/usr/bin/env bash\nawk -F, 'NR > 1 && $2 == \"west\"' data/sales.csv\n"


def test_csv_filter(tmp_path):
    assert reward(tmp_path, "csv-filter", lambda ws: _exe(ws / "filter.sh", CSV_OK)) == 1.0
    wrong = '#!/usr/bin/env bash\ngrep west data/sales.csv | sed \'s/,/"\\t/\'\n'
    assert reward(tmp_path, "csv-filter", lambda ws: _exe(ws / "filter.sh", wrong)) == 0.0
    loose = "#!/usr/bin/env bash\ngrep west data/sales.csv\n"
    assert reward(tmp_path, "csv-filter", lambda ws: _exe(ws / "filter.sh", loose)) == 0.0


# ---------------------------------------------------------------- hello-file --
def _todo(text):
    def solve(ws):
        (ws / "notes").mkdir(exist_ok=True)
        (ws / "notes" / "todo.txt").write_text(text)
    return solve


def test_hello_file(tmp_path):
    assert reward(tmp_path, "hello-file", _todo("BUY MILK\n")) == 1.0
    # harness-installed top-level files do not fail a correct run
    assert reward(tmp_path, "hello-file", _todo("BUY MILK\n"),
                  harness_files=("hooks/gate.sh", "AGENTS.md", "tools/x.py")) == 1.0
    assert reward(tmp_path, "hello-file", _todo("buy milk\n")) == 0.0
    assert reward(tmp_path, "hello-file", _todo("BUY MILK")) == 0.0

    def extra(ws):
        _todo("BUY MILK\n")(ws)
        (ws / "notes" / "extra.txt").write_text("a\nb\"c\n")
    assert reward(tmp_path, "hello-file", extra) == 0.0


# ------------------------------------------------------------ fix-off-by-one --
def _fix(body: str, fixed: str = "sum=31\n"):
    def solve(ws):
        src = (ws / "math" / "sum.py").read_text()
        start = src.index("def buggy_sum")
        end = src.index('\n\nif __name__')
        (ws / "math" / "sum.py").write_text(src[:start] + body + src[end:])
        (ws / "FIXED").write_text(fixed)
    return solve


FIX_OK = ("def buggy_sum(xs):\n    # BUG: starts at 1, skipping the first element.\n"
          "    total = 0\n    for i in range(0, len(xs)):\n        total += xs[i]\n"
          "    return total")


def test_fix_off_by_one(tmp_path):
    # the minimal fix passes even with the stale comment left in
    assert reward(tmp_path, "fix-off-by-one", _fix(FIX_OK)) == 1.0
    # a hard-coded answer with the loop still broken fails the hidden tests
    def hardcoded(ws):
        p = ws / "math" / "sum.py"
        p.write_text(p.read_text().replace("print(buggy_sum(SAMPLE))", "print(31)"))
        (ws / "FIXED").write_text("sum=31\n")
    assert reward(tmp_path, "fix-off-by-one", hardcoded) == 0.0
    # a FIXED line with quotes used to break the reward line into a pass
    assert reward(tmp_path, "fix-off-by-one", _fix(FIX_OK, 'sum="31"\n')) == 0.0
    # half a fix (start only) fails
    half = FIX_OK.replace("range(0, len(xs))", "range(0, len(xs) - 1)")
    assert reward(tmp_path, "fix-off-by-one", _fix(half, "sum=25\n")) == 0.0


# --------------------------------------------------------------- median-bug --
def _median(body: str):
    def solve(ws):
        p = ws / "stats" / "median.py"
        src = p.read_text()
        start = src.index("def median")
        end = src.index("\n\nif __name__")
        p.write_text(src[:start] + body + src[end:])
    return solve


MED_OK = ("def median(xs):\n    s = sorted(xs)\n    n = len(s)\n    m = n // 2\n"
          "    return s[m] if n % 2 else (s[m - 1] + s[m]) / 2")


def test_median_bug(tmp_path):
    assert reward(tmp_path, "median-bug", _median(MED_OK)) == 1.0
    two_lines = MED_OK + "\n\nprint('note:\\n')"
    assert reward(tmp_path, "median-bug", _median(two_lines)) == 0.0
    upper = "def median(xs):\n    s = sorted(xs)\n    return s[len(s) // 2]"
    assert reward(tmp_path, "median-bug", _median(upper)) == 0.0


# ------------------------------------------------------------ reverse-words --
REV_OK = ("#!/usr/bin/env bash\nwhile IFS= read -r line; do\n"
          "  read -ra w <<< \"$line\"\n  out=()\n"
          "  for ((i=${#w[@]}-1; i>=0; i--)); do out+=(\"${w[i]}\"); done\n"
          "  echo \"${out[*]}\"\ndone < words/phrases.txt\n")


def test_reverse_words(tmp_path):
    assert reward(tmp_path, "reverse-words", lambda ws: _exe(ws / "reverse.sh", REV_OK)) == 1.0
    tabbed = REV_OK.replace('echo "${out[*]}"', 'printf \'%s\\t"\\n\' "${out[*]}"')
    assert reward(tmp_path, "reverse-words", lambda ws: _exe(ws / "reverse.sh", tabbed)) == 0.0

    def not_exec(ws):
        (ws / "reverse.sh").write_text(REV_OK)
    assert reward(tmp_path, "reverse-words", not_exec) == 0.0


@pytest.mark.parametrize("line,rc,want", [
    ("nan", 0, 0.0), ('{"reward": NaN}', 0, 0.0), ('{"reward": 0.0, "detail": "x', 0, 0.0),
    ('{"reward": 1.0}', 1, 1.0), ("0.5", 0, 0.5), ("", 0, 1.0), ("", 3, 0.0),
])
def test_parse_reward_edges(line, rc, want):
    assert ClaudeCodeDomain._parse_reward(line + "\n", rc)[0] == want
