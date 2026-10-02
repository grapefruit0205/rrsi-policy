# Part of rrsi-policy. Ports google-research/rrsi (Apache-2.0).
"""Claude Code instance: the frozen policy is Claude Code headless (`claude -p`),
run in a fresh workspace per trial under the candidate's project configuration.

Evaluate = one `claude -p --output-format stream-json --verbose` session per
trial, cwd = a temp workspace. The candidate harness (CLAUDE.md, .claude/*,
.mcp.json, ...) is snapshotted once per job and overlaid onto the workspace
before the policy starts; the task's optional fixture is copied in first (a
harness file may not collide with a fixture file: static smoke rejects it).
After the agent stops, the task's hidden `check.sh` runs in the same
workspace; a trial's reward is the checker's last stdout line (a float, or a
JSON object with "reward"), else 1.0 when it exits 0. A missing or
infra-failed trial counts 0.0 with the full denominator; a timeout is NOT an
infra failure (the agent's own fault, as in RRSI's coding domain).

The task suite is read from the MAIN repo (`<repo>/tasks_dir`), never from a
worktree, so candidates cannot alter tasks or checkers.

RRSI ran the policy in a container. Here, with `sandbox` "auto" (the default,
or RRSI_EVOLVE_SANDBOX) on Linux with bubblewrap, each trial runs in a bwrap
PID, IPC, mount and network namespace: the host read-only; a fresh tmpfs
HOME with only toolchain dirs bound back read-only; empty /run, temp and
user runtime dirs, and every other host unix socket masked (docker, the
system and session buses, systemd); the repo, the runs dir and the shared
temp dir hidden; Claude Code on a fresh private config dir per trial (the
real one, with every session's transcripts and the user's own skills and
settings, absent; only the credentials file bound in); the installed claude
binaries covered by a refusing stub; no network but an allowlist HTTPS
proxy (netproxy); the workspace and the per-job state dir writable; and
every process the agent started dies with the namespace. Without a sandbox the
policy's tools run on the host: the
adapter still hands it no pointer (scrubbed environment, PWD = workspace,
state outside the repo), pins the frozen-policy settings through --settings,
excludes instruction files above the workspace, and kills the agent's
processes by a per-trial environment token; `policy_wrapper` takes any other
wrapper (firejail, a container).
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

HERE = Path(__file__).resolve().parent
for _p in (str(HERE.parent.parent), str(HERE)):
    if _p not in sys.path:
        sys.path.insert(0, _p)
from rrsi_evolve.domain import Domain          # noqa: E402
from rrsi_policy import models                 # noqa: E402
from rrsi_evolve.evaluate import TaskResult    # noqa: E402
from rrsi_evolve import netproxy                # noqa: E402
from rrsi_evolve.procenv import child_env, pinned_settings, session_residue  # noqa: E402
import briefs                            # noqa: E402
import render                            # noqa: E402

SETUP_TIMEOUT_S = 300
TERM_GRACE_S = 5          # SIGTERM -> SIGKILL grace on a policy timeout

_INFRA_TEXT = re.compile(
    r"api error|overloaded|rate.?limit|authenticat|credit|quota"
    r"|\b(?:status|http|error|code)\W{0,5}5\d\d\b"
    r"|usage limit|hit your .{0,30}limit|limit reached|not logged in|/login"
    r"|invalid api key|login expired|you(?:'|’)ve reached your|out of usage"
    r"|shared budget|seat type doesn(?:'|’)t include|usage allocation"
    r"|invalid auth token|credentials expired|could not refresh access token"
    r"|disabled organization|gateway refused|another claude code process is refreshing"
    r"|unable to connect to (?:the )?api|disabled claude subscription access"
    r"|does not have access to claude|(?:please )?(?:log|sign) ?in again", re.I)
# result subtypes that mean the CLI itself failed (not the agent)
_INFRA_SUBTYPES = {"error_during_execution"}

# The policy's built-in tool set. --allowedTools only pre-approves; --tools
# decides what exists. Skill and Task carry the harness's skills/subagents and
# are always pre-approved: a skill whose frontmatter has allowed-tools or hooks
# asks for approval, and -p denies every ask.
DEFAULT_TOOLS = ["Read", "Edit", "Write", "Glob", "Grep", "Bash", "Skill", "Task"]
ALWAYS_ALLOWED = ["Skill", "Task"]

# Claude Code 2.1.280's hook events and hook types; anything else is dropped
# (an unknown event) or makes -p skip the whole settings file (a bad type).
HOOK_EVENTS = frozenset({
    "PreToolUse", "PostToolUse", "PostToolUseFailure", "PostToolBatch", "Notification",
    "UserPromptSubmit", "UserPromptExpansion", "SessionStart", "SessionEnd", "Stop",
    "StopFailure", "SubagentStart", "SubagentStop", "PreCompact", "PostCompact",
    "PreModelSwitch", "PostModelSwitch", "PermissionRequest", "PermissionDenied",
    "Setup", "TeammateIdle", "TaskCreated", "TaskCompleted", "Elicitation",
    "ElicitationResult", "ConfigChange", "WorktreeCreate", "WorktreeRemove",
    "InstructionsLoaded", "CwdChanged", "FileChanged", "DirectoryAdded", "MessageDisplay"})
HOOK_TYPES = frozenset({"command", "http", "mcp_tool", "prompt", "agent"})

# The frozen policy (model, effort, thinking, advisor, fallbacks, plugins,
# auto-memory, auth) and the permission scope, as settings keys...
_FROZEN_KEY = re.compile(
    r"model|effort|think|advisor|fallback|fastmode|ultracode|plugin|marketplace"
    r"|automemory|autodream|apikey|authrefresh|helper$|^agent$|forcelogin"
    r"|defaultmode|additionaldirectories|disableallhooks|enableallprojectmcpservers"
    r"|bypass|dangerous", re.I)
# ...and as variables in a settings `env` block
_FROZEN_ENV = re.compile(
    r"^(?:ANTHROPIC_|RRSI_|AWS_|GOOGLE_|VERTEX|CLOUD_ML|CLAUDE_CODE_USE_|CLAUDE_CODE_SKIP_"
    r"|CLAUDE_CODE_OAUTH|CLAUDE_CODE_API|CLAUDE_CONFIG_DIR|CLAUDE_CODE_ENTRYPOINT"
    r"|CLAUDE_CODE_SIMPLE|CLAUDE_CODE_CLIENT_|CLAUDE_CODE_EXTRA_BODY"
    r"|CLAUDE_CODE_MAX_OUTPUT_TOKENS|MAX_THINKING_TOKENS$|FORCE_PROMPT_CACHING"
    r"|(?:HTTPS?|ALL|NO)_PROXY$|NODE_OPTIONS$|NODE_EXTRA_CA_CERTS$|PATH$|HOME$"
    r"|TMPDIR$|PWD$|XDG_CONFIG_HOME$"
    # Claude Code's own switches for the frozen parts (names vary by version)
    r"|(?:CLAUDE|DISABLE|ENABLE|MAX|FORCE|USE|FALLBACK)_\w*?(?:MODEL|EFFORT|THINK|ADVISOR"
    r"|FALLBACK|FAST_MODE|ULTRA|MEMORY|DREAM|PLUGIN|MARKETPLACE))",
    re.I)
# model / effort keys in skill, agent and command frontmatter, in any YAML
# spelling a top-level key can take: plain, quoted, tagged, `? key`, a
# flow-mapping document, a merge key, an escaped quoted key
_FM_KEY = re.compile(
    r"""(?m)^[ \t]*(?:-[ \t]+)*(?:\?[ \t]*)?(?:[!&]\S*[ \t]+)*["']?(model|effort)["']?\s*:"""
    r"""(?![ \t]*["']?inherit["']?[ \t]*(?:#.*)?$)""")
_FM_FLOW_KEY = re.compile(
    r"""[{,]\s*(?:\?\s*)?(?:[!&]\S*\s+)*["']?(model|effort)["']?\s*:"""
    r"""(?!\s*["']?inherit["']?\s*[,}])""")
_FM_MERGE = re.compile(r"""(?m)^[ \t]*["']?<<["']?[ \t]*:""")
_FM_ESCAPED_KEY = re.compile(r"""(?m)(?:^|[{,])\s*(?:\?\s*)?"[^"]{0,200}?\\[^"]{0,200}"\s*:""")
# `? key` (an explicit YAML key) has no use in a frontmatter but hiding one
_FM_COMPLEX_KEY = re.compile(r"""(?m)(?:^[ \t]*(?:-[ \t]+)*|[{,][ \t]*)\?(?:[ \t]|$)""")

# HOME entries (toolchains, git config) a sandboxed trial sees, read-only;
# the rest of HOME is a fresh tmpfs
SANDBOX_HOME = (".local/bin", ".local/lib", ".local/share/uv", ".nvm", ".cargo/bin",
                ".rustup", ".pyenv", ".volta", ".bun", ".deno", "go/bin", ".sdkman",
                ".local/share/mise", ".config/mise", ".asdf", ".tool-versions",
                ".local/share/pnpm", ".npm-global", ".local/share/fnm", ".fnm",
                "miniconda3", "miniforge3", "anaconda3", ".conda", ".local/pipx",
                ".nix-profile", ".rbenv", ".gitconfig", ".config/git")
PROXY_PORT = 3128        # the forwarder's port on the sandbox's own loopback
MAX_HARNESS_ENTRIES = 5000   # paths a harness may expand to (symlinked dirs)
# host session sockets a sandboxed process must not be pointed at
_SESSION_VARS = ("DBUS_SESSION_BUS_ADDRESS", "XDG_RUNTIME_DIR", "SSH_AUTH_SOCK", "DISPLAY",
                 "WAYLAND_DISPLAY", "XAUTHORITY", "GPG_AGENT_INFO", "SESSION_MANAGER",
                 "ALL_PROXY", "all_proxy")

_SANDBOX_PROBE: dict = {}
_SANDBOX_LOCK = threading.Lock()


def _tool_name(rule: str) -> str:
    m = re.match(r"[A-Za-z_][\w-]*", str(rule))
    return m.group(0) if m else str(rule)


def _signal_group(proc, sig) -> None:
    try:
        os.killpg(proc.pid, sig)
    except (ProcessLookupError, PermissionError, OSError):
        pass


def _killpg(proc) -> None:
    _signal_group(proc, signal.SIGKILL)


def _sweep(token: str | None) -> int:
    """SIGKILL every process whose environment carries this trial's token.
    Claude Code starts Bash-tool commands and hooks detached (setsid), so
    killpg on the policy never reaches them; the token survives setsid."""
    if not token:
        return 0
    needle = f"RRSI_TRIAL_TOKEN={token}".encode()
    me, killed = os.getpid(), 0
    for _ in range(5):              # a dying parent can still fork: repeat
        found = []
        if os.path.isdir("/proc"):
            for d in os.listdir("/proc"):
                if not d.isdigit() or int(d) == me:
                    continue
                try:
                    with open(f"/proc/{d}/environ", "rb") as f:
                        if needle in f.read().split(b"\0"):
                            found.append(int(d))
                except OSError:
                    continue
        else:                       # BSD/macOS ps: -E appends the environment
            try:
                out = subprocess.run(["ps", "-axE", "-ww", "-o", "pid=,command="],
                                     capture_output=True, timeout=10).stdout
            except (OSError, subprocess.SubprocessError):
                return killed
            for line in out.splitlines():
                parts = line.split(None, 1)
                if len(parts) == 2 and parts[0].isdigit() and int(parts[0]) != me \
                        and needle in parts[1].split():
                    found.append(int(parts[0]))
        if not found:
            break
        for pid in found:
            try:
                os.kill(pid, signal.SIGKILL)
                killed += 1
            except OSError:
                pass
        time.sleep(0.05)
    return killed


def _host_env(ws: str) -> dict:
    """The evaluator's environment for setup.sh and check.sh, with PWD the
    workspace (they run there) and no OLDPWD pointing back at the caller."""
    env = {k: v for k, v in os.environ.items() if k != "OLDPWD"}
    env["PWD"] = ws
    return env


def _log(msg: str) -> None:
    print(f"[claudecode] {msg}", flush=True)


def _load_result(trial_dir: Path) -> dict | None:
    rj = Path(trial_dir) / "result.json"
    if not rj.exists():
        return None
    try:
        return json.loads(rj.read_text())
    except json.JSONDecodeError:
        return None


def _trial_no(td: Path) -> int:
    tail = td.name.rsplit("__", 1)[-1]
    return int(tail) if tail.isdigit() else 1 << 30


def _has_result_event(trial_dir: Path) -> bool:
    sj = Path(trial_dir) / "stream.jsonl"
    if not sj.exists():
        return False
    for line in sj.read_text(errors="replace").splitlines()[::-1]:
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if ev.get("type") == "result":
            return True
    return False


# JavaScript's \s (Claude Code's frontmatter regex): Python's leaves out U+FEFF
_JS_WS = "\t\n\v\f\r \u00a0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff"
# Claude Code 2.1.280: /^---\s*\n([\s\S]*?)---\s*\n?/ after one leading BOM
_CC_FM = re.compile(rf"\A---[{_JS_WS}]*\n([\s\S]*?)---[{_JS_WS}]*\n?")


def _fm_blocks(text: str) -> list[str]:
    """The frontmatter block as Claude Code reads it (closed by the first
    `---`, even inside a line) and as a YAML reader would (closed by a `---`
    line): checks look at both."""
    text = text[1:] if text.startswith("\ufeff") else text
    out = []
    m = _CC_FM.match(text)
    if m:
        out.append(m.group(1))
    lines = text.splitlines()
    ws = _JS_WS.replace("\u2000-\u200a", "".join(map(chr, range(0x2000, 0x200b))))
    if lines and lines[0].strip(ws) == "---":
        for i, line in enumerate(lines[1:], 1):
            if line.strip(ws) == "---":
                block = "\n".join(lines[1:i])
                if block not in out:
                    out.append(block)
                break
    return out


def _fm_block(text: str) -> str | None:
    blocks = _fm_blocks(text)
    return blocks[0] if blocks else None


def _fm_value(v: str) -> str:
    v = re.sub(r"\s+#.*$", "", v).strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
        v = v[1:-1]
    return v


def _frontmatter(text: str) -> dict | None:
    """YAML frontmatter (--- block) as flat `key: value` pairs, values with a
    trailing comment and one pair of quotes removed; enough for `name:` and
    `description:`, not a full YAML parser."""
    block = _fm_block(text)
    if block is None:
        return None
    meta = {}
    for line in block.splitlines():
        m = re.match(r"""^["']?([A-Za-z_][\w-]*)["']?\s*:""", line)
        if m:
            meta[m.group(1)] = _fm_value(line[m.end():])
    return meta


def _frozen_frontmatter(text: str) -> str | None:
    """Why a skill/agent/command frontmatter overrides the frozen policy."""
    for block in _fm_blocks(text):
        why = _frozen_block(block)
        if why:
            return why
    return None


def _frozen_block(block: str) -> str | None:
    # nested keys count too: a hook in the frontmatter may not set a model,
    # also not inside a flow mapping ({...}, possibly nested)
    m = _FM_KEY.search(block)
    if m is None:
        for flow in _flow_mappings(block):
            m = _FM_FLOW_KEY.search(flow)
            if m:
                break
    if m:
        return f"'{m.group(1)}' key"
    if _FM_MERGE.search(block):
        return "a YAML merge key (<<)"
    try:
        import yaml  # optional: exact keys when PyYAML is installed
        obj = yaml.safe_load(block)
        parsed = True
    except ImportError:
        obj, parsed = None, False
    except Exception:  # noqa: BLE001
        obj, parsed = None, False
    if parsed:
        # a parser resolves escaped and explicit keys itself
        why = _frozen_keys(obj)
        if why:
            return why
        if isinstance(obj, (dict, type(None))):
            return None
    if _FM_ESCAPED_KEY.search(block):
        return "an escaped (quoted with backslash) key"
    if _FM_COMPLEX_KEY.search(block):
        return "an explicit YAML key (?)"
    return None


def _frozen_keys(obj, depth: int = 0) -> str | None:
    """A model / effort key anywhere in parsed frontmatter (not inherit)."""
    if depth > 20:
        return None
    if isinstance(obj, dict):
        for key, val in obj.items():
            if str(key).strip() in ("model", "effort") and str(val).strip() != "inherit":
                return f"'{key}' key"
            why = _frozen_keys(val, depth + 1)
            if why:
                return why
    elif isinstance(obj, list):
        for x in obj:
            why = _frozen_keys(x, depth + 1)
            if why:
                return why
    return None


def _flow_mappings(block: str) -> list[str]:
    """The flow mappings of a YAML block: each `{` that starts the document
    or a value (after `key:` or a `- `) up to its matching `}`; quoted text
    inside is skipped. A `{` inside a plain scalar (a description) is not one."""
    out = []
    for m in re.finditer(r"(?m)(?:\A\s*|:[ \t]*|^[ \t]*-[ \t]+)(\{)", block):
        i, depth, q = m.start(1), 0, None
        for j in range(i, len(block)):
            ch = block[j]
            if q:
                if ch == q:
                    q = None
            elif ch in "\"'":
                q = ch
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    out.append(block[i:j + 1])
                    break
        else:
            out.append(block[i:])
    return out


def _yaml_frontmatter(text: str):
    """The frontmatter parsed with PyYAML when it is installed, else None."""
    block = _fm_block(text)
    if block is None:
        return None
    try:
        import yaml
        return yaml.safe_load(block)
    except Exception:  # noqa: BLE001
        return None


def _in_file(tail: str, line: str) -> str:
    """A precheck regex: an added line matching `line` inside the diff of a
    file whose path ends with `tail`."""
    # anchored at the file's `diff --git` line (an added line cannot fake it),
    # so every scan stops at the next file: linear in the diff length
    return (rf"(?m)^diff --git [^\n]*\n(?:(?!\+\+\+ |diff --git )[^\n]*\n){{0,8}}"
            rf"\+\+\+ (?:[a-z]/)?[^\n]*{tail}\n"
            rf"(?:(?!diff --git )[^\n]*\n)*?"
            rf"\+{line}")


class ClaudeCodeDomain(Domain):
    name = "claudecode"
    source_exts = {".md", ".json", ".sh", ".py", ".txt", ".yaml", ".yml",
                   ".js", ".mjs", ".toml"}

    def __init__(self, repo=None, raw_cfg: dict | None = None):
        cfg = dict(raw_cfg or {})
        self.harness_path = cfg.get("harness_path", "harness")
        self.tasks_dir = cfg.get("tasks_dir", "tasks")
        # "inherit" (default): the model you use, so the score is about it;
        # resolved once here and pinned per run directory by the CLI
        self.policy_model, self.policy_model_source = models.resolve(
            cfg.get("policy_model", models.INHERIT), cwd=repo)
        self.policy_label = cfg.get("policy_label", self.policy_model)
        self.policy_effort = cfg.get("policy_effort")
        if self.policy_effort not in (None, "low", "medium", "high", "xhigh", "max"):
            raise SystemExit(f"claudecode domain: policy_effort must be low, medium, "
                             f"high, xhigh or max, not {self.policy_effort!r}")
        self.permission_mode = cfg.get("permission_mode", "acceptEdits")
        self.allowed_tools = list(cfg.get(
            "allowed_tools", ["Read", "Edit", "Write", "Glob", "Grep", "Bash"]))
        self.disallowed_tools = list(cfg.get("disallowed_tools", []))
        self.tools = list(cfg.get("tools", list(dict.fromkeys(
            [_tool_name(r) for r in self.allowed_tools] + DEFAULT_TOOLS))))
        self.env_passthrough = list(cfg.get("env_passthrough", []))
        self.policy_wrapper = [str(a) for a in cfg.get("policy_wrapper", [])]
        self.sandbox = str(cfg.get("sandbox") or os.environ.get("RRSI_EVOLVE_SANDBOX")
                           or "auto")
        if self.sandbox not in ("auto", "bwrap", "none"):
            raise SystemExit(f"claudecode domain: sandbox must be auto, bwrap or none, "
                             f"not {self.sandbox!r}")
        hp = os.path.normpath(str(self.harness_path))
        tdn = os.path.normpath(str(self.tasks_dir))
        if hp in (".", "") or hp.startswith("..") or os.path.isabs(hp) \
                or tdn == hp or tdn.startswith(hp + os.sep):
            raise SystemExit(
                f"claudecode domain: harness_path {self.harness_path!r} must be a "
                f"subdirectory of the repo that does not contain tasks_dir "
                f"{self.tasks_dir!r} (the policy and the proposer would see the "
                f"hidden checkers)")
        self.task_timeout_s = int(cfg.get("task_timeout_s", 600))
        self.check_timeout_s = int(cfg.get("check_timeout_s", 300))
        self.max_budget_usd_per_trial = cfg.get("max_budget_usd_per_trial")
        self.concurrency = int(cfg.get("concurrency", 4))
        self.smoke_tasks = list(cfg.get("smoke_tasks", []))
        self.keep_workspaces = bool(cfg.get("keep_workspaces", False))
        self.extra_claude_args = list(cfg.get("extra_claude_args", []))
        self.repo: Path | None = Path(repo) if repo is not None else None
        self.sandbox_rw = [os.path.abspath(os.path.expanduser(str(x)))
                           for x in cfg.get("sandbox_rw", [])]
        # adds to the defaults (sandbox_home_only replaces them)
        self.sandbox_home = list(dict.fromkeys(
            ([] if cfg.get("sandbox_home_only") else list(SANDBOX_HOME))
            + [str(x) for x in cfg.get("sandbox_home", [])]))
        self.sandbox_network = str(cfg.get("sandbox_network", "proxy"))
        if self.sandbox_network not in ("proxy", "host"):
            raise SystemExit(f"claudecode domain: sandbox_network must be proxy or host, "
                             f"not {self.sandbox_network!r}")
        self.sandbox_net_allow = [str(x) for x in cfg.get("sandbox_net_allow", [])]
        self.sandbox_net_ports = [int(x) for x in cfg.get("sandbox_net_ports", [443])]
        self._hidden: list[Path] = []
        self._layout: dict = {}
        self._state_lock = threading.Lock()
        self.briefs = {k: v.replace("{policy}", self.policy_label)
                       for k, v in {"analyst": briefs.ANALYST, "digester": briefs.DIGESTER,
                                    "proposer": briefs.PROPOSER, "critic": briefs.CRITIC}.items()}
        settings_file = r"\.claude/settings(?:\.local)?\.json"
        agent_md = r"(?:agents|skills|commands)/[^\n]*\.md"
        script = (r"(?:\.(?:sh|bash|zsh|py|js|mjs|cjs|ts|rb|pl)"
                  r"|/(?:bin|hooks|tools|scripts)/[^/\n.]+)")
        code_line = r"(?![ \t]*(?:#|//))[^#\n]*?"    # an added line, before any # comment
        self.critic_patterns = [
            (r"check\.sh", "a reference to 'check.sh', the hidden checker's file name "
                           "(give harness helpers other names)"),
            (r"RRSI_WORKSPACE|RRSI_EVOLVE_TRIAL|RRSI_TRIAL_TOKEN|RRSI_PRE_MANIFEST",
             "evaluation env var in diff"),
            (r"rrsi-(?:ws|run|state|role)-", "evaluation temp path in diff"),
            (r"\.rrsi/", "runs directory in diff"),
            (r"\b(verifier\.txt|result\.json|stream\.jsonl|pre_manifest\.txt)\b",
             "trial artifact in diff"),
            (rf"(?<![\w-]){re.escape(self.tasks_dir)}/", "task suite path in diff"),
            (r"(?m)^(?:Binary files .* differ|GIT binary patch)$",
             "binary file in diff (the critic cannot read it)"),
            (r"(?m)^\+\+\+ (?:[a-z]/)?[^\n]*\.gitattributes$",
             ".gitattributes in the harness (it changes what the critic's diff shows)"),
            (r"(?s)\A.{120000}",
             "diff longer than the critic reads (120k characters): split the change"),
            # the frozen policy: no model / effort / thinking / provider overrides
            (rf"(?m)^diff --git [^\n]*\n(?:(?!\+\+\+ |diff --git )[^\n]*\n){{0,8}}"
             rf"\+\+\+ (?:[a-z]/)?[^\n]*{agent_md}\n(?:(?!diff --git )[^\n]*\n)*?"
             r"@@ -\d+(?:,\d+)? \+1(?:,\d+)? @@[^\n]*\n[+ ]\ufeff?---"
             r"[ \t\v\f\r\u00a0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]*\n"
             r"(?:[+ -](?!---)[^\n]*\n)*?"
             r"""\+(?:[ \t]*(?:-[ \t]+)*(?:\?[ \t]*)?(?:[!&]\S*[ \t]+)*["']?(?:model|effort)["']?"""
             r"""[ \t]*:(?![ \t]*["']?inherit["']?[ \t]*(?:#[^\n]*)?$)"""
             # or inside a flow mapping that starts the line or a value
             r"""|(?:[^\n:{]*:[ \t]*|[ \t]*(?:-[ \t]+)?)(?:[!&]\S*[ \t]+)*\{[^\n]*?[{,][ \t]*"""
             r"""["']?(?:model|effort)["']?[ \t]*:(?![ \t]*["']?inherit["']?[ \t]*[,}]))""",
             "model/effort key in skill/agent/command frontmatter (the policy is frozen)"),
            (r"(?m)^\+[^\n]+\ufeff", "a byte-order mark inside a line (it can hide a "
                                     "frontmatter key from the checks)"),
            (_in_file(settings_file, r"""[^\n]*"(?!(?:Pre|Post)ModelSwitch"|disable|[A-Z0-9_]+")"""
                                     r"""[^"\n]*(?i:model|effort|think|advisor"""
                                     r"""|fallback|fastmode|ultracode|plugin|marketplace"""
                                     r"""|automemory|autodream|apikey|authrefresh|helper"""
                                     r"""|defaultmode|additionaldirectories|disableallhooks"""
                                     r"""|enableallprojectmcpservers|bypass|dangerous)"""
                                     r"""[^"\n]*"[ \t]*:"""),
             "settings key that changes the frozen policy (model, effort, thinking, "
             "advisor, fallback, plugins, auto-memory, auth helpers) or widens permissions"),
            (r"(?m)^\+.*\b(?:CLAUDE_CODE_EFFORT_LEVEL|MAX_THINKING_TOKENS"
             r"|CLAUDE_CODE_DISABLE_(?:ADAPTIVE_)?THINKING|DISABLE_INTERLEAVED_THINKING"
             r"|CLAUDE_CODE_MAX_OUTPUT_TOKENS|CLAUDE_CODE_EXTRA_BODY"
             r"|CLAUDE_CODE_SUBAGENT_MODEL\w*|CLAUDE_CODE_AUTO_MODE_MODEL"
             r"|CLAUDE_CODE_(?:DISABLE|ENABLE_EXPERIMENTAL)_ADVISOR_TOOL"
             r"|FALLBACK_FOR_ALL_PRIMARY_MODELS"
             r"|CLAUDE_CODE_DISABLE_AUTO_MEMORY|CLAUDE_CODE_USE_\w+|CLAUDE_CONFIG_DIR"
             r"|ANTHROPIC_(?:BASE_URL|MODEL|SMALL_FAST_MODEL|DEFAULT_\w+_MODEL|BETAS"
             r"|CUSTOM_HEADERS|API_KEY|AUTH_TOKEN)|apiKeyHelper)\b",
             "model/effort/thinking/provider variable in diff (the policy is frozen)"),
            (r"(?m)^\+.*(?:(?<![$\w{])RRSI_HARNESS_STATE_DIR(?:[\"']\])?\s*=(?!=)"
             r"|[\"']RRSI_HARNESS_STATE_DIR[\"']\s*:|(?:setenv|putenv)\W+RRSI_HARNESS_STATE_DIR)",
             "RRSI_HARNESS_STATE_DIR reassigned (per-job state must stay per job)"),
            # (each flag token parses one way only: no exponential backtracking)
            (r"(?m)^\+.*(?:(?<![\w.-])claude[\"']?"
             r"(?:[ \t]+--?[A-Za-z][\w-]*(?:=[^\s;|&]*|[ \t]+(?!-)[^\s;|&]+)?)*"
             r"[ \t]+(?:-p|--print|--model|-c|--continue|-r|--resume)\b"
             r"|[\"']claude[\"']\s*,"
             r"|@anthropic-ai/(?:sdk|claude-agent-sdk|claude-code)|claude_agent_sdk"
             r"|claude-code-sdk|\b(?:npx|bunx|pnpx|dlx)\b[^\n]{0,300}?\bclaude\b"
             r"|api\.anthropic\.com|\banthropic\.(?:Async)?Anthropic\b"
             r"|(?<![\w.])(?:import|from)[ \t]+(?:anthropic|openai|litellm"
             r"|google\.generativeai|google\.genai)\b"
             r"|require\([\"'](?:@?anthropic|openai)|from [\"'](?:openai|@google/genai)[\"']"
             r"|api\.openai\.com|generativelanguage\.googleapis\.com|openrouter\.ai"
             r"|bedrock-runtime)",
             "model call outside the policy (claude CLI, a model API or SDK) in diff"),
            (_in_file(script, code_line + r"(?<![\w./-])claude(?![\w./-])"),
             "the claude command in a harness script (the policy model is frozen; "
             "nothing in the harness may start another model)"),
            (_in_file(script, code_line + r"(?:~/|\$HOME\b|\$\{HOME\}|expanduser\(\s*[\"']~"
                      r"|Path\.home\(\)|homedir\(\)|getenv\([\"']HOME|environ\[[\"']HOME)"),
             "home-directory path in a harness script (it outlives the trial and the "
             "job; keep state in $RRSI_HARNESS_STATE_DIR)"),
            (r"(?m)^\+.*(?:(?:~|\$HOME|\$\{HOME\})/\.claude(?:\.json)?\b"
             r"|(?:\.claude|\$\{?CLAUDE_CONFIG_DIR\}?)/(?:projects|history\.jsonl|file-history"
             r"|shell-snapshots|session-env|todos)\b|\.credentials\.json)",
             "Claude Code's own data (transcripts, history, credentials) in diff"),
            (r"(?m)^\+.*(?<![\w-])(?:systemd-run|busctl|dbus-send|gdbus|loginctl|machinectl"
             r"|nsenter|xdotool|ssh-add|/proc/(?:\d+|\$\w+|\$\{\w+\}|self|1)/(?:root|cwd|exe"
             r"|environ))\b",
             "a way out of the trial's sandbox (session bus, another process's view) in diff"),
            (r"(?m)^\+.*(?:dangerously-skip-permissions|allowDangerouslySkipPermissions"
             r"|dangerouslyDisableSandbox|bypassPermissions)",
             "permission widening in diff"),
        ]
        self._base_patterns = list(self.critic_patterns)
        # like the coding adapter's __init__: one pattern per task id, len >= 3
        if self.repo is not None and (self.repo / self.tasks_dir).is_dir():
            self._add_task_patterns(sorted(self._tasks()))

    # ---- task suite (read from the MAIN repo, never a worktree) -----------
    def _task_dir(self, task_id: str) -> Path:
        return self.repo / self.tasks_dir / task_id

    def _tasks(self) -> dict:
        out = {}
        base = self.repo / self.tasks_dir
        if not base.is_dir():
            return out
        for tid in sorted(p.name for p in base.iterdir() if p.is_dir()):
            tj = base / tid / "task.json"
            if not tj.is_file():
                continue
            try:
                raw = json.loads(tj.read_text())
            except json.JSONDecodeError:
                raw = {}
            prompt = raw.get("prompt")
            if "prompt_file" in raw and not prompt:
                pf = base / tid / str(raw["prompt_file"])
                prompt = pf.read_text() if pf.exists() else ""
            try:
                weight = float(raw.get("weight", 1))
            except (TypeError, ValueError):
                weight = float("nan")
            if not (math.isfinite(weight) and weight > 0):
                raise SystemExit(f"claudecode domain: {tj}: weight must be a number > 0, "
                                 f"not {raw.get('weight')!r}")
            out[tid] = {"prompt": str(prompt or ""),
                        "split": raw.get("split", "evolve"),
                        "timeout_s": int(raw.get("timeout_s", self.task_timeout_s)),
                        "weight": weight}
        return out

    def evolve_ids(self) -> list[str]:
        return [t for t, spec in self._tasks().items() if spec["split"] == "evolve"]

    def heldout_ids(self) -> list[str]:
        return [t for t, spec in self._tasks().items() if spec["split"] == "heldout"]

    def smoke_ids(self, incumbent_per_task=None) -> list[str]:
        if self.smoke_tasks:
            return list(self.smoke_tasks)
        evolve = self.evolve_ids()
        if incumbent_per_task:
            have = [t for t in evolve if t in incumbent_per_task]
            if len(have) >= 2:
                top = sorted(have, key=lambda t: incumbent_per_task[t].mean,
                             reverse=True)
                return top[:2]
        return evolve[:2]

    def _add_task_patterns(self, tasks) -> None:
        extra = [(rf"(?<![\w-]){re.escape(n)}(?![\w-])",
                  f"benchmark task name '{n}' in diff")
                 for n in tasks if len(n) >= 3]
        self.critic_patterns = list(self._base_patterns) + extra

    # ---- harness files -----------------------------------------------------
    def _git_ls(self, hdir: Path, *flags: str) -> list[str] | None:
        r = subprocess.run(["git", "-C", str(hdir), "ls-files", "-z", *flags, "--", "."],
                           capture_output=True)
        if r.returncode != 0:
            return None
        return [x for x in r.stdout.decode("utf-8", "surrogateescape").split("\0") if x]

    def _harness_entries(self, root: Path) -> tuple[list[tuple[str, Path]], list[str]]:
        """(relative path, source file) pairs the candidate commit installs:
        tracked plus untracked-but-not-ignored files (what the critic's diff and
        `git add -A` see). A symlink into the harness installs its target's
        content; a symlink leaving the harness is a problem (second list).
        Ignored files and .git entries are never installed."""
        hdir = (root / self.harness_path).resolve()
        if not hdir.is_dir():
            return [], []
        listed = self._git_ls(hdir, "--cached", "--others", "--exclude-standard")
        paths = ([hdir / x for x in listed] if listed is not None
                 else list(hdir.rglob("*")))
        out: dict[str, Path] = {}
        problems: list[str] = []
        budget = [MAX_HARNESS_ENTRIES]

        def listing(d: Path, rel: Path) -> list[Path]:
            try:
                return sorted(d.iterdir())
            except OSError as e:
                problems.append(f"{rel} (unreadable: {e.__class__.__name__})")
                return []

        def add(p: Path, rel: Path, seen: frozenset) -> None:
            if ".git" in rel.parts:
                return
            if seen:                # reached through a symlinked dir
                budget[0] -= 1
                if budget[0] == 0:
                    problems.append(f"symlinked dirs expand to more than "
                                    f"{MAX_HARNESS_ENTRIES} paths")
                if budget[0] <= 0:
                    return
            if p.is_symlink():
                try:
                    target = p.resolve(strict=True)
                except (OSError, RuntimeError):         # dangling or a loop
                    problems.append(f"{rel} -> {os.readlink(p)}")
                    return
                if not (target == hdir or hdir in target.parents):
                    problems.append(f"{rel} -> {os.readlink(p)}")
                    return
                if target.is_dir():
                    # a link to its own directory or an ancestor (directly
                    # or through other links) would expand forever
                    here = p.parent.resolve()
                    if any(t == target or target in t.parents
                           for t in seen | {here}):
                        problems.append(f"{rel} -> {os.readlink(p)} (link loop)")
                        return
                    for f in listing(target, rel):
                        add(f, rel / f.name, seen | {target})
                    return
                p = target
            elif p.is_dir() and seen:                   # inside a linked dir
                for f in listing(p, rel):
                    add(f, rel / f.name, seen)
                return
            if p.is_file():
                out[str(rel)] = p

        for p in paths:
            add(p, p.relative_to(hdir), frozenset())
        return sorted(out.items()), problems

    def _harness_files(self, root: Path) -> list[Path]:
        return [src for _, src in self._harness_entries(root)[0]]

    @staticmethod
    def _install(entries, dest: str | Path, merge: bool = False) -> None:
        """Write harness files (rel, source path or bytes) under dest. With
        merge, an instruction or settings file the task fixture already has is
        combined with the harness's (text appended, JSON deep-merged) instead
        of replaced: both the task's and the candidate's apply."""
        for rel, src in entries:
            data = src if isinstance(src, bytes) else Path(src).read_bytes()
            dst = Path(dest) / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            if merge and rel in MERGED_FILES and dst.is_file() and not dst.is_symlink():
                data = _merge_file(rel, dst.read_bytes(), data)
            elif merge and rel == "CLAUDE.md" and not dst.exists() \
                    and (Path(dest) / "AGENTS.md").is_file():
                # Claude Code reads AGENTS.md only where there is no CLAUDE.md:
                # keep the task's instructions in front of the harness's
                data = _merge_file(rel, (Path(dest) / "AGENTS.md").read_bytes(), data)
            if dst.is_symlink() or dst.is_file():
                dst.unlink()
            dst.write_bytes(data)
            # proposer-written scripts carry no exec bit (Workspace.write)
            if data[:2] == b"#!":
                dst.chmod(dst.stat().st_mode | 0o111)

    def _overlay(self, root: Path, ws: str) -> None:
        self._install(self._harness_entries(root)[0], ws)

    def _check_committed(self, root: Path) -> None:
        """A candidate (a linked worktree of the repo, drafted and committed
        by the loop) is evaluated as committed: harness edits on disk after
        the commit (by a trial that found the worktree) fail the job. The
        main checkout's working copy is the harness as it stands."""
        def git(*a):
            return subprocess.run(["git", "-C", str(root), *a], capture_output=True,
                                  text=True, errors="replace")
        gd, cd = git("rev-parse", "--absolute-git-dir"), git("rev-parse", "--git-common-dir")
        if gd.returncode != 0 or cd.returncode != 0:
            return
        common = Path(cd.stdout.strip())
        common = common if common.is_absolute() else (Path(root) / common)
        if Path(gd.stdout.strip()).resolve() == common.resolve():
            return                              # not a linked worktree
        st = git("status", "--porcelain", "--untracked-files=all", "--", self.harness_path)
        if st.returncode == 0 and st.stdout.strip():
            raise RuntimeError(f"the harness in {root} changed after its commit: "
                               f"{st.stdout.strip()[:300]}")

    def _snapshot(self, root: Path) -> tuple[list[tuple[str, bytes]], str]:
        """Read the harness once per job, into memory: trials install these
        bytes, so nothing that touches the worktree (or any file) during the
        job reaches later trials."""
        entries, _ = self._harness_entries(root)
        h = hashlib.sha256()
        snap = []
        for rel, src in entries:
            data = src.read_bytes()
            h.update(rel.encode("utf-8", "surrogateescape") + b"\0")
            h.update(data + b"\0")
            snap.append((rel, data))
        return snap, h.hexdigest()

    # ---- sandbox -----------------------------------------------------------
    def _sandbox_mode(self) -> str:
        """"bwrap" or "none" for this run (auto: bwrap when it works here)."""
        if self.policy_wrapper or self.sandbox == "none":
            return "none"
        with _SANDBOX_LOCK:
            if "ok" not in _SANDBOX_PROBE:
                ok, why = False, "bwrap not found"
                if sys.platform.startswith("linux") and shutil.which("bwrap"):
                    net = ["--unshare-net"] if self.sandbox_network == "proxy" else []
                    r = subprocess.run(["bwrap", "--ro-bind", "/", "/", "--dev", "/dev",
                                        "--proc", "/proc", "--unshare-pid", "--unshare-ipc",
                                        *net, "--die-with-parent", "true"],
                                       capture_output=True, text=True, errors="replace")
                    ok, why = r.returncode == 0, (r.stderr or "").strip()[:200]
                _SANDBOX_PROBE.update(ok=ok, why=why)
                if not ok and self.sandbox == "auto":
                    _log(f"WARNING: no sandbox ({why}): the policy's tools run on the "
                         f"host and can reach the repo, the checkers and other trials. "
                         f"Install bubblewrap or set policy_wrapper; sandbox: none "
                         f"silences this.")
        if _SANDBOX_PROBE["ok"]:
            return "bwrap"
        if self.sandbox == "bwrap":
            raise SystemExit(f"claudecode domain: sandbox 'bwrap' requested but it does "
                             f"not work here: {_SANDBOX_PROBE['why']}")
        return "none"

    def _prepare_sandbox(self, base: Path, shim: Path) -> dict:
        """Per-run pieces of the bwrap layout: the template of the private
        Claude Code config dir (the real one holds every session's
        transcripts, the user's own skills, agents, settings and CLAUDE.md;
        each trial gets a fresh copy), where the policy binary runs from (its
        install paths are covered by the claude-refusing stub), the toolchain
        and credential paths to bind back, and the network proxy."""
        home = Path.home().resolve()
        real_cfg = Path(os.environ.get("CLAUDE_CONFIG_DIR")
                        or Path.home() / ".claude").expanduser()
        # every trial mounts its own config dir at the same path inside one
        # shared dir: Claude Code's refresh lock beside the config dir
        # (<dir>.lock) is then one lock for all trials of the run
        shared = base / "shared"
        shared.mkdir(mode=0o700)
        (shared / "config").mkdir(mode=0o700)
        tmpl = base / "config-template"
        tmpl.mkdir(mode=0o700)
        gfile = (real_cfg / ".claude.json" if os.environ.get("CLAUDE_CONFIG_DIR")
                 else Path.home() / ".claude.json")
        try:
            g = json.loads(gfile.read_text())
        except (OSError, ValueError):
            g = {}
        keep = ("oauthAccount", "userID", "hasCompletedOnboarding", "lastOnboardingVersion",
                "migrationVersion", "firstStartTime", "opusProMigrationComplete",
                "sonnet1m45MigrationComplete",
                # a Console (API key) login keeps its key here
                "primaryApiKey", "customApiKeyResponses")
        fd = os.open(tmpl / ".claude.json", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump({k: g[k] for k in keep if isinstance(g, dict) and k in g}, f)
        creds = real_cfg / ".credentials.json"
        lay = {"cfg": shared / "config", "shared": shared, "template": tmpl,
               "creds": None, "bin": None, "covers": [], "binds": [],
               "net": None, "python": None,
               # config dirs outside HOME (HOME itself is replaced)
               "hide": sorted({str(p.resolve()) for p in (real_cfg, Path.home() / ".claude")
                               if p.is_dir() and home not in p.resolve().parents
                               and p.resolve() != home})}
        if creds.is_file():
            # bound in place, not copied: an OAuth refresh inside a trial must
            # reach the real file (Claude Code writes it in place when the
            # rename onto a mount point fails)
            lay["creds"] = creds.resolve()
        own = {real_cfg.resolve(), (Path.home() / ".claude").resolve()}

        def bindable(d: Path) -> bool:
            # a host path to show read-only: never HOME itself, above it, or
            # Claude Code's own data
            d = d.resolve()
            return d != home and d not in home.parents and d != Path("/") \
                and not any(o == d or o in d.parents or d in o.parents for o in own)

        real = Path(os.path.realpath(self._claude_bin()))
        try:
            with open(real, "rb") as f:
                head = f.read(256)
        except OSError:
            head = b""
        native = head[:4] == b"\x7fELF"
        if native:
            # a single-file native build can run from anywhere
            lay["bin"] = (real, base / "pbin" / "claude")
        else:
            # a script or JS install keeps its own path, its package dir and
            # its interpreter's install (often under HOME: nvm, mise, volta)
            lay["binds"].append(str(real.parent))
            if head[:2] == b"#!":
                words = head[2:].split(b"\n", 1)[0].decode("utf-8", "replace").split()
                interp = words[1] if words and words[0].endswith("/env") and len(words) > 1 \
                    else (words[0] if words else "")
                found = shutil.which(interp) if interp else None
                if found:
                    prefix = Path(os.path.realpath(found)).parent.parent
                    if home in prefix.parents and bindable(prefix):
                        lay["binds"].append(str(prefix))
        # PATH entries under HOME (toolchain shims and bins) stay usable
        for d in os.environ.get("PATH", "").split(os.pathsep):
            if d and os.path.isabs(d) and os.path.isdir(d):
                rd = Path(d).resolve()
                if home in rd.parents and bindable(rd):
                    lay["binds"].append(str(rd))
        # CA bundles the policy's TLS stack is pointed at
        for var in ("NODE_EXTRA_CA_CERTS", "SSL_CERT_FILE", "SSL_CERT_DIR",
                    "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE", "PIP_CERT", "GIT_SSL_CAINFO",
                    "AWS_CA_BUNDLE"):
            v = os.environ.get(var)
            if v and os.path.isabs(v) and os.path.exists(v) and bindable(Path(v)):
                lay["binds"].append(os.path.realpath(v))
        # a cloud provider's credentials, when the policy runs there
        cloud = []
        if os.environ.get("CLAUDE_CODE_USE_BEDROCK"):
            cloud += [os.environ.get("AWS_CONFIG_FILE"),
                      os.environ.get("AWS_SHARED_CREDENTIALS_FILE"), str(Path.home() / ".aws")]
        if os.environ.get("CLAUDE_CODE_USE_VERTEX"):
            cloud += [os.environ.get("GOOGLE_APPLICATION_CREDENTIALS"),
                      os.environ.get("CLOUDSDK_CONFIG"), str(Path.home() / ".config/gcloud")]
        if os.environ.get("CLAUDE_CODE_USE_FOUNDRY"):
            cloud += [str(Path.home() / ".azure")]
        for c in cloud:
            if c and os.path.exists(c) and bindable(Path(c)):
                lay["binds"].append(os.path.realpath(c))
        lay["binds"] = list(dict.fromkeys(lay["binds"]))
        covers = set()
        for d in os.environ.get("PATH", "").split(os.pathsep):
            c = Path(d or ".") / "claude"
            if c.is_file():
                covers.add(c if not c.is_symlink() else Path(os.path.realpath(c)))
        if native:
            covers.add(real)
        else:
            covers.discard(real)
        lay["covers"] = sorted(covers)
        if self.sandbox_network == "proxy":
            net = base / "net"
            net.mkdir(mode=0o700)
            allow = netproxy.default_allow() + list(self.sandbox_net_allow)
            # a gateway on a privileged port (or the proxy's) is reached on
            # another sandbox port: the trial's base URL says which
            trusted, forwards, lay["env"] = netproxy.gateways(reserved=(PROXY_PORT,))
            lay["net"] = netproxy.Proxy(net / "proxy.sock", allow, self.sandbox_net_ports,
                                        trusted=trusted, forwards=forwards,
                                        via=netproxy.upstream())
            py = None
            for cand in ("/usr/bin/python3", "/usr/local/bin/python3", "/bin/python3"):
                if os.path.isfile(cand) and home not in Path(os.path.realpath(cand)).parents:
                    py = cand
                    break
            if py is None:
                exe = Path(os.path.realpath(sys.executable))
                prefix = exe.parent.parent
                if not bindable(prefix):
                    raise SystemExit("claudecode domain: no python3 outside your home "
                                     "directory for the sandbox's network forwarder; "
                                     "install one or set sandbox_network: host")
                lay["binds"].append(str(prefix))
                py = str(exe)
            lay["python"] = py
        self._layout = lay
        return lay

    def _probe_layout(self, base: Path, shim: Path, ctx: dict) -> None:
        """Start the policy (`--version`) once in the run's real layout, so a
        layout that hides it (a policy under a temp or hidden dir, an
        interpreter outside the bound paths) stops the run with the reason
        instead of failing every trial."""
        probe = base / "probe"
        probe.mkdir()
        argv = self._bwrap_argv(str(probe), [str(probe)], [str(shim)], ctx) + \
            [self._policy_bin(ctx), "--version"]
        env = {k: v for k, v in os.environ.items() if k not in _SESSION_VARS}
        env["PATH"] = f"{shim}{os.pathsep}{env.get('PATH', '')}"
        try:
            r = subprocess.run(argv, capture_output=True, text=True, errors="replace",
                               timeout=120, stdin=subprocess.DEVNULL, env=env)
        except subprocess.TimeoutExpired:
            raise SystemExit("claudecode domain: the policy did not answer --version "
                             "in the sandbox within 120 s")
        if r.returncode != 0:
            raise SystemExit(f"claudecode domain: the policy does not start in the sandbox "
                             f"(exit {r.returncode}): {(r.stderr or r.stdout).strip()[-400:]}"
                             f"\n(a policy under a temp or hidden dir, or an interpreter "
                             f"outside sandbox_home / PATH; sandbox: none runs without it)")

    @staticmethod
    def _trial_config(lay: dict) -> Path | None:
        """A fresh private config dir for one trial (removed after it): the
        trials of a run share nothing through Claude Code's own files
        (transcripts, todos, settings a trial might write)."""
        tmpl = lay.get("template")
        if tmpl is None:
            return None
        d = Path(tempfile.mkdtemp(prefix="cfg-", dir=tmpl.parent))
        shutil.copy2(tmpl / ".claude.json", d / ".claude.json")
        if lay.get("creds") is not None:
            (d / ".credentials.json").touch(mode=0o600)
        return d

    def _bwrap_argv(self, ws: str, binds: list[str], ro_binds: list[str],
                    ctx: dict | None = None) -> list[str]:
        """bwrap: the host read-only; a fresh HOME (a tmpfs, the toolchain dirs
        of `sandbox_home` and PATH bound back read-only), fresh temp dirs, an
        empty /run (no session or system bus, systemd, docker, Wayland) and
        every other host unix socket masked; its own network namespace (with
        the proxy) so no abstract socket of the host (X11, D-Bus) is
        reachable; the repo, runs dir and worktree hidden; a private config
        dir; the trial's workspace and state dir writable. bwrap applies the
        mounts in order and takes every source from the host, so a later
        mount may cover an earlier source."""
        ctx = ctx if ctx is not None else {"hidden": self._hidden, "layout": self._layout}
        lay = ctx.get("layout") or {}
        home = Path.home().resolve()
        if home == Path("/") or not home.is_dir():
            raise SystemExit(f"claudecode domain: cannot sandbox with HOME={home}; "
                             f"set HOME to your home directory")
        argv = ["bwrap", "--die-with-parent", "--unshare-pid", "--unshare-ipc"]
        if lay.get("net") is not None:
            argv.append("--unshare-net")
        argv += ["--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc"]
        tmps = sorted(t for t in {Path("/tmp"), Path("/var/tmp"),
                                  Path(tempfile.gettempdir()).resolve()} if t.is_dir())
        for t in tmps:
            argv += ["--tmpfs", str(t)]
        # host daemons listen under /run (docker, the system and session
        # buses, systemd, containerd, snapd): none of them in a trial
        runs = {Path("/run"), Path("/var/run").resolve(), Path(f"/run/user/{os.getuid()}")}
        if os.environ.get("XDG_RUNTIME_DIR"):
            runs.add(Path(os.environ["XDG_RUNTIME_DIR"]).resolve())
        runs = sorted(r for r in runs if r.is_dir() and not r.is_symlink())
        runs = [r for r in runs if not any(o in r.parents for o in runs)]
        for r in runs:
            argv += ["--tmpfs", str(r)]
        back = []      # what of /run a trial needs: PATH dirs (NixOS), resolv.conf
        for d in os.environ.get("PATH", "").split(os.pathsep):
            if d.startswith("/run/") and os.path.isdir(d):
                top = Path(*Path(d).parts[:3])
                if top.name not in ("user", "wrappers"):
                    back.append(str(top))
        rc = os.path.realpath("/etc/resolv.conf")
        if rc.startswith("/run/") and os.path.isfile(rc):
            back.append(rc)
        for b in dict.fromkeys(back):
            argv += ["--ro-bind", b, b]
        argv += ["--tmpfs", str(home)]        # writable, empty, per trial
        for rel in self.sandbox_home:
            src = home / rel
            if src.exists():
                argv += ["--ro-bind", str(src), str(src)]
        for b in lay.get("binds", []):
            argv += ["--ro-bind", b, b]
        own = [Path(os.path.realpath(x)) for x in (
            os.environ.get("CLAUDE_CONFIG_DIR") or home / ".claude", home / ".claude",
            home / ".claude.json")]
        for p in self.sandbox_rw:
            rp = Path(os.path.realpath(p))
            # a writable bind of / , HOME, a temp or run dir, or Claude Code's
            # own data would undo the layout (other trials, the run's private
            # dir, transcripts); hidden paths are covered again below
            for x in (Path("/"), home, *tmps, *runs, *own):
                if rp == x or rp in x.parents:
                    raise SystemExit(f"claudecode domain: sandbox_rw entry {p} would expose "
                                     f"{x} to trials; name a narrower path")
            argv += ["--bind", p, p]
        # after sandbox_rw: a writable path never re-exposes what is hidden.
        # Under the fresh HOME, temp or run dirs a hidden path is gone already;
        # a tmpfs there would only leave an empty skeleton of its name
        fresh = [*tmps, *runs, home]
        shown = [Path(p) for p in (*lay.get("binds", []), *self.sandbox_rw, *back)] + \
            [home / rel for rel in self.sandbox_home]
        for h in ctx.get("hidden", []):
            if h == home or h in home.parents:
                raise SystemExit(f"claudecode domain: cannot sandbox: {h} (to hide) "
                                 f"contains your home directory")
            gone = any(f in h.parents for f in fresh) and \
                not any(e == h or e in h.parents or h in e.parents for e in shown)
            if h.is_dir() and not gone:
                argv += ["--tmpfs", str(h)]
        for h in lay.get("hide", []):
            argv += ["--tmpfs", h]
        # any other unix socket of the host still in view (a pathname socket
        # ignores a read-only mount): /dev/null over it
        covered = [Path(os.path.realpath(c)) for c in
                   (*ctx.get("hidden", []), *lay.get("hide", []))]
        shown_r = [Path(os.path.realpath(e)) for e in shown]
        masked = set()
        for bound in _host_sockets():
            # where the socket really is (it may be bound through /var/run,
            # /var/lock or a symlinked HOME): a mask needs an existing path
            sock = Path(os.path.realpath(bound.parent)) / bound.name
            if sock in masked or any(c in sock.parents for c in covered):
                continue
            if any(f in sock.parents for f in fresh) and \
                    not any(e == sock or e in sock.parents for e in shown_r):
                continue
            masked.add(sock)
            argv += ["--ro-bind", "/dev/null", str(sock)]
        if lay.get("shared") is not None:
            argv += ["--bind", str(lay["shared"]), str(lay["shared"])]
        if lay.get("cfg") is not None and ctx.get("cfg_src") is not None:
            argv += ["--bind", str(ctx["cfg_src"]), str(lay["cfg"])]
            if lay.get("creds") is not None:
                argv += ["--bind", str(lay["creds"]), str(lay["cfg"] / ".credentials.json")]
        net = None
        if lay.get("net") is not None:
            # read-only: a trial connects to the sockets, it never changes them
            net = Path(lay["net"].path).parent
            argv += ["--ro-bind", str(net), str(net)]
        if lay.get("bin") is not None:
            real, run_as = lay["bin"]
            argv += ["--ro-bind", str(real), str(run_as)]
        stub = Path(ro_binds[0]) / "claude" if ro_binds else None
        if stub is not None:
            for c in lay.get("covers", []):
                if c.parent.name == "versions" and home not in c.parents:
                    argv += ["--tmpfs", str(c.parent)]   # every other version too
            for c in lay.get("covers", []):
                argv += ["--ro-bind", str(stub), str(c)]
        for b in binds:
            argv += ["--bind", b, b]
        for b in ro_binds:
            argv += ["--ro-bind", b, b]
        argv += ["--chdir", ws]
        if net is not None:
            # inside: loopback forwarders to the proxy (and to a local model
            # gateway), then the policy
            pairs = [f"{PROXY_PORT}={lay['net'].path}"] + \
                [f"{port}={path}" for port, path in sorted(lay["net"].forwards.items())]
            argv += [lay["python"], "-I", "-c", netproxy.FORWARDER, *pairs, "--"]
        return argv

    def _policy_bin(self, ctx: dict | None = None) -> str:
        """The path the policy is started from inside the sandbox."""
        lay = (ctx or {}).get("layout") if ctx is not None else self._layout
        lay = lay or {}
        if lay.get("bin") is not None:
            return str(lay["bin"][1])
        return os.path.realpath(self._claude_bin()) if lay else self._claude_bin()

    # ---- Evaluate ----------------------------------------------------------
    def _check(self, task_dir: Path, ws: str, out_dir: Path | None = None,
               env_extra: dict | None = None) -> tuple[float, float | None, int, str]:
        """Run check.sh -> (reward, checker weight, rc, stdout+stderr). Output
        goes to files, so a process that inherited it cannot stall the trial."""
        script = (task_dir / "check.sh").resolve()
        own = out_dir is None
        out_dir = Path(tempfile.mkdtemp(prefix="rrsi-check-")) if own else Path(out_dir)
        so, se = out_dir / ".checker.out", out_dir / ".checker.err"
        env = {**_host_env(ws), "RRSI_WORKSPACE": ws, **(env_extra or {})}
        try:
            with open(so, "wb") as fo, open(se, "wb") as fe:
                proc = subprocess.Popen(["bash", str(script)], cwd=ws,
                                        stdin=subprocess.DEVNULL, stdout=fo, stderr=fe,
                                        env=env, start_new_session=True)
                try:
                    proc.wait(timeout=self.check_timeout_s)
                except subprocess.TimeoutExpired:
                    _killpg(proc)
                    proc.wait()
                    raise
                finally:
                    _killpg(proc)   # whatever the checker started dies with it
                    _sweep((env_extra or {}).get("RRSI_TRIAL_TOKEN"))
            stdout = so.read_bytes().decode("utf-8", "replace")
            out = stdout + se.read_bytes().decode("utf-8", "replace")
        finally:
            if own:
                shutil.rmtree(out_dir, ignore_errors=True)
            else:
                for f in (so, se):
                    f.unlink(missing_ok=True)
        reward, weight = self._parse_reward(stdout, proc.returncode)
        return reward, weight, proc.returncode, out

    @staticmethod
    def _parse_reward(stdout: str, rc: int) -> tuple[float, float]:
        line = ""
        for l in stdout.splitlines():
            if l.strip():
                line = l.strip()
        reward, weight = (1.0 if rc == 0 else 0.0), None
        if line:
            try:
                reward = float(line)
            except ValueError:
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    obj = None
                    if line.startswith("{"):
                        reward = 0.0     # a broken reward object never passes
                if isinstance(obj, dict) and "reward" in obj:
                    try:
                        reward = float(obj["reward"])
                    except (TypeError, ValueError):
                        reward = 1.0 if rc == 0 else 0.0
                    if obj.get("weight") is not None:
                        try:
                            weight = float(obj["weight"])
                        except (TypeError, ValueError):
                            weight = None
        if not math.isfinite(reward):
            reward = 0.0
        if weight is not None and not (math.isfinite(weight) and weight > 0):
            weight = None
        reward = max(0.0, min(1.0, reward))
        return reward, weight

    def _claude_bin(self) -> str:
        b = os.environ.get("RRSI_EVOLVE_POLICY_BIN", "claude")
        return shutil.which(b) or b

    def _pins(self, ws: str, state_dir: Path | None, token: str | None,
              path: str | None) -> dict:
        """Environment the harness may not override (also passed as --settings
        env, which outranks the project's env block)."""
        pins = {"RRSI_POLICY_ACTIVE": "1", "RRSI_EVOLVE_TRIAL": "1",
                # subagents run on the frozen policy model whatever their
                # frontmatter says
                "CLAUDE_CODE_SUBAGENT_MODEL": self.policy_model,
                "CLAUDE_CODE_SUBAGENT_MODEL_FORCE": "1",
                # no second (advisor) model next to the policy
                "CLAUDE_CODE_DISABLE_ADVISOR_TOOL": "1"}
        if state_dir is not None:
            pins["RRSI_HARNESS_STATE_DIR"] = str(state_dir)
        if token:
            pins["RRSI_TRIAL_TOKEN"] = token
        if path:
            pins["PATH"] = path
        if self.policy_effort:
            pins["CLAUDE_CODE_EFFORT_LEVEL"] = str(self.policy_effort)
        return pins

    def _policy_cmd(self, ws: str, pins: dict | None = None,
                    wrapper: list[str] | None = None, binary: str | None = None) -> list[str]:
        if wrapper is None:
            wrapper = [a.replace("{ws}", ws) for a in self.policy_wrapper]
        pins = self._pins(ws, None, None, None) if pins is None else pins
        cmd = [*wrapper, binary or self._claude_bin(), "-p", "--model", self.policy_model,
               "--output-format", "stream-json", "--verbose",
               "--permission-mode", self.permission_mode,
               "--setting-sources", "project,local",
               "--settings", pinned_settings(ws, pins),
               "--strict-mcp-config", "--no-session-persistence"]
        if self.policy_effort:
            cmd += ["--effort", str(self.policy_effort)]
        allowed = list(self.allowed_tools)
        mcp = Path(ws) / ".mcp.json"
        if mcp.exists():
            cmd += ["--mcp-config", str(mcp)]
            # harness MCP servers are harness tools: their calls would ask,
            # and -p denies every ask
            try:
                servers = (json.loads(mcp.read_text()).get("mcpServers") or {})
            except (json.JSONDecodeError, AttributeError, OSError):
                servers = {}
            allowed += [f"mcp__{re.sub(r'[^A-Za-z0-9_-]', '_', n)}"
                        for n in servers if isinstance(n, str)]
        cmd += ["--tools", ",".join(self.tools)]
        allowed = list(dict.fromkeys(allowed + ALWAYS_ALLOWED))
        cmd += ["--allowedTools", ",".join(allowed)]
        if self.disallowed_tools:
            cmd += ["--disallowedTools", ",".join(self.disallowed_tools)]
        if self.max_budget_usd_per_trial is not None:
            cmd += ["--max-budget-usd", str(self.max_budget_usd_per_trial)]
        cmd += [str(a) for a in self.extra_claude_args]
        return cmd

    def _policy(self, task: dict, ws: str, trial_dir: Path, state_dir: Path,
                shim: Path | None = None, ctx: dict | None = None) -> dict:
        """Run the policy -> {timed_out, exit_code, result_event}."""
        ctx = ctx if ctx is not None else {"hidden": self._hidden, "layout": self._layout}
        lay = ctx.get("layout") or {}
        claude_bin = self._claude_bin()
        if not shutil.which(claude_bin) and not os.path.isfile(claude_bin):
            return {"timed_out": False, "exit_code": None, "result": None,
                    "missing_bin": claude_bin}
        token = uuid.uuid4().hex
        sandboxed = self._sandbox_mode() == "bwrap"
        path = os.environ.get("PATH", "")
        if shim is not None:
            path = f"{shim}{os.pathsep}{path}"
        pins = self._pins(ws, state_dir, token, path)
        extra = {**pins, "PWD": ws}
        binary = None
        tcfg = None
        if sandboxed:
            extra["TMPDIR"] = "/tmp"
            if lay.get("cfg") is not None:
                extra["CLAUDE_CONFIG_DIR"] = str(lay["cfg"])
                tcfg = self._trial_config(lay)
                ctx = {**ctx, "cfg_src": tcfg}
            extra.update(lay.get("env") or {})
            if lay.get("net") is not None:
                url = f"http://127.0.0.1:{PROXY_PORT}"
                local = "localhost,127.0.0.1,::1"
                extra.update(HTTPS_PROXY=url, HTTP_PROXY=url, https_proxy=url,
                             http_proxy=url, NO_PROXY=local, no_proxy=local)
            ro = [str(shim)] if shim is not None else []
            wrapper = self._bwrap_argv(ws, [ws, str(state_dir)], ro, ctx)
            binary = self._policy_bin(ctx)
        else:
            wrapper = [a.replace("{ws}", ws) for a in self.policy_wrapper]
        cmd = self._policy_cmd(ws, pins, wrapper, binary)
        env = child_env(extra, self.env_passthrough)
        if sandboxed:
            for k in _SESSION_VARS:
                env.pop(k, None)
        with open(trial_dir / "stream.jsonl", "wb") as out, \
                open(trial_dir / "stderr.txt", "wb") as err:
            proc = subprocess.Popen(cmd, cwd=ws, stdin=subprocess.PIPE,
                                    stdout=out, stderr=err, env=env,
                                    start_new_session=True)
            live = ctx["procs"] if "procs" in ctx else ctx.setdefault("procs", set())
            live.add(proc)
            timed_out = False
            try:
                proc.communicate(task["prompt"].encode(), timeout=task["timeout_s"])
            except subprocess.TimeoutExpired:
                timed_out = True
                # SIGTERM first: Claude Code then stops its own detached shells
                _signal_group(proc, signal.SIGTERM)
                try:
                    proc.wait(timeout=TERM_GRACE_S)
                except subprocess.TimeoutExpired:
                    _killpg(proc)
                    try:
                        proc.wait(timeout=30)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        proc.wait()
            _killpg(proc)   # background jobs the agent started die with it
            _sweep(token)   # ... and so do its detached (setsid) processes
            live.discard(proc)
        if tcfg is not None:
            _set_writable(tcfg, True)
            shutil.rmtree(tcfg, ignore_errors=True)
        result, limited, sids = None, None, set()
        for ev in render._stream_events(trial_dir):
            if ev.get("session_id"):
                sids.add(str(ev["session_id"]))
            if ev.get("type") == "result":
                result = ev      # the last one: background agents add more
            elif ev.get("type") == "rate_limit_event":
                info = ev.get("rate_limit_info") or {}
                if info.get("status") not in (None, "allowed", "allowed_warning"):
                    limited = f"rate limit {info.get('status')} " \
                              f"({info.get('rateLimitType')})"
        tail = ""
        if result is None:
            try:
                tail = (trial_dir / "stderr.txt").read_bytes()[-300:].decode("utf-8", "replace")
            except OSError:
                pass
        return {"timed_out": timed_out, "exit_code": proc.returncode, "result": result,
                "rate_limited": limited, "session_ids": sorted(sids), "token": token,
                "stderr_tail": tail.strip()}

    def _result(self, task_id: str, j: int, task: dict, reward: float, weight,
                timed_out: bool, infra_error: str | None, exit_code, result_ev: dict | None,
                duration_s: float) -> dict:
        tokens = num_turns = cost = None
        subtype = None
        if result_ev is not None:
            # modelUsage is cumulative over the session and includes subagents;
            # `usage` covers only the last top-level segment
            mu = result_ev.get("modelUsage") or {}
            total = sum(int(m.get(f) or 0) for m in mu.values() if isinstance(m, dict)
                        for f in ("inputTokens", "outputTokens", "cacheReadInputTokens",
                                  "cacheCreationInputTokens"))
            if not total:
                u = result_ev.get("usage") or {}
                total = sum(int(u.get(f) or 0) for f in (
                    "input_tokens", "cache_creation_input_tokens",
                    "cache_read_input_tokens", "output_tokens"))
            tokens = total or None
            num_turns = result_ev.get("num_turns")
            cost = result_ev.get("total_cost_usd")
            subtype = result_ev.get("subtype")
        return {"task_id": task_id, "trial": j, "reward": reward,
                "weight": weight if weight is not None else task["weight"],
                "checker_weight": weight,
                "tokens": tokens, "timed_out": timed_out,
                "infra_error": infra_error, "exit_code": exit_code,
                "num_turns": num_turns, "cost_usd": cost,
                "duration_s": round(duration_s, 2), "subtype": subtype}

    def _save_result(self, trial_dir: Path, res: dict, ws: str | None = None) -> None:
        if self.keep_workspaces and ws:
            res = dict(res)
            res["workspace"] = ws
        (trial_dir / "result.json").write_text(json.dumps(res, indent=1))

    def _trial(self, root: Path, jdir: Path, task_id: str, j: int, task: dict,
               state_dir: Path | None = None, harness=None, base: Path | None = None,
               shim: Path | None = None, ctx: dict | None = None) -> None:
        trial_dir = jdir / f"{task_id}__{j}"
        trial_dir.mkdir(parents=True, exist_ok=True)
        t0 = time.monotonic()
        # kept workspaces live outside the run dir, which is removed after the job
        ws = tempfile.mkdtemp(prefix="rrsi-ws-", dir=str(base) if base is not None
                              and not self.keep_workspaces else None)
        saved = False
        sids: list[str] = []

        def finish(res: dict) -> None:
            nonlocal saved
            self._save_result(trial_dir, res, ws)
            saved = True

        try:
            if harness is None:
                harness = self._harness_entries(root)[0]
            sids = self._trial_body(root, jdir, task_id, j, task, ws, trial_dir, t0,
                                    state_dir or jdir / "state", finish, harness, shim,
                                    ctx) or []
        except Exception as e:  # noqa: BLE001 - one trial never sinks the evaluation
            if not saved:
                finish(self._result(task_id, j, task, 0.0, None, False,
                                    f"adapter error: {e!r}"[:300], None, None,
                                    time.monotonic() - t0))
        finally:
            if not self.keep_workspaces:
                shutil.rmtree(ws, ignore_errors=True)
            if not ((ctx or {}).get("layout") or {}).get("cfg"):
                # (a sandboxed policy wrote into the run's private config dir)
                for d in session_residue(ws, sids):
                    shutil.rmtree(d, ignore_errors=True)

    def _trial_body(self, root, jdir, task_id, j, task, ws, trial_dir, t0,
                    state_dir, finish, harness, shim, ctx=None) -> list[str]:
        task_dir = self._task_dir(task_id)
        if not (task_dir / "check.sh").is_file():
            finish(self._result(task_id, j, task, 0.0, None, False,
                                f"no check.sh in {task_dir}", None, None,
                                time.monotonic() - t0))
            return []
        try:
            fixture = task_dir / "workspace"
            if fixture.is_dir():
                shutil.copytree(fixture, ws, dirs_exist_ok=True)
            self._install(harness, ws, merge=True)
        except Exception as e:  # noqa: BLE001
            finish(self._result(task_id, j, task, 0.0, None, False,
                                f"setup failed: {e!r}", None, None,
                                time.monotonic() - t0))
            return []

        setup = task_dir / "setup.sh"
        if setup.exists():
            try:
                r = subprocess.run(["bash", str(setup.resolve())], cwd=ws,
                                   capture_output=True, text=True, errors="replace",
                                   timeout=SETUP_TIMEOUT_S,
                                   env={**_host_env(ws), "RRSI_WORKSPACE": ws})
                if r.returncode != 0:
                    detail = f"setup failed (rc={r.returncode}): " \
                             f"{((r.stdout or '') + (r.stderr or ''))[-400:]}"
                    finish(self._result(task_id, j, task, 0.0, None, False,
                                        detail, r.returncode, None,
                                        time.monotonic() - t0))
                    return []
            except subprocess.TimeoutExpired:
                finish(self._result(task_id, j, task, 0.0, None, False,
                                    "setup failed (timeout)", None, None,
                                    time.monotonic() - t0))
                return []
        # what the workspace held before the agent ran, for checkers that
        # police stray files (outside the workspace: the agent never sees it)
        manifest = trial_dir / "pre_manifest.txt"
        manifest.write_text("".join(
            f"{os.path.relpath(os.path.join(d, f), ws)}\n"
            for d, _dirs, files in os.walk(ws) for f in sorted(files)))

        state_dir.mkdir(parents=True, exist_ok=True)
        run = self._policy(task, ws, trial_dir, state_dir, shim, ctx) if ctx is not None \
            else self._policy(task, ws, trial_dir, state_dir, shim)
        if (ctx or {}).get("aborting"):
            return run.get("session_ids") or []     # cut short: re-run on resume
        result_ev = run["result"]
        timed_out = run["timed_out"]
        if run.get("missing_bin"):
            finish(self._result(task_id, j, task, 0.0, None, False,
                                f"policy binary not found: {run['missing_bin']}",
                                None, None, time.monotonic() - t0))
            return []

        reward, weight = None, None
        try:
            reward, weight, rc, out = self._check(
                task_dir, ws, trial_dir,
                {"RRSI_PRE_MANIFEST": str(manifest), "RRSI_TRIAL_TOKEN": run["token"]})
            (trial_dir / "verifier.txt").write_text(out)
        except subprocess.TimeoutExpired:
            reward = 0.0
            (trial_dir / "verifier.txt").write_text("(checker timed out)\n")
        if reward is None:
            reward = 0.0

        res = self._result(task_id, j, task, reward, weight, timed_out,
                           self._infra_error(run), run["exit_code"], result_ev,
                           time.monotonic() - t0)
        foreign = self._foreign_models(result_ev, [
            e for e in render._stream_events(trial_dir)
            if e.get("type") == "system" and "fallback" in str(e.get("subtype"))])
        if foreign:
            # a harness that reached a stronger model scores nothing for it
            res["reward"] = 0.0
            res["policy_violation"] = f"models other than the policy's: {', '.join(foreign)}"
        finish(res)
        return run.get("session_ids") or []

    def _foreign_models(self, result_ev: dict | None, events=()) -> list[str]:
        """Models a session used that are neither the policy's (its tier, as
        the alias resolves: ANTHROPIC_DEFAULT_<TIER>_MODEL, ANTHROPIC_MODEL)
        nor Claude Code's small background model (a haiku), nor a fallback
        Claude Code itself switched to. Unknown when the policy model names
        no model family (a gateway's own name)."""
        mu = (result_ev or {}).get("modelUsage") or {}
        pm = str(self.policy_model).lower()
        fams = {f for f in ("haiku", "sonnet", "opus", "fable") if f in pm}
        if "opusplan" in pm:
            fams = {"opus", "sonnet"}       # Sonnet outside plan mode
        if not fams or not isinstance(mu, dict):
            return []
        fams.add("haiku")
        env = os.environ
        exact = {pm, *(str(env.get(v) or "").lower() for v in (
            "ANTHROPIC_MODEL", "ANTHROPIC_SMALL_FAST_MODEL",
            *(f"ANTHROPIC_DEFAULT_{f.upper()}_MODEL" for f in fams)))} - {""}
        for ev in events:
            # the server or Claude Code switched models (refusal, unavailable)
            for k in ("fallbackModel", "fallback_model"):
                if ev.get(k):
                    exact.add(str(ev[k]).lower())

        def ours(m, info) -> bool:
            names = [str(m).lower()]
            if isinstance(info, dict) and info.get("canonicalModel"):
                names.append(str(info["canonicalModel"]).lower())
            return any(n in exact or any(f in n for f in fams) for n in names)
        return sorted(str(m) for m, info in mu.items() if not ours(m, info))

    @staticmethod
    def _infra_error(run: dict) -> str | None:
        """Failures of the machinery (API, auth, limits, the CLI itself), which
        RRSI re-runs instead of scoring. A timeout or a max-turns / budget stop
        is the agent's own outcome, as in RRSI's coding domain; a checker
        timeout is the checker's."""
        result_ev, timed_out = run["result"], run["timed_out"]
        # a rejected rate-limit window matters only if the session then failed
        if run.get("rate_limited") and (result_ev is None or result_ev.get("is_error")):
            return run["rate_limited"]
        if result_ev is None:
            if timed_out:
                return None
            tail = run.get("stderr_tail") or ""
            return f"no result event (exit {run['exit_code']})" + (f": {tail}" if tail else "")
        if not result_ev.get("is_error"):
            return None
        text = " ".join([str(result_ev.get("result") or "")]
                        + [str(e) for e in (result_ev.get("errors") or [])])
        status = result_ev.get("api_error_status")
        if isinstance(status, int) and (status in (401, 403, 429) or status >= 500):
            return f"api error {status}: {text[:180]}"
        if result_ev.get("subtype") in _INFRA_SUBTYPES:
            return f"{result_ev.get('subtype')}: {text[:180]}"
        if _INFRA_TEXT.search(text):
            return text[:200]
        return None

    @staticmethod
    def _copy_state(src: Path, dst: Path) -> None:
        """Copy a state dir as is: symlinks stay links (a dangling one is
        fine), FIFOs, sockets and devices are dropped."""
        def special(d, names):
            return [n for n in names
                    if not (os.path.islink(os.path.join(d, n))
                            or os.path.isdir(os.path.join(d, n))
                            or os.path.isfile(os.path.join(d, n)))]
        shutil.copytree(src, dst, symlinks=True, ignore=special)

    def _persist_state(self, state: Path, jdir: Path) -> None:
        """Copy the live state dir to <job>/state (via state.new, then a swap),
        after every trial: a run killed mid-job keeps what finished trials
        learned."""
        persisted = jdir / "state"

        def rm(d: Path) -> None:
            if d.exists() or d.is_symlink():
                _set_writable(d, True)       # a read-only subdir would stay
                shutil.rmtree(d, ignore_errors=True)
            if d.exists():
                raise OSError(f"cannot remove {d}")
        with self._state_lock:
            try:
                if state.is_dir():
                    tmp, old = jdir / "state.new", jdir / "state.old"
                    rm(tmp)
                    self._copy_state(state, tmp)
                    rm(old)
                    if persisted.exists():
                        persisted.rename(old)
                    tmp.rename(persisted)
                    rm(old)
            except (OSError, shutil.Error) as e:
                _log(f"{jdir.name}: could not persist the job state: {e!r}"[:400])

    def run(self, root, runs_dir, job, ids, k, log_prefix=""):
        root, runs_dir = Path(root), Path(runs_dir)
        if self.repo is None:
            raise SystemExit("claudecode domain: no repo set (load_domain must set it)")
        jdir = runs_dir / "jobs" / job
        jdir.mkdir(parents=True, exist_ok=True)
        persisted = jdir / "state"
        if not persisted.exists() and (jdir / "state.old").is_dir():
            (jdir / "state.old").rename(persisted)      # killed mid-swap
        persisted.mkdir(parents=True, exist_ok=True)
        tasks = self._tasks()
        self._add_task_patterns(tasks)
        ids = list(ids)
        hidden = sorted({Path(self.repo).resolve(), runs_dir.resolve(), root.resolve()})
        self._hidden = hidden
        _log(f"{log_prefix or job}: {len(ids)} tasks x k={k} -> {jdir}")
        _clean_stale_runs()
        # everything the policy may touch lives in one private dir per run,
        # flocked while the run lives (see _clean_stale_runs)
        base = _run_base()
        owner = os.open(base / ".owner", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        _flock(owner)
        os.write(owner, f"{os.getpid()}\n".encode())
        state = base / "state"
        # per-run context (several jobs may run at once on this domain)
        ctx: dict = {"hidden": hidden, "layout": {}, "procs": set()}
        try:
            self._check_committed(root)
            harness, digest = self._snapshot(root)
            dig = jdir / "harness.sha256"
            done = any(_load_result(td) for td in jdir.iterdir() if td.is_dir()
                       and _TRIAL_DIR.fullmatch(td.name))
            if done and dig.exists() and dig.read_text().strip() != digest:
                raise RuntimeError(
                    f"job {job}: the harness differs from the one its finished trials "
                    f"ran with; remove {jdir} or use another job name")
            dig.write_text(digest + "\n")
            self._copy_state(persisted, state)
            shim = base / "bin"
            shim.mkdir()
            (shim / "claude").write_text(
                "#!/bin/sh\necho 'rrsi-evolve: claude cannot be called inside a trial "
                "(the policy model is frozen)' >&2\nexit 126\n")
            (shim / "claude").chmod(0o755)
            if self._sandbox_mode() == "bwrap":
                ctx["layout"] = self._prepare_sandbox(base, shim)
                self._probe_layout(base, shim, ctx)

            def one(tid: str, j: int) -> None:
                if ctx.get("aborting"):
                    return
                td = jdir / f"{tid}__{j}"
                res = _load_result(td)
                if res is not None and not res.get("infra_error"):
                    return                      # finished: resume-safe skip
                spec = tasks.get(tid) or {"prompt": "", "timeout_s": self.task_timeout_s,
                                          "weight": 1.0}
                try:
                    self._trial(root, jdir, tid, j, spec, state, harness, base, shim, ctx)
                finally:
                    self._persist_state(state, jdir)

            pairs = [(t, j) for t in ids for j in range(k)]
            with ThreadPoolExecutor(max_workers=max(1, self.concurrency)) as ex:
                try:
                    list(ex.map(lambda ij: one(*ij), pairs))
                except BaseException:
                    # interrupted (SIGTERM, Ctrl-C): stop the running policies
                    # now; their trials are left unscored and re-run on resume
                    ctx["aborting"] = True
                    for proc in list(ctx["procs"]):
                        _signal_group(proc, signal.SIGTERM)
                    raise
        finally:
            px = (ctx.get("layout") or {}).get("net")
            if px is not None:
                if px.denied:
                    _log(f"{log_prefix or job}: the sandbox proxy refused "
                         f"{', '.join(sorted(px.denied)[:8])} (sandbox_net_allow adds hosts)")
                px.close()
            if self._layout is ctx.get("layout"):
                self._layout = {}
            _set_writable(base, True)
            shutil.rmtree(base, ignore_errors=True)
            os.close(owner)

    def _trials(self, runs_dir: Path, job: str) -> dict[str, list[Path]]:
        """Trial dirs (<task>__<n>) per task, in trial-number order (not
        lexicographic); the task comes from the dir name, never from a
        result.json's contents (the job's state dir is not a trial)."""
        by_task: dict[str, list[Path]] = {}
        jdir = Path(runs_dir) / "jobs" / job
        if not jdir.exists():
            return by_task
        for td in sorted(jdir.iterdir(), key=lambda d: (d.name.rsplit("__", 1)[0],
                                                          _trial_no(d))):
            if not td.is_dir() or not _TRIAL_DIR.fullmatch(td.name):
                continue
            if _load_result(td) is None:
                continue
            by_task.setdefault(td.name.rsplit("__", 1)[0], []).append(td)
        return by_task

    def _counted(self, dirs: list[Path]) -> list[tuple[Path, dict]]:
        out = []
        for td in dirs:
            res = _load_result(td) or {}
            if not res.get("infra_error"):
                out.append((td, res))
        return out

    @staticmethod
    def _checker_weight(res: dict):
        # results written before checker_weight existed stored it in weight
        cw = res["checker_weight"] if "checker_weight" in res else res.get("weight")
        return float(cw) if cw is not None else None

    def score(self, runs_dir, job, ids, k):
        runs_dir = Path(runs_dir)
        if self.repo is None:
            raise SystemExit("claudecode domain: no repo set (load_domain must set it)")
        tasks = self._tasks()
        self._add_task_patterns(tasks)
        trials = self._trials(runs_dir, job)
        wfile = runs_dir / "checker_weights.json"
        try:
            remembered = {str(k): float(v) for k, v in json.loads(wfile.read_text()).items()
                          if isinstance(v, (int, float)) and v > 0}
        except (OSError, ValueError, AttributeError):
            remembered = {}
        before = dict(remembered)
        per, total_pass, total_cost = {}, 0, 0.0
        for t in ids:
            w = float((tasks.get(t) or {}).get("weight", 1.0))
            dirs = trials.get(t, [])[:k]
            counted = self._counted(dirs)     # infra trials count as missing
            rewards = [float(res.get("reward") or 0.0) for _, res in counted]
            # a checker may weight its own trial ({"reward", "weight"}); a
            # missing trial weighs what a measured one would (RRSI: the task's
            # full criteria count), so losing a heavy trial never raises S
            cws = [self._checker_weight(res) for _, res in counted]
            known = [cw for cw in cws if cw is not None]
            # a counted trial whose checker reported no weight (it timed out or
            # crashed) and a missing one weigh what the checker reported in this
            # job, else what it last reported for the task, else the task's weight
            if known:
                fill = remembered[t] = max(known)
            else:
                fill = remembered.get(t) or w
            weights = [cw if cw is not None else fill for cw in cws]
            toks = [res.get("tokens") for _, res in counted]
            total_cost += sum(float(res.get("cost_usd") or 0.0) for _, res in counted)
            missing = k - len(rewards)
            rewards += [0.0] * missing
            weights += [fill] * missing
            toks += [None] * missing
            passes = sum(1 for x in rewards if x == 1.0)
            total_pass += passes
            per[t] = TaskResult(rewards=rewards, weights=weights, tokens=toks,
                                missing=missing,
                                extra={"passes": passes,
                                       "trial_dirs": [str(d) for d, _ in counted]})
        if remembered != before and runs_dir.is_dir():
            _save_weights(wfile, {t: remembered[t] for t in remembered
                                  if remembered[t] != before.get(t)})
        return per, {"total_passes": total_pass, "n_trials": len(ids) * k,
                     "pass_rate": total_pass / max(1, len(ids) * k),
                     "cost_usd": round(total_cost, 4)}

    # ---- evidence ----------------------------------------------------------
    def load_trial(self, runs_dir, job, task_id, trial):
        """`trial` is a position in score()'s reward list (counted trials in
        trial-number order), which is what Loop.build_traces passes."""
        counted = self._counted(self._trials(runs_dir, job).get(task_id, []))
        if not 0 <= int(trial) < len(counted):
            return None                     # a missing slot: nothing to render
        td, res = counted[int(trial)]
        task = self._tasks().get(task_id) or {}
        return {"task_id": task_id, "trial_dir": str(td),
                "prompt": task.get("prompt", ""), "reward": res.get("reward"),
                "turns": res.get("num_turns"), "tokens": res.get("tokens"),
                "timed_out": res.get("timed_out"),
                "exception": None}

    def render_trace(self, rec, detail=False):
        return render.render_full(rec, detail=detail)

    def task_row(self, task_id, rec, tr):
        return (f"{task_id} | rewards={tr.rewards if tr else '?'} | "
                f"rendered_trial_reward={rec.get('reward')} | "
                f"turns={rec.get('turns')} | timed_out={rec.get('timed_out')} | "
                f"tokens={rec.get('tokens')}")

    # ---- gates -------------------------------------------------------------
    def smoke(self, root, runs_dir, job, ids):
        root, runs_dir = Path(root), Path(runs_dir)
        static = self._smoke_static(root)
        if static is not None:
            return static
        jdir = runs_dir / "jobs" / job
        _set_writable(jdir, True)
        subprocess.run(["rm", "-rf", str(jdir)], check=False)
        self.run(root, runs_dir, job, ids, 1, log_prefix=job)
        want_skills, want_agents = self._declared(root)
        found, details = 0, {}
        for tid in ids:
            td = jdir / f"{tid}__0"
            res = _load_result(td)
            if res is None:
                details[tid] = "no trial / no result.json"
                continue
            if res.get("infra_error"):
                details[tid] = f"infra: {str(res.get('infra_error'))[:200]}"
                continue
            if res.get("timed_out"):
                details[tid] = "timed out"
                continue
            if res.get("policy_violation"):
                details[tid] = res["policy_violation"]
                continue
            events = render._stream_events(td)
            result = [e for e in events if e.get("type") == "result"]
            if not result:
                details[tid] = "no result event in stream.jsonl"
                continue
            if result[-1].get("is_error"):
                details[tid] = f"error result: {result[-1].get('subtype')} " \
                               f"{str(result[-1].get('result') or '')[:160]}"
                continue
            init = next((e for e in events if e.get("type") == "system"
                         and e.get("subtype") == "init"), None)
            if init is not None:
                # Claude Code drops a skill/agent with bad frontmatter silently
                lost = [f"skill {n}" for n in want_skills
                        if n not in _names(init.get("skills"))]
                lost += [f"agent {n}" for n in want_agents
                         if n not in _names(init.get("agents"))]
                # a harness MCP server that does not start in a trial (often:
                # it downloads itself, and the sandbox has no such network)
                status = {str(m.get("name")): str(m.get("status"))
                          for m in init.get("mcp_servers") or [] if isinstance(m, dict)}
                lost += [f"MCP server {n} ({status.get(n, 'missing')})"
                         for n in self._declared_mcp(root)
                         if status.get(n) != "connected"]
                if lost:
                    details[tid] = "not loaded by Claude Code: " + ", ".join(lost)
                    if any(x.startswith("MCP") for x in lost) and \
                            self.sandbox_network == "proxy":
                        details[tid] += " (the sandbox reaches only allowed hosts: " \
                                        "sandbox_net_allow)"
                    continue
            found += 1
        if details:
            if found < len(ids):
                details["missing_trials"] = f"{found}/{len(ids)}"
            return False, {"stage": "smoke_run", **details}
        return True, {"stage": "smoke_run", "n": found}

    def _declared_mcp(self, root: Path) -> list[str]:
        f = (root / self.harness_path).resolve() / ".mcp.json"
        try:
            servers = json.loads(f.read_text()).get("mcpServers") or {}
        except (OSError, ValueError, AttributeError):
            return []
        return sorted(str(n) for n in servers) if isinstance(servers, dict) else []

    def _declared(self, root: Path) -> tuple[list[str], list[str]]:
        hdir = (root / self.harness_path).resolve()
        skills, agents = [], []
        for p in sorted(hdir.glob(".claude/skills/*/SKILL.md")):
            meta = _frontmatter(p.read_text(errors="replace")) or {}
            skills.append(str(meta.get("name") or p.parent.name))
        for p in sorted(hdir.glob(".claude/agents/*.md")):
            meta = _frontmatter(p.read_text(errors="replace")) or {}
            agents.append(str(meta.get("name") or p.stem))
        return skills, agents

    def _smoke_static(self, root: Path) -> tuple[bool, dict] | None:
        hdir = (root / self.harness_path).resolve()
        if not hdir.is_dir():
            return False, {"stage": "static",
                           "detail": f"no harness dir {hdir}"}

        def fail(detail):
            return False, {"stage": "static", "detail": detail}

        ignored = self._git_ls(hdir, "--others", "--ignored", "--exclude-standard")
        if ignored:
            return fail(f"harness files ignored by .gitignore are never committed or "
                        f"installed: {', '.join(sorted(ignored)[:10])}")
        entries, outside = self._harness_entries(root)
        if outside:
            return fail(f"harness symlinks must point inside the harness: "
                        f"{', '.join(outside[:5])}")
        rels = {rel for rel, _ in entries}
        fixtures = [self._task_dir(t) / "workspace" for t in sorted(self._tasks())] \
            if self.repo is not None else []
        fixtures = [f for f in fixtures if f.is_dir()]
        for fixture in fixtures:
            # instruction and settings files are merged with the fixture's
            clash = sorted(r for r in rels if (fixture / r).exists()
                           and (r not in MERGED_FILES or not (fixture / r).is_file()
                                or (fixture / r).is_symlink()))
            if clash:
                return fail(f"harness files would replace task fixture files: "
                            f"{', '.join(clash[:5])}")
        for rel, src in entries:
            if rel.endswith(".json"):
                try:
                    json.loads(src.read_text())
                except (json.JSONDecodeError, UnicodeDecodeError) as e:
                    return fail(f"{rel}: invalid JSON: {e}")
        by_rel = dict(entries)
        for name in ("settings.json", "settings.local.json"):
            settings = by_rel.get(f".claude/{name}")
            if settings is None:
                continue
            obj = json.loads(settings.read_text())
            if not isinstance(obj, dict):
                return fail(f".claude/{name}: not a JSON object")
            bad = _frozen_settings(obj)
            if bad:
                return fail(f".claude/{name}: {bad}")
            bad = _check_hooks(obj.get("hooks"), hdir, fixtures)
            if bad:
                return fail(f".claude/{name}: {bad} (Claude Code silently ignores "
                            f"invalid settings in -p)")
        for rel, src in entries:
            parts = Path(rel).parts
            if not rel.endswith(".md") or not {"agents", "skills", "commands"} & set(parts):
                continue
            text = src.read_text(errors="replace")
            why = _frozen_frontmatter(text)
            if why:
                return fail(f"{rel}: frontmatter sets {why}; the policy model and "
                            f"effort are frozen (use model: inherit or leave it out)")
            fm = _yaml_frontmatter(text)
            if isinstance(fm, dict) and fm.get("hooks") is not None:
                bad = _check_hooks(fm["hooks"], hdir, fixtures)
                if bad:
                    return fail(f"{rel}: frontmatter {bad}")
        for p in (sorted(hdir.glob("skills/*/SKILL.md"))
                  + sorted(hdir.glob(".claude/skills/*/SKILL.md"))
                  + sorted(hdir.glob("agents/*.md"))
                  + sorted(hdir.glob(".claude/agents/*.md"))):
            meta = _frontmatter(p.read_text(errors="replace"))
            if meta is None or "name" not in meta or "description" not in meta:
                return fail(f"{p.relative_to(hdir)}: frontmatter needs "
                            f"name: and description:")
        return None

    # extra parities with the base class -------------------------------------
    component_signals = [
        ("skill",           [r"(^|/)skills/", r"SKILL\.md"]),
        ("subagent",        [r"(^|/)agents/\S*\.md", r'"agents"\s*:']),
        ("client_tool",     [r"\.mcp\.json", r"mcpServers", r"(^|/)bin/", r"(^|/)tools/"]),
        ("memory",          [r"RRSI_HARNESS_STATE_DIR", r"(^|/)memory/", r"MEMORY\.md"]),
        ("context_mgmt",    [r"PreCompact", r"[Cc]ompact"]),
        ("output_plumbing", [r"PostToolUse", r"additionalContext", r"outputStyle",
                             r"output-styles/", r"statusLine"]),
        ("control_flow",    [r'"hooks"\s*:', r"PreToolUse", r"UserPromptSubmit",
                             r"SessionStart", r'"Stop"', r"SubagentStop", r"(^|/)hooks/"]),
        ("config",          [r"settings(\.local)?\.json", r'"permissions"', r'"env"\s*:']),
        ("prompt",          [r"CLAUDE\.md", r"AGENTS\.md", r"(^|/)commands/", r"\.md\b"]),
    ]


# Files a task fixture and the harness may both have: combined, not replaced
_MERGED_TEXT = frozenset({"CLAUDE.md", "CLAUDE.local.md", "AGENTS.md", ".claude/CLAUDE.md",
                          ".gitignore"})
_MERGED_JSON = frozenset({".claude/settings.json", ".claude/settings.local.json",
                          ".mcp.json"})
MERGED_FILES = _MERGED_TEXT | _MERGED_JSON


def _deep_merge(base, over):
    if isinstance(base, dict) and isinstance(over, dict):
        out = dict(base)
        for k, v in over.items():
            out[k] = _deep_merge(base[k], v) if k in base else v
        return out
    if isinstance(base, list) and isinstance(over, list):
        return base + [x for x in over if x not in base]
    return over


def _merge_file(rel: str, fixture: bytes, harness: bytes) -> bytes:
    """The fixture's file with the harness's added: text appended, JSON
    deep-merged (lists concatenated, the harness's scalars win)."""
    if rel in _MERGED_JSON:
        try:
            fx, hx = json.loads(fixture), json.loads(harness)
            merged = _deep_merge(fx, hx)
            if rel == ".mcp.json" and isinstance(fx, dict) and isinstance(hx, dict):
                # a server is one unit: the harness's entry replaces a same-named one
                servers = {**(fx.get("mcpServers") or {}), **(hx.get("mcpServers") or {})}
                merged["mcpServers"] = servers
            return (json.dumps(merged, indent=2) + "\n").encode()
        except (ValueError, TypeError, AttributeError):
            return harness
    sep = b"" if fixture.endswith(b"\n\n") else b"\n" if fixture.endswith(b"\n") else b"\n\n"
    return fixture + sep + harness


_TRIAL_DIR = re.compile(r".+__\d+")


def _flock(fd: int, block: bool = True) -> bool:
    try:
        import fcntl
    except ImportError:            # no flock: never treat a run dir as stale
        return False
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | (0 if block else fcntl.LOCK_NB))
        return True
    except OSError:
        return False


def _host_sockets() -> list[Path]:
    """Pathname unix sockets listening on this host (/proc/net/unix): a
    read-only mount does not stop a connect() to one."""
    import stat as _stat
    out = set()
    try:
        lines = Path("/proc/net/unix").read_text(errors="replace").splitlines()[1:]
    except OSError:
        return []
    for line in lines:
        parts = line.split(None, 7)
        if len(parts) < 8 or not parts[7].startswith("/"):
            continue
        p = Path(parts[7].strip())
        try:
            if _stat.S_ISSOCK(os.lstat(p).st_mode):
                out.add(p)
        except OSError:
            pass
    return sorted(out)


def _run_base() -> Path:
    """The run's private dir. The proxy's socket lives in it, and a unix
    socket path has at most 107 bytes: with a long TMPDIR, /tmp instead."""
    tmp = tempfile.gettempdir()
    if len(os.fsencode(tmp)) > 48 and os.path.isdir("/tmp") and os.access("/tmp", os.W_OK):
        tmp = "/tmp"
    return Path(tempfile.mkdtemp(prefix="rrsi-run-", dir=tmp))


def _clean_stale_runs() -> None:
    """Remove rrsi-run-* dirs left by runs that were killed (SIGKILL skips the
    cleanup): ours, with a regular .owner file nobody holds a lock on. A
    live run holds an flock on its .owner, which works across PID
    namespaces, unlike a pid check."""
    tmps = dict.fromkeys([Path(tempfile.gettempdir()), Path("/tmp")])
    for d in (d for t in tmps if t.is_dir() for d in t.glob("rrsi-run-*")):
        try:
            st = d.lstat()
            # a run that just started may not hold its lock yet
            if not d.is_dir() or d.is_symlink() or st.st_uid != os.getuid() \
                    or time.time() - st.st_mtime < 60:
                continue
            fd = os.open(d / ".owner", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        except OSError:
            continue
        try:
            import stat as _stat
            if not _stat.S_ISREG(os.fstat(fd).st_mode) or not _flock(fd, block=False):
                continue                      # not ours, or still running
        finally:
            os.close(fd)
        _set_writable(d, True)
        shutil.rmtree(d, ignore_errors=True)


_WEIGHTS_LOCK = threading.Lock()


def _save_weights(wfile: Path, updates: dict) -> None:
    """Merge per-task checker weights into <runs>/checker_weights.json (a
    cache: a failure to write it never fails scoring)."""
    with _WEIGHTS_LOCK:
        try:
            try:
                cur = json.loads(wfile.read_text())
                cur = cur if isinstance(cur, dict) else {}
            except (OSError, ValueError):
                cur = {}
            cur.update(updates)
            fd, tmp = tempfile.mkstemp(prefix=".checker_weights.", dir=str(wfile.parent))
            with os.fdopen(fd, "w") as f:
                f.write(json.dumps(cur, indent=1, sort_keys=True))
            os.replace(tmp, wfile)
        except OSError as e:
            _log(f"could not save {wfile.name}: {e!r}"[:200])


def _set_writable(top: Path, on: bool) -> None:
    """chmod a tree's owner write bits (the per-job harness snapshot is
    read-only while trials run)."""
    if not Path(top).is_dir():
        return
    for d, dirs, files in os.walk(top, topdown=not on):
        for n in files + dirs:
            p = os.path.join(d, n)
            if os.path.islink(p):
                continue
            try:
                m = os.stat(p).st_mode
                os.chmod(p, m | 0o200 if on else m & ~0o222)
            except OSError:
                pass
    try:
        m = os.stat(top).st_mode
        os.chmod(top, m | 0o200 if on else m & ~0o222)
    except OSError:
        pass


def _names(items) -> set[str]:
    out = set()
    for x in items or []:
        n = x if isinstance(x, str) else (x.get("name") if isinstance(x, dict) else None)
        if n:
            out.add(str(n))
            out.add(str(n).split(":")[-1])     # plugin-qualified names
    return out


def _frozen_settings(obj: dict) -> str | None:
    """Settings that change the frozen policy or widen permissions."""
    keys = [k for k in obj if k not in ("env", "hooks", "permissions")
            and _FROZEN_KEY.search(str(k))]
    perms = obj.get("permissions")
    if isinstance(perms, dict):
        keys += [f"permissions.{k}" for k in perms if _FROZEN_KEY.search(str(k))
                 and not str(k).lower().startswith("disable")]
    env = obj.get("env")
    if env is not None and not isinstance(env, dict):
        return "env must be an object"
    keys += [f"env.{k}" for k in (env or {}) if _FROZEN_ENV.search(str(k))]
    if keys:
        return (f"{', '.join(keys[:6])}: these change the frozen policy (model, effort, "
                f"thinking, advisor, fallback, plugins, auto-memory, provider/auth, "
                f"PATH, the evaluator's RRSI_* variables) or widen permissions")
    return None


_HOOK_SCRIPT = re.compile(
    r'^\s*(?P<interp>(?:bash|sh|python3?|node)\s+)?"?\$\{?CLAUDE_PROJECT_DIR\}?"?/'
    r'(?P<path>[^"\s]+)')


def _check_hooks(hooks, hdir: Path, fixtures=()) -> str | None:
    """The shape Claude Code accepts (event -> [{matcher?, hooks: [{type, ...}]}]),
    known events and types, no per-hook model, and that every harness script a
    command hook names exists and can run."""
    if hooks is None:
        return None
    if not isinstance(hooks, dict):
        return "hooks must be an object"
    for event, groups in hooks.items():
        if event not in HOOK_EVENTS:
            return f"hooks.{event}: unknown hook event (Claude Code drops it)"
        if not isinstance(groups, list):
            return f"hooks.{event} must be a list"
        for g in groups:
            if not isinstance(g, dict) or not isinstance(g.get("hooks"), list):
                return f"hooks.{event}: each entry needs a 'hooks' list"
            if "matcher" in g and not isinstance(g["matcher"], str):
                return f"hooks.{event}: matcher must be a string"
            for h in g["hooks"]:
                if not isinstance(h, dict) or not isinstance(h.get("type"), str):
                    return f"hooks.{event}: each hook needs a string 'type'"
                if h["type"] not in HOOK_TYPES:
                    return (f"hooks.{event}: hook type {h['type']!r} is not one of "
                            f"{', '.join(sorted(HOOK_TYPES))}")
                if "model" in h:
                    return f"hooks.{event}: a hook may not set 'model' (the policy is frozen)"
                if "timeout" in h and not isinstance(h["timeout"], (int, float)):
                    return f"hooks.{event}: timeout must be a number"
                if h["type"] != "command":
                    continue
                cmd = h.get("command")
                if not isinstance(cmd, str) or not cmd.strip():
                    return f"hooks.{event}: command hook needs a 'command' string"
                m = _HOOK_SCRIPT.match(cmd)
                if not m:
                    continue
                path = m.group("path")
                # only harness scripts: workspace tools (.venv/bin/python,
                # node_modules/.bin/..., gradlew) come from the task fixture
                first = Path(path).parts[0] if Path(path).parts else ""
                if not (path.startswith(".claude/") or (hdir / first).exists()):
                    continue
                target = hdir / path
                if not target.is_file():
                    if any((f / path).is_file() for f in fixtures):
                        continue              # a task's own tool
                    return f"hooks.{event}: {path} is not in the harness"
                if not m.group("interp") and \
                        not target.read_bytes().startswith(b"#!"):
                    return (f"hooks.{event}: {path} is run directly but "
                            f"has no #! line; call it as bash/python3 <path>")
    return None


def make_domain(repo, raw_cfg: dict | None = None) -> "ClaudeCodeDomain":
    dom = ClaudeCodeDomain(repo=repo, raw_cfg=raw_cfg)
    dom.root = HERE
    return dom
