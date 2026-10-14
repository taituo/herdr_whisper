#!/usr/bin/env python3
"""End-to-end test against a real Herdr server.

Starts an isolated headless Herdr (its own HOME), links this plugin, runs fake
agents in real panes and checks every tracked outcome.

    python3 tests/e2e_herdr.py [/path/to/herdr]

The fake agent must run as an executable called `claude`: Herdr only lets
`agent prompt` address agents whose process it recognises.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
HERDR = sys.argv[1] if len(sys.argv) > 1 else shutil.which("herdr") or "herdr"
TMP = Path(tempfile.mkdtemp(prefix="whisper-e2e-"))
HOME = TMP / "home"
BIN = TMP / "bin"

ENV = {
    **{k: v for k, v in os.environ.items() if not k.startswith(("HERDR_", "XDG_", "WHISPER_"))},
    "HOME": str(HOME),
    "PATH": f"{HOME}/.local/bin:{BIN}:{Path(HERDR).resolve().parent}:{os.environ['PATH']}",
}
failures: list[str] = []


def sh(*args: str, env: dict | None = None, check: bool = False) -> subprocess.CompletedProcess:
    proc = subprocess.run(list(args), capture_output=True, text=True, env={**ENV, **(env or {})})
    if check and proc.returncode != 0:
        raise SystemExit(f"command failed: {args}\n{proc.stdout}\n{proc.stderr}")
    return proc


def herdr(*args: str) -> dict:
    out = sh(HERDR, *args)
    text = out.stdout.strip() or out.stderr.strip()
    return json.loads(text.splitlines()[-1]) if text.startswith("{") else {"raw": text}


def whisper(*args: str, pane: str | None = None) -> subprocess.CompletedProcess:
    env = {"HERDR_PANE_ID": pane} if pane else {}
    return sh("whisper", *args, env=env)


def state(mid: str) -> dict:
    out = whisper("show", mid, "--json")
    return json.loads(out.stdout) if out.returncode == 0 else {}


def wait_state(mid: str, wanted: set[str], timeout: float = 20) -> dict:
    deadline = time.time() + timeout
    m = {}
    while time.time() < deadline:
        m = state(mid)
        if m.get("state") in wanted:
            return m
        time.sleep(0.3)
    return m


def check(name: str, cond: bool, info: object = "") -> None:
    print(("  ok   " if cond else "  FAIL ") + name)
    if not cond:
        failures.append(name)
        if info:
            print("       " + str(info)[:1500].replace("\n", "\n       "))


def sent_id(proc: subprocess.CompletedProcess) -> str:
    for word in (proc.stdout + proc.stderr).split():
        if word.startswith("msg:"):
            return word[4:10]
    raise AssertionError(f"no msg id in output: {proc.stdout} {proc.stderr}")


def spawn_agent(ws_pane: str, name: str, mode: str, direction: str = "right") -> str:
    res = herdr("pane", "split", ws_pane, "--direction", direction)
    pane = res["result"]["pane"]["pane_id"]
    time.sleep(0.5)
    sh(HERDR, "pane", "run", pane,
       f"FAKE_NAME={name} FAKE_MODE={mode} FAKE_WORK_S=1 claude", check=True)
    for _ in range(40):
        info = herdr("agent", "get", pane).get("result", {}).get("agent", {})
        if info.get("agent") == "claude" and info.get("agent_status") in ("idle", "done"):
            return pane
        time.sleep(0.25)
    raise SystemExit(f"fake agent {name} did not start in {pane}")


def main() -> int:
    HOME.mkdir(parents=True)
    BIN.mkdir()
    shutil.copy(REPO / "tests" / "fake_agent.py", BIN / "claude")
    (BIN / "claude").chmod(0o755)
    cfg = HOME / ".config/herdr/plugins/config/whisper"
    cfg.mkdir(parents=True)
    (cfg / "config.toml").write_text('mode = "active"\nsettle_grace_s = 3\nstart_timeout_s = 8\n')

    server = subprocess.Popen([HERDR, "server"], env=ENV, stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL)
    try:
        for _ in range(50):
            if "running" in sh(HERDR, "status", "server").stdout:
                break
            time.sleep(0.2)
        link = herdr("plugin", "link", str(REPO))
        check("plugin links without warnings", "warnings" not in json.dumps(link), link)
        acts = sh(HERDR, "plugin", "action", "list", "--plugin", "whisper").stdout
        check("actions registered", '"action_id":"feed"' in acts and '"action_id":"sweep"' in acts, acts)
        inv = herdr("plugin", "action", "invoke", "whisper.install-shim")
        shim = HOME / ".local/bin/whisper"
        for _ in range(40):
            if shim.exists():
                break
            time.sleep(0.25)
        check("install-shim action puts whisper on PATH", shim.exists(), inv)

        root = herdr("workspace", "create", "--label", "e2e", "--cwd", str(TMP))["result"]["root_pane"]["pane_id"]
        planner = spawn_agent(root, "planner", "echo")
        reviewer = spawn_agent(root, "reviewer", "reply", "down")
        silent = spawn_agent(reviewer, "silent", "silent", "right")
        rawbot = spawn_agent(planner, "rawbot", "raw", "down")
        blocker = spawn_agent(rawbot, "blocker", "block", "right")
        sh(HERDR, "agent", "rename", reviewer, "reviewer")
        print(f"panes: planner={planner} reviewer={reviewer} silent={silent} "
              f"rawbot={rawbot} blocker={blocker} shell={root}")

        print("\n1. reply through `whisper reply`")
        out = whisper("send", "reviewer", "what is the answer?", "--expect-reply", pane=planner)
        check("send exits 0", out.returncode == 0, out.stderr)
        mid = sent_id(out)
        m = wait_state(mid, {"replied"})
        check("question → replied", m.get("state") == "replied", m.get("history"))
        check("went through delivered and working",
              [h[1] for h in m.get("history", [])][:3] == ["queued", "delivered", "working"],
              m.get("history"))
        reply_id = (m.get("replies") or [""])[0]
        r = state(reply_id) if reply_id else {}
        check("reply logged and linked", r.get("re") == mid and "42" in r.get("text", ""), r)
        check("reply delivered into the sender's pane",
              f"re:{mid}" in sh(HERDR, "pane", "read", planner, "--lines", "40").stdout)
        r = wait_state(reply_id, {"done"})
        check("reply itself tracked to done", r.get("state") == "done", r.get("history"))

        print("\n2. raw reply (herdr agent prompt with re:<id>)")
        mid = sent_id(whisper("send", rawbot, "raw question", "--expect-reply", pane=planner))
        m = wait_state(mid, {"replied", "unanswered", "nudged"})
        check("raw reply detected from sender pane", m.get("state") == "replied", m.get("history"))

        print("\n3. silent recipient → nudge → unanswered")
        mid = sent_id(whisper("send", silent, "please answer", "--expect-reply", pane=planner))
        m = wait_state(mid, {"unanswered"}, timeout=25)
        states = [h[1] for h in m.get("history", [])]
        check("nudged once, then unanswered", "nudged" in states and m.get("state") == "unanswered",
              m.get("history"))
        check("nudge text reached recipient",
              "still waiting for your reply" in sh(HERDR, "pane", "read", silent, "--source",
                                                  "recent-unwrapped", "--lines", "60").stdout)

        print("\n4. fire-and-forget")
        mid = sent_id(whisper("send", silent, "fyi only", pane=planner))
        m = wait_state(mid, {"done"})
        check("no reply expected → done when recipient settles", m.get("state") == "done", m.get("history"))

        print("\n5. recipient goes blocked")
        mid = sent_id(whisper("send", blocker, "do the risky thing", "--expect-reply", pane=planner))
        m = wait_state(mid, {"blocked"})
        check("tracked as blocked", m.get("state") == "blocked", m.get("history"))
        out = whisper("send", blocker, "another one", "--expect-reply", pane=planner)
        check("Herdr's agent_blocked check still applies (send fails)",
              out.returncode == 1 and "agent_blocked" in out.stderr, out.stdout + out.stderr)

        print("\n6. plain shell (not an agent)")
        out = whisper("send", root, "hello shell", pane=planner)
        typed = sh(HERDR, "pane", "read", root, "--lines", "20").stdout
        check("refused for a non-agent pane, nothing typed", out.returncode == 1
              and "agent_not_found" in out.stderr and "hello shell" not in typed, out.stderr)

        print("\n7. recipient pane closes before replying")
        mid = sent_id(whisper("send", silent, "slow question", "--expect-reply", pane=planner))
        time.sleep(0.3)
        sh(HERDR, "pane", "close", silent)
        m = wait_state(mid, {"orphaned"})
        check("orphaned on pane close", m.get("state") == "orphaned", m.get("history"))

        print("\n8. sender judgment")
        mid = sent_id(whisper("send", "reviewer", "second question", "--expect-reply", pane=planner))
        wait_state(mid, {"replied"})
        out = whisper("reject", mid, "that is not what I asked", pane=planner)
        m = state(mid)
        check("reject recorded", m.get("state") == "rejected"
              and m.get("verdict", {}).get("ok") is False, m)

        print("\n9. --wait returns the reply text")
        out = whisper("send", "reviewer", "third", "--expect-reply", "--wait", "--timeout", "20",
                      pane=planner)
        check("--wait prints reply", out.returncode == 0 and "reply: reviewer says" in out.stdout,
              out.stdout + out.stderr)

        print("\n10. human sender outside Herdr")
        env_human = {"HERDR_SOCKET_PATH": str(HOME / ".config/herdr/herdr.sock")}
        out = sh("whisper", "send", "reviewer", "from the human", "--expect-reply", env=env_human)
        mid = sent_id(out)
        m = wait_state(mid, {"replied"})
        check("reply to a human is kept in the log", m.get("state") == "replied", m.get("history"))

        print("\n10b. message arrives while the recipient is busy")
        a = sent_id(whisper("send", "reviewer", "first of two", "--expect-reply", pane=planner))
        time.sleep(0.4)
        b_out = whisper("send", "reviewer", "second of two", "--expect-reply", pane=planner)
        b = sent_id(b_out)
        mb = state(b)
        check("second message saw recipient busy", mb.get("to", {}).get("status_at_send") == "working", mb)
        ma, mb = wait_state(a, {"replied"}), wait_state(b, {"replied"})
        check("both answered and tracked separately",
              ma.get("state") == "replied" and mb.get("state") == "replied",
              (ma.get("history"), mb.get("history")))

        print("\n11. views")
        out = whisper("status", "--all")
        check("status lists threads", out.returncode == 0 and "→" in out.stdout, out.stdout[-800:])
        out = whisper("inbox", pane=blocker)
        check("inbox shows pending question", "do the risky thing" in out.stdout, out.stdout)
        res = herdr("plugin", "pane", "open", "--plugin", "whisper", "--entrypoint", "feed",
                    "--placement", "split")
        feed_pane = res.get("result", {}).get("plugin_pane", {}).get("pane", {}).get("pane_id")
        sh(HERDR, "pane", "zoom", feed_pane or "", "--on")
        time.sleep(2)
        screen = sh(HERDR, "pane", "read", feed_pane or "", "--source", "visible").stdout
        check("feed pane renders", "whisper ·" in screen and "messages" in screen, screen[:800])

        print("\n12. log integrity")
        store = Path(whisper("where").stdout.split()[1])
        lines = store.read_text().splitlines()
        check("every log line is valid JSON", all(json.loads(l) for l in lines), "")
        snap = store.parent / "snapshot.json"
        snap.unlink()
        rebuilt = json.loads(whisper("status", "--all", "--json").stdout)
        check("snapshot rebuilds from log alone", len(rebuilt) > 5, len(rebuilt))
        logs = sh(HERDR, "plugin", "log", "list", "--plugin", "whisper").stdout
        check("no failing hook runs", '"exit_code":1' not in logs.replace(" ", ""), logs[-1500:])
    finally:
        sh(HERDR, "server", "stop")
        server.wait(timeout=10)
    print(f"\n{'PASS' if not failures else 'FAIL'}: {len(failures)} failure(s) {failures}")
    print(f"artifacts: {TMP}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
