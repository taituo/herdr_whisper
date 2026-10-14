"""Append-only JSONL log + a folded snapshot cache.

The log (`log.jsonl`) is the source of truth. Every message and every state
change is one line. `snapshot.json` caches the folded state together with the
byte offset it covers; readers replay whatever the log gained since then, so a
crash between the two writes only costs a little replay, never data.

All writers take an exclusive flock on `lock`, so concurrent hook processes
(Herdr spawns one per event) serialize cleanly.
"""
from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import time
from pathlib import Path
from typing import Iterator, Optional

PLUGIN_ID = "whisper"
SCHEMA_V = 1

# States a message is still being tracked in.
OPEN_STATES = {"queued", "delivered", "working", "blocked", "nudged", "stalled"}
FINAL_STATES = {"done", "replied", "unanswered", "failed", "orphaned", "acked", "rejected"}


# --- locations ---------------------------------------------------------------

def _xdg(var: str, fallback: str) -> Path:
    value = os.environ.get(var)
    return Path(value) if value else Path.home() / fallback


def state_root() -> Path:
    if os.environ.get("WHISPER_HOME"):
        return Path(os.environ["WHISPER_HOME"])
    if os.environ.get("HERDR_PLUGIN_STATE_DIR"):
        return Path(os.environ["HERDR_PLUGIN_STATE_DIR"])
    return _xdg("XDG_STATE_HOME", ".local/state") / "herdr" / "plugins" / PLUGIN_ID


def config_root() -> Path:
    if os.environ.get("HERDR_PLUGIN_CONFIG_DIR"):
        return Path(os.environ["HERDR_PLUGIN_CONFIG_DIR"])
    return _xdg("XDG_CONFIG_HOME", ".config") / "herdr" / "plugins" / "config" / PLUGIN_ID


def socket_path() -> str:
    """Which Herdr server we talk to; one log per server (session)."""
    if os.environ.get("HERDR_SOCKET_PATH"):
        return os.environ["HERDR_SOCKET_PATH"]
    base = _xdg("XDG_CONFIG_HOME", ".config") / "herdr"
    session = os.environ.get("HERDR_SESSION")
    if session:
        return str(base / "sessions" / session / "herdr.sock")
    return str(base / "herdr.sock")


def session_dir() -> Path:
    sock = os.path.realpath(socket_path())
    key = hashlib.sha1(sock.encode()).hexdigest()[:10]
    path = state_root() / "sessions" / key
    path.mkdir(parents=True, exist_ok=True)
    meta = path / "session.json"
    if not meta.exists():
        meta.write_text(json.dumps({"socket": sock}) + "\n")
    return path


# --- config ------------------------------------------------------------------

DEFAULT_CONFIG = {
    # observe: track, log, notify the human. active: also nudge recipients
    # that settle without replying, and ask senders to ack/reject replies.
    "mode": "observe",
    "notify": True,
    "max_nudges": 1,
    # If the recipient was already busy when the message arrived, its first
    # settle may belong to the previous turn. Wait this long for a second turn
    # before judging.
    "settle_grace_s": 20,
    # Delivered but recipient never started working.
    "start_timeout_s": 120,
    # Working for longer than this with a reply outstanding -> "stalled" notice.
    "work_timeout_s": 1800,
    # When `herdr agent prompt` cannot address the recipient (agent_not_ready),
    # fall back to `herdr pane run`. Still refuses blocked recipients.
    "raw_fallback": False,
    # How many lines of the sender's pane to scan for a raw `re:<id>` reply.
    "scan_lines": 400,
}


def load_config() -> dict:
    cfg = dict(DEFAULT_CONFIG)
    path = config_root() / "config.toml"
    if path.exists():
        cfg.update(_parse_toml(path.read_text()))
    for key, env in (("mode", "WHISPER_MODE"),):
        if os.environ.get(env):
            cfg[key] = os.environ[env]
    return cfg


def _parse_toml(text: str) -> dict:
    try:
        import tomllib  # py3.11+
        return tomllib.loads(text)
    except ModuleNotFoundError:
        pass
    out: dict = {}
    for line in text.splitlines():
        line = line.split("#", 1)[0].strip()
        if "=" not in line or line.startswith("["):
            continue
        key, value = (part.strip() for part in line.split("=", 1))
        if value in ("true", "false"):
            out[key] = value == "true"
        elif value.lstrip("-").isdigit():
            out[key] = int(value)
        else:
            out[key] = value.strip("\"'")
    return out


# --- log ---------------------------------------------------------------------

class Store:
    def __init__(self, directory: Optional[Path] = None):
        self.dir = directory or session_dir()
        self.dir.mkdir(parents=True, exist_ok=True)
        self.log_path = self.dir / "log.jsonl"
        self.snap_path = self.dir / "snapshot.json"
        self.lock_path = self.dir / "lock"
        self._locked = False

    @contextlib.contextmanager
    def lock(self) -> Iterator["Store"]:
        if self._locked:
            yield self
            return
        with open(self.lock_path, "a") as fh:
            fcntl.flock(fh, fcntl.LOCK_EX)
            self._locked = True
            try:
                yield self
            finally:
                self._locked = False
                fcntl.flock(fh, fcntl.LOCK_UN)

    def append(self, record: dict) -> dict:
        record = {"v": SCHEMA_V, "ts": round(time.time(), 3), **record}
        line = json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"
        with self.lock():
            with open(self.log_path, "a+b") as fh:
                # A crash mid-write leaves a torn last line; terminate it so
                # this record is not glued onto it (readers skip the torn one).
                if fh.tell() > 0:
                    fh.seek(-1, os.SEEK_END)
                    if fh.read(1) != b"\n":
                        fh.write(b"\n")
                fh.write(line.encode("utf-8"))
                fh.flush()
                os.fsync(fh.fileno())
        return record

    def records(self, start: int = 0) -> Iterator[tuple[int, dict]]:
        """Yield (end_offset, record). Skips a torn trailing line."""
        if not self.log_path.exists():
            return
        with open(self.log_path, "rb") as fh:
            fh.seek(start)
            offset = start
            for raw in fh:
                if not raw.endswith(b"\n"):
                    break
                offset += len(raw)
                try:
                    yield offset, json.loads(raw)
                except json.JSONDecodeError:
                    continue

    # -- folded view --

    def state(self, save: bool = True) -> dict:
        snap = {"offset": 0, "messages": {}}
        if self.snap_path.exists():
            try:
                snap = json.loads(self.snap_path.read_text())
            except (json.JSONDecodeError, OSError):
                snap = {"offset": 0, "messages": {}}
        size = self.log_path.stat().st_size if self.log_path.exists() else 0
        if snap.get("offset", 0) > size:  # log was replaced/truncated
            snap = {"offset": 0, "messages": {}}
        messages = snap["messages"]
        offset = snap.get("offset", 0)
        changed = False
        for offset, rec in self.records(offset):
            fold(messages, rec)
            changed = True
        snap["offset"] = offset
        if changed and save:
            tmp = self.snap_path.with_suffix(f".{os.getpid()}.tmp")
            tmp.write_text(json.dumps(snap, ensure_ascii=False))
            os.replace(tmp, self.snap_path)
        return messages

    def has_open(self) -> bool:
        return any(m["state"] in OPEN_STATES for m in self.state().values())


def fold(messages: dict, rec: dict) -> None:
    """Apply one log record. Pure and deterministic: replaying the log from
    the start always yields the same state."""
    kind = rec.get("t")
    mid = rec.get("id")
    if kind == "msg":
        m = {k: rec.get(k) for k in ("id", "ts", "from", "to", "text", "expect_reply", "re", "via")}
        m.update(state="queued", history=[[rec["ts"], "queued", ""]], replies=[],
                 nudges=0, notices=[], updated=rec["ts"], verdict=None)
        messages[mid] = m
        parent = messages.get(rec.get("re") or "")
        if parent is not None and mid not in parent["replies"]:
            parent["replies"].append(mid)
        return
    m = messages.get(mid)
    if m is None:
        return
    if kind == "state":
        m["state"] = rec["state"]
        m["updated"] = rec["ts"]
        m["history"].append([rec["ts"], rec["state"], rec.get("detail", "")])
        if rec["state"] == "nudged":
            m["nudges"] += 1
    elif kind == "notice":
        m.setdefault("notices", []).append([rec["ts"], rec.get("detail", "")])
    elif kind == "remap":
        side = rec.get("side", "to")
        if isinstance(m.get(side), dict):
            m[side]["pane"] = rec.get("pane")
    elif kind == "verdict":
        m["verdict"] = {"ok": rec.get("ok"), "note": rec.get("note", ""), "ts": rec["ts"]}
