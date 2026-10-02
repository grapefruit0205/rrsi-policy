"""Which model "the model you use" is.

Measurements are only about the model they ran on, so every model knob of
rrsi-policy and rrsi-evolve defaults to "inherit": the model Claude Code would
pick for you. Resolution order (the same precedence Claude Code uses, minus
--model, which a separate process cannot see):

  0. RRSI_MODEL, to pin "inherit" without touching Claude Code's settings
  1. the live session's model, when a hook hands us its transcript
  2. ANTHROPIC_MODEL
  3. "model" in .claude/settings.local.json, then .claude/settings.json of the
     project (cwd and its parents), then the user settings
     ($CLAUDE_CONFIG_DIR or ~/.claude)/settings.json
  4. FALLBACK, reported as such so callers can say so

Only the settings files are read, never credentials or ~/.claude.json.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

INHERIT = "inherit"
FALLBACK = "opus"
_INHERIT_NAMES = {"inherit", "current", "default"}
_TAIL_BYTES = 512 * 1024


def is_inherit(name) -> bool:
    return name is None or str(name).strip().lower() in _INHERIT_NAMES | {""}


def _settings_model(path: Path) -> str | None:
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    m = data.get("model") if isinstance(data, dict) else None
    return m.strip() if isinstance(m, str) and m.strip() else None


def session_model(transcript_path) -> str | None:
    """Model of the newest assistant turn in a Claude Code transcript."""
    if not transcript_path:
        return None
    try:
        with open(transcript_path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - _TAIL_BYTES))
            tail = f.read().decode("utf-8", "replace").splitlines()
    except OSError:
        return None
    for line in reversed(tail):
        if '"assistant"' not in line:
            continue
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if ev.get("type") != "assistant" or ev.get("isSidechain"):
            continue
        m = (ev.get("message") or {}).get("model")
        if isinstance(m, str) and m and not m.startswith("<"):
            return m
    return None


def configured_model(cwd=None, env=None) -> tuple[str | None, str]:
    env = os.environ if env is None else env
    if env.get("ANTHROPIC_MODEL"):
        return env["ANTHROPIC_MODEL"], "ANTHROPIC_MODEL"
    here = Path(cwd or os.getcwd()).resolve()
    home = Path(env.get("HOME") or Path.home())
    for d in (here, *here.parents):
        if d == home:
            break
        for name in ("settings.local.json", "settings.json"):
            p = d / ".claude" / name
            m = _settings_model(p)
            if m:
                return m, str(p)
    user = Path(env.get("CLAUDE_CONFIG_DIR") or home / ".claude") / "settings.json"
    m = _settings_model(user)
    if m:
        return m, str(user)
    return None, "none"


def resolve(name, cwd=None, transcript=None, env=None) -> tuple[str, str]:
    """(model, where it came from). An explicit name wins over inheriting."""
    if not is_inherit(name):
        return str(name), "config"
    env = os.environ if env is None else env
    if env.get("RRSI_MODEL"):
        return env["RRSI_MODEL"], "RRSI_MODEL"
    m = session_model(transcript)
    if m:
        return m, "session"
    m, src = configured_model(cwd, env)
    if m:
        return m, src
    return FALLBACK, "fallback (no model configured)"
