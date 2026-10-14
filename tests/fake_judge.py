#!/usr/bin/env python3
"""Deterministic stand-in for the LLM judge (reads the prompt on stdin)."""
import json
import re
import sys

prompt = sys.stdin.read()
message = re.search(r"<<<MESSAGE FROM A TO B\n(.*?)\nMESSAGE>>>", prompt, re.S).group(1)
output = re.search(r"<<<B's RECENT TERMINAL OUTPUT[^\n]*\n(.*?)\nOUTPUT>>>", prompt, re.S).group(1)
tail = [l for l in output.splitlines() if l.strip() and "idle" not in l][-3:]
last = "\n".join(tail)

if "IGNORED" in last:
    verdict = {"action": "resend", "reason": "recipient ignored the message"}
elif "FYI" in message:
    verdict = {"action": "close", "reason": "information only"}
elif "ANSWER from" in last:
    answer = next(l for l in reversed(tail) if "ANSWER from" in l)
    verdict = {"action": "forward", "message": answer.strip(), "reason": "recipient answered"}
else:
    verdict = {"action": "human", "reason": "cannot tell"}
print("Sure, here is my decision:\n```json\n" + json.dumps(verdict) + "\n```")
