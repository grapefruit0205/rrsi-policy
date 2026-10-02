#!/usr/bin/env bash
# Hidden checker for csv-filter.
set -u
ws="${RRSI_WORKSPACE:-$PWD}"

# The reward line is built by json.dumps: agent output inside the detail can
# never break it (a broken line would fall back to the exit code).
fail() {
  python3 -c 'import json, sys; print(json.dumps({"reward": 0.0, "detail": sys.argv[1][:400]}))' "$1"
  exit 1
}

if [ ! -f "$ws/filter.sh" ]; then
  fail "no filter.sh"
fi
if [ ! -x "$ws/filter.sh" ]; then
  fail "filter.sh is not executable"
fi

out="$(cd "$ws" && ./filter.sh 2>/dev/null)" || fail "filter.sh exited non-zero"

want=$'alice,west,12\ncarol,west,3\nfrank,west,0'
if [ "$out" != "$want" ]; then
  fail "output mismatch; got: $(echo "$out" | head -5 | tr '\n' '|')"
fi

# Edge cases baked into the fixture: northwest/westcoast rows and the east row
# must be excluded, the zero-units west row must be INCLUDED, header excluded.
if echo "$out" | grep -q "bob\|dave\|erin\|name,"; then
  fail "output contains rows that must be excluded"
fi
if ! echo "$out" | grep -q "frank,west,0"; then
  fail "zero-units row frank,west,0 must be included"
fi

echo '{"reward": 1.0}'
