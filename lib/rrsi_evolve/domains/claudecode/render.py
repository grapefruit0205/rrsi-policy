# Part of rrsi-policy. Ports google-research/rrsi (Apache-2.0).
"""Render one Claude Code trial directory into the text form the analyst,
digester and proposer read: the task instruction, every assistant step
(analysis/plan text, tool calls, tool results), run metadata and the
checker's ground truth (verifier.txt).

The transcript is the `stream.jsonl` emitted by
`claude -p --output-format stream-json --verbose`: one JSON object per line
with types

  {"type": "system", "subtype": "init", ...}          session start
  {"type": "assistant", "message": {"content": [...]}} one model turn; blocks
      are {"type": "text", "text"}, {"type": "thinking", "thinking"} or
      {"type": "tool_use", "name", "input"}
  {"type": "user", "message": {"content": [{"type": "tool_result",
      "content": <str | [{"type": "text", "text"}, ...]>}}]}
  {"type": "rate_limit_event", ...}                    ignored
  {"type": "result", "usage", "num_turns", "total_cost_usd", "is_error",
   "subtype", "result"}                                final event
"""

from __future__ import annotations

import json
from pathlib import Path

TEXT_HEAD, TEXT_DETAIL = 2000, 12000
TOOL_IN_HEAD, TOOL_IN_DETAIL = 1500, 8000
TOOL_OUT_HEAD, TOOL_OUT_DETAIL = 2000, 20000
VERIFIER_HEAD, VERIFIER_DETAIL = 6000, 20000
MAX_CHARS = 120_000
DETAIL_CAPS = {"text": TEXT_DETAIL, "tool_in": TOOL_IN_DETAIL,
               "tool_out": TOOL_OUT_DETAIL, "verifier": VERIFIER_DETAIL}


def _clip(s, n):
    s = str(s)
    return s if len(s) <= n else s[:n] + f" ...[truncated {len(s) - n} chars]"


def _caps(detail: bool) -> dict:
    return DETAIL_CAPS if detail else {}


def _result_text(content) -> str:
    """A tool_result block's content is a string or a list of text parts."""
    if isinstance(content, str):
        return content
    parts = []
    for c in content or []:
        if isinstance(c, dict) and c.get("type") == "text" and c.get("text"):
            parts.append(str(c["text"]))
        elif isinstance(c, str):
            parts.append(c)
    return "\n".join(p for p in parts if p)


def _stream_events(trial_dir: Path) -> list:
    sj = trial_dir / "stream.jsonl"
    if not sj.exists():
        return []
    events = []
    for line in sj.read_text(errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            pass
    return events


def render_steps(events: list, text_head=TEXT_HEAD, tool_in_head=TOOL_IN_HEAD,
                 tool_out_head=TOOL_OUT_HEAD) -> str:
    """One `[step N]` per assistant message (stream-json emits one event per
    content block, sharing message.id); a TOOL_RESULT carries the step of the
    TOOL_USE it answers. Events of a subagent (parent_tool_use_id set) are
    tagged SUBAGENT(<id>) so they are not read as the policy's own steps."""
    lines, step = [], 0
    msg_step: dict = {}                 # (agent, message id) -> its step
    use_step: dict = {}                 # tool_use_id -> step
    for ev in events:
        typ = ev.get("type")
        parent = ev.get("parent_tool_use_id")
        who = f"SUBAGENT({str(parent)[-8:]}) " if parent else ""
        if typ == "assistant":
            msg = ev.get("message") or {}
            mid = msg.get("id")
            # a message's blocks can arrive around a subagent's events: they
            # keep the step their message got first
            if mid is None or (parent, mid) not in msg_step:
                step += 1
                if mid is not None:
                    msg_step[(parent, mid)] = step
            cur = step if mid is None else msg_step[(parent, mid)]
            for block in msg.get("content") or []:
                bt = block.get("type")
                if bt == "text":
                    if block.get("text"):
                        lines.append(f"[step {cur}] {who}ASSISTANT: "
                                     f"{_clip(block['text'], text_head)}")
                elif bt == "tool_use":
                    use_step[block.get("id")] = cur
                    lines.append(f"[step {cur}] {who}TOOL_USE {block.get('name')}: "
                                 f"{_clip(json.dumps(block.get('input') or {}), tool_in_head)}")
                elif bt == "thinking":
                    if block.get("thinking"):
                        lines.append(f"[step {cur}] {who}THINKING: "
                                     f"{_clip(block['thinking'], text_head)}")
                # empty thinking blocks (thinking == "") are skipped
        elif typ == "user":
            msg = ev.get("message") or {}
            content = msg.get("content")
            if parent and isinstance(content, str) and content.strip():
                step += 1               # a subagent's task prompt
                lines.append(f"[step {step}] {who}PROMPT: {_clip(content, text_head)}")
                continue
            blocks = content if isinstance(content, list) else []
            for block in blocks:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "tool_result":
                    n = use_step.get(block.get("tool_use_id"), step)
                    txt = _result_text(block.get("content"))
                    tag = "TOOL_RESULT (error): " if block.get("is_error") \
                        else "TOOL_RESULT: "
                    lines.append(f"[step {n}] {who}{tag}{_clip(txt, tool_out_head)}")
                elif parent and block.get("type") == "text" and block.get("text"):
                    step += 1
                    lines.append(f"[step {step}] {who}PROMPT: "
                                 f"{_clip(block['text'], text_head)}")
        # "system", "rate_limit_event" and other events are skipped
    text = "\n".join(lines)
    if len(text) > MAX_CHARS:
        head, tail = int(MAX_CHARS * 0.6), int(MAX_CHARS * 0.4)
        text = text[:head] + f"\n...[TRUNCATED {len(text) - head - tail} chars]...\n" + text[-tail:]
    return text


def render_run_meta(trial_dir: Path) -> str:
    result = {}
    rj = trial_dir / "result.json"
    if rj.exists():
        try:
            result = json.loads(rj.read_text())
        except json.JSONDecodeError:
            pass
    for ev in _stream_events(trial_dir):
        if ev.get("type") == "result":
            result.setdefault("_stream", ev)
    stream = result.get("_stream") or {}
    parts = [
        f"reward: {result.get('reward')} | turns: {result.get('num_turns')} | "
        f"tokens: {result.get('tokens')} | cost: {result.get('cost_usd')}",
        f"timed_out: {result.get('timed_out')} | subtype: {result.get('subtype')} | "
        f"infra_error: {result.get('infra_error')}",
    ]
    if stream:
        parts.append(f"stream result: subtype={stream.get('subtype')} "
                     f"is_error={stream.get('is_error')} "
                     f"result={_clip(stream.get('result'), 2000)}")
    err = trial_dir / "stderr.txt"
    if err.exists():
        tail = err.read_text(errors="replace")[-1500:]
        if tail.strip():
            parts.append(f"--- stderr tail ---\n{tail}")
    return "\n".join(parts)


def render_verifier(trial_dir: Path, cap: int) -> str:
    vf = trial_dir / "verifier.txt"
    if not vf.exists():
        return "(no verifier output)"
    text = vf.read_text(errors="replace")
    return text if len(text) <= cap else text[:cap] + \
        f" ...[truncated {len(text) - cap} chars]"


def render_full(rec: dict, detail: bool = False) -> str:
    trial_dir = Path(rec["trial_dir"])
    caps = _caps(detail)
    events = _stream_events(trial_dir)
    parts = [
        "=== TASK ===",
        str(rec.get("prompt") or "(no prompt)"),
        "",
        "=== TRAJECTORY ===",
        render_steps(events,
                     text_head=caps.get("text", TEXT_HEAD),
                     tool_in_head=caps.get("tool_in", TOOL_IN_HEAD),
                     tool_out_head=caps.get("tool_out", TOOL_OUT_HEAD)),
        "",
        "=== RUN METADATA ===",
        render_run_meta(trial_dir),
        "",
        "=== VERIFIER (ground truth) ===",
        render_verifier(trial_dir, caps.get("verifier", VERIFIER_HEAD)),
    ]
    return "\n".join(p for p in parts if p != "")
