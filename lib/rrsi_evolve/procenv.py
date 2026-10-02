# Part of rrsi-policy. Ports google-research/rrsi (Apache-2.0).
"""Environment for child `claude` processes (trial policy and search roles).

RRSI froze the policy inside a container. Here the child inherits the
caller's environment, and a parent Claude Code session exports variables
that change the child's behaviour: the subagent model
(CLAUDE_CODE_SUBAGENT_MODEL), reasoning effort, the entrypoint
(CLAUDE_CODE_ENTRYPOINT, which unlocks host-only tools), session and
messaging sockets, feature toggles, timeouts. Measured on a desktop-app
parent: the policy saw 34 tools instead of 8 and ~18k extra context tokens
per call.

`child_env` drops every CLAUDE*/ANTHROPIC_*/MCP_*/OTEL_*/DISABLE_*/ENABLE_*/
BASH_* variable and the other Claude Code behaviour variables (thinking
budget, timeouts), except authentication, provider routing and model-alias
mapping; it drops OLDPWD, disables auto-memory and the auto-updater (the
policy binary stays the same for the whole run). `passthrough` names extra
variables to keep verbatim.

Project settings can still set variables through their `env` block, and a
CLAUDE.md in any directory above the cwd is loaded as project memory.
`pinned_settings` builds the `--settings` JSON that outranks project and
local settings (measured on 2.1.280: a flag-settings env key beats the same
key in .claude/settings.json; claudeMdExcludes there drops an ancestor's
CLAUDE.md). Subagent metadata still lands in ~/.claude/projects/<cwd>;
`session_dir` names that directory so callers can remove it with their cwd.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

_PREFIXES = ("CLAUDE", "ANTHROPIC_", "MCP_", "OTEL_", "DISABLE_", "ENABLE_", "BASH_")
# Claude Code behaviour variables without one of the prefixes above
_BEHAVIOUR = frozenset({
    "MAX_THINKING_TOKENS", "MAX_MCP_OUTPUT_TOKENS", "MAX_STRUCTURED_OUTPUT_RETRIES",
    "API_TIMEOUT_MS", "AI_AGENT", "OLDPWD", "FALLBACK_FOR_ALL_PRIMARY_MODELS",
    "FORCE_PROMPT_CACHING_5M", "SYSTEM_REMINDER_MEMORY_CONTEXT", "USE_BUILTIN_RIPGREP",
    "IS_SANDBOX",
})

# Authentication and provider routing: needed to reach the model at all, and
# the same for every candidate (the user's choice, not the harness's).
AUTH_KEEP = frozenset({
    "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL",
    "ANTHROPIC_CUSTOM_HEADERS", "ANTHROPIC_MODEL", "ANTHROPIC_SMALL_FAST_MODEL",
    "CLAUDE_CODE_OAUTH_TOKEN", "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX",
    "CLAUDE_CODE_USE_FOUNDRY", "CLAUDE_CODE_SKIP_BEDROCK_AUTH",
    "CLAUDE_CODE_SKIP_VERTEX_AUTH", "CLAUDE_CODE_SKIP_FOUNDRY_AUTH",
    "CLAUDE_CODE_CLIENT_CERT", "CLAUDE_CODE_CLIENT_KEY",
    "CLAUDE_CODE_CLIENT_KEY_PASSPHRASE", "CLAUDE_CONFIG_DIR",
})
# model-alias mapping (Bedrock/Vertex/Foundry account IDs) and provider blocks
AUTH_KEEP_PREFIXES = ("ANTHROPIC_DEFAULT_", "ANTHROPIC_BEDROCK_", "ANTHROPIC_VERTEX_",
                      "ANTHROPIC_FOUNDRY_", "VERTEX_REGION_")

FIXED = {"CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1", "DISABLE_AUTOUPDATER": "1"}


def child_env(extra: dict | None = None, passthrough=()) -> dict:
    keep = AUTH_KEEP | set(passthrough)
    env = {k: v for k, v in os.environ.items()
           if k in keep or k.startswith(AUTH_KEEP_PREFIXES)
           or not (k.startswith(_PREFIXES) or k in _BEHAVIOUR)}
    env.update(FIXED)
    env.update(extra or {})
    return env


def ancestor_excludes(cwd: str) -> list[str]:
    """claudeMdExcludes entries for the instruction files of every directory
    above `cwd` (a shared temp dir is writable by anyone, and by every trial)."""
    out = []
    p = Path(os.path.abspath(cwd))
    for a in {p, Path(os.path.realpath(cwd))}:
        for d in a.parents:
            s = str(d).rstrip("/")
            out += [f"{s}/CLAUDE.md", f"{s}/CLAUDE.local.md", f"{s}/.claude/CLAUDE.md",
                    f"{s}/.claude/rules/**", f"{s}/AGENTS.md", f"{s}/.claude/AGENTS.md"]
    return sorted(set(out))


def pinned_settings(cwd: str, env: dict | None = None, extra: dict | None = None) -> str:
    """`--settings` JSON: flag settings outrank project and local settings, so
    the harness cannot override what is pinned here."""
    obj = {"env": {**FIXED, **(env or {})}, "autoMemoryEnabled": False,
           "claudeMdExcludes": ancestor_excludes(cwd)}
    obj.update(extra or {})
    return json.dumps(obj)


def _js_hash36(s: str) -> str:
    """Claude Code's path hash: Java-style hashCode over UTF-16 units, base 36."""
    raw = s.encode("utf-16-le")
    h = 0
    for i in range(0, len(raw), 2):
        h = ((h << 5) - h + int.from_bytes(raw[i:i + 2], "little")) & 0xFFFFFFFF
    if h >= 1 << 31:
        h -= 1 << 32
    h = abs(h)
    digits = "0123456789abcdefghijklmnopqrstuvwxyz"
    out = ""
    while True:
        h, r = divmod(h, 36)
        out = digits[r] + out
        if not h:
            return out


def _config_dir() -> Path:
    return Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")


def session_dir(cwd: str) -> Path:
    """Claude Code's per-project data dir for a session whose cwd was `cwd`
    (sanitized path; over 200 chars it is cut and a hash appended)."""
    name = re.sub(r"[^A-Za-z0-9]", "-", str(cwd))
    if len(name) > 200:
        name = f"{name[:200]}-{_js_hash36(str(cwd))}"
    return _config_dir() / "projects" / name


def session_residue(cwd: str, session_ids=()) -> list[Path]:
    """Everything a finished session leaves under the config dir: the project
    dir of its cwd (and of the cwd's realpath) and session-env/<id>."""
    out = [session_dir(d) for d in {str(cwd), os.path.realpath(cwd)}]
    for sid in session_ids:
        if sid and re.fullmatch(r"[A-Za-z0-9-]+", str(sid)):
            out.append(_config_dir() / "session-env" / str(sid))
    return out
