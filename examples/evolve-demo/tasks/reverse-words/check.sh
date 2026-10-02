#!/usr/bin/env bash
# Hidden checker for reverse-words.
set -u
ws="${RRSI_WORKSPACE:-$PWD}"

# The reward line is built by json.dumps: agent output inside the detail can
# never break it (a broken line would fall back to the exit code).
fail() {
  python3 -c 'import json, sys; print(json.dumps({"reward": 0.0, "detail": sys.argv[1][:400]}))' "$1"
  exit 1
}

if [ ! -f "$ws/reverse.sh" ]; then
  fail "no reverse.sh"
fi
if [ ! -x "$ws/reverse.sh" ]; then
  fail "reverse.sh is not executable"
fi

out="$(cd "$ws" && ./reverse.sh 2>/dev/null)" || fail "reverse.sh exited non-zero"

want=$'world hello\ntwice tests the run\none\nwords between spaces multiple\nhere spaces trailing'
if [ "$out" != "$want" ]; then
  fail "output mismatch; got: $(echo "$out" | tr '\n' '|')"
fi

line_count="$(printf '%s\n' "$out" | grep -c . || true)"
if [ "$line_count" -ne 5 ]; then
  fail "expected 5 output lines, got $line_count"
fi

echo '{"reward": 1.0}'
