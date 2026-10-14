#!/usr/bin/env python3
"""Fake coding agent (install it as an executable named `claude`) for testing whisper inside a real Herdr pane.

Reports lifecycle state through `herdr pane report-agent`, reads submitted
prompts from stdin, and reacts according to FAKE_MODE:
  reply  - answer any whisper message that asks for a reply (via `whisper reply`)
  raw    - answer with a raw `herdr agent prompt` containing re:<id>
  silent - do the work, never reply
  block  - go to `blocked` on the next prompt and stay there
  echo   - just work and settle
"""
import os
import re
import subprocess
import sys
import time

HERDR = os.environ.get("HERDR_BIN_PATH", "herdr")
PANE = os.environ["HERDR_PANE_ID"]
NAME = os.environ.get("FAKE_NAME", "fake")
MODE = os.environ.get("FAKE_MODE", "echo")
WORK = float(os.environ.get("FAKE_WORK_S", "1.5"))
seq = 0


def report(state):
    global seq
    seq = max(seq + 1, int(time.time() * 1000))
    subprocess.run([HERDR, "pane", "report-agent", PANE, "--source", "custom:fake",
                    "--agent", "claude", "--state", state, "--seq", str(seq)],
                   capture_output=True)


report("idle")
print(f"[{NAME}] ready ({MODE})", flush=True)
for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    print(f"[{NAME}] got: {line}", flush=True)
    if MODE == "block":
        report("blocked")
        print(f"[{NAME}] Allow this? (y/n)", flush=True)
        continue
    report("working")
    time.sleep(WORK)
    m = re.search(r"msg:([0-9a-f]{6}).*?reply expected", line)
    if m and MODE == "reply":
        r = subprocess.run(["whisper", "reply", m.group(1), f"{NAME} says: done, 42"],
                           capture_output=True, text=True)
        print(f"[{NAME}] whisper reply -> {r.returncode} {r.stdout.strip()} {r.stderr.strip()}", flush=True)
    elif m and MODE == "raw":
        sender = re.search(r"from \S*?\(?(w\d+:p\d+)\)?", line)
        if sender:
            subprocess.run([HERDR, "agent", "prompt", sender.group(1),
                            f"raw answer re:{m.group(1)} it is 42"], capture_output=True)
    report("idle")
    print(f"[{NAME}] idle", flush=True)
