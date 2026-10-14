"""Sending, reply tracking and follow-up.

Lifecycle of a message (states live in the log, see store.fold):

  queued ──deliver──> delivered ──recipient working──> working ──recipient settles──┐
     │                    │  ▲                            │                         │
     │                    │  └────────── nudged <─────────┼── (active mode, no reply)
     └─> failed           └─> stalled (never started)     └─> blocked (approval UI) │
                                                                                     v
          done (no reply expected) | replied | unanswered | orphaned (pane closed)
          replied ──sender judges──> acked | rejected

Delivery, state tracking and "no reply arrived" are mechanical. The only
judgment call — did the reply actually answer the question — is left to the
sender agent itself (`whisper ack` / `whisper reject`), never to whisper.
"""
from __future__ import annotations

import os
import re
import secrets
import time
from typing import Optional

from . import herdr
from .store import FINAL_STATES, OPEN_STATES, PLUGIN_ID, Store, load_config

SETTLED = {"idle", "done"}


class WhisperError(Exception):
    pass


# --- identity ----------------------------------------------------------------

def whoami() -> dict:
    """The sender as claimed by the environment. Not proof of anything: any
    process that sets HERDR_PANE_ID can claim to be that pane."""
    pane = os.environ.get("HERDR_PANE_ID")
    if pane:
        info = herdr.describe(pane)
        if info:
            return info
        return {"pane": pane, "terminal": None, "agent": None, "name": None, "status": None}
    return {"pane": None, "terminal": None, "agent": None,
            "name": os.environ.get("USER") or "human", "status": None, "human": True}


def label(side: Optional[dict]) -> str:
    if not side:
        return "?"
    who = side.get("name") or side.get("agent") or ("human" if side.get("human") else "pane")
    return f"{who}@{side['pane']}" if side.get("pane") else who


def new_id(messages: dict) -> str:
    while True:
        mid = secrets.token_hex(3)
        if mid not in messages:
            return mid


def envelope(m: dict, cfg: dict) -> str:
    """The text the recipient actually sees. The header carries the token the
    tracker looks for and tells the recipient how to answer."""
    head = f"[whisper msg:{m['id']}"
    if m.get("re"):
        head += f" re:{m['re']}"
    head += f" from {label(m['from'])}"
    if m.get("expect_reply"):
        head += f" · reply expected: whisper reply {m['id']} \"<answer>\""
    elif m.get("re") and cfg.get("mode") == "active":
        head += f" · if this does not answer you: whisper reject {m['re']} \"<why>\", else whisper ack {m['re']}"
    return f"{head}] {m['text']}"


# --- sending -----------------------------------------------------------------

def send(target: str, text: str, expect_reply: bool = False, re_id: Optional[str] = None,
         store: Optional[Store] = None, cfg: Optional[dict] = None,
         to_override: Optional[dict] = None) -> dict:
    store = store or Store()
    cfg = cfg or load_config()
    text = text.strip()
    if not text:
        raise WhisperError("empty message")
    sender = whoami()

    if to_override is not None:
        to = dict(to_override)
    else:
        to = herdr.describe(target)
        if to is None:
            raise WhisperError(f"no agent or pane named {target!r} (try `herdr agent list`)")
        to["target"] = target
    if to.get("pane") and to.get("pane") == sender.get("pane"):
        raise WhisperError("refusing to whisper to yourself")
    to["status_at_send"] = to.pop("status", None)

    with store.lock():
        messages = store.state()
        if re_id and re_id not in messages:
            raise WhisperError(f"unknown message id {re_id}")
        mid = new_id(messages)
        rec = store.append({"t": "msg", "id": mid, "from": sender, "to": to, "text": text,
                            "expect_reply": bool(expect_reply), "re": re_id, "via": None})
        # Deliver while holding the lock: the recipient's status hooks fire as
        # soon as the prompt lands, and they must see `delivered`, not `queued`.
        _deliver(store, cfg, dict(rec))
    sweep(store, cfg)
    return store.state()[mid]


def reply(parent_id: str, text: str, store: Optional[Store] = None,
          cfg: Optional[dict] = None) -> dict:
    store = store or Store()
    cfg = cfg or load_config()
    parent = store.state().get(parent_id)
    if parent is None:
        raise WhisperError(f"unknown message id {parent_id}")
    origin = parent["from"] or {}
    if origin.get("pane"):
        to = herdr.describe(origin["pane"]) or {"pane": origin["pane"], "terminal": None,
                                                 "agent": None, "name": None, "status": None}
        if origin.get("terminal") and to.get("terminal") and origin["terminal"] != to["terminal"]:
            raise WhisperError(f"the sender of {parent_id} is gone: pane {origin['pane']} "
                               "now hosts a different terminal")
        to["target"] = origin["pane"]
    else:
        to = {**origin, "target": origin.get("name"), "status": None}
    return send(to["target"] or "", text, expect_reply=False, re_id=parent_id,
                store=store, cfg=cfg, to_override=to)


def verdict(parent_id: str, ok: bool, note: str = "", store: Optional[Store] = None,
            cfg: Optional[dict] = None) -> dict:
    store = store or Store()
    cfg = cfg or load_config()
    with store.lock():
        m = store.state().get(parent_id)
        if m is None:
            raise WhisperError(f"unknown message id {parent_id}")
        store.append({"t": "verdict", "id": parent_id, "ok": ok, "note": note})
        _set(store, m, "acked" if ok else "rejected", note)
    if not ok:
        _notify(cfg, f"whisper: reply rejected by {label(m['from'])}",
                f"msg {parent_id} → {label(m['to'])}: {note or 'no reason given'}")
    return store.state()[parent_id]


def _deliver(store: Store, cfg: dict, m: dict) -> None:
    to = m["to"] or {}
    text = envelope(m, cfg)

    if not to.get("pane"):
        # The other end is a human outside Herdr: the log is the mailbox.
        _set(store, m, "delivered", "logged only (recipient is not in a pane)")
        _after_delivery(store, cfg, m)
        return

    res = herdr.agent_prompt(to["pane"], text)
    via = "agent_prompt"
    if not res.ok and res.code == "agent_not_ready" and cfg.get("raw_fallback"):
        current = herdr.describe(to["pane"]) or {}
        if current.get("status") == "blocked":
            res = herdr.HerdrResult(False, {}, "agent_blocked", "recipient is blocked")
        else:
            res = herdr.pane_run(to["pane"], text)
            via = "pane_run"
    if res.ok:
        _set(store, m, "delivered", via)
        _after_delivery(store, cfg, m)
    else:
        detail = f"{res.code}: {res.message}".strip(": ")
        if res.code == "agent_not_found":
            detail += " (no agent is running in that pane)"
        elif res.code == "agent_not_ready" and not cfg.get("raw_fallback"):
            detail += " (Herdr can't prompt this agent; set raw_fallback = true to use pane run)"
        _set(store, m, "failed", detail)
        if m.get("re"):
            _notify(cfg, f"whisper: reply to {m['re']} did not reach {label(to)}", detail)
        elif m.get("expect_reply"):
            _notify(cfg, f"whisper: could not deliver to {label(to)}", detail)


def _after_delivery(store: Store, cfg: dict, m: dict) -> None:
    """A reply that reached its sender closes the question it answers."""
    parent_id = m.get("re")
    if not parent_id:
        return
    with store.lock():
        parent = store.state().get(parent_id)
        if parent and parent["state"] not in {"replied", "acked", "rejected"}:
            late = " (late)" if parent["state"] in FINAL_STATES else ""
            _set(store, parent, "replied", f"reply {m['id']} delivered{late}")
    if not (m["to"] or {}).get("pane"):
        _set(store, m, "done", "reply kept in log")


# --- tracking ----------------------------------------------------------------

def handle_event(event: str, data: dict, store: Optional[Store] = None,
                 cfg: Optional[dict] = None) -> None:
    store = store or Store()
    cfg = cfg or load_config()
    pane = data.get("pane_id")
    if not pane or not store.has_open():
        return
    if event == "pane.agent_status_changed":
        on_status(store, cfg, pane, data.get("agent_status") or "")
    elif event in ("pane.closed", "pane.exited"):
        on_gone(store, cfg, pane, event.split(".")[1])
    elif event == "pane.moved":
        on_moved(store, pane)


def on_status(store: Store, cfg: dict, pane: str, status: str) -> None:
    with store.lock():
        for m in _open_for(store, pane):
            state = m["state"]
            if status == "working" and state in {"delivered", "nudged", "stalled", "blocked"}:
                _set(store, m, "working")
            elif status == "blocked" and state in {"delivered", "working", "nudged", "stalled"}:
                _set(store, m, "blocked", "recipient is waiting on an approval or question")
                if m.get("expect_reply"):
                    _notify(cfg, f"whisper: {label(m['to'])} is blocked",
                            f"msg {m['id']} from {label(m['from'])} is waiting on it")
            elif status in SETTLED and state in {"working", "blocked"}:
                _evaluate(store, cfg, m)
            # A settle while still `delivered` belongs to a turn that started
            # before our message arrived; sweep() judges it after a grace period.


def on_gone(store: Store, cfg: dict, pane: str, how: str) -> None:
    with store.lock():
        for m in _open_for(store, pane):
            _set(store, m, "orphaned", f"recipient pane {how}")
            if m.get("expect_reply"):
                _notify(cfg, f"whisper: {label(m['to'])} {how} before replying",
                        f"msg {m['id']} from {label(m['from'])}")


def on_moved(store: Store, pane: str) -> None:
    info = herdr.pane_get(pane)
    term = (info or {}).get("terminal_id")
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


def _evaluate(store: Store, cfg: dict, m: dict) -> None:
    """The recipient settled. Decide what that means for this message."""
    if not m.get("expect_reply"):
        _set(store, m, "done", "recipient finished its turn")
        return
    current = store.state()[m["id"]]
    if current["state"] in {"replied", "acked", "rejected"}:
        return
    sender_pane = (m.get("from") or {}).get("pane")
    if sender_pane:
        output = herdr.pane_read(sender_pane, int(cfg.get("scan_lines", 400))) or ""
        if re.search(rf"\bre:{m['id']}\b", output):
            _set(store, m, "replied", f"saw re:{m['id']} in sender pane (raw reply)")
            return
    if cfg.get("mode") == "active" and current["nudges"] < int(cfg.get("max_nudges", 1)):
        nudge = (f"[whisper] msg:{m['id']} from {label(m['from'])} is still waiting for your "
                 f"reply. Answer with: whisper reply {m['id']} \"<answer>\"")
        res = herdr.agent_prompt(m["to"]["pane"], nudge)
        if res.ok:
            _set(store, m, "nudged", "recipient settled without replying; nudged")
            return
    _set(store, m, "unanswered", "recipient finished without replying")
    _notify(cfg, f"whisper: no reply from {label(m['to'])}",
            f"msg {m['id']} from {label(m['from'])}: {m['text'][:160]}")


def sweep(store: Optional[Store] = None, cfg: Optional[dict] = None,
          now: Optional[float] = None) -> int:
    """Time-based checks. Runs from every hook, CLI call and the feed pane, so
    there is no daemon to keep alive. Returns the number of open messages."""
    store = store or Store()
    cfg = cfg or load_config()
    now = now or time.time()
    grace = float(cfg.get("settle_grace_s", 20))
    start_to = float(cfg.get("start_timeout_s", 120))
    work_to = float(cfg.get("work_timeout_s", 1800))
    with store.lock():
        open_msgs = [m for m in store.state().values() if m["state"] in OPEN_STATES]
        for m in open_msgs:
            age = now - m["updated"]
            state = m["state"]
            if state == "queued" and age > 60:
                _set(store, m, "failed", "delivery never completed")
                continue
            pane = (m.get("to") or {}).get("pane")
            if not pane:
                continue
            if state in {"delivered", "nudged", "stalled"}:
                info = herdr.describe(pane)
                if info is None:
                    _set(store, m, "orphaned", "recipient pane no longer exists")
                    continue
                status = info.get("status")
                busy_at_send = (m["to"] or {}).get("status_at_send") == "working"
                if status == "working":
                    # If it was already busy at send time this may still be the
                    # earlier turn, so only the status hooks may promote it.
                    if not (busy_at_send and state == "delivered"):
                        _set(store, m, "working", "picked up (seen by sweep)")
                elif status in SETTLED and age > grace and (busy_at_send or state == "nudged"):
                    # Message landed during a busy turn and was most likely
                    # folded into it; judge now.
                    _evaluate(store, cfg, m)
                elif state == "delivered" and age > start_to:
                    _set(store, m, "stalled", f"recipient has not started after {int(age)}s "
                                              f"(status {status})")
                    _notify(cfg, f"whisper: {label(m['to'])} has not picked up msg {m['id']}",
                            m["text"][:160])
            elif state == "working" and m.get("expect_reply") and age > work_to:
                noted = any("long-running" in n[1] for n in m.get("notices", []))
                if not noted:
                    store.append({"t": "notice", "id": m["id"],
                                  "detail": f"long-running: working for {int(age)}s"})
                    _notify(cfg, f"whisper: {label(m['to'])} still working on msg {m['id']}",
                            f"{int(age // 60)} min, reply outstanding")
        return sum(1 for m in store.state().values() if m["state"] in OPEN_STATES)


# --- helpers -----------------------------------------------------------------

def _set(store: Store, m: dict, state: str, detail: str = "") -> None:
    store.append({"t": "state", "id": m["id"], "state": state, "detail": detail})
    m["state"] = state


def _notify(cfg: dict, title: str, body: str = "") -> None:
    if cfg.get("notify", True):
        herdr.notify(title, body)


def wait_for(mid: str, timeout: float, store: Optional[Store] = None) -> dict:
    store = store or Store()
    deadline = time.time() + timeout
    cfg = load_config()
    last_sweep = 0.0
    while True:
        m = store.state().get(mid)
        if m is None:
            raise WhisperError(f"unknown message id {mid}")
        if m["state"] not in OPEN_STATES:
            return m
        if time.time() >= deadline:
            return m
        if time.time() - last_sweep > 5:
            sweep(store, cfg)
            last_sweep = time.time()
        time.sleep(0.5)


def plugin_id() -> str:
    return os.environ.get("HERDR_PLUGIN_ID") or PLUGIN_ID
