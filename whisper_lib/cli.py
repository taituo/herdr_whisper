"""whisper — observable agent-to-agent messages for Herdr.

Agents:
  whisper send <agent|pane> "text" [--expect-reply] [--wait [--timeout S]]
  whisper reply <msg-id> "text"
  whisper ack <msg-id> | whisper reject <msg-id> "why"
  whisper inbox                      messages waiting for *your* reply

Humans:
  whisper feed                       live view (also a Herdr plugin pane)
  whisper status | log [-f] | show <msg-id> | sweep

Plugin internals:
  whisper hook                       Herdr event/startup hook entrypoint
  whisper open-feed | install-shim
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from . import core, herdr, view
from .store import OPEN_STATES, Store, config_root, load_config, state_root


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="whisper", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("send", help="send a message to another agent")
    p.add_argument("to", help="agent name or pane id (see `herdr agent list`)")
    p.add_argument("text", nargs="+")
    p.add_argument("--expect-reply", "-r", action="store_true",
                   help="track this message until the recipient replies")
    p.add_argument("--wait", "-w", action="store_true",
                   help="block until the message is resolved; prints the reply")
    p.add_argument("--timeout", type=float, default=900, help="seconds for --wait (default 900)")
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("reply", help="answer a message you received")
    p.add_argument("id")
    p.add_argument("text", nargs="+")
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("ack", help="the reply to your message answered it")
    p.add_argument("id")
    p.add_argument("note", nargs="*")
    p = sub.add_parser("reject", help="the reply to your message did not answer it")
    p.add_argument("id")
    p.add_argument("note", nargs="+")

    p = sub.add_parser("inbox", help="messages addressed to this pane that still need a reply")
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("status", help="open and recently failed messages")
    p.add_argument("--all", "-a", action="store_true")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("log", help="message history")
    p.add_argument("-f", "--follow", action="store_true")
    p.add_argument("-n", type=int, default=40)
    p.add_argument("--raw", action="store_true", help="print raw JSONL records")
    p = sub.add_parser("show", help="one message with its full history")
    p.add_argument("id")
    p.add_argument("--json", action="store_true")
    sub.add_parser("feed", help="live view")
    sub.add_parser("sweep", help="run time-based checks now")
    sub.add_parser("hook", help=argparse.SUPPRESS)
    sub.add_parser("open-feed", help=argparse.SUPPRESS)
    p = sub.add_parser("install-shim", help="link `whisper` into ~/.local/bin")
    p.add_argument("--dir", default=str(Path.home() / ".local" / "bin"))
    sub.add_parser("where", help="print log and config locations")

    args = parser.parse_args(argv)
    try:
        return COMMANDS[args.cmd](args)
    except core.WhisperError as exc:
        print(f"whisper: {exc}", file=sys.stderr)
        return 1
    except herdr.HerdrUnavailable as exc:
        print(f"whisper: {exc}", file=sys.stderr)
        return 3


# --- agent commands ----------------------------------------------------------

def cmd_send(args) -> int:
    m = core.send(args.to, " ".join(args.text), expect_reply=args.expect_reply)
    return _report_sent(m, args)


def cmd_reply(args) -> int:
    m = core.reply(args.id, " ".join(args.text))
    if args.json:
        print(json.dumps(m))
    elif m["state"] == "failed":
        print(f"whisper: reply {m['id']} not delivered: {m['history'][-1][2]}", file=sys.stderr)
        return 1
    else:
        print(f"replied msg:{m['id']} re:{args.id} → {core.label(m['to'])}")
    return 0


def _report_sent(m: dict, args) -> int:
    if m["state"] == "failed":
        if args.json:
            print(json.dumps(m))
        print(f"whisper: msg {m['id']} not delivered: {m['history'][-1][2]}", file=sys.stderr)
        return 1
    if args.wait:
        m = core.wait_for(m["id"], args.timeout)
    if args.json:
        print(json.dumps(_with_replies(m)))
        return 0
    flag = " [reply expected]" if m.get("expect_reply") else ""
    print(f"sent msg:{m['id']} → {core.label(m['to'])}{flag} ({m['state']})")
    if args.wait:
        replies = _with_replies(m).get("reply_texts") or []
        for text in replies:
            print(f"reply: {text}")
        if m["state"] in OPEN_STATES:
            print(f"(still {m['state']} after {int(args.timeout)}s)")
            return 4
        if m["state"] not in {"done", "replied", "acked"}:
            return 5
    return 0


def _with_replies(m: dict) -> dict:
    messages = Store().state()
    m = dict(messages.get(m["id"], m))
    m["reply_texts"] = [messages[r]["text"] for r in m.get("replies", []) if r in messages]
    return m


def cmd_verdict(args) -> int:
    ok = args.cmd == "ack"
    m = core.verdict(args.id, ok, " ".join(args.note))
    print(f"{'acked' if ok else 'rejected'} msg:{m['id']}")
    return 0


def cmd_inbox(args) -> int:
    me = os.environ.get("HERDR_PANE_ID")
    if not me:
        raise core.WhisperError("inbox needs HERDR_PANE_ID (run it inside a Herdr pane)")
    waiting = {"delivered", "working", "blocked", "nudged", "stalled", "unanswered"}
    items = [m for m in Store().state().values()
             if (m.get("to") or {}).get("pane") == me and m.get("expect_reply")
             and m["state"] in waiting]
    items.sort(key=lambda m: m["ts"])
    if args.json:
        print(json.dumps(items))
        return 0
    if not items:
        print("inbox empty: nothing is waiting for your reply")
    for m in items:
        print(f"msg:{m['id']} from {core.label(m['from'])} ({m['state']}): {m['text']}")
        print(f"  answer with: whisper reply {m['id']} \"<answer>\"")
    return 0


# --- human commands ----------------------------------------------------------

def cmd_status(args) -> int:
    store = Store()
    core.sweep(store)
    messages = store.state()
    items = view.threads(messages, open_only=not args.all)
    if args.json:
        print(json.dumps([m for m, _ in items]))
        return 0
    if not items:
        print("no open messages" + ("" if args.all else " (whisper status --all for history)"))
    view.print_rows(items, view.use_color())
    return 0


def cmd_log(args) -> int:
    store = Store()
    if args.raw:
        recs = [rec for _, rec in store.records()]
        for rec in recs[-args.n:]:
            print(json.dumps(rec, ensure_ascii=False))
    else:
        messages = sorted(store.state().values(), key=lambda m: m["ts"])[-args.n:]
        view.print_rows(((m, 0) for m in messages), view.use_color())
    if args.follow:
        return view.follow(store, view.use_color())
    return 0


def cmd_show(args) -> int:
    m = Store().state().get(args.id)
    if m is None:
        raise core.WhisperError(f"unknown message id {args.id}")
    print(json.dumps(m, indent=2, ensure_ascii=False) if args.json else view.detail(m))
    return 0


def cmd_feed(args) -> int:
    return view.feed(Store())


def cmd_sweep(args) -> int:
    n = core.sweep()
    print(f"{n} open message(s)")
    return 0


def cmd_where(args) -> int:
    store = Store()
    print(f"log     {store.log_path}")
    print(f"state   {state_root()}")
    print(f"config  {config_root() / 'config.toml'}")
    print(f"mode    {load_config()['mode']}")
    return 0


# --- plugin internals --------------------------------------------------------

def cmd_hook(args) -> int:
    """Event and startup hook. Must be fast and must never fail loudly: Herdr
    spawns one of these for every agent status change in the session."""
    event = os.environ.get("HERDR_PLUGIN_EVENT", "")
    try:
        store = Store()
        if event == "startup":
            core.sweep(store)
            return 0
        payload = json.loads(os.environ.get("HERDR_PLUGIN_EVENT_JSON") or "{}")
        data = payload.get("data") or {}
        if event:
            core.handle_event(event, data, store)
        if store.has_open():
            core.sweep(store)
    except Exception as exc:  # logged by Herdr's plugin command log
        print(f"whisper hook error ({event}): {exc!r}", file=sys.stderr)
        return 1
    return 0


def cmd_open_feed(args) -> int:
    res = herdr.open_plugin_pane(core.plugin_id(), "feed", "split")
    if not res.ok:
        print(f"whisper: could not open feed: {res.code} {res.message}", file=sys.stderr)
        return 1
    return 0


def cmd_install_shim(args) -> int:
    target = Path(args.dir)
    target.mkdir(parents=True, exist_ok=True)
    source = Path(__file__).resolve().parent.parent / "bin" / "whisper"
    link = target / "whisper"
    if link.is_symlink() or link.exists():
        link.unlink()
    # A wrapper rather than a symlink: GitHub installs live in versioned
    # directories that Herdr cleans up after updates, so ask Herdr for the
    # current plugin root each time and fall back to this install.
    link.write_text(SHIM.format(plugin=core.plugin_id(), fallback=str(source.parent.parent)))
    link.chmod(0o755)
    print(f"installed {link} → {source}")
    if str(target) not in os.environ.get("PATH", "").split(os.pathsep):
        print(f"note: {target} is not on PATH for this shell")
    return 0


SHIM = """#!/bin/sh
# whisper shim, written by `whisper install-shim`
root=$("${{HERDR_BIN_PATH:-herdr}}" plugin list --plugin {plugin} --json 2>/dev/null | python3 -c '
import json, sys
try:
    data = json.load(sys.stdin).get("result", {{}})
    plugins = data.get("plugins") or [data.get("plugin") or {{}}]
    print(next(p["plugin_root"] for p in plugins if p.get("plugin_id") == "{plugin}"))
except Exception:
    pass
')
[ -f "$root/bin/whisper" ] || root='{fallback}'
exec python3 "$root/bin/whisper" "$@"
"""


COMMANDS = {
    "send": cmd_send, "reply": cmd_reply, "ack": cmd_verdict, "reject": cmd_verdict,
    "inbox": cmd_inbox, "status": cmd_status, "log": cmd_log, "show": cmd_show,
    "feed": cmd_feed, "sweep": cmd_sweep, "hook": cmd_hook, "open-feed": cmd_open_feed,
    "install-shim": cmd_install_shim, "where": cmd_where,
}

if __name__ == "__main__":
    sys.exit(main())
