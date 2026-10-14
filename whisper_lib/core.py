"""The loop: log agent-to-agent prompts, and when a recipient returns, make
sure the exchange does not silently die.

Normally whisper does nothing visible. The `herdr` shim only records
"A prompted B" and runs the real herdr unchanged, so agents keep using Herdr
exactly as the Herdr skill teaches. Whisper only steps in when B returns and
A would otherwise never hear back (broken-telephone prevention):

  B settles ──> A was not an agent ................................ returned
            ──> A was waiting (`agent prompt --wait`) .............. returned
            ──> B prompted A back during its turn .................. replied
            ──> otherwise: judging (LLM reads the message + B's output)
                   close   ─> closed      (exchange was meant to end)
                   forward ─> forwarded   (B's answer pushed to A)
                   resend  ─> resent      (A's message sent to B again; loop)
                   nudge   ─> nudged      (B asked to continue; loop)
                   human   ─> escalated   (notification to you)

Limits (max_pushes, max_forwards_per_pair_per_hour) turn any runaway into an
escalation instead of an endless ping-pong.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from typing import Optional

from . import herdr, judge
from .store import OPEN_STATES, PLUGIN_ID, Store, load_config

SETTLED = {"idle", "done"}


# --- identity ----------------------------------------------------------------

def whoami() -> dict:
    """The caller as claimed by its environment (HERDR_PANE_ID). Not proof."""
    pane = os.environ.get("HERDR_PANE_ID")
    info = herdr.describe(pane) if pane else None
    if info:
        return info
    return {"pane": pane, "terminal": None, "agent": None, "name": None, "status": None}


def label(side: Optional[dict]) -> str:
    if not side:
        return "?"
    who = side.get("name") or side.get("agent") or "shell"
    return f"{who}@{side['pane']}" if side.get("pane") else who


def clock(ts: float) -> str:
    return time.strftime("%H:%M", time.localtime(ts))


# --- the tap (called by the herdr shim for `herdr agent prompt`) -------------

def parse_prompt_args(rest: list) -> Optional[dict]:
    """`agent prompt <target> <text> [--wait] [--until S]... [--timeout MS]`.
    Returns None for anything we do not understand (it is then passed through
    untouched and simply not tracked)."""
    pos, wait, i = [], False, 0
    while i < len(rest):
        tok = rest[i]
        if tok == "--":
            pos += rest[i + 1:]
            break
        if tok in ("--until", "--timeout"):
            i += 2
            continue
        if tok.startswith(("--until=", "--timeout=")):
            pass
        elif tok == "--wait":
            wait = True
        elif tok.startswith("-") and tok != "-":
            return None
        else:
            pos.append(tok)
        i += 1
    if len(pos) != 2:
        return None
    return {"target": pos[0], "text": pos[1], "wait": wait}


def tap(real: str, argv: list, store: Optional[Store] = None) -> int:
    """Run `real argv` exactly as asked. Around it, record the message. Any
    failure of whisper itself is swallowed: the agent's command must behave
    as if whisper did not exist."""
    mid = None
    try:
        mid = _record_prompt(argv, store)
    except Exception as exc:  # never break the agent's herdr call
        _debug(f"tap: not tracked ({exc!r})")
    try:
        proc = subprocess.run([real, *argv], capture_output=True)
    except KeyboardInterrupt:
        _safe(lambda: (store or Store()).append({"t": "wait", "id": mid, "result": "interrupted"})
              if mid else None)
        raise
    sys.stdout.buffer.write(proc.stdout)
    sys.stdout.buffer.flush()
    sys.stderr.buffer.write(proc.stderr)
    sys.stderr.buffer.flush()
    if mid:
        _safe(lambda: _record_result(mid, proc, store))
    return proc.returncode


def _record_prompt(argv: list, store: Optional[Store]) -> Optional[str]:
    if argv[:2] != ["agent", "prompt"]:
        return None
    parsed = parse_prompt_args(argv[2:])
    if not parsed:
        return None
    sender = whoami()
    to = herdr.describe(parsed["target"])
    if not to or not to.get("pane") or to.get("pane") == sender.get("pane"):
        return None
    to["target"] = parsed["target"]
    to["status_at_send"] = to.pop("status", None)
    store = store or Store()
    with store.lock():
        messages = store.state()
        mid = _new_id(messages)
        store.append({"t": "msg", "id": mid, "from": sender, "to": to, "text": parsed["text"],
                      "wait": parsed["wait"], "via": "herdr agent prompt"})
        # Recorded before submission so the recipient's status hooks, which
        # fire as soon as the prompt lands, already find it.
        store.append({"t": "state", "id": mid, "state": "delivered", "detail": "submitted"})
    return mid


def _record_result(mid: str, proc: subprocess.CompletedProcess, store: Optional[Store]) -> None:
    store = store or Store()
    code = ""
    if proc.returncode != 0:
        err = herdr._last_json(proc.stderr.decode("utf-8", "replace")) or {}
        code = str((err.get("error") or {}).get("code") or f"exit_{proc.returncode}")
    with store.lock():
        m = store.state().get(mid)
        if m is None:
            return
        if proc.returncode == 0:
            if m.get("wait"):
                store.append({"t": "wait", "id": mid, "result": "ok"})
        elif m.get("wait") and code in ("timeout", "agent_prompt_stalled"):
            # Submitted; only the caller's wait ended early. Keep tracking.
            store.append({"t": "wait", "id": mid, "result": code})
        elif m["state"] in OPEN_STATES:
            _set(store, m, "failed", code)


def _new_id(messages: dict) -> str:
    import secrets
    while True:
        mid = secrets.token_hex(3)
        if mid not in messages:
            return mid


# --- events ------------------------------------------------------------------

def handle_event(event: str, data: dict, store: Optional[Store] = None,
                 cfg: Optional[dict] = None) -> None:
    store = store or Store()
    cfg = cfg or load_config()
    pane = data.get("pane_id")
    if not pane or not store.has_open():
        return
    to_judge: list = []
    if event == "pane.agent_status_changed":
        to_judge = on_status(store, cfg, pane, data.get("agent_status") or "")
    elif event in ("pane.closed", "pane.exited"):
        on_gone(store, cfg, pane, event.split(".")[1])
    elif event == "pane.moved":
        on_moved(store, pane)
    for mid in to_judge:
        run_judge(store, cfg, mid)


def on_status(store: Store, cfg: dict, pane: str, status: str) -> list:
    """Returns ids that need the judge (run after the lock is released)."""
    to_judge = []
    with store.lock():
        for m in _open_for(store, pane):
            state = m["state"]
            if status == "working" and state in {"delivered", "resent", "nudged", "stalled", "blocked"}:
                _set(store, m, "working")
            elif status == "blocked" and state in {"delivered", "working", "resent", "nudged", "stalled"}:
                _set(store, m, "blocked", "recipient is waiting on an approval or question")
                _notify(cfg, f"whisper: {label(m['to'])} is blocked",
                        f"while handling a message from {label(m['from'])}: {m['text'][:160]}")
            elif status in SETTLED and state in {"working", "blocked"}:
                if settle(store, m):
                    to_judge.append(m["id"])
            # A settle while still `delivered` belongs to a turn that started
            # before this message arrived; sweep() handles it after a grace.
    return to_judge


def settle(store: Store, m: dict) -> bool:
    """B has returned. Mechanical checks first; True means: ask the judge."""
    sender, to = m.get("from") or {}, m.get("to") or {}
    if not sender.get("agent"):
        _set(store, m, "returned", "sender is not an agent; nothing to push")
        return False
    busy_at_send = to.get("status_at_send") == "working"
    if m.get("wait") and (m.get("wait_result") is None
                          or (m.get("wait_result") == "ok" and not busy_at_send)):
        _set(store, m, "returned", "sender was waiting for this turn")
        return False
    for other in store.state().values():
        if (other["ts"] >= m["ts"] and other["id"] != m["id"]
                and (other.get("from") or {}).get("pane") == to.get("pane")
                and (other.get("to") or {}).get("pane") == sender.get("pane")):
            _set(store, m, "replied", f"recipient prompted the sender back ({other['id']})")
            return False
    _set(store, m, "judging")
    return True


def on_gone(store: Store, cfg: dict, pane: str, how: str) -> None:
    with store.lock():
        for m in _open_for(store, pane):
            _set(store, m, "orphaned", f"recipient pane {how}")
            _notify(cfg, f"whisper: {label(m['to'])} {how} mid-conversation",
                    f"message from {label(m['from'])}: {m['text'][:160]}")


def on_moved(store: Store, pane: str) -> None:
    term = (herdr.pane_get(pane) or {}).get("terminal_id")
    if not term:
        return
    with store.lock():
        for m in store.state().values():
            if m["state"] not in OPEN_STATES:
                continue
            for side in ("to", "from"):
                end = m.get(side) or {}
                if end.get("terminal") == term and end.get("pane") != pane:
                    store.append({"t": "remap", "id": m["id"], "side": side, "pane": pane})


def _open_for(store: Store, pane: str) -> list:
    return [m for m in store.state().values()
            if m["state"] in OPEN_STATES and (m.get("to") or {}).get("pane") == pane]


# --- judge + push ------------------------------------------------------------

def run_judge(store: Store, cfg: dict, mid: str, dry: bool = False) -> judge.Verdict:
    m = store.state()[mid]
    output = herdr.pane_read(m["to"]["pane"], int(cfg.get("read_lines", 150))) or ""
    prompt = judge.build_prompt(label(m["from"]), label(m["to"]), m["text"], output,
                                m["pushes"], int(cfg.get("max_pushes", 2)))
    verdict = judge.run(prompt, cfg.get("judge_command") or [], float(cfg.get("judge_timeout_s", 120)))
    if not dry:
        apply_verdict(store, cfg, mid, verdict)
    return verdict


def apply_verdict(store: Store, cfg: dict, mid: str, v: judge.Verdict) -> None:
    with store.lock():
        m = store.state()[mid]
        if m["state"] != "judging":  # e.g. pane closed meanwhile
            return
        store.append({"t": "verdict", "id": mid, "action": v.action, "message": v.message,
                      "reason": v.error or v.reason})
        if v.error:
            return _escalate(store, cfg, m, v.error)
        if v.action == "close":
            return _set(store, m, "closed", v.reason)
        if v.action == "human":
            return _escalate(store, cfg, m, v.reason or "judge asked for the human")
        if not cfg.get("push", True):
            return _escalate(store, cfg, m, f"dry run: would {v.action} ({v.reason})")

        sender, to = m["from"], m["to"]
        if v.action == "forward":
            if _forwards_last_hour(store, sender, to) >= int(cfg.get("max_forwards_per_pair_per_hour", 12)):
                return _escalate(store, cfg, m, "forward limit for this pair reached (possible loop)")
            if not _same_terminal(sender):
                return _escalate(store, cfg, m, "sender pane is gone or hosts something else")
            text = (f"[whisper] {label(to)} finished the message you sent at {clock(m['ts'])} "
                    f"(\"{_short(m['text'], 80)}\"):\n{v.message}\n"
                    f"(full output: herdr agent read {to['pane']} --source recent-unwrapped --lines 200)")
            return _push(store, cfg, m, sender["pane"], text, "forwarded", v.reason)

        if m["pushes"] >= int(cfg.get("max_pushes", 2)):
            return _escalate(store, cfg, m, f"would {v.action} again, but the push limit is reached")
        if v.action == "resend":
            text = (f"[whisper] Resending a message from {label(sender)} (sent {clock(m['ts'])}) "
                    f"that looks unhandled:\n{m['text']}")
            return _push(store, cfg, m, to["pane"], text, "resent", v.reason)
        text = f"[whisper] About the message from {label(sender)} (sent {clock(m['ts'])}): {v.message}"
        return _push(store, cfg, m, to["pane"], text, "nudged", v.reason)


def _push(store: Store, cfg: dict, m: dict, pane: str, text: str, state: str, reason: str) -> None:
    res = herdr.agent_prompt(pane, text)
    if res.ok:
        _set(store, m, state, reason)
    else:
        _escalate(store, cfg, m, f"could not push ({state}): {res.code} {res.message}".strip())


def _escalate(store: Store, cfg: dict, m: dict, reason: str) -> None:
    _set(store, m, "escalated", reason)
    _notify(cfg, f"whisper: {label(m['from'])} → {label(m['to'])} needs you",
            f"{reason} · message: {m['text'][:150]}")


def _forwards_last_hour(store: Store, a: dict, b: dict) -> int:
    since = time.time() - 3600
    n = 0
    for m in store.state().values():
        if ((m.get("from") or {}).get("pane"), (m.get("to") or {}).get("pane")) in (
                (a.get("pane"), b.get("pane")), (b.get("pane"), a.get("pane"))):
            n += sum(1 for ts, st, _ in m["history"] if st == "forwarded" and ts >= since)
    return n


def _same_terminal(side: dict) -> bool:
    now = herdr.describe(side.get("pane") or "")
    if not now:
        return False
    return not side.get("terminal") or not now.get("terminal") or now["terminal"] == side["terminal"]


# --- time-based checks -------------------------------------------------------

def sweep(store: Optional[Store] = None, cfg: Optional[dict] = None,
          now: Optional[float] = None) -> int:
    """Runs from every hook, the feed pane (every 5 s) and server startup,
    so there is no daemon. Returns the number of open messages."""
    store = store or Store()
    cfg = cfg or load_config()
    now = now or time.time()
    grace = float(cfg.get("settle_grace_s", 20))
    to_judge = []
    with store.lock():
        for m in [m for m in store.state().values() if m["state"] in OPEN_STATES]:
            age, state = now - m["updated"], m["state"]
            pane = (m.get("to") or {}).get("pane")
            if state == "queued" and age > 60:
                _set(store, m, "failed", "delivery never completed")
            elif state == "judging" and age > float(cfg.get("judge_timeout_s", 120)) + 60:
                _escalate(store, cfg, m, "judge never finished")
            elif state in {"delivered", "resent", "nudged", "stalled"} and pane:
                info = herdr.describe(pane)
                if info is None:
                    _set(store, m, "orphaned", "recipient pane no longer exists")
                    continue
                status = info.get("status")
                busy = (m["to"] or {}).get("status_at_send") == "working"
                if status == "working":
                    # Busy at send: this may still be the earlier turn, so
                    # only the status hooks may promote it.
                    if not (busy and state == "delivered"):
                        _set(store, m, "working", "picked up (seen by sweep)")
                elif status in SETTLED and age > grace and (busy or state in {"resent", "nudged"}):
                    # The message was most likely folded into the busy turn.
                    if settle(store, m):
                        to_judge.append(m["id"])
                elif state == "delivered" and age > float(cfg.get("start_timeout_s", 120)):
                    _set(store, m, "stalled", f"recipient has not started after {int(age)}s ({status})")
                    _notify(cfg, f"whisper: {label(m['to'])} has not picked up a message",
                            f"from {label(m['from'])}: {m['text'][:160]}")
            elif state == "working" and age > float(cfg.get("work_timeout_s", 1800)):
                if not any("long-running" in n[1] for n in m.get("notices", [])):
                    store.append({"t": "notice", "id": m["id"],
                                  "detail": f"long-running: working for {int(age)}s"})
                    _notify(cfg, f"whisper: {label(m['to'])} still on a message",
                            f"{int(age // 60)} min, from {label(m['from'])}")
    for mid in to_judge:
        run_judge(store, cfg, mid)
    return sum(1 for m in store.state().values() if m["state"] in OPEN_STATES)


# --- helpers -----------------------------------------------------------------

def _set(store: Store, m: dict, state: str, detail: str = "") -> None:
    store.append({"t": "state", "id": m["id"], "state": state, "detail": detail})
    m["state"] = state


def _notify(cfg: dict, title: str, body: str = "") -> None:
    if cfg.get("notify", True):
        herdr.notify(title, body)


def _short(text: str, n: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= n else text[: n - 1] + "…"


def _safe(fn) -> None:
    try:
        fn()
    except Exception as exc:
        _debug(f"ignored: {exc!r}")


def _debug(msg: str) -> None:
    if os.environ.get("WHISPER_DEBUG"):
        print(f"whisper: {msg}", file=sys.stderr)


def plugin_id() -> str:
    return os.environ.get("HERDR_PLUGIN_ID") or PLUGIN_ID


def dumps(m: dict) -> str:
    return json.dumps(m, indent=2, ensure_ascii=False)
