#!/usr/bin/env python3
"""End-to-end test against a real Herdr server.

Starts an isolated headless Herdr (its own HOME), links this plugin, installs
the herdr shim, runs fake agents in real panes and a deterministic fake judge,
and checks every outcome of the loop.

    python3 tests/e2e_herdr.py [/path/to/herdr]
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
HERDR = str(Path(sys.argv[1] if len(sys.argv) > 1 else shutil.which("herdr") or "herdr").resolve())
TMP = Path(tempfile.mkdtemp(prefix="whisper-e2e-"))
HOME = TMP / "home"
BIN = TMP / "bin"            # fake `claude` + a copy of the real herdr
SHIM = HOME / ".local/share/herdr-whisper/bin"
CFG = HOME / ".config/herdr/plugins/config/whisper/config.toml"

BASE_CFG = (f'judge_command = ["python3", "{REPO}/tests/fake_judge.py"]\n'
            "settle_grace_s = 3\nstart_timeout_s = 8\n")
ENV = {
    **{k: v for k, v in os.environ.items() if not k.startswith(("HERDR_", "XDG_", "WHISPER_"))},
    "HOME": str(HOME),
    # Same order a user gets from the rc line: shim first, then real herdr.
    "PATH": f"{SHIM}:{HOME}/.local/bin:{BIN}:{os.environ['PATH']}",
    "FAKE_REAL_HERDR": str(BIN / "herdr"),
}
REAL = str(BIN / "herdr")
failures: list[str] = []


def sh(*args: str, env: dict | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(list(args), capture_output=True, text=True, env={**ENV, **(env or {})})


def real(*args: str) -> dict:
    out = sh(REAL, *args)
    text = (out.stdout.strip() or out.stderr.strip()).splitlines()
    return json.loads(text[-1]) if text and text[-1].startswith("{") else {}


def as_agent(pane: str, *args: str) -> subprocess.CompletedProcess:
    """Run `herdr ...` the way an agent in `pane` would: through PATH (the shim)."""
    return sh("herdr", *args, env={"HERDR_PANE_ID": pane})


def whisper(*args: str) -> subprocess.CompletedProcess:
    return sh("whisper", *args)


def messages() -> list:
    out = whisper("status", "--all", "--json")
    return json.loads(out.stdout) if out.returncode == 0 else []


def latest(to_pane: str, text: str) -> dict:
    for m in messages():
        if m["to"]["pane"] == to_pane and m["text"] == text:
            return m
    return {}


def wait_for(to_pane: str, text: str, wanted: set, timeout: float = 25) -> dict:
    deadline, m = time.time() + timeout, {}
    while time.time() < deadline:
        m = latest(to_pane, text)
        if m.get("state") in wanted:
            return m
        time.sleep(0.4)
    return m


def screen(pane: str, lines: int = 60) -> str:
    return sh(REAL, "pane", "read", pane, "--source", "recent-unwrapped", "--lines", str(lines)).stdout


def check(name: str, cond: bool, info: object = "") -> None:
    print(("  ok   " if cond else "  FAIL ") + name)
    if not cond:
        failures.append(name)
        if info:
            print("       " + str(info)[:1500].replace("\n", "\n       "))


def states(m: dict) -> list:
    return [h[1] for h in m.get("history", [])]


def spawn(next_to: str, name: str, mode: str, direction: str = "right") -> str:
    pane = real("pane", "split", next_to, "--direction", direction)["result"]["pane"]["pane_id"]
    time.sleep(0.5)
    sh(REAL, "pane", "run", pane, f"FAKE_NAME={name} FAKE_MODE={mode} claude")
    for _ in range(40):
        info = real("agent", "get", pane).get("result", {}).get("agent", {})
        if info.get("agent") == "claude" and info.get("agent_status") in ("idle", "done"):
            return pane
        time.sleep(0.25)
    raise SystemExit(f"fake agent {name} did not start in {pane}")


def main() -> int:
    HOME.mkdir(parents=True)
    BIN.mkdir()
    shutil.copy(REPO / "tests" / "fake_agent.py", BIN / "claude")
    (BIN / "claude").chmod(0o755)
    shutil.copy(HERDR, REAL)
    CFG.parent.mkdir(parents=True)
    CFG.write_text(BASE_CFG)
    (HOME / ".bashrc").write_text("# user rc\n")

    server = subprocess.Popen([REAL, "server"], env=ENV, stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL)
    try:
        for _ in range(50):
            if "running" in sh(REAL, "status", "server").stdout:
                break
            time.sleep(0.2)

        print("setup")
        link = real("plugin", "link", str(REPO))
        check("plugin links without warnings", "warnings" not in json.dumps(link), link)
        real("plugin", "action", "invoke", "whisper.install")
        for _ in range(40):
            if (SHIM / "herdr").exists() and "herdr-whisper" in (HOME / ".bashrc").read_text():
                break
            time.sleep(0.25)
        check("install action writes shim, command and rc line",
              (SHIM / "herdr").exists() and (HOME / ".local/bin/whisper").exists()
              and "herdr-whisper" in (HOME / ".bashrc").read_text())
        check("`herdr` on PATH is now the shim", sh("sh", "-c", "command -v herdr").stdout.strip()
              == str(SHIM / "herdr"))

        root = real("workspace", "create", "--label", "e2e", "--cwd", str(TMP))["result"]["root_pane"]["pane_id"]
        planner = spawn(root, "planner", "silent")
        answer = spawn(root, "answerbot", "answer", "down")
        callback = spawn(answer, "callbackbot", "callback")
        ignore = spawn(planner, "ignorebot", "ignore", "down")
        blocker = spawn(ignore, "blocker", "block")
        silent = spawn(callback, "silentbot", "silent", "down")
        print(f"panes: planner={planner} answer={answer} callback={callback} ignore={ignore} "
              f"blocker={blocker} silent={silent} shell={root}")

        print("\n1. shim is transparent")
        via_shim = as_agent(planner, "agent", "list")
        direct = sh(REAL, "agent", "list")
        check("non-prompt commands pass straight through",
              via_shim.returncode == direct.returncode == 0
              and json.loads(via_shim.stdout)["result"]["type"] == "agent_list")
        bad_shim = as_agent(planner, "agent", "prompt", "nosuchagent", "hi")
        bad_real = sh(REAL, "agent", "prompt", "nosuchagent", "hi")
        check("errors identical (exit code and JSON)",
              (bad_shim.returncode, bad_shim.stderr) == (bad_real.returncode, bad_real.stderr),
              (bad_shim.stderr, bad_real.stderr))
        check("unknown target not logged", not any(m["to"].get("target") == "nosuchagent"
                                                   for m in messages()))

        print("\n2. sender waits (--wait): whisper stays out of it")
        out = as_agent(planner, "agent", "prompt", answer, "q-wait", "--wait", "--timeout", "30000")
        check("prompt --wait returns normally", out.returncode == 0
              and json.loads(out.stdout)["result"]["type"] == "agent_prompted", out.stdout + out.stderr)
        m = wait_for(answer, "q-wait", {"returned"})
        check("→ returned, no judge, no push", m.get("state") == "returned"
              and "judging" not in states(m), m.get("history"))

        print("\n3. sender does not wait: answer is forwarded")
        out = as_agent(planner, "agent", "prompt", answer, "q-nowait")
        check("prompt returns immediately", out.returncode == 0, out.stderr)
        m = wait_for(answer, "q-nowait", {"forwarded", "escalated"})
        check("→ judging → forwarded", states(m)[-2:] == ["judging", "forwarded"], m.get("history"))
        check("verdict recorded", (m.get("verdict") or {}).get("action") == "forward", m.get("verdict"))
        s = screen(planner)
        check("planner received the answer", "[whisper] claude@" in s and "ANSWER from answerbot" in s,
              s[-800:])

        print("\n4. recipient prompts the sender back")
        as_agent(planner, "agent", "prompt", callback, f"please ask back {planner}")
        m = wait_for(callback, f"please ask back {planner}", {"replied", "forwarded", "escalated"})
        check("→ replied, no judge", m.get("state") == "replied" and "judging" not in states(m),
              m.get("history"))
        back = latest(planner, "callback from callbackbot: 42")
        check("the callback itself was logged by the shim", bool(back), messages()[:2])

        print("\n5. recipient ignores the message: resend, then forward")
        as_agent(planner, "agent", "prompt", ignore, "q-ignored")
        m = wait_for(ignore, "q-ignored", {"forwarded", "escalated"}, timeout=40)
        check("→ resent → … → forwarded", "resent" in states(m) and m.get("state") == "forwarded",
              m.get("history"))

        print("\n6. information only: judge closes")
        as_agent(planner, "agent", "prompt", silent, "FYI: tests are green")
        m = wait_for(silent, "FYI: tests are green", {"closed", "forwarded", "escalated"})
        check("→ closed, nothing pushed", m.get("state") == "closed", m.get("history"))

        print("\n7. blocked recipient")
        as_agent(planner, "agent", "prompt", blocker, "do the risky thing")
        m = wait_for(blocker, "do the risky thing", {"blocked"})
        check("tracked as blocked", m.get("state") == "blocked", m.get("history"))
        out = as_agent(planner, "agent", "prompt", blocker, "another one")
        check("Herdr's agent_blocked still enforced through the shim",
              out.returncode == 1 and "agent_blocked" in out.stderr, out.stderr)
        m = wait_for(blocker, "another one", {"failed"})
        check("refused prompt logged as failed", m.get("state") == "failed", m.get("history"))

        print("\n8. sender is a plain shell, not an agent")
        as_agent(root, "agent", "prompt", answer, "from a shell")
        m = wait_for(answer, "from a shell", {"returned", "forwarded", "escalated"})
        check("→ returned, nothing typed into the shell", m.get("state") == "returned"
              and "[whisper]" not in screen(root), m.get("history"))

        print("\n9. message lands while the recipient is busy")
        as_agent(planner, "agent", "prompt", answer, "busy-1")
        time.sleep(0.4)
        as_agent(planner, "agent", "prompt", answer, "busy-2")
        m2 = latest(answer, "busy-2")
        check("second one saw recipient working", m2.get("to", {}).get("status_at_send") == "working", m2)
        m1 = wait_for(answer, "busy-1", {"forwarded", "escalated"})
        m2 = wait_for(answer, "busy-2", {"forwarded", "escalated"})
        check("both forwarded", m1.get("state") == m2.get("state") == "forwarded",
              (m1.get("history"), m2.get("history")))

        print("\n10. judge unavailable → you are notified")
        CFG.write_text(BASE_CFG.replace(f'"python3", "{REPO}/tests/fake_judge.py"', '"/nonexistent/llm"'))
        as_agent(planner, "agent", "prompt", answer, "q-nojudge")
        m = wait_for(answer, "q-nojudge", {"escalated", "forwarded"})
        check("→ escalated with the reason", m.get("state") == "escalated"
              and "not found" in m["history"][-1][2], m.get("history"))

        print("\n11. dry run (push = false)")
        CFG.write_text(BASE_CFG + "push = false\n")
        as_agent(planner, "agent", "prompt", answer, "q-dry")
        m = wait_for(answer, "q-dry", {"escalated", "forwarded"})
        check("→ escalated 'dry run: would forward'", m.get("state") == "escalated"
              and "would forward" in m["history"][-1][2], m.get("history"))
        CFG.write_text(BASE_CFG)

        print("\n12. recipient pane closes mid-turn")
        as_agent(planner, "agent", "prompt", silent, "slow question")
        time.sleep(0.3)
        sh(REAL, "pane", "close", silent)
        m = wait_for(silent, "slow question", {"orphaned"})
        check("→ orphaned", m.get("state") == "orphaned", m.get("history"))

        print("\n13. views and integrity")
        out = whisper("status", "--all")
        check("status lists conversations", out.returncode == 0 and "→" in out.stdout, out.stdout[-600:])
        mid = latest(answer, "q-nowait").get("id", "")
        out = whisper("show", mid)
        check("show includes the judge verdict", "judge forward" in out.stdout, out.stdout)
        res = real("plugin", "pane", "open", "--plugin", "whisper", "--entrypoint", "feed",
                   "--placement", "split")
        feed = res.get("result", {}).get("plugin_pane", {}).get("pane", {}).get("pane_id", "")
        sh(REAL, "pane", "zoom", feed, "--on")
        time.sleep(2)
        vis = sh(REAL, "pane", "read", feed, "--source", "visible").stdout
        check("feed pane renders", "whisper ·" in vis and "forward" in vis, vis[:600])
        log = Path(whisper("where").stdout.split()[1])
        check("every log line is valid JSON", all(json.loads(l) for l in log.read_text().splitlines()))
        before = {m["id"]: m["state"] for m in messages()}
        (log.parent / "snapshot.json").unlink()
        after = {m["id"]: m["state"] for m in messages()}
        check("snapshot rebuilds identically from the log", before == after)
        logs = sh(REAL, "plugin", "log", "list", "--plugin", "whisper").stdout.replace(" ", "")
        check("no failing hook runs", '"exit_code":1' not in logs, logs[-1200:])
        whisper("uninstall")
        check("uninstall removes shim and rc line", not (SHIM / "herdr").exists()
              and "herdr-whisper" not in (HOME / ".bashrc").read_text())
    finally:
        sh(REAL, "server", "stop")
        server.wait(timeout=10)
    print(f"\n{'PASS' if not failures else 'FAIL'}: {len(failures)} failure(s) {failures}")
    print(f"artifacts: {TMP}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
