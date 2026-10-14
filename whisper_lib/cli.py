"""whisper — keeps agent-to-agent conversations in Herdr from silently dying.

Agents use Herdr exactly as the Herdr skill teaches (`herdr agent prompt ...`).
A thin `herdr` shim records who prompted whom; when the recipient returns,
whisper checks whether the sender will hear back and, if not, asks an LLM
judge whether to forward the answer, resend, nudge, close, or call you.

  whisper install [--rc]    install the herdr shim (+ PATH line for Herdr panes)
  whisper status [--all]    open conversations (or everything)
  whisper feed              live view (also the plugin pane `whisper.feed`)
  whisper show <id>         one message with its history and the judge's verdict
  whisper log [-f] [--raw]  history
  whisper judge <id>        dry-run the judge on a message (no pushing)
  whisper sweep | where
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from . import core, herdr, view
from .store import Store, config_root, load_config, state_root

SHIM_DIR = Path.home() / ".local" / "share" / "herdr-whisper" / "bin"
RC_BEGIN, RC_END = "# >>> herdr-whisper >>>", "# <<< herdr-whisper <<<"


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["tap"]:  # hot path from the shim: no argparse
        return core.tap(argv[1], argv[2:])

    parser = argparse.ArgumentParser(prog="whisper", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("install", help="install the herdr shim and the whisper command")
    p.add_argument("--rc", action="store_true",
                   help="also add the PATH line to ~/.bashrc and ~/.zshrc")
    sub.add_parser("uninstall", help="remove the shim, the whisper command and the rc lines")
    p = sub.add_parser("status", help="open conversations")
    p.add_argument("--all", "-a", action="store_true")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("log", help="history")
    p.add_argument("-f", "--follow", action="store_true")
    p.add_argument("-n", type=int, default=40)
    p.add_argument("--raw", action="store_true", help="raw JSONL records")
    p = sub.add_parser("show", help="one message with its full history")
    p.add_argument("id")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("judge", help="run the judge on a message without acting")
    p.add_argument("id")
    p.add_argument("--prompt", action="store_true", help="print the prompt instead")
    sub.add_parser("feed", help="live view")
    sub.add_parser("sweep", help="run time-based checks now")
    sub.add_parser("where", help="paths and active config")
    sub.add_parser("hook", help=argparse.SUPPRESS)
    sub.add_parser("open-feed", help=argparse.SUPPRESS)

    args = parser.parse_args(argv)
    try:
        return COMMANDS[args.cmd](args)
    except herdr.HerdrUnavailable as exc:
        print(f"whisper: {exc}", file=sys.stderr)
        return 3
    except KeyError as exc:
        print(f"whisper: unknown message id {exc}", file=sys.stderr)
        return 1


# --- views -------------------------------------------------------------------

def cmd_status(args) -> int:
    store = Store()
    core.sweep(store)
    items = view.rows_for(store.state(), open_only=not args.all)
    if args.json:
        print(json.dumps(items))
        return 0
    if not items:
        print("no open conversations" + ("" if args.all else " (whisper status --all for history)"))
    view.print_rows(items, view.use_color())
    return 0


def cmd_log(args) -> int:
    store = Store()
    if args.raw:
        for rec in [rec for _, rec in store.records()][-args.n:]:
            print(json.dumps(rec, ensure_ascii=False))
    else:
        view.print_rows(sorted(store.state().values(), key=lambda m: m["ts"])[-args.n:],
                        view.use_color())
    return view.follow(store, view.use_color()) if args.follow else 0


def cmd_show(args) -> int:
    m = Store().state()[args.id]
    print(core.dumps(m) if args.json else view.detail(m))
    return 0


def cmd_judge(args) -> int:
    store, cfg = Store(), load_config()
    m = store.state()[args.id]
    if args.prompt:
        from . import judge
        output = herdr.pane_read(m["to"]["pane"], int(cfg.get("read_lines", 150))) or ""
        print(judge.build_prompt(core.label(m["from"]), core.label(m["to"]), m["text"], output,
                                 m["pushes"], int(cfg.get("max_pushes", 2))))
        return 0
    v = core.run_judge(store, cfg, args.id, dry=True)
    print(json.dumps({"action": v.action, "message": v.message, "reason": v.reason,
                      "error": v.error}, indent=2, ensure_ascii=False))
    return 0


def cmd_feed(args) -> int:
    return view.feed(Store())


def cmd_sweep(args) -> int:
    print(f"{core.sweep()} open message(s)")
    return 0


def cmd_where(args) -> int:
    cfg = load_config()
    print(f"log     {Store().log_path}")
    print(f"state   {state_root()}")
    print(f"config  {config_root() / 'config.toml'}")
    print(f"shim    {SHIM_DIR / 'herdr'} ({'installed' if (SHIM_DIR / 'herdr').exists() else 'missing'})")
    print(f"judge   {' '.join(cfg['judge_command'])}   push={cfg['push']}")
    return 0


# --- plugin internals --------------------------------------------------------

def cmd_hook(args) -> int:
    """Event + startup hook. Herdr spawns one per agent status change, so it
    exits immediately when nothing is being tracked."""
    event = os.environ.get("HERDR_PLUGIN_EVENT", "")
    try:
        store = Store()
        if event and event != "startup":
            payload = json.loads(os.environ.get("HERDR_PLUGIN_EVENT_JSON") or "{}")
            core.handle_event(event, payload.get("data") or {}, store)
        if store.has_open():
            core.sweep(store)
    except Exception as exc:  # shows up in `herdr plugin log list`
        print(f"whisper hook error ({event}): {exc!r}", file=sys.stderr)
        return 1
    return 0


def cmd_open_feed(args) -> int:
    res = herdr.open_plugin_pane(core.plugin_id(), "feed", "split")
    if not res.ok:
        print(f"whisper: could not open feed: {res.code} {res.message}", file=sys.stderr)
        return 1
    return 0


# --- install -----------------------------------------------------------------

HERDR_SHIM = """#!/bin/sh
# herdr shim installed by whisper. It records `herdr agent prompt` calls made
# from Herdr panes, then runs the real herdr with the same arguments. Every
# other command goes straight through.
self_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
real="${{WHISPER_REAL_HERDR:-}}"
if [ -z "$real" ]; then
  old_ifs=$IFS; IFS=:
  for d in $PATH; do
    [ "$d" = "$self_dir" ] && continue
    if [ -x "$d/herdr" ]; then real="$d/herdr"; break; fi
  done
  IFS=$old_ifs
fi
if [ -z "$real" ]; then echo "herdr (whisper shim): real herdr not found on PATH" >&2; exit 127; fi
if [ "$1" = agent ] && [ "$2" = prompt ] && [ -n "${{HERDR_PANE_ID:-}}" ] \
   && [ -x "{entry}" ] && command -v python3 >/dev/null 2>&1; then
  HERDR_BIN_PATH="$real" exec "{entry}" tap "$real" "$@"
fi
exec "$real" "$@"
"""

WHISPER_CMD = """#!/bin/sh
# whisper command, written by `whisper install`. Asks Herdr for the current
# plugin root (GitHub installs move on update), falls back to this install.
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
if [ "$1" = tap ] && [ ! -f "$root/bin/whisper" ]; then
  real=$2; shift 2; exec "$real" "$@"   # whisper is gone: stay out of the way
fi
exec python3 "$root/bin/whisper" "$@"
"""

RC_LINE = (f'{RC_BEGIN}\n'
           '[ "${HERDR_ENV:-}" = 1 ] && case ":$PATH:" in *":$HOME/.local/share/herdr-whisper/bin:"*) ;; '
           '*) PATH="$HOME/.local/share/herdr-whisper/bin:$PATH" ;; esac\n'
           f'{RC_END}\n')


def cmd_install(args) -> int:
    root = Path(__file__).resolve().parent.parent
    SHIM_DIR.mkdir(parents=True, exist_ok=True)
    # The shim calls a stable launcher that asks Herdr for the current plugin
    # root, so plugin updates (new install directories) keep working.
    entry = SHIM_DIR.parent / "whisper-entry"
    entry.write_text(WHISPER_CMD.format(plugin=core.plugin_id(), fallback=str(root)))
    entry.chmod(0o755)
    shim = SHIM_DIR / "herdr"
    shim.write_text(HERDR_SHIM.format(entry=str(entry)))
    shim.chmod(0o755)
    local_bin = Path.home() / ".local" / "bin"
    local_bin.mkdir(parents=True, exist_ok=True)
    cmd = local_bin / "whisper"
    if cmd.is_symlink() or cmd.exists():
        cmd.unlink()
    cmd.write_text(WHISPER_CMD.format(plugin=core.plugin_id(), fallback=str(root)))
    cmd.chmod(0o755)
    print(f"shim     {shim}")
    print(f"command  {cmd}")
    edited = []
    if args.rc:
        for name in (".bashrc", ".zshrc"):
            rc = Path.home() / name
            if name == ".bashrc" or rc.exists():
                text = rc.read_text() if rc.exists() else ""
                if RC_BEGIN not in text:
                    rc.write_text(text + ("\n" if text and not text.endswith("\n") else "") + RC_LINE)
                    edited.append(str(rc))
        print("rc       " + (", ".join(edited) if edited else "already set up"))
    else:
        print("\nAdd this to your shell rc so agents in Herdr panes get the shim first on PATH:\n")
        print(RC_LINE)
    print("New Herdr panes (and agents started in them) will use the shim. "
          "Restart agents in existing panes.")
    return 0


def cmd_uninstall(args) -> int:
    for path in (SHIM_DIR / "herdr", SHIM_DIR.parent / "whisper-entry",
                 Path.home() / ".local" / "bin" / "whisper"):
        if path.exists():
            path.unlink()
            print(f"removed  {path}")
    for name in (".bashrc", ".zshrc"):
        rc = Path.home() / name
        if rc.exists() and RC_BEGIN in rc.read_text():
            text = rc.read_text()
            a, b = text.index(RC_BEGIN), text.index(RC_END) + len(RC_END)
            rc.write_text(text[:a] + text[b:].lstrip("\n"))
            print(f"cleaned  {rc}")
    return 0


COMMANDS = {
    "install": cmd_install, "uninstall": cmd_uninstall, "status": cmd_status, "log": cmd_log,
    "show": cmd_show, "judge": cmd_judge, "feed": cmd_feed, "sweep": cmd_sweep,
    "where": cmd_where, "hook": cmd_hook, "open-feed": cmd_open_feed,
}

if __name__ == "__main__":
    sys.exit(main())
