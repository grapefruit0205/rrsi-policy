#!/usr/bin/env bash
# Hidden checker for fix-off-by-one.
set -u
ws="${RRSI_WORKSPACE:-$PWD}"

# The reward line is built by json.dumps: agent output inside the detail can
# never break it (a broken line would fall back to the exit code).
fail() {
  python3 -c 'import json, sys; print(json.dumps({"reward": 0.0, "detail": sys.argv[1][:400]}))' "$1"
  exit 1
}

if [ ! -f "$ws/math/sum.py" ]; then
  fail "math/sum.py missing"
fi
if [ ! -f "$ws/FIXED" ]; then
  fail "no FIXED file"
fi

# The FIXED file must be a single line: sum=<number>.
content="$(cat "$ws/FIXED")"
lines="$(printf '%s\n' "$content" | grep -c . || true)"
if [ "$lines" -ne 1 ]; then
  fail "FIXED must contain exactly one line (got $lines)"
fi
case "$content" in
  sum=*) ;;
  *) fail "FIXED must be in the form sum=<number>" ;;
esac

# The corrected program must print 31 (full sum of the sample).
out="$(cd "$ws" && python3 math/sum.py)" || fail "python3 math/sum.py failed"
if [ "$out" != "31" ]; then
  fail "corrected program prints '$out', want 31"
fi
if [ "$content" != "sum=31" ]; then
  fail "FIXED says '$content', want sum=31"
fi

# The bug must actually be fixed: hidden behavioural tests of buggy_sum
# (an empty list, a singleton, the first and last elements).
python3 - "$ws/math/sum.py" <<'PYEOF' || fail "buggy_sum still wrong on hidden inputs"
import importlib.util, sys
spec = importlib.util.spec_from_file_location("sum_mod", sys.argv[1])
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
for xs in ([], [5], [1, 2, 3, 4], [7, 0, 0, 7], list(mod.SAMPLE)):
    got = mod.buggy_sum(list(xs))
    assert got == sum(xs), f"buggy_sum({xs}) = {got}, want {sum(xs)}"
PYEOF

echo '{"reward": 1.0}'
