#!/usr/bin/env bash
# Hidden checker for median-bug: property tests the agent cannot see.
set -u
ws="${RRSI_WORKSPACE:-$PWD}"

# The reward line is built by json.dumps: agent output inside the detail can
# never break it (a broken line would fall back to the exit code).
fail() {
  python3 -c 'import json, sys; print(json.dumps({"reward": 0.0, "detail": sys.argv[1][:400]}))' "$1"
  exit 1
}

if [ ! -f "$ws/stats/median.py" ]; then
  fail "stats/median.py missing"
fi

python3 - "$ws/stats/median.py" <<'PYEOF' || fail "median property tests failed"
import importlib.util
import statistics
import sys

path = sys.argv[1]
spec = importlib.util.spec_from_file_location("median_mod", path)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
med = mod.median

# The canonical even case.
assert med([1, 2, 3, 4]) == 2.5, f"even case: {med([1, 2, 3, 4])}"
assert med([4, 1, 3, 2]) == 2.5, "order must not matter"
assert med([5]) == 5, "singleton"
assert med([2, 4]) == 3.0, "two elements"
assert med([1, 3]) == 2.0, "two elements 2"
assert med([10, -1, 3]) == 3, "odd case"
# Random property test against the stdlib reference.
import random
random.seed(7)
for _ in range(50):
    xs = [random.randint(-100, 100) for _ in range(random.randint(1, 20))]
    got, want = med(list(xs)), statistics.median(xs)
    assert abs(got - want) < 1e-9, f"mismatch for {xs}: {got} != {want}"
# The function name and module must stay importable.
assert mod.__name__ == "median_mod"
print("PROPS_OK")
PYEOF

# The program must still run and print its result.
out="$(cd "$ws" && python3 stats/median.py)" || fail "python3 stats/median.py failed"
case "$out" in
  2.5|2.50) ;;
  *) fail "program prints '$out', want 2.5 for [1, 2, 3, 4]" ;;
esac

echo '{"reward": 1.0}'
