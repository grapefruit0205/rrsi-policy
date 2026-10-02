"""rrsi-evolve mine: recurring failures in your local Claude Code transcripts.

A task suite needs tasks the current harness fails, and most people don't know
offhand what those are. Their transcripts do: every "no, I said only check it"
is a failure the user had to catch. This reads the transcripts Claude Code
keeps under <config dir>/projects (<config dir> is $CLAUDE_CONFIG_DIR, else
~/.claude), finds the messages that correct the previous answer, sorts them
into failure types and prints, per type, a count, redacted excerpts and a task
idea for rrsi-evolve.

It runs locally: no model is called and nothing is sent anywhere. Excerpts are
redacted (keys, tokens, emails, the home path) and truncated, but they are
still the user's conversations, so the output file is written mode 0600.

Classification is keyword-based (English and Korean) and approximate. It finds
candidates; turning one into a task (a workspace and a check.sh) is still a
human decision.
"""

import collections
import json
import os
import re
import stat
import sys
import unicodedata
from datetime import datetime, timedelta, timezone
from pathlib import Path

MAX_USER_CHARS = 1500       # longer messages are pastes (logs, specs), not corrections
USER_EXCERPT = 240
ASSISTANT_EXCERPT = 200

# Failure types. Each user message following an answer is matched against
# every pattern; it can land in several types. "other" holds corrections that
# match only the generic pattern.
CATEGORIES = [
    ("scope", re.compile(
        r"하지 ?마|하지말|만들지 ?말|확인만|보기만|작성만|설명만|반영(은|하지) ?(말|아니|마)|"
        r"손대지|건드리지|바꾸지 ?마|지우지 ?마|고치지 ?마|병합하(지|면 안)|커밋하지 ?마|푸시하지 ?마|"
        r"\bdon'?t (change|touch|edit|create|apply|commit|push|delete|modify|run|merge|deploy)\b|"
        r"\bonly (check|look|read|review|explain|tell|answer)\b|\bjust (check|look|read|explain|tell|answer)\b|"
        r"\bi (didn'?t|did not) ask\b|\bwithout (changing|applying|editing)\b|\bread-only\b|"
        r"\bstop (editing|changing|touching)\b", re.I)),
    ("verify", re.compile(
        r"추정|추측|확인해 ?봐|확인부터|확인 ?안 ?했|확인했어\?|확인한 거야|근거가|근거는|확실해\?|"
        r"\bdid you (actually )?(check|verify|test|run|look)\b|\b(verify|check) (it )?first\b|"
        r"\bare you sure\b|\byou'?re guessing\b|\bdon'?t (guess|assume)\b|\bsource\?|\bevidence\b", re.I)),
    ("language", re.compile(
        r"한국어로|한글로|영어로 (쓰|답|말)하지|영어 ?말고|"
        r"\bwhy (english|not korean|don'?t (you )?write korean)\b|\b(reply|respond|answer|write) in [a-z]+\b|"
        r"\bin korean\b", re.I)),
    ("format", re.compile(
        r"비유|표 ?말고|표로 하지|목록으로|번호 ?(아래|로)|짧게|간단히 (해|써|정리|말)|"
        r"쉽게 (풀어|설명|써|정리|다시|말)|더 쉽게|쉽게[.!?]|너무 (길|어려)|길어|어렵다[.!?…]*$|"
        r"이해가 안|말투|낱말|용어 말고|"
        r"\btoo (long|verbose|technical)\b|\bshorter\b|\bsimpler\b|\bplain (english|language|words)\b|"
        r"\bno (tables?|metaphors?|analog(y|ies)|bullets?)\b|\bconcise\b", re.I)),
    ("misread", re.compile(
        r"내 ?말은|내가 원하는|그게 아니라|그 말이 아니|물어본 ?(건|거)|질문은|그런 뜻이 아니|원한 ?(건|거)|"
        r"\bi meant\b|\bwhat i meant\b|\bthat'?s not what i\b|\bi (was asking|asked) (about|for)\b|"
        r"\bmisunderst|\bnot what i asked\b", re.I)),
    ("repeat", re.compile(
        r"또 ?(안|에러|오류|그러|틀|실패|같은|이러|저러)|왜 또|여전히|아직도|왜 자꾸|"
        r"계속 (안|같은|에러|오류|실패)|같은 (에러|오류|실수|문제)|"
        r"\bagain\?|\bstill (fail|broken|not|doesn'?t|error|the same)|\bsame (error|mistake|problem)\b|"
        r"\bkeeps? (failing|happening|doing)\b", re.I)),
    ("broken", re.compile(
        r"안 ?되는데|안 ?되네|왜 안 ?(돼|되)|작동 ?안|동작 ?안|안 ?먹|"
        r"(에러|오류) ?(가 ?)?(나|떠|발생)|실패(했|하네|해요|함)|버그 ?(가 ?)?(있|나)|깨졌|"
        r"\bdoesn'?t work\b|\bnot working\b|\bbroken\b|\bfails\b|\bfailed\b|\bcrash(es|ed)?\b|"
        r"\b(still|same|got|getting|throws?|another|new) (an? )?error\b|\berror (again|still)\b", re.I)),
]
GENERIC = re.compile(
    r"^\s*(아니|ㄴㄴ)|아닌데|아니잖|틀렸|틀린|잘못|원래대로|되돌|롤백|"
    r"^\s*no\b(?!\s*(worries|problem|prob|need|thanks))|\bwrong\b|\bincorrect\b|\brevert\b|\bundo\b|\bthat'?s not\b", re.I)
WANTS_OTHER_LANGUAGE = re.compile(r"영어로|in english|translate", re.I)

SECRET = re.compile(
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(-----END [A-Z ]*PRIVATE KEY-----|$)|"
    r"\b(sk-[A-Za-z0-9_-]{8,}|gh[pousr]_[A-Za-z0-9]{8,}|github_pat_[A-Za-z0-9_]{8,}|"
    r"AKIA[A-Z0-9]{8,}|ASIA[A-Z0-9]{8,}|xox[abprs]-[A-Za-z0-9-]{8,}|AIza[A-Za-z0-9_-]{20,}|"
    r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_.-]+)|"
    r"\b[a-z][a-z0-9+.-]*://[^/\s:@]+:[^@\s/]+@|"                      # user:password@ in URLs
    r"(?i:authorization\s*[:=]\s*)(?i:basic|bearer|token)?\s*\S+|"
    r"(?i:bearer\s*[:=]?\s*)[A-Za-z0-9._~+/=-]{8,}|"
    r"(?i:(?:api[_-]?key|access[_-]?key|token|secret|password|passwd|pwd)\s*[:=]\s*)\S+|"
    r"(?i:\b(?:api[_-]?key|token|secret|password|passwd)\s+is\s+)\S+|"
    r"(?:비밀번호|비번|패스워드|암호)\s*(?:는|은|:|=)?\s*\S+", re.S)
EMAIL = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
PASTED = re.compile(r"<pasted_content\b[^>]*>.*?(</pasted_content>|$)", re.S)
# Claude Code's own placeholders, not something the model wrote
NOT_A_REPLY = re.compile(r"^\s*(\[Tool use interrupted\]|\[Request interrupted[^\]]*\]|API Error\b.*|"
                         r"\(keep-alive\)|You'?ve hit your .*limit.*|No response requested\.?)\s*$",
                         re.I | re.M)
MAX_SCAN_CHARS = 4000       # enough to tell a reply's language, cheap on long answers
CODE = re.compile(r"```.*?(```|$)|`[^`\n]*`|<[^<>\n]{1,200}>|https?://\S+", re.S)
SKIP_PREFIXES = ("<", "Caveat:", "This session is being continued")
INTERRUPT = "[Request interrupted by user"

TEXT = {
    "en": {
        "title": "rrsi-evolve mine: failure patterns in your Claude Code transcripts",
        "intro": ("Generated locally from {sessions} sessions in {projects} projects{since}. "
                  "No model was called and nothing was sent anywhere. Excerpts are redacted "
                  "and truncated, but they are still your conversations: review before sharing."),
        "since": " (last {days} days)",
        "approx": ("Counts come from keyword matching (English and Korean) and are approximate; "
                   "duplicates from resumed sessions are removed."),
        "projects": "Projects",
        "cols": "| project | sessions | your messages | corrections | interrupts | wrong-language answers |",
        "quiet": "{n} projects with no corrections, interrupts or wrong-language answers are not listed.",
        "types": "Failure types",
        "none": "No corrections found.",
        "task": "Task idea",
        "after": "after",
        "hidden": "headless (`claude -p`) sessions skipped: {n}; pass --include-headless to read them",
        "next": ("Next: pick 5 to 8 failures that recur, turn each into a task "
                 "(`task.json`, `workspace/`, a hidden `check.sh`; see examples/evolve-demo), "
                 "and set 1 or 2 aside as `\"split\": \"heldout\"`."),
    },
    "ko": {
        "title": "rrsi-evolve mine: Claude Code 대화 기록에서 찾은 실패 유형",
        "intro": ("{projects}개 프로젝트의 세션 {sessions}개를 이 컴퓨터에서 읽어 만들었습니다{since}. "
                  "모델을 부르지 않았고 어디로도 보내지 않았습니다. 발췌는 가리고 잘랐지만 "
                  "여전히 내 대화이니, 공유하기 전에 읽어 보세요."),
        "since": " (최근 {days}일)",
        "approx": "건수는 영어·한국어 키워드로 센 대략적인 값입니다. 이어 하기로 겹친 메시지는 뺐습니다.",
        "projects": "프로젝트",
        "cols": "| 프로젝트 | 세션 | 내 메시지 | 수정 요청 | 중단 | 다른 언어로 답함 |",
        "quiet": "수정 요청·중단·다른 언어 답변이 없는 프로젝트 {n}개는 표에서 뺐습니다.",
        "types": "실패 유형",
        "none": "수정 요청을 찾지 못했습니다.",
        "task": "태스크 아이디어",
        "after": "직전 답변",
        "hidden": "헤드리스(`claude -p`) 세션 {n}개는 건너뛰었습니다. 포함하려면 --include-headless",
        "next": ("다음 단계: 반복되는 실패 5~8개를 골라 태스크(`task.json`, `workspace/`, 숨긴 "
                 "`check.sh`, examples/evolve-demo 참고)로 만들고, 1~2개는 "
                 "`\"split\": \"heldout\"`으로 빼 두세요."),
    },
}

TYPES = {
    "scope": {
        "en": ("Did more than asked",
               "Ask for a check only (or code only, no apply). Put a fake `bin/terraform` / `bin/git` "
               "in the workspace that logs its calls. check.sh fails if files outside the expected one "
               "changed (compare with $RRSI_PRE_MANIFEST) or the log shows apply/push/create."),
        "ko": ("시킨 것보다 더 함",
               "확인만(또는 코드만, 반영은 하지 말라고) 시킵니다. 작업 폴더에 호출을 기록하는 가짜 "
               "`bin/terraform`, `bin/git`을 둡니다. check.sh는 정해진 파일 밖이 바뀌었거나"
               "($RRSI_PRE_MANIFEST와 비교) apply·push·create 기록이 있으면 0점을 줍니다."),
    },
    "verify": {
        "en": ("Answered from a guess instead of checking",
               "Workspace logs or config where the obvious guess is wrong. Ask for the cause in "
               "ANSWER.md. check.sh needs the real cause plus a file:line from the workspace, and "
               "scores 0 when only the common guess appears."),
        "ko": ("확인하지 않고 추정으로 답함",
               "흔한 추측과 실제 원인이 다른 로그나 설정을 작업 폴더에 둡니다. 원인을 ANSWER.md에 쓰게 "
               "합니다. check.sh는 실제 원인과 작업 폴더의 파일:줄 근거를 요구하고, 흔한 추측만 "
               "있으면 0점을 줍니다."),
    },
    "language": {
        "en": ("Answered in a different language than asked",
               "Ask in your language over an English-heavy workspace (README, error logs). check.sh "
               "measures the share of your script's letters in ANSWER.md and checks one fact."),
        "ko": ("물어본 언어와 다른 언어로 답함",
               "영어 README와 영어 에러 로그가 있는 작업 폴더에 내 언어로 묻습니다. check.sh는 "
               "ANSWER.md에서 내 언어 글자 비율과 사실 하나를 확인합니다."),
    },
    "format": {
        "en": ("Explanation in the wrong form",
               "Ask for the form you keep asking for (numbered list, no tables, no metaphors, at "
               "most N lines). check.sh tests the shape of ANSWER.md plus one fact from the workspace."),
        "ko": ("설명 형식이 맞지 않음",
               "자주 요구하는 형식(번호 목록, 표 없이, 비유 없이, N줄 이내)으로 설명을 시킵니다. "
               "check.sh는 ANSWER.md의 형식과 작업 폴더의 사실 하나를 확인합니다."),
    },
    "misread": {
        "en": ("Misread the request",
               "Reuse a request you had to rephrase, with the context that made it clear in the "
               "workspace. check.sh tests the outcome you meant."),
        "ko": ("요청을 잘못 이해함",
               "다시 설명해야 했던 요청을 그대로 쓰고, 뜻을 알 수 있는 맥락을 작업 폴더에 둡니다. "
               "check.sh는 원래 의도한 결과를 확인합니다."),
    },
    "repeat": {
        "en": ("Repeated the same failure",
               "A fixture where the obvious first attempt fails (a required flag, a wrong path). A "
               "wrapper logs the calls; check.sh scores 0 if the same failing call ran more than twice."),
        "ko": ("같은 실패를 반복함",
               "뻔한 첫 시도가 실패하는 상황(빠진 옵션, 틀린 경로)을 만듭니다. 감싼 명령이 호출을 "
               "기록하고, check.sh는 같은 실패 호출이 두 번 넘게 나오면 0점을 줍니다."),
    },
    "broken": {
        "en": ("The result didn't work",
               "Copy the project state from before the fix; the hidden test that proves it works is "
               "check.sh (pytest, npm test, terraform validate)."),
        "ko": ("결과가 동작하지 않음",
               "고치기 전 프로젝트 상태를 작업 폴더로 복사합니다. 동작을 증명하는 숨긴 테스트가 "
               "check.sh입니다(pytest, npm test, terraform validate 등)."),
    },
    "other": {
        "en": ("Other corrections",
               "Read the examples and keep the ones that recur."),
        "ko": ("기타 수정 요청",
               "예시를 읽고 반복되는 것만 고르세요."),
    },
}
ORDER = [name for name, _ in CATEGORIES] + ["other"]


def config_dir(env=None) -> Path:
    env = os.environ if env is None else env
    d = env.get("CLAUDE_CONFIG_DIR")
    return Path(d).expanduser() if d else Path.home() / ".claude"


def redact(text: str, limit: int, home: str = "", tail: bool = False) -> str:
    t = SECRET.sub("[REDACTED]", text)
    t = EMAIL.sub("[email]", t)
    if home:
        t = t.replace(home, "~")
    t = re.sub(r"\s+", " ", t).strip()
    if len(t) <= limit:
        return t
    return "…" + t[-limit:] if tail else t[:limit] + "…"


def _script(ch: str) -> str:
    if ch.isascii():
        return "LATIN"
    try:
        name = unicodedata.name(ch)
    except ValueError:
        return "?"
    head = name.split(" ")[0]
    if head in ("HIRAGANA", "KATAKANA"):
        return "KANA"
    return head


def scripts(text: str) -> collections.Counter:
    c = collections.Counter()
    for ch in CODE.sub(" ", text):
        if ch.isalpha():
            c[_script(ch)] += 1
    return c


def language_mismatch(user: str, reply: str) -> bool:
    """The user wrote in a non-Latin script and the reply is almost all Latin."""
    u = scripts(PASTED.sub(" ", user)[:MAX_SCAN_CHARS])
    total = sum(u.values())
    if total < 4 or WANTS_OTHER_LANGUAGE.search(user):
        return False
    script, n = u.most_common(1)[0]
    if script == "LATIN" or n * 2 < total:
        return False
    r = scripts(NOT_A_REPLY.sub(" ", reply)[-MAX_SCAN_CHARS:])
    rtotal = sum(r.values())
    return rtotal >= 40 and r[script] < 0.15 * rtotal


def _text_blocks(content):
    return [b["text"] for b in content
            if isinstance(b, dict) and b.get("type") == "text" and isinstance(b.get("text"), str)]


def _user_text(msg) -> str:
    c = msg.get("content")
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        return "\n".join(_text_blocks(c))
    return ""


def _assistant_text(msg) -> str:
    c = msg.get("content")
    if isinstance(c, list):
        return "\n".join(_text_blocks(c))
    return c if isinstance(c, str) else ""


def _last_reply(turn) -> str:
    """The last block the model wrote, skipping Claude Code's placeholders."""
    for block in reversed(turn):
        if NOT_A_REPLY.sub("", block).strip():
            return block
    return turn[-1]


def _when(ev):
    ts = ev.get("timestamp")
    if not isinstance(ts, str):
        return None
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _home_masked(path: str, home: str) -> str:
    if home and (path == home or path.startswith(home + os.sep)):
        return "~" + path[len(home):]
    return path


def _project_name(cwd, dirname: str, home: str) -> str:
    if isinstance(cwd, str) and cwd:
        return _home_masked(cwd, home)
    return dirname


def classify(text: str) -> list:
    hits = [name for name, rx in CATEGORIES if rx.search(text)]
    if not hits and GENERIC.search(text):
        hits = ["other"]
    return hits


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().lower()


def mine(projects_dir: Path, project: str = "", days=None, examples: int = 5,
         include_headless: bool = False, exclude_sessions=(), now=None) -> dict:
    home = str(Path.home())
    cutoff = None
    if days is not None:
        cutoff = (now or datetime.now(timezone.utc)) - timedelta(days=days)
    stats = collections.defaultdict(collections.Counter)
    counts = collections.Counter()
    samples = collections.defaultdict(list)
    seen = set()
    skipped_headless = 0
    sessions = 0

    files = sorted(projects_dir.glob("*/*.jsonl"))
    for f in files:
        if f.stem in exclude_sessions:
            continue
        try:
            fh = open(f, encoding="utf-8", errors="replace")
        except OSError:
            continue
        with fh:
            events = []
            entrypoint = None
            cwd = None
            for line in fh:
                try:
                    ev = json.loads(line)
                except (ValueError, RecursionError):
                    continue
                if not isinstance(ev, dict):
                    continue
                if not ev.get("isSidechain"):
                    entrypoint = entrypoint or ev.get("entrypoint")
                cwd = cwd or ev.get("cwd")
                events.append(ev)
        if not events:
            continue
        if entrypoint == "sdk-cli" and not include_headless:
            skipped_headless += 1
            continue
        proj = _project_name(cwd, f.parent.name, home)
        if project and project not in proj and project not in (cwd or ""):
            continue
        st = stats[proj]
        counted = False
        turn = []          # assistant text blocks since the last user message
        last_user = ""

        def check_language():
            """Was the reply in `turn` written in another script than `last_user`?"""
            reply = "\n".join(turn)
            if not (last_user and reply and language_mismatch(last_user, reply)):
                return False
            key = (proj, "language", _norm(reply)[:300])
            if key not in seen:
                seen.add(key)
                st["wrong_language"] += 1
                counts["language"] += 1
                if len(samples["language"]) < examples:
                    samples["language"].append({
                        "project": proj, "user": redact(last_user, USER_EXCERPT, home),
                        "after": redact(_last_reply(turn), ASSISTANT_EXCERPT, home, tail=True)})
            return True

        for ev in events:
            if ev.get("isSidechain"):
                continue
            t = ev.get("type")
            msg = ev.get("message") if isinstance(ev.get("message"), dict) else {}
            if t == "assistant":
                tx = _assistant_text(msg)
                if tx.strip():
                    turn.append(tx)
                continue
            if t != "user" or ev.get("isMeta"):
                continue
            text = _user_text(msg)
            if not text.strip():
                continue    # tool results: the turn goes on
            when = _when(ev)
            if cutoff and (when is None or when < cutoff):
                turn, last_user = [], ""
                continue
            if not counted:
                sessions += 1
                st["sessions"] += 1
                counted = True
            if INTERRUPT in text:
                # the interrupted reply stays the one the next message answers
                st["interrupts"] += 1
                continue
            if text.lstrip().startswith(SKIP_PREFIXES):
                continue
            mismatch = check_language()
            st["user_messages"] += 1
            text = PASTED.sub(" ", text)     # pasted logs and docs are not the user's words
            if turn and text.strip() and len(text) <= MAX_USER_CHARS:
                # a reply already counted as wrong-language isn't counted twice
                hits = [h for h in classify(text) if not (mismatch and h == "language")]
                key = (proj, "user", _norm(text), _norm(_last_reply(turn))[:300])
                if hits and key not in seen:
                    seen.add(key)
                    st["corrections"] += 1
                    sample = {"project": proj, "user": redact(text, USER_EXCERPT, home),
                              "after": redact(_last_reply(turn), ASSISTANT_EXCERPT, home, tail=True)}
                    for h in hits:
                        counts[h] += 1
                        if len(samples[h]) < examples:
                            samples[h].append(sample)
            turn, last_user = [], text
        if counted:
            check_language()   # the session's last reply

    projects = [{"project": p, **{k: st[k] for k in
                 ("sessions", "user_messages", "corrections", "interrupts", "wrong_language")}}
                for p, st in stats.items()
                if st["corrections"] or st["wrong_language"] or st["interrupts"]]
    projects.sort(key=lambda r: (-r["corrections"], -r["sessions"], r["project"]))
    return {
        "projects_dir": _home_masked(str(projects_dir), home),
        "sessions": sessions,
        "days": days,
        "skipped_headless": skipped_headless,
        "projects": projects,
        "quiet_projects": sum(1 for st in stats.values()
                              if st["sessions"] and not (st["corrections"] or st["wrong_language"]
                                                         or st["interrupts"])),
        "types": [{"type": name, "count": counts[name], "examples": samples[name]}
                  for name in sorted(ORDER, key=lambda n: (-counts[n], ORDER.index(n)))
                  if counts[name]],
    }


def _short(name: str, limit: int = 60) -> str:
    return name if len(name) <= limit else "…" + name[-(limit - 1):]


def _cell(s) -> str:
    return str(s).replace("|", "\\|")


def render_markdown(report: dict, lang: str = "en") -> str:
    tx = TEXT[lang]
    since = tx["since"].format(days=report["days"]) if report["days"] is not None else ""
    out = [f"# {tx['title']}", "",
           tx["intro"].format(sessions=report["sessions"],
                              projects=len(report["projects"]) + report["quiet_projects"],
                              since=since),
           "", tx["approx"]]
    if report["skipped_headless"]:
        out += ["", tx["hidden"].format(n=report["skipped_headless"])]
    out += ["", f"## {tx['projects']}", "", tx["cols"], "|---|---:|---:|---:|---:|---:|"]
    for r in report["projects"]:
        out.append(f"| {_cell(_short(r['project']))} | {r['sessions']} | {r['user_messages']} | "
                   f"{r['corrections']} | {r['interrupts']} | {r['wrong_language']} |")
    if report["quiet_projects"]:
        out += ["", tx["quiet"].format(n=report["quiet_projects"])]
    out += ["", f"## {tx['types']}", ""]
    if not report["types"]:
        out.append(tx["none"])
    for t in report["types"]:
        label, idea = TYPES[t["type"]][lang]
        out += [f"### {label} ({t['count']})", "", f"**{tx['task']}:** {idea}", ""]
        for ex in t["examples"]:
            out.append(f"- [{ex['project']}] \"{ex['user']}\"")
            out.append(f"  - {tx['after']}: \"{ex['after']}\"")
        out.append("")
    out += [tx["next"], ""]
    return "\n".join(out)


def run_cli(args) -> int:
    projects_dir = Path(args.projects_dir).expanduser() if args.projects_dir \
        else config_dir() / "projects"
    if not projects_dir.is_dir():
        print(f"no transcripts at {projects_dir} (set --projects-dir or CLAUDE_CONFIG_DIR)",
              file=sys.stderr)
        return 1
    if args.days is not None and args.days < 0:
        print("--days must be 0 or more", file=sys.stderr)
        return 2
    if args.examples < 0:
        print("--examples must be 0 or more", file=sys.stderr)
        return 2
    report = mine(projects_dir, project=args.project or "", days=args.days,
                  examples=args.examples, include_headless=args.include_headless,
                  exclude_sessions=set(args.exclude_session or ()))
    text = (json.dumps(report, ensure_ascii=False, indent=1) + "\n") if args.json \
        else render_markdown(report, args.lang)
    if args.out:
        out = Path(args.out).expanduser()
        if out.is_symlink():
            print(f"{out} is a symlink; not writing through it", file=sys.stderr)
            return 1
        try:
            # the report quotes the user's conversations: owner-only, even if the file existed
            fd = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_TRUNC
                         | getattr(os, "O_NOFOLLOW", 0), 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                if stat.S_ISREG(os.fstat(fh.fileno()).st_mode):
                    os.fchmod(fh.fileno(), 0o600)
                fh.write(text)
        except OSError as e:
            print(f"cannot write {out}: {e.strerror or e}", file=sys.stderr)
            return 1
        print(f"wrote {out} ({report['sessions']} sessions, "
              f"{sum(t['count'] for t in report['types'])} matches)", file=sys.stderr)
    else:
        sys.stdout.write(text)
    return 0
