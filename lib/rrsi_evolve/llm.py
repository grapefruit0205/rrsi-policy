# Adapted from google-research/rrsi rrsi/llm.py (Copyright 2026 Google LLC, Apache-2.0).
"""LLM transport for the search roles (proposer, analyst, digester, critic).

RRSI called AnthropicVertex directly with retry-and-rotate over GCP projects.
rrsi-evolve keeps RRSI's `generate` signature and response handling behind a
backend switch:

  "claude-cli"      headless `claude -p` with an empty tool set and the role's
                    system prompt in a temp file. The prompt (plus the optional
                    `cache_prefix`) goes on stdin; stdout is one JSON object
                    whose `result` field is the text. cwd is a fresh private
                    temp dir (neutral: no project CLAUDE.md reaches the roles),
                    the environment is procenv.child_env (the parent
                    session's CLAUDE_* settings do not leak) and the
                    invocation is marked RRSI_POLICY_ACTIVE=1.
  "anthropic"       the Anthropic API, request shape identical to RRSI
                    (ephemeral cache_control on the cache_prefix block).
  "fake:<file.py>"  a scripted backend for tests: that module is imported once
                    and called as mod.generate(prompt, system, model,
                    json_only, cache_prefix).

Shared behaviour is unchanged from RRSI: retry up to max_retries with
time.sleep(min(2 ** attempt, 30)) between attempts, an empty response counts
as a failure, the JSON system suffix is appended when json_only, the text is
run through extract_json when json_only, and exhaustion raises RuntimeError.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time

from . import procenv

MODEL = os.environ.get("RRSI_SEARCH_MODEL", "opus")
MAX_TOKENS = 20_000
_JSON_SUFFIX = ("\n\nOutput ONLY a single valid JSON object. No prose before or "
                "after, no markdown fences.")

# Model aliases for the anthropic backend (claude-cli resolves them itself).
ALIASES = {"opus": "claude-opus-5-5", "sonnet": "claude-sonnet-5-5",
           "haiku": "claude-haiku-4-5-20251001"}

# Written to the temp system-prompt file when the caller passes no system.
_FALLBACK_SYSTEM = "You are a careful assistant."

# Accumulated policy/role spend of this process (claude-cli reports it).
USAGE = {"calls": 0, "cost_usd": 0.0}

_backend: str = "claude-cli"
_timeout_s: int = 900
_env_passthrough: tuple = ()
_fake_mod = None
_fake_path: str | None = None


def configure(backend: str | None = None, timeout_s: int | None = None,
              env_passthrough=None) -> None:
    """Set the backend, its timeout and the extra environment variables the
    claude-cli child keeps (procenv.child_env); the env var RRSI_EVOLVE_LLM
    still overrides the backend at call time. Cheap; idempotent."""
    global _backend, _timeout_s, _env_passthrough
    if backend is not None:
        _backend = backend
    if timeout_s is not None:
        _timeout_s = int(timeout_s)
    if env_passthrough is not None:
        _env_passthrough = tuple(env_passthrough)


def extract_json(text: str) -> str:
    t = text.strip()
    m = re.search(r"```(?:json)?\s*(.*?)```", t, re.S)
    if m:
        t = m.group(1).strip()
    if not t.startswith("{") and not t.startswith("["):
        start = min([i for i in (t.find("{"), t.find("[")) if i != -1], default=-1)
        if start != -1:
            t = t[start:]
    if t and t[0] == "{" and not t.endswith("}"):
        end = t.rfind("}")
        if end != -1:
            t = t[:end + 1]
    if t and t[0] == "[" and not t.endswith("]"):
        end = t.rfind("]")
        if end != -1:
            t = t[:end + 1]
    return t


def _current_backend() -> str:
    return os.environ.get("RRSI_EVOLVE_LLM") or _backend


def _fake_module(path: str):
    global _fake_mod, _fake_path
    if _fake_mod is None or _fake_path != path:
        spec = importlib.util.spec_from_file_location("rrsi_evolve_llm_fake", path)
        mod = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = mod
        spec.loader.exec_module(mod)
        _fake_mod, _fake_path = mod, path
    return _fake_mod


def _call_claude_cli(prompt: str, sys_prompt: str, mdl: str, max_tokens: int,
                     cache_prefix: str | None) -> tuple[str, float]:
    if not sys_prompt:
        sys_prompt = _FALLBACK_SYSTEM
    # a private cwd: a shared temp dir could hold someone's .claude/ or CLAUDE.md;
    # instruction files above it (e.g. /tmp/CLAUDE.md) are excluded by --settings
    cwd = tempfile.mkdtemp(prefix="rrsi-role-")
    path = os.path.join(cwd, "system.md")
    resp = None
    try:
        with open(path, "w") as f:
            f.write(sys_prompt)
        argv = [os.environ.get("RRSI_EVOLVE_CLAUDE_BIN", "claude"), "-p",
                "--model", mdl, "--tools", "", "--no-session-persistence",
                "--output-format", "json", "--strict-mcp-config",
                "--setting-sources", "project",
                "--settings", procenv.pinned_settings(cwd),
                "--system-prompt-file", path]
        stdin = cache_prefix + "\n\n" + prompt if cache_prefix else prompt
        env = procenv.child_env({"RRSI_POLICY_ACTIVE": "1", "PWD": cwd,
                                 "CLAUDE_CODE_MAX_OUTPUT_TOKENS": str(max_tokens)},
                                _env_passthrough)
        r = subprocess.run(argv, input=stdin, capture_output=True, text=True,
                           errors="replace", cwd=cwd, env=env, timeout=_timeout_s)
        try:
            resp = json.loads(r.stdout)
        except json.JSONDecodeError:
            resp = None
    finally:
        sid = resp.get("session_id") if isinstance(resp, dict) else None
        for d in [cwd, *procenv.session_residue(cwd, [sid])]:
            shutil.rmtree(d, ignore_errors=True)
    USAGE["calls"] += 1
    if not isinstance(resp, dict):
        raise RuntimeError(f"claude-cli: stdout is not a JSON object ({r.stderr[-200:]!r}): "
                           f"{r.stdout[:200]!r}")
    if resp.get("is_error") or "result" not in resp:
        raise RuntimeError(f"claude-cli: error response: {str(resp)[:400]}")
    cost = float(resp.get("total_cost_usd") or 0.0)
    USAGE["cost_usd"] += cost
    return str(resp["result"]), cost


def _call_anthropic(prompt: str, sys_prompt: str, mdl: str, max_tokens: int,
                    cache_prefix: str | None) -> tuple[str, float]:
    import anthropic  # imported lazily; optional dependency
    client = anthropic.Anthropic()
    content = ([{"type": "text", "text": cache_prefix,
                 "cache_control": {"type": "ephemeral"}},
                {"type": "text", "text": prompt}] if cache_prefix else prompt)
    kwargs = {"model": ALIASES.get(mdl, mdl), "max_tokens": max_tokens,
              "messages": [{"role": "user", "content": content}]}
    if sys_prompt:
        kwargs["system"] = sys_prompt
    resp = client.messages.create(**kwargs)
    text = "".join(b.text for b in resp.content
                   if getattr(b, "type", "") == "text")
    return text, 0.0        # the Anthropic API does not report cost


def _call_fake(prompt: str, sys_prompt: str, mdl: str, max_tokens: int,
               cache_prefix: str | None, json_only: bool) -> tuple[str, float]:
    del max_tokens
    backend = _current_backend()
    mod = _fake_module(backend[len("fake:"):])
    out = mod.generate(prompt=prompt, system=sys_prompt, model=mdl,
                       json_only=json_only, cache_prefix=cache_prefix)
    return out, 0.0


def _log_call(backend: str, mdl: str, sys_prompt: str, prompt_chars: int,
              ok: bool, cost_usd: float, seconds: float) -> None:
    log = os.environ.get("RRSI_EVOLVE_LLM_LOG")
    if not log:
        return
    rec = {"backend": backend, "model": mdl, "system_head": sys_prompt[:80],
           "prompt_chars": prompt_chars, "ok": bool(ok),
           "cost_usd": round(cost_usd, 6), "seconds": round(seconds, 3)}
    try:
        with open(log, "a") as f:
            f.write(json.dumps(rec) + "\n")
    except OSError:
        pass


def generate(prompt: str, system: str | None = None, max_retries: int = 6,
             json_only: bool = False, model: str | None = None,
             max_tokens: int = MAX_TOKENS, cache_prefix: str | None = None) -> str:
    mdl = model or MODEL
    sys_prompt = (system or "") + (_JSON_SUFFIX if json_only else "")
    backend = _current_backend()
    if backend != "claude-cli" and backend != "anthropic" \
            and not backend.startswith("fake:"):
        raise RuntimeError(f"unknown llm backend {backend!r} "
                           f"(claude-cli, anthropic, fake:<file.py>)")
    t0 = time.time()
    cost_seen = 0.0
    last_err: Exception | None = None
    for attempt in range(max_retries):
        try:
            if backend == "claude-cli":
                text, cost_seen = _call_claude_cli(prompt, sys_prompt, mdl,
                                                   max_tokens, cache_prefix)
            elif backend == "anthropic":
                text, cost_seen = _call_anthropic(prompt, sys_prompt, mdl,
                                                  max_tokens, cache_prefix)
            else:
                text, cost_seen = _call_fake(prompt, sys_prompt, mdl,
                                             max_tokens, cache_prefix, json_only)
            if text:
                _log_call(backend, mdl, sys_prompt, len(prompt), True,
                          cost_seen, time.time() - t0)
                return extract_json(text) if json_only else text
            last_err = RuntimeError("empty response")
        except Exception as e:  # noqa: BLE001 - any backend failure is retried
            last_err = e
        if attempt < max_retries - 1:
            time.sleep(min(2 ** attempt, 30))
    _log_call(backend, mdl, sys_prompt, len(prompt), False, cost_seen,
              time.time() - t0)
    raise RuntimeError(f"generate failed after {max_retries} tries: {last_err}")


if __name__ == "__main__":
    print(generate("Reply with exactly: OK", max_tokens=16))
