#!/usr/bin/env bash
# Hidden checker for hello-file. Prints a reward line (JSON with "reward") on
# stdout; the runner reads the LAST non-empty stdout line.
set -u
ws="${RRSI_WORKSPACE:-$PWD}"
target="$ws/notes/todo.txt"

# The reward line is built by json.dumps: agent output inside the detail can
# never break it (a broken line would fall back to the exit code).
fail() {
  python3 -c 'import json, sys; print(json.dumps({"reward": 0.0, "detail": sys.argv[1][:400]}))' "$1"
  exit 1
}

if [ ! -f "$target" ]; then
  fail "no notes/todo.txt"
fi

# Exactly two bytes beyond the 8 text bytes: the trailing newline.
if [ "$(wc -c < "$target")" -ne 9 ]; then
  fail "file must be exactly 'BUY MILK' plus one trailing newline ($(wc -c < "$target") bytes)"
fi

# Byte-exact content: uppercase, single spaces, no trailing whitespace.
if [ "$(cat "$target")" != "BUY MILK" ]; then
  fail "content mismatch; want exactly: BUY MILK"
fi

# No other files in notes/.
extra="$(cd "$ws/notes" && ls -A | grep -v -x 'todo.txt' || true)"
if [ -n "$extra" ]; then
  fail "unexpected extra files in notes/: $extra"
fi

# No other files anywhere: the runner lists what the workspace held before the
# agent ran (fixture + harness) in RRSI_PRE_MANIFEST; .claude/ is the
# harness's own runtime state.
if [ -n "${RRSI_PRE_MANIFEST:-}" ] && [ -f "$RRSI_PRE_MANIFEST" ]; then
  extra="$(cd "$ws" && find . \( -type f -o -type l \) ! -path './.claude/*' \
             ! -path './notes/todo.txt' | sed 's|^\./||' \
           | grep -v -x -F -f "$RRSI_PRE_MANIFEST" | head -5 || true)"
  if [ -n "$extra" ]; then
    fail "unexpected extra files: $(echo $extra)"
  fi
fi

echo '{"reward": 1.0}'
