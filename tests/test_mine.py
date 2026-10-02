#!/usr/bin/env python3
"""Tests for `rrsi-evolve mine` (lib/rrsi_evolve/mine.py).

Every transcript here is synthetic, written into tmp_path. No test reads the
real ~/.claude.

    cd <plugin> && uvx --quiet pytest tests/test_mine.py -q
"""

import json
import os
import stat
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

PLUGIN = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLUGIN / "lib"))

from rrsi_evolve import mine as M  # noqa: E402

NOW = datetime(2026, 10, 1, tzinfo=timezone.utc)


def _ts(days_ago=0):
    return (NOW - timedelta(days=days_ago)).isoformat().replace("+00:00", "Z")


def user(text, days_ago=0, **extra):
    return {"type": "user", "timestamp": _ts(days_ago),
            "message": {"role": "user", "content": text}, **extra}


def assistant(text, days_ago=0, **extra):
    return {"type": "assistant", "timestamp": _ts(days_ago),
            "message": {"role": "assistant", "content": [{"type": "text", "text": text}]},
            **extra}


def tool_result(days_ago=0):
    return {"type": "user", "timestamp": _ts(days_ago),
            "message": {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "t1", "content": "ok"}]}}


def write_session(root, project_dir, sid, events, cwd="/work/app", entrypoint="cli"):
    d = root / project_dir
    d.mkdir(parents=True, exist_ok=True)
    with open(d / f"{sid}.jsonl", "w") as fh:
        for ev in events:
            fh.write(json.dumps({"cwd": cwd, "entrypoint": entrypoint, "sessionId": sid, **ev},
                                ensure_ascii=False) + "\n")
        fh.write("not json\n")


def run(root, **kw):
    kw.setdefault("now", NOW)
    return M.mine(root, **kw)


def types(report):
    return {t["type"]: t["count"] for t in report["types"]}


def test_classifies_corrections_after_an_answer(tmp_path):
    write_session(tmp_path, "-work-app", "s1", [
        user("Explain the ALB setup"),
        assistant("I changed the listener and applied it."),
        user("I didn't ask you to change anything, only check it"),
        assistant("Sorry. Here is the current config."),
        user("are you sure? did you actually check the logs?"),
        assistant("Ich habe nachgesehen."),
        user("no, that's wrong"),
    ])
    r = run(tmp_path)
    t = types(r)
    assert t["scope"] == 1
    assert t["verify"] == 1
    assert t["other"] == 1
    proj = r["projects"][0]
    assert proj["project"] == "/work/app"
    assert proj["sessions"] == 1 and proj["user_messages"] == 4 and proj["corrections"] == 3


def test_first_message_and_ordinary_messages_are_not_corrections(tmp_path):
    write_session(tmp_path, "-work-app", "s1", [
        user("don't change anything, only check the config"),   # no answer before it
        assistant("Checked."),
        user("thanks, looks good"),
    ])
    r = run(tmp_path)
    assert r["types"] == []
    assert r["projects"] == []          # nothing to show
    assert r["quiet_projects"] == 1


def test_tool_results_do_not_end_the_turn(tmp_path):
    write_session(tmp_path, "-work-app", "s1", [
        user("check it"),
        assistant("Running the check."),
        tool_result(),
        assistant("Done, I also pushed the fix."),
        user("don't push without asking"),
    ])
    r = run(tmp_path)
    ex = r["types"][0]["examples"][0]
    assert r["types"][0]["type"] == "scope"
    assert "pushed the fix" in ex["after"]


def test_korean_patterns(tmp_path):
    write_session(tmp_path, "-work-app", "s1", [
        user("버킷 설정 확인해줘"),
        assistant("버킷을 새로 만들었습니다."),
        user("아니 만들지 말고 확인만 해줘"),
        assistant("원인은 아마 DB 같습니다."),
        user("추정 말고 확인해봐"),
        assistant("표로 정리했습니다."),
        user("표 말고 번호 아래 텍스트로 써줘"),
        assistant("다시 실행했습니다."),
        user("또 안되네 같은 에러야"),
        assistant("이렇게 했습니다."),
        user("내 말은 그게 아니라 콘솔에서 보이게 해달라는 거야"),
    ], cwd="/work/infra")
    t = types(run(tmp_path))
    assert t["scope"] == 1
    assert t["verify"] == 1
    assert t["format"] == 1
    assert t["repeat"] == 1
    assert t["misread"] == 1


def test_wrong_language_reply(tmp_path):
    write_session(tmp_path, "-work-app", "s1", [
        user("배포는 어떻게 해?"),
        assistant("You can deploy it with the release script; it builds and uploads."),
        user("응 그렇게 해줘"),
        assistant("한국어로 답합니다. 배포를 시작했습니다. `npm run release` 실행 중입니다."),
        user("영어로 다시 써줘"),           # asked for English: not a mismatch
        assistant("Here it is in English, as you asked, with all the steps listed."),
        user("고마워 계속 해줘"),
        assistant("[Tool use interrupted]"),
    ])
    r = run(tmp_path)
    assert types(r) == {"language": 1}
    ex = r["types"][0]["examples"][0]
    assert ex["user"] == "배포는 어떻게 해?"
    assert "release script" in ex["after"]
    assert r["projects"][0]["wrong_language"] == 1


def test_wrong_language_last_reply_of_session_counts(tmp_path):
    write_session(tmp_path, "-work-app", "s1", [
        user("로그 그룹 정리해줘"),
        assistant("I listed the log groups and their retention settings in the table below."),
    ])
    assert types(run(tmp_path)) == {"language": 1}


def test_code_does_not_count_as_wrong_language(tmp_path):
    write_session(tmp_path, "-work-app", "s1", [
        user("설정 파일 보여줘"),
        assistant("설정은 이렇습니다.\n```json\n{\"listener\": \"https\", \"stickiness\": false, "
                  "\"algorithm\": \"round_robin\"}\n```\n`terraform plan` 결과도 같습니다."),
    ])
    assert types(run(tmp_path)) == {}


def test_language_correction_not_counted_twice(tmp_path):
    write_session(tmp_path, "-work-app", "s1", [
        user("이거 설명해줘"),
        assistant("This explains how the cache works and why it expires after an hour."),
        user("한국어로 써줘"),
    ])
    assert types(run(tmp_path)) == {"language": 1}


def test_interrupt_counted_and_keeps_the_reply(tmp_path):
    write_session(tmp_path, "-work-app", "s1", [
        user("check the bucket"),
        assistant("Deleting the old bucket now."),
        user("[Request interrupted by user for tool use]"),
        user("don't delete anything"),
    ])
    r = run(tmp_path)
    assert r["projects"][0]["interrupts"] == 1
    assert types(r) == {"scope": 1}
    assert "Deleting" in r["types"][0]["examples"][0]["after"]


def test_dedup_across_resumed_sessions(tmp_path):
    events = [user("check it"), assistant("Applied."), user("I didn't ask you to apply")]
    write_session(tmp_path, "-work-app", "s1", events)
    write_session(tmp_path, "-work-app", "s2", events)
    r = run(tmp_path)
    assert types(r) == {"scope": 1}
    assert r["sessions"] == 2


def test_skips_meta_sidechain_system_text_and_long_pastes(tmp_path):
    write_session(tmp_path, "-work-app", "s1", [
        user("check it"),
        assistant("Done."),
        user("don't change it", isMeta=True),
        user("don't change it either", isSidechain=True),
        user("<command-name>/clear</command-name> don't"),
        user("This session is being continued... don't change"),
        user("error " * 400),
        user("<pasted_content id=\"x\">ERROR: build failed, wrong path</pasted_content>"),
    ])
    assert types(run(tmp_path)) == {}


def test_headless_sessions_skipped_unless_asked(tmp_path):
    events = [user("review this diff"), assistant("Rejected."), user("no, wrong verdict")]
    write_session(tmp_path, "-work-app", "s1", events, entrypoint="sdk-cli")
    r = run(tmp_path)
    assert r["skipped_headless"] == 1 and r["sessions"] == 0
    assert types(run(tmp_path, include_headless=True)) == {"other": 1}


def test_project_days_and_exclude_filters(tmp_path):
    write_session(tmp_path, "-work-app", "old", [
        user("check", days_ago=40), assistant("Applied.", days_ago=40),
        user("I didn't ask you to apply", days_ago=40)], cwd="/work/app")
    write_session(tmp_path, "-work-api", "new", [
        user("check"), assistant("Pushed."), user("don't push")], cwd="/work/api")
    assert types(run(tmp_path)) == {"scope": 2}
    assert types(run(tmp_path, days=30)) == {"scope": 1}
    assert types(run(tmp_path, project="/work/app")) == {"scope": 1}
    assert types(run(tmp_path, exclude_sessions={"new"})) == {"scope": 1}


def test_redaction():
    home = "/home/someone"
    t = M.redact("key sk-ant-abcdef123456 token=hunter2 mail me@example.com in "
                 "/home/someone/proj ghp_abcdefghijk AKIAABCDEFGHIJKL "
                 "Authorization: Bearer abc.def.ghi123", 500, home)
    for secret in ("sk-ant", "hunter2", "me@example.com", "/home/someone", "ghp_", "AKIA",
                   "abc.def.ghi123"):
        assert secret not in t
    assert "~/proj" in t
    assert M.redact("x" * 50, 10).endswith("…")
    assert M.redact("abcdef" * 10, 10, tail=True).startswith("…")
    key = "-----BEGIN RSA PRIVATE KEY-----\nMIIabc\n-----END RSA PRIVATE KEY-----"
    assert "MIIabc" not in M.redact(key, 500)


def test_home_project_name(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "h"))
    write_session(tmp_path / "p", "-x", "s1", [
        user("check"), assistant("Applied."), user("don't apply")],
        cwd=str(tmp_path / "h" / "repo"))
    assert run(tmp_path / "p")["projects"][0]["project"] == "~/repo"


def test_markdown_render_both_languages(tmp_path):
    write_session(tmp_path, "-work-app", "s1", [
        user("check"), assistant("Applied | done."), user("don't apply")], cwd="/work/a|b")
    r = run(tmp_path)
    en = M.render_markdown(r, "en")
    assert "Did more than asked (1)" in en and "Task idea" in en
    assert "/work/a\\|b" in en        # table cell escaped
    assert "nothing was sent anywhere" in en
    ko = M.render_markdown(r, "ko")
    assert "시킨 것보다 더 함 (1)" in ko and "어디로도 보내지 않았습니다" in ko
    (tmp_path / "none").mkdir()
    assert "No corrections found." in M.render_markdown(run(tmp_path / "none"), "en")


def _cli(*args, env=None):
    return subprocess.run([sys.executable, str(PLUGIN / "bin" / "rrsi-evolve"), "mine", *args],
                          capture_output=True, text=True, env=env, cwd="/")


def test_cli_out_file_is_private_and_json(tmp_path):
    root = tmp_path / "projects"
    write_session(root, "-work-app", "s1", [
        user("check"), assistant("Applied."), user("don't apply")])
    out = tmp_path / "report.md"
    out.write_text("old")
    os.chmod(out, 0o644)
    p = _cli("--projects-dir", str(root), "--out", str(out), "--lang", "ko")
    assert p.returncode == 0, p.stderr
    assert stat.S_IMODE(out.stat().st_mode) == 0o600
    assert "시킨 것보다 더 함 (1)" in out.read_text()
    p = _cli("--projects-dir", str(root), "--json")
    assert p.returncode == 0, p.stderr
    assert json.loads(p.stdout)["types"][0]["type"] == "scope"


def test_cli_uses_claude_config_dir_and_needs_no_repo(tmp_path):
    cfg = tmp_path / "cfg"
    write_session(cfg / "projects", "-work-app", "s1", [
        user("check"), assistant("Applied."), user("don't apply")])
    env = {**os.environ, "CLAUDE_CONFIG_DIR": str(cfg)}
    p = _cli("--json", env=env)        # cwd "/" is not a repo and has no rrsi.json
    assert p.returncode == 0, p.stderr
    assert json.loads(p.stdout)["sessions"] == 1


def test_cli_missing_dir_and_bad_args(tmp_path):
    p = _cli("--projects-dir", str(tmp_path / "nope"))
    assert p.returncode == 1 and "no transcripts" in p.stderr
    (tmp_path / "p").mkdir()
    assert _cli("--projects-dir", str(tmp_path / "p"), "--days", "-1").returncode == 2
    assert _cli("--projects-dir", str(tmp_path / "p"), "--examples", "-1").returncode == 2


def test_malformed_lines_and_blocks_do_not_crash(tmp_path):
    write_session(tmp_path, "-work-app", "s1", [
        user("check"),
        {"type": "assistant", "message": {"content": [{"type": "text", "text": None},
                                                      {"type": "text", "text": 3},
                                                      {"type": "text", "text": "Applied."}]}},
        {"type": "user", "message": {"content": [{"type": "text", "text": {"x": 1}}]}},
        {"type": "user", "message": "not a dict"},
        user("don't apply"),
    ])
    with open(tmp_path / "-work-app" / "s1.jsonl", "a") as fh:
        fh.write("[" * 100000 + "1" + "]" * 100000 + "\n")
    assert types(run(tmp_path)) == {"scope": 1}


def test_days_window_does_not_pair_old_question_with_new_text(tmp_path):
    write_session(tmp_path, "-work-app", "s1", [
        user("배포는 어떻게 해?", days_ago=40),
        assistant("You can deploy it with the release script; it builds and uploads.", days_ago=40),
        user("thanks"),
        {"type": "user", "message": {"content": "undated, don't count me"}},
    ])
    r = run(tmp_path, days=30)
    assert types(r) == {}
    assert r["projects"] == [] and r["sessions"] == 1


def test_fewer_false_positives():
    assert M.classify("No worries, that worked great, thanks!") == []
    assert M.classify("What does that error mean?") == []
    assert M.classify("502 에러 메시지 뜻이 뭐야?") == []
    assert "broken" in M.classify("still getting the same error")
    assert "broken" in M.classify("빌드하니까 에러가 나")
    assert M.classify("no, that's wrong") == ["other"]


def test_same_words_in_different_exchanges_count_twice(tmp_path):
    write_session(tmp_path, "-work-app", "s1", [
        user("check"), assistant("Applied the plan."), user("don't apply")])
    write_session(tmp_path, "-work-app", "s2", [
        user("check"), assistant("Pushed the branch."), user("don't apply")])
    assert types(run(tmp_path)) == {"scope": 2}


def test_more_secret_shapes():
    t = M.redact("redis://default:sup3rs3cret@10.0.0.5:6379/0 "
                 "Authorization: Basic dXNlcjpwYXNzd29yZA== Bearer: abc123def456ghi789 "
                 "the password is hunter2 비밀번호는 qwer1234", 500)
    for secret in ("sup3rs3cret", "dXNlcjpw", "abc123def456", "hunter2", "qwer1234"):
        assert secret not in t


def test_json_masks_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / "p").mkdir()
    assert run(tmp_path / "p")["projects_dir"] == "~/p"


def test_cli_out_refuses_symlink_and_reports_errors(tmp_path):
    root = tmp_path / "projects"
    write_session(root, "-work-app", "s1", [user("check"), assistant("Applied."), user("don't apply")])
    victim = tmp_path / "victim.md"
    victim.write_text("keep me")
    link = tmp_path / "link.md"
    link.symlink_to(victim)
    p = _cli("--projects-dir", str(root), "--out", str(link))
    assert p.returncode == 1 and "symlink" in p.stderr
    assert victim.read_text() == "keep me"
    p = _cli("--projects-dir", str(root), "--out", str(tmp_path / "missing" / "r.md"))
    assert p.returncode == 1 and "cannot write" in p.stderr and "Traceback" not in p.stderr
    p = _cli("--projects-dir", str(root), "--out", os.devnull)
    assert p.returncode == 0, p.stderr
