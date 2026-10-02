#!/usr/bin/env python3
"""Stand-in for `claude -p` in tests: answers with $FAKE_VERDICT and logs argv."""
import json, os, sys
payload = sys.stdin.read()
log = os.environ.get("FAKE_LOG")
if log:
    with open(log, "a") as f:
        f.write(json.dumps({"argv": sys.argv[1:], "payload": payload, "cwd": os.getcwd(),
                            "guard": os.environ.get("RRSI_POLICY_ACTIVE")}) + "\n")
if os.environ.get("FAKE_FAIL"):
    sys.exit(1)
v = os.environ.get("FAKE_VERDICT", "accept")
print(json.dumps({"type": "result", "subtype": "success", "is_error": False,
                  "total_cost_usd": 0.01, "duration_ms": 5, "result": "",
                  "structured_output": {"verdict": v, "component": os.environ.get("FAKE_COMPONENT", "claude_md"),
                                        "intent": os.environ.get("FAKE_INTENT", "test intent"),
                                        "rule": "none" if v == "accept" else "task_specific",
                                        "reasons": [] if v == "accept" else ["hard-codes one task"]}}))
