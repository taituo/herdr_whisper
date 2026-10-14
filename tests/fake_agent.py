#!/usr/bin/env python3
"""Fake coding agent for the end-to-end test. Install it as an executable
named `claude`: Herdr only lets `agent prompt` address agents it recognises.

It reports lifecycle state with `herdr pane report-agent`, treats lines that
arrive together as one prompt (like a paste), and behaves per FAKE_MODE:
  answer   - prints "ANSWER from <name>: 42"
  callback - prompts the pane named in the prompt back via `herdr agent prompt`
  ignore   - ignores a prompt unless it is a whisper resend, then answers
  block    - goes to `blocked` and stays there
  silent   - prints "ok." (acknowledges without content)
"""
import os
import re
import select
import subprocess
import sys
import time

REAL = os.environ.get("FAKE_REAL_HERDR", "herdr")  # status reports bypass the shim
PANE = os.environ["HERDR_PANE_ID"]
NAME = os.environ.get("FAKE_NAME", "fake")
MODE = os.environ.get("FAKE_MODE", "answer")
WORK = float(os.environ.get("FAKE_WORK_S", "1"))
seq = 0


def report(state):
    global seq
    seq = max(seq + 1, int(time.time() * 1000))
    subprocess.run([REAL, "pane", "report-agent", PANE, "--source", "custom:fake",
                    "--agent", "claude", "--state", state, "--seq", str(seq)],
                   capture_output=True)


def read_prompt():
    line = sys.stdin.readline()
    if not line:
        return None
    parts = [line.rstrip("\n")]
    while select.select([sys.stdin], [], [], 0.3)[0]:
        more = sys.stdin.readline()
        if not more:
            break
        parts.append(more.rstrip("\n"))
    return "\n".join(parts).strip()


report("idle")
print(f"[{NAME}] ready ({MODE})", flush=True)
while True:
    prompt = read_prompt()
    if prompt is None:
        break
    if not prompt:
        continue
    print(f"[{NAME}] got: {prompt}", flush=True)
    if MODE == "block":
        report("blocked")
        print(f"[{NAME}] Allow this? (y/n)", flush=True)
        continue
    report("working")
    time.sleep(WORK)
    if MODE == "answer" or (MODE == "ignore" and "Resending" in prompt):
        print(f"ANSWER from {NAME}: 42", flush=True)
    elif MODE == "ignore":
        print(f"IGNORED: {NAME} is busy with other work", flush=True)
    elif MODE == "callback":
        target = re.search(r"ask back (w\d+:p\d+)", prompt)
        if target:
            # Through PATH, i.e. through the whisper shim, like a real agent.
            r = subprocess.run(["herdr", "agent", "prompt", target.group(1),
                                f"callback from {NAME}: 42"], capture_output=True, text=True)
            print(f"[{NAME}] prompted back: rc={r.returncode}", flush=True)
    else:
        print("ok.", flush=True)
    report("idle")
    print(f"[{NAME}] idle", flush=True)
