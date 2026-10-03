#!/usr/bin/env python3
"""A stand-in for `claude -p --input-format stream-json`: same wire protocol, canned answers.

Answers "answer[<model>/<effort>] <first line of the turn's last entry>", honours set_model and
apply_flag_settings, and appends every launch's argv to $FAKE_CLAUDE_LOG for assertions.
"""
import json
import os
import sys

args = sys.argv[1:]
model = args[args.index("--model") + 1]
effort = args[args.index("--effort") + 1]
session = args[args.index("--resume") + 1] if "--resume" in args else f"fake-{os.getpid()}"
with open(os.environ["FAKE_CLAUDE_LOG"], "a") as fh:
    fh.write(json.dumps({"pid": os.getpid(), "args": args}) + "\n")


def out(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


out({"type": "system", "subtype": "init", "session_id": session, "model": model})
for line in sys.stdin:
    msg = json.loads(line)
    if msg.get("type") == "control_request":
        req = msg["request"]
        if req["subtype"] == "set_model":
            model = req["model"]
        elif req["subtype"] == "apply_flag_settings":
            effort = req["settings"].get("effortLevel", effort)
        out({"type": "control_response", "response": {"subtype": "success", "request_id": msg["request_id"]}})
    elif msg.get("type") == "user":
        text = msg["message"]["content"]
        last = [l for l in text.splitlines() if l.strip()][-1]
        answer = f"answer[{model}/{effort}] pid={os.getpid()} briefed={'# Briefing' in text} {last}"
        # stream it like --include-partial-messages does, slowly enough for a test to watch the draft
        import time
        out({"type": "stream_event", "event": {"type": "message_start"}})
        out({"type": "stream_event", "event": {"type": "content_block_start", "content_block": {"type": "text"}}})
        for k in range(0, len(answer), 12):
            out({"type": "stream_event", "event": {"type": "content_block_delta", "delta": {"type": "text_delta", "text": answer[k:k + 12]}}})
            time.sleep(0.12)
        out({"type": "assistant", "message": {"model": model, "content": [{"type": "text", "text": answer}]}})
        out({"type": "result", "subtype": "success", "is_error": False, "result": answer, "session_id": session})
