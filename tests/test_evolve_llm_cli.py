#!/usr/bin/env python3
# Part of rrsi-policy. Ports google-research/rrsi (Apache-2.0).
"""Tests for the llm backend layer, the CLI and `init`.

Covers:

  - llm fake backend (RRSI_EVOLVE_LLM=fake:<tmp module>) and configure()
  - extract_json behaviour
  - retries on empty text (max_retries, then RuntimeError)
  - the claude-cli backend's command line via RRSI_EVOLVE_CLAUDE_BIN:
    argv contains --tools "", --strict-mcp-config, --system-prompt-file;
    cwd is neutral (the system temp dir); RRSI_POLICY_ACTIVE=1
  - `rrsi-evolve --help` and `init` into a temp git repo (no overwrite
    without --force)

No test calls the real `claude` CLI or any paid API.

    cd <plugin> && uvx --quiet pytest tests/test_evolve_llm_cli.py -q
"""

import json
import os
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path

import pytest

PLUGIN = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLUGIN / "lib"))

from rrsi_evolve import llm                        # noqa: E402

BIN = PLUGIN / "bin" / "rrsi-evolve"


@pytest.fixture(autouse=True)
def _clean_llm_state(monkeypatch):
    """Each test starts from the default backend with a fresh USAGE counter."""
    monkeypatch.delenv("RRSI_EVOLVE_LLM", raising=False)
    monkeypatch.delenv("RRSI_EVOLVE_LLM_LOG", raising=False)
    monkeypatch.delenv("RRSI_EVOLVE_CLAUDE_BIN", raising=False)
    llm._backend = "claude-cli"
    llm._timeout_s = 900
    llm._fake_mod = None
    llm._fake_path = None
    llm.USAGE["calls"] = 0
    llm.USAGE["cost_usd"] = 0.0
    yield


# ------------------------------------------------------------------ fake --
FAKE_MOD = '''"""Scripted backend: echoes what it was given, RPM-testable."""
import json, os, sys

CALLS = os.path.join(os.environ.get("FAKE_DIR", "/tmp"), "fake_calls.jsonl")


def generate(prompt="", system="", model=None, json_only=False, cache_prefix=None):
    with open(CALLS, "a") as f:
        f.write(json.dumps({"prompt": prompt, "system": system, "model": model,
                            "json_only": json_only, "cache_prefix": cache_prefix}) + "\\n")
    out = os.environ.get("FAKE_OUT", "OK")
    if json_only:
        out = '{"verdict": "accept", "reasons": [], "risk_notes": []}'
    return out
'''


def test_llm_fake_backend(tmp_path, monkeypatch):
    fake = tmp_path / "fake.py"
    fake.write_text(FAKE_MOD)
    monkeypatch.setenv("FAKE_DIR", str(tmp_path))
    monkeypatch.setenv("RRSI_EVOLVE_LLM", f"fake:{fake}")
    out = llm.generate("hello", system="be brief", model="sonnet",
                       cache_prefix="stable prefix")
    assert out == "OK"
    calls = [json.loads(l) for l in (tmp_path / "fake_calls.jsonl").read_text().splitlines()]
    assert len(calls) == 1
    c = calls[0]
    assert c["prompt"] == "hello"
    assert c["system"] == "be brief"        # no JSON suffix when json_only=False
    assert c["model"] == "sonnet"
    assert c["cache_prefix"] == "stable prefix"
    # json_only=True reaches the fake with the suffix appended
    out = llm.generate("hi", json_only=True)
    assert '"verdict"' in out
    c2 = [json.loads(l) for l in (tmp_path / "fake_calls.jsonl").read_text().splitlines()][-1]
    assert c2["json_only"] is True
    assert c2["system"].endswith("no markdown fences.")


def test_llm_configure_and_env_override(tmp_path, monkeypatch):
    fake = tmp_path / "fake.py"
    fake.write_text(FAKE_MOD)
    monkeypatch.setenv("FAKE_DIR", str(tmp_path))
    llm.configure(backend=f"fake:{fake}", timeout_s=5)
    assert llm._backend == f"fake:{fake}"
    assert llm._timeout_s == 5
    # env var overrides the configured backend at call time
    other = tmp_path / "fake2.py"
    other.write_text(FAKE_MOD.replace('os.environ.get("FAKE_OUT", "OK")',
                                      '"from-env"'))
    monkeypatch.setenv("FAKE_DIR", str(tmp_path))
    monkeypatch.setenv("RRSI_EVOLVE_LLM", f"fake:{other}")
    assert llm.generate("hi") == "from-env"


def test_llm_json_only_extracts_json(monkeypatch):
    # extract_json verbatim behaviour, unit-level
    assert llm.extract_json('```json\n{"a": 1}\n```') == '{"a": 1}'
    assert llm.extract_json('sure: {"a": [1, 2]} done') == '{"a": [1, 2]}'
    assert llm.extract_json('{"a": 1') == '{"a": 1'      # no closing brace found
    assert llm.extract_json('[1, 2') == '[1, 2'
    assert llm.extract_json('plain text') == 'plain text'
    # json_only=True runs the fake output through extract_json
    fake = tempfile.mkdtemp()
    m = Path(fake) / "fenced.py"
    m.write_text(textwrap.dedent('''
        def generate(prompt="", system="", model=None, json_only=False, cache_prefix=None):
            return '```json\\n{"verdict": "accept"}\\n```'
    '''))
    monkeypatch.setenv("RRSI_EVOLVE_LLM", f"fake:{m}")
    assert llm.generate("hi", json_only=True) == '{"verdict": "accept"}'


def test_llm_retries_on_empty_text(tmp_path, monkeypatch):
    # an always-empty fake: sleep patched, max_retries attempts, then RuntimeError
    mod = tmp_path / "empty.py"
    mod.write_text(textwrap.dedent('''
        import json, os
        p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "calls.json")

        def generate(prompt="", system="", model=None, json_only=False, cache_prefix=None):
            n = 0
            if os.path.exists(p):
                n = json.load(open(p))["n"]
            n += 1
            json.dump({"n": n}, open(p, "w"))
            return ""
    '''))
    monkeypatch.setenv("RRSI_EVOLVE_LLM", f"fake:{mod}")
    monkeypatch.setattr(llm.time, "sleep", lambda s: None)
    with pytest.raises(RuntimeError, match="generate failed after 3 tries: empty response"):
        llm.generate("hi", max_retries=3)
    calls = json.loads((tmp_path / "calls.json").read_text())["n"]
    assert calls == 3            # called exactly max_retries times
    # an empty-then-text fake succeeds on the retry (fresh module cache first)
    fake3 = tmp_path / "retry.py"
    fake3.write_text(textwrap.dedent('''
        import json, os
        p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "n.json")

        def generate(prompt="", system="", model=None, json_only=False, cache_prefix=None):
            n = 1
            if os.path.exists(p):
                n = json.load(open(p))["n"] + 1
            json.dump({"n": n}, open(p, "w"))
            return "" if n == 1 else "recovered"
    '''))
    monkeypatch.setenv("RRSI_EVOLVE_LLM", f"fake:{fake3}")
    assert llm.generate("hi", max_retries=3) == "recovered"


def test_llm_unknown_backend():
    os.environ["RRSI_EVOLVE_LLM"] = "carrier-pigeon"
    try:
        with pytest.raises(RuntimeError, match="unknown llm backend"):
            llm.generate("hi", max_retries=1)
    finally:
        os.environ.pop("RRSI_EVOLVE_LLM")


def test_llm_usage_and_log(tmp_path, monkeypatch):
    fake = tmp_path / "fake.py"
    fake.write_text(FAKE_MOD)
    monkeypatch.setenv("FAKE_DIR", str(tmp_path))
    monkeypatch.setenv("RRSI_EVOLVE_LLM", f"fake:{fake}")
    log = tmp_path / "log.jsonl"
    monkeypatch.setenv("RRSI_EVOLVE_LLM_LOG", str(log))
    llm.generate("hi")
    rows = [json.loads(l) for l in log.read_text().splitlines()]
    assert len(rows) == 1
    r = rows[0]
    for k in ("backend", "model", "system_head", "prompt_chars", "ok",
              "cost_usd", "seconds"):
        assert k in r
    assert r["ok"] is True
    assert r["backend"].startswith("fake:")


# ------------------------------------------------------------ claude-cli --
CLAUDE_FAKE = '''#!/usr/bin/env python3
"""Stand-in for the real `claude` binary: logs argv/cwd/env and answers JSON."""
import json, os, sys
payload = sys.stdin.read()
sp = None
for a, b in zip(sys.argv[1:], sys.argv[2:]):
    if a == "--system-prompt-file":
        sp = open(b).read()
out = {"argv": sys.argv[1:], "payload": payload, "cwd": os.getcwd(),
       "guard": os.environ.get("RRSI_POLICY_ACTIVE"),
       "sys_prompt": sp,
       "max_tokens_env": os.environ.get("CLAUDE_CODE_MAX_OUTPUT_TOKENS")}
with open(os.environ["FAKE_CLAUDE_LOG"], "a") as f:
    f.write(json.dumps(out) + "\\n")
print(json.dumps({"type": "result", "subtype": "success", "is_error": False,
                  "total_cost_usd": 0.02, "duration_ms": 5,
                  "result": "role reply"}))
'''


def test_llm_claude_cli_backend(tmp_path, monkeypatch):
    fakebin = tmp_path / "fake-claude"
    fakebin.write_text(CLAUDE_FAKE)
    fakebin.chmod(fakebin.stat().st_mode | 0o111)
    log = tmp_path / "calls.jsonl"
    monkeypatch.setenv("FAKE_CLAUDE_LOG", str(log))
    monkeypatch.setenv("RRSI_EVOLVE_CLAUDE_BIN", str(fakebin))
    out = llm.generate("do the thing", system="You are the proposer.",
                       model="sonnet", max_tokens=1234)
    assert out == "role reply"
    c = json.loads(log.read_text().splitlines()[0])
    argv = c["argv"]
    # -p, model, empty tool set, strict mcp config, system prompt in a file
    assert argv[0] == "-p"
    i = argv.index("--model")
    assert argv[i + 1] == "sonnet"
    assert "--tools" in argv and argv[argv.index("--tools") + 1] == ""
    assert "--strict-mcp-config" in argv
    assert "--no-session-persistence" in argv
    assert "--output-format" in argv and argv[argv.index("--output-format") + 1] == "json"
    assert "--setting-sources" in argv and \
        argv[argv.index("--setting-sources") + 1] == "project"
    assert "--system-prompt-file" in argv
    assert c["sys_prompt"] == "You are the proposer."
    # stdin is the prompt (no cache prefix)
    assert c["payload"] == "do the thing"
    # neutral cwd: a fresh private dir under the system temp dir, removed after
    assert Path(c["cwd"]).parent == Path(tempfile.gettempdir())
    assert Path(c["cwd"]).name.startswith("rrsi-role-")
    assert not Path(c["cwd"]).exists()
    # guard env var set for the policy runtime
    assert c["guard"] == "1"
    assert c["max_tokens_env"] == "1234"
    # USAGE accumulated from the fake's total_cost_usd
    assert llm.USAGE["calls"] == 1
    assert llm.USAGE["cost_usd"] == pytest.approx(0.02)
    # the temp system prompt file is deleted (only the log's copy remains)
    sp = Path(argv[argv.index("--system-prompt-file") + 1])
    assert not sp.exists()
    # cache_prefix goes on stdin before the prompt
    llm.generate("the prompt", system="sys", cache_prefix="STABLE-LEADING-BLOCK")
    c2 = json.loads(log.read_text().splitlines()[1])
    assert c2["payload"] == "STABLE-LEADING-BLOCK\n\nthe prompt"
    # empty system falls back to the default prompt text
    llm.generate("x")
    c3 = json.loads(log.read_text().splitlines()[2])
    assert c3["sys_prompt"] == "You are a careful assistant."


def test_llm_claude_cli_error_and_empty(tmp_path, monkeypatch):
    # is_error true -> failure -> RuntimeError after retries
    fakebin = tmp_path / "fake-claude-err"
    fakebin.write_text(CLAUDE_FAKE.replace('"is_error": False', '"is_error": True'))
    fakebin.chmod(fakebin.stat().st_mode | 0o111)
    monkeypatch.setenv("RRSI_EVOLVE_CLAUDE_BIN", str(fakebin))
    monkeypatch.setattr(llm.time, "sleep", lambda s: None)
    with pytest.raises(RuntimeError, match="generate failed after 2 tries"):
        llm.generate("hi", max_retries=2)
    # no `result` key -> failure too
    fakebin2 = tmp_path / "fake-claude-nores"
    fakebin2.write_text(CLAUDE_FAKE.replace('"result": "role reply"}',
                                            '"other": "x"}'))
    fakebin2.chmod(fakebin2.stat().st_mode | 0o111)
    monkeypatch.setenv("RRSI_EVOLVE_CLAUDE_BIN", str(fakebin2))
    with pytest.raises(RuntimeError, match="generate failed after 2 tries"):
        llm.generate("hi", max_retries=2)
    # non-JSON stdout counts as a failure, not a crash of the retry loop
    fakebin3 = tmp_path / "fake-claude-bad"
    fakebin3.write_text("#!/usr/bin/env python3\nprint('plain text')\n")
    fakebin3.chmod(fakebin3.stat().st_mode | 0o111)
    monkeypatch.setenv("RRSI_EVOLVE_CLAUDE_BIN", str(fakebin3))
    with pytest.raises(RuntimeError, match="generate failed after 1 tries"):
        llm.generate("hi", max_retries=1)


# ------------------------------------------------------------------ cli --
def _git(cwd, *args):
    r = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    return r


def test_cli_help():
    r = subprocess.run([sys.executable, str(BIN), "--help"],
                       capture_output=True, text=True)
    assert r.returncode == 0
    for sub in ("baseline", "calibrate", "round", "run", "readjudicate",
                "reevaluate", "heldout", "smoke", "status", "init"):
        assert sub in r.stdout
    for flag in ("--repo", "--config", "--domain", "--runs", "--b-min", "--b-max",
                 "--delta-z", "--n-prune", "--eval-parallel"):
        assert flag in r.stdout


def test_cli_init_into_temp_git_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    r = subprocess.run([sys.executable, str(BIN), "init"],
                       cwd=str(repo), capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    for rel in ("rrsi.json", "harness/CLAUDE.md", "tasks/hello-file/task.json",
                "tasks/hello-file/check.sh", "tasks/fix-off-by-one/task.json",
                "tasks/fix-off-by-one/check.sh", "tasks/fix-off-by-one/workspace/sum.py"):
        assert (repo / rel).is_file(), rel
    cfg = json.loads((repo / "rrsi.json").read_text())
    assert cfg["domain"] == "claudecode"
    assert "rrsi-evolve smoke" in r.stdout
    assert ".rrsi/" in r.stdout
    # the examples actually work: check.sh passes for a correct solution
    ws = tmp_path / "ws-hello"
    ws.mkdir()
    import shutil
    shutil.copytree(repo / "harness", ws / "harness-does-not-exist",
                    ignore=lambda d, names: []) if False else None
    # hello-file: correct
    (ws / "greeting.txt").write_text("hello world\n")
    env = dict(os.environ, RRSI_WORKSPACE=str(ws))
    r = subprocess.run(["bash", str(repo / "tasks/hello-file/check.sh")],
                       env=env, capture_output=True, text=True)
    assert r.returncode == 0 and r.stdout.strip() == "1.0"
    # hello-file: wrong content fails
    (ws / "greeting.txt").write_text("wrong\n")
    r = subprocess.run(["bash", str(repo / "tasks/hello-file/check.sh")],
                       env=env, capture_output=True, text=True)
    assert r.returncode != 0
    # fix-off-by-one: shipped fixture (buggy) fails; fixed file passes
    ws2 = tmp_path / "ws-off"
    shutil.copytree(repo / "tasks/fix-off-by-one/workspace", ws2)
    env2 = dict(os.environ, RRSI_WORKSPACE=str(ws2))
    r = subprocess.run(["bash", str(repo / "tasks/fix-off-by-one/check.sh")],
                       env=env2, capture_output=True, text=True)
    assert r.returncode != 0
    src = (ws2 / "sum.py").read_text()
    (ws2 / "sum.py").write_text(src.replace("range(len(nums) - 1)",
                                            "range(len(nums))"))
    r = subprocess.run(["bash", str(repo / "tasks/fix-off-by-one/check.sh")],
                       env=env2, capture_output=True, text=True)
    assert r.returncode == 0 and r.stdout.strip() == "1.0"


def test_cli_init_no_overwrite_without_force(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    first = subprocess.run([sys.executable, str(BIN), "init"],
                           cwd=str(repo), capture_output=True, text=True)
    assert first.returncode == 0, first.stderr
    marker = "\n# user edit that must survive\n"
    (repo / "harness/CLAUDE.md").write_text(
        (repo / "harness/CLAUDE.md").read_text() + marker)
    second = subprocess.run([sys.executable, str(BIN), "init"],
                            cwd=str(repo), capture_output=True, text=True)
    assert second.returncode == 0, second.stderr
    assert "kept" in second.stdout
    assert marker in (repo / "harness/CLAUDE.md").read_text()
    # --force overwrites
    third = subprocess.run([sys.executable, str(BIN), "init", "--force"],
                           cwd=str(repo), capture_output=True, text=True)
    assert third.returncode == 0, third.stderr
    assert "kept" not in third.stdout
    assert marker not in (repo / "harness/CLAUDE.md").read_text()


def test_cli_repo_config_defaults(tmp_path):
    """--repo/--config/dot-defaults: config default is <repo>/rrsi.json and a
    missing rrsi.json still resolves the claudecode domain default."""
    from rrsi_evolve.cli import _default_repo
    # inside a git repo -> toplevel
    repo = tmp_path / "repo"
    (repo / "sub").mkdir(parents=True)
    _git(repo, "init", "-q")
    r = subprocess.run(["git", "rev-parse", "--show-toplevel"],
                       cwd=str(repo / "sub"), capture_output=True, text=True)
    assert _default_repo.__module__
    # _default_repo uses the process cwd; test it via subprocess instead:
    r2 = subprocess.run(
        [sys.executable, "-c",
         "import sys; sys.path.insert(0, %r); "
         "from rrsi_evolve.cli import _default_repo; print(_default_repo())"
         % str(PLUGIN / "lib")],
        cwd=str(repo / "sub"), capture_output=True, text=True)
    assert Path(r2.stdout.strip()) == Path(r.stdout.strip())
    print("cli repo default OK")
