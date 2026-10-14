"""Human-facing rendering: one-line rows, message detail, and the live feed."""
from __future__ import annotations

import os
import select
import shutil
import sys
import time
from typing import Iterable

from .core import label, sweep
from .store import OPEN_STATES, Store, load_config

COLORS = {
    "queued": "37", "delivered": "36", "working": "33", "blocked": "35;1",
    "stalled": "35", "judging": "34;1", "resent": "33;1", "nudged": "33;1",
    "returned": "32", "replied": "32;1", "forwarded": "32;1", "closed": "32",
    "escalated": "31;1", "failed": "31;1", "orphaned": "31",
}
# Worth a human look even though tracking ended.
ATTENTION = {"escalated", "failed", "orphaned"}


def _c(code: str, text: str, color: bool) -> str:
    return f"\x1b[{code}m{text}\x1b[0m" if color else text


def clock(ts: float) -> str:
    return time.strftime("%H:%M:%S", time.localtime(ts))


def row(m: dict, width: int = 120, color: bool = False, indent: str = "") -> str:
    state = m["state"]
    flag = "w" if m.get("wait") else " "
    segments = [
        (f"{indent}{clock(m['ts'])} {m['id']} {flag} ", ""),
        (f"{state:<10}", COLORS.get(state, "0")),
        (f" {label(m['from'])} → {label(m['to'])}  ", ""),
        (" ".join(m["text"].split()), "2"),
    ]
    out, used = [], 0
    for text, code in segments:  # never wrap: cut the line at the pane width
        room = width - used
        if room <= 0:
            break
        if len(text) > room:
            text = text[: max(0, room - 1)] + "…"
        used += len(text)
        out.append(_c(code, text, color) if code else text)
    return "".join(out)


def rows_for(messages: dict, open_only: bool = False) -> list:
    items = sorted(messages.values(), key=lambda m: m["ts"], reverse=True)
    if open_only:
        items = [m for m in items if m["state"] in OPEN_STATES or m["state"] in ATTENTION]
    return items


def detail(m: dict) -> str:
    to = m.get("to") or {}
    lines = [
        f"msg {m['id']}   {m['state']}" + ("   (sender used --wait)" if m.get("wait") else ""),
        f"from  {label(m['from'])}   [claimed by the sender's environment, not verified]",
        f"to    {label(to)}" + (f"   (was {to.get('status_at_send')} at send)"
                               if to.get("status_at_send") else ""),
    ]
    if m.get("wait_result"):
        lines.append(f"wait  {m['wait_result']}")
    v = m.get("verdict")
    if v:
        lines.append(f"judge {v.get('action')}: {v.get('reason') or ''}".rstrip())
        if v.get("message"):
            lines.append("      " + v["message"].replace("\n", "\n      "))
    lines += ["", m["text"], "", "history:"]
    for ts, state, info in m["history"]:
        lines.append(f"  {clock(ts)}  {state:<10} {info}")
    for ts, info in m.get("notices", []):
        lines.append(f"  {clock(ts)}  notice     {info}")
    return "\n".join(lines)


def print_rows(items: Iterable[dict], color: bool) -> None:
    width = shutil.get_terminal_size((120, 40)).columns
    for m in items:
        print(row(m, width, color))


# --- live feed ---------------------------------------------------------------

def feed(store: Store) -> int:
    """Full-screen feed for a Herdr plugin pane. q quits, o toggles
    open-only, j/k scroll."""
    cfg = load_config()
    tty = sys.stdin.isatty()
    old = None
    if tty:
        import termios
        import tty as tty_mod
        old = termios.tcgetattr(sys.stdin)
        tty_mod.setcbreak(sys.stdin.fileno())
    sys.stdout.write("\x1b[?25l")
    open_only, scroll = False, 0
    last_size, last_draw, last_sweep = -1, 0.0, 0.0
    try:
        while True:
            now = time.time()
            if now - last_sweep > 5:
                try:
                    sweep(store, cfg)
                except Exception as exc:  # keep the view alive
                    sys.stderr.write(f"sweep failed: {exc}\n")
                last_sweep = now
            size = store.log_path.stat().st_size if store.log_path.exists() else 0
            if size != last_size or now - last_draw > 15:
                _draw(store, open_only, scroll)
                last_size, last_draw = size, now
            if tty and select.select([sys.stdin], [], [], 0.5)[0]:
                key = sys.stdin.read(1)
                if key in ("q", "\x03"):
                    return 0
                if key == "o":
                    open_only, scroll = not open_only, 0
                elif key == "j":
                    scroll += 1
                elif key == "k":
                    scroll = max(0, scroll - 1)
                last_size = -1
            elif not tty:
                time.sleep(0.5)
    except KeyboardInterrupt:
        return 0
    finally:
        sys.stdout.write("\x1b[?25h\n")
        if old is not None:
            import termios
            termios.tcsetattr(sys.stdin, termios.TCSADRAIN, old)


def _draw(store: Store, open_only: bool, scroll: int) -> None:
    cols, rows_ = shutil.get_terminal_size((100, 30))
    messages = store.state()
    items = rows_for(messages, open_only)
    n_open = sum(1 for m in messages.values() if m["state"] in OPEN_STATES)
    header = (f" whisper · {len(messages)} messages · {n_open} open"
              f"{' · open + needs attention' if open_only else ''}")
    keys = "q quit · o open-only · j/k scroll"
    bar = (header.ljust(max(0, cols - len(keys) - 1)) + keys + " ")[:cols]
    out = ["\x1b[H\x1b[2J", _c("7", bar, True), ""]
    body = items[scroll: scroll + max(1, rows_ - 4)]
    if not body:
        out.append("  no agent-to-agent messages yet (agents use `herdr agent prompt` as usual)")
    for m in body:
        out.append(row(m, cols, True, indent=" "))
    sys.stdout.write("\n".join(out))
    sys.stdout.flush()


def follow(store: Store, color: bool) -> int:
    """`whisper log -f`: print new log records as they arrive."""
    offset = store.log_path.stat().st_size if store.log_path.exists() else 0
    try:
        while True:
            for offset, rec in store.records(offset):
                print(_record_line(rec, store, color), flush=True)
            time.sleep(0.5)
    except KeyboardInterrupt:
        return 0


def _record_line(rec: dict, store: Store, color: bool) -> str:
    t = rec.get("t")
    if t == "msg":
        m = store.state(save=False).get(rec["id"]) or {**rec, "state": "queued"}
        return row({**m, "state": "queued"}, shutil.get_terminal_size((120, 40)).columns, color)
    if t == "verdict":
        return f"{clock(rec['ts'])} {rec['id']}   judge: {rec.get('action')} — {rec.get('reason') or ''}".rstrip()
    if t == "state":
        badge = _c(COLORS.get(rec["state"], "0"), rec["state"], color)
        return f"{clock(rec['ts'])} {rec['id']}   → {badge} {rec.get('detail', '')}".rstrip()
    return f"{clock(rec['ts'])} {rec.get('id', '')}   {t} {rec.get('detail') or rec.get('note') or ''}".rstrip()


def use_color() -> bool:
    return sys.stdout.isatty() and not os.environ.get("NO_COLOR")
