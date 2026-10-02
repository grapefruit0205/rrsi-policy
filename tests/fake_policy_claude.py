#!/usr/bin/env python3
# Part of rrsi-policy. Ports google-research/rrsi (Apache-2.0).
"""Stand-in for `claude -p --output-format stream-json --verbose` in tests.

Reads the task prompt from stdin, emits a stream-json transcript on stdout
(system/init, assistant tool_use, user tool_result, assistant text, result
with usage) and performs one action in its cwd chosen by FAKE_POLICY_MODE:

  solve      Write the file the checker wants (from the prompt line
             "FILE: <name>"): the task is then solved for real.
  write      Write FAKE_WRITE_FILE (default ./done.txt) with FAKE_WRITE_TEXT.
  fail       Do nothing (the checker will fail the task).
  hang       Sleep far longer than any test timeout (tests the kill path).
  crash      Exit 1 immediately, without a result event (infra failure).
  api_error  Emit a result event with is_error and "API Error: overloaded".
  CLAUDE.md marker: when the workspace CLAUDE.md contains the marker
  "<!-- FAKE-MARKER -->", the mode defaults to "write" with
  FAKE_MARKER_FILE (default ./marker.txt): loop tests can see a harness
  change matter without editing the fake.

Env knobs: FAKE_POLICY_MODE, FAKE_WRITE_FILE, FAKE_WRITE_TEXT,
FAKE_MARKER_FILE, FAKE_INIT_CWD, and for edge cases: FAKE_RESULT_JSON (merged
into the final result event), FAKE_PRE_EVENTS (a JSON list emitted before
it), FAKE_INIT_SKILLS / FAKE_INIT_AGENTS (comma lists for system/init),
FAKE_BG_PIDFILE (start a background `sleep` and write its pid there; with
FAKE_BG_SETSID=1 in its own session, as Claude Code's Bash tool does),
FAKE_ENV_DUMP (write env and argv as JSON), FAKE_PROBE (write, into the
workspace, what the policy can see: the FAKE_PROBE_PATHS that exist, /tmp,
the number of visible processes, and what a nested `claude` call returns).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time

SESSION = "f0000000-0000-4000-8000-000000000000"
MODEL = "claude-haiku-4-5-fake"


def emit(obj) -> None:
    print(json.dumps(obj), flush=True)


def line(text: str, step: int, input_tokens: int, output_tokens: int) -> None:
    emit({"type": "assistant", "message": {
        "model": MODEL, "id": f"msg_fake_{step}", "type": "message",
        "role": "assistant",
        "content": [{"type": "text", "text": text}],
        "stop_reason": "end_turn"},
        "session_id": SESSION,
        "usage": {"input_tokens": input_tokens, "output_tokens": output_tokens}})


def _socket_probe() -> dict:
    """Host daemons' unix sockets: none may answer inside a sandbox."""
    import socket
    out = {}
    for p in ("/run/docker.sock", "/var/run/docker.sock", "/run/dbus/system_bus_socket",
              f"/run/user/{os.getuid()}/bus", "/run/systemd/private"):
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            s.connect(p)
            out[p] = "connected"
        except OSError as e:
            out[p] = type(e).__name__
        finally:
            s.close()
    return out


def _net_probe() -> dict:
    """What the network looks like from here: a direct connection out, an
    abstract X11 socket, the proxy's answer for a host it must refuse."""
    import socket
    from urllib.parse import urlparse
    out = {"proxy": os.environ.get("HTTPS_PROXY")}
    try:
        socket.create_connection(("1.1.1.1", 443), timeout=3).close()
        out["direct"] = "connected"
    except OSError as e:
        out["direct"] = type(e).__name__
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.connect("\0/tmp/.X11-unix/X0")
        s.close()
        out["x11"] = "connected"
    except OSError as e:
        out["x11"] = type(e).__name__
    if out["proxy"]:
        u = urlparse(out["proxy"])
        try:
            s = socket.create_connection((u.hostname, u.port), timeout=5)
            s.sendall(b"CONNECT example.org:443 HTTP/1.1\r\nHost: example.org:443\r\n\r\n")
            out["proxy_denied"] = s.recv(64).split(b"\r\n")[0].decode()
            s.close()
        except OSError as e:
            out["proxy_denied"] = type(e).__name__
    return out


def main() -> int:
    if "--version" in sys.argv[1:]:
        print("0.0.0 (fake policy)")
        return 0
    prompt = sys.stdin.read()
    mode = os.environ.get("FAKE_POLICY_MODE", "solve")
    dump = os.environ.get("FAKE_ENV_DUMP")
    if dump:
        with open(dump, "w") as f:
            json.dump({"env": dict(os.environ), "argv": sys.argv[1:]}, f)
    cwd = os.environ.get("FAKE_INIT_CWD", os.getcwd())

    def _csv(name):
        return [x for x in os.environ.get(name, "").split(",") if x]
    emit({"type": "system", "subtype": "init", "cwd": cwd, "session_id": SESSION,
          "tools": ["Bash", "Read", "Edit", "Write"],
          "mcp_servers": json.loads(os.environ.get("FAKE_INIT_MCP") or "[]"),
          "model": MODEL, "permissionMode": "acceptEdits",
          "skills": _csv("FAKE_INIT_SKILLS"), "agents": _csv("FAKE_INIT_AGENTS")})
    pidfile = os.environ.get("FAKE_BG_PIDFILE")
    if pidfile:
        bg = subprocess.Popen(["sleep", "60"], stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL,
                              start_new_session=os.environ.get("FAKE_BG_SETSID") == "1")
        with open(pidfile, "w") as f:
            f.write(str(bg.pid))

    probe = os.environ.get("FAKE_PROBE")
    if probe:
        paths = [p for p in os.environ.get("FAKE_PROBE_PATHS", "").split(os.pathsep) if p]
        nested = subprocess.run(["claude", "-p", "hi"], capture_output=True, text=True) \
            if shutil.which("claude") else None
        home = os.path.expanduser("~")
        mark = os.path.join(home, f".rrsi-probe-{os.getpid()}")
        try:
            with open(mark, "w"):
                pass
            os.unlink(mark)
            home_writable = True
        except OSError:
            home_writable = False
        if os.environ.get("FAKE_PROBE_LEAK") == "1":     # sandboxed runs only
            try:
                with open(os.path.join(home, ".rrsi-probe-leak"), "w"):
                    pass
            except OSError:
                pass
        net = _net_probe()
        cfgd = os.environ.get("CLAUDE_CONFIG_DIR")
        cfg_seen = sorted(os.listdir(cfgd)) if cfgd and os.path.isdir(cfgd) else None
        if cfgd and os.environ.get("FAKE_PROBE_LEAK") == "1":
            try:
                with open(os.path.join(cfgd, f"trial-mark-{os.getpid()}"), "w"):
                    pass
            except OSError:
                pass
        cdir = os.path.join(home, ".claude")
        gcfg = os.path.join(home, ".claude.json")
        with open(probe, "w") as f:
            json.dump({"visible": {p: os.path.exists(p) for p in paths},
                       "home_writable": home_writable,
                       "config_dir": os.environ.get("CLAUDE_CONFIG_DIR"),
                       "claude_dir": sorted(os.listdir(cdir)) if os.path.isdir(cdir) else None,
                       "global_config": open(gcfg).read() if os.path.isfile(gcfg) else None,
                       "cache_writable": os.access(os.path.join(home, ".cache"), os.W_OK),
                       "tmp": sorted(os.listdir("/tmp")),
                       "nproc": sum(1 for d in os.listdir("/proc") if d.isdigit()),
                       "cwd": os.getcwd(), "pwd": os.environ.get("PWD"),
                       "nested_rc": nested.returncode if nested else None,
                       "nested_err": nested.stderr[:200] if nested else None,
                       "home_entries": sorted(os.listdir(home)),
                       "session_env": sorted(k for k in ("DBUS_SESSION_BUS_ADDRESS",
                                                        "XDG_RUNTIME_DIR", "DISPLAY",
                                                        "WAYLAND_DISPLAY", "SSH_AUTH_SOCK")
                                             if k in os.environ),
                       "bus_visible": os.path.exists(f"/run/user/{os.getuid()}/bus"),
                       "config_entries": cfg_seen,
                       "host_sockets": _socket_probe(),
                       **net}, f)

    claude_md = os.path.join(cwd, "CLAUDE.md")
    if mode == "solve" and os.path.exists(claude_md):
        try:
            with open(claude_md, errors="replace") as f:
                if "<!-- FAKE-MARKER -->" in f.read():
                    mode = "write"
                    os.environ.setdefault("FAKE_WRITE_FILE", "./marker.txt")
                    os.environ.setdefault("FAKE_WRITE_TEXT", "marker harness active")
        except OSError:
            pass

    # one tool call
    tool_id = "toolu_fake_1"
    if mode == "write":
        path = os.path.abspath(os.environ.get("FAKE_WRITE_FILE", "./done.txt"))
        text = os.environ.get("FAKE_WRITE_TEXT", "done")
        tool_name, tool_input = "Write", {"file_path": path, "content": text}
    else:
        # solve: find the FILE: <name> line in the prompt; default hello.txt
        target = "hello.txt"
        for tok in prompt.replace(":", " ").split():
            if tok.endswith(".txt"):
                target = tok.strip(",.;")
                break
        path = os.path.abspath(target)
        tool_name, tool_input = "Write", {"file_path": path, "content": "hi"}

    emit({"type": "assistant", "message": {
        "model": MODEL, "id": "msg_fake_0", "type": "message", "role": "assistant",
        "content": [{"type": "tool_use", "id": tool_id, "name": tool_name,
                     "input": tool_input}],
        "stop_reason": "tool_use"},
        "session_id": SESSION,
        "usage": {"input_tokens": 10, "output_tokens": 8}})

    # perform the action
    result_text = ""
    if mode == "solve" or mode == "write":
        try:
            with open(tool_input["file_path"], "w") as f:
                f.write(tool_input["content"])
            result_text = f"File created successfully at: {tool_input['file_path']}"
        except OSError as e:
            result_text = f"Error: {e}"
        # memory-lever substrate: the trial's shared state dir, when set
        state = os.environ.get("RRSI_HARNESS_STATE_DIR")
        if state:
            try:
                os.makedirs(state, exist_ok=True)
                with open(os.path.join(state, "state.jsonl"), "a") as f:
                    f.write(json.dumps({"tool_use_id": tool_id,
                                        "file": tool_input["file_path"]}) + "\n")
            except OSError:
                pass
    else:
        result_text = "Tool use denied by fake policy mode."

    emit({"type": "user", "message": {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": tool_id, "content": result_text}]},
        "session_id": SESSION})

    if mode == "hang":
        time.sleep(float(os.environ.get("FAKE_HANG_S", "600")))
        return 0
    if mode == "crash":
        # infra failure: die with no result event
        sys.stderr.write("fake policy crashed\n")
        return 1
    if mode == "api_error":
        emit({"type": "result", "subtype": "error_api", "is_error": True,
              "num_turns": 1, "total_cost_usd": 0.0,
              "usage": {"input_tokens": 5, "cache_creation_input_tokens": 0,
                        "cache_read_input_tokens": 0, "output_tokens": 1},
              "result": "API Error: overloaded.", "session_id": SESSION})
        return 0

    line("Done.", step=1, input_tokens=8, output_tokens=3)
    for ev in json.loads(os.environ.get("FAKE_PRE_EVENTS") or "[]"):
        emit(ev)
    final = {"duration_api_ms": 5, "stop_reason": "end_turn", "session_id": SESSION,
             "total_cost_usd": 0.01,
             "usage": {"input_tokens": 20, "cache_creation_input_tokens": 100,
                       "cache_read_input_tokens": 30, "output_tokens": 15},
             "modelUsage": {MODEL: {"costUSD": 0.01}},
             "permission_denials": [], "is_error": False, "num_turns": 2,
             "subtype": "success", "result": "Done.", "type": "result"}
    final.update(json.loads(os.environ.get("FAKE_RESULT_JSON") or "{}"))
    emit(final)
    return 0


if __name__ == "__main__":
    sys.exit(main())
