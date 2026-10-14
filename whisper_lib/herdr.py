"""Thin wrapper around the Herdr CLI.

The Herdr CLI prints one JSON response per call: success on stdout (exit 0),
`{"error": {...}}` on stderr (exit 1). Usage errors exit 2 with plain text.
Everything here goes through HERDR_BIN_PATH so plugins and agents both reach
the right server without dealing with sockets directly.
"""
from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
from typing import Any, Optional

TIMEOUT_S = 20


@dataclass
class HerdrResult:
    ok: bool
    result: dict
    code: str = ""
    message: str = ""
    raw: str = ""


class HerdrUnavailable(RuntimeError):
    pass


def herdr_bin() -> str:
    return os.environ.get("HERDR_BIN_PATH") or "herdr"


def call(*args: str, timeout: float = TIMEOUT_S) -> HerdrResult:
    try:
        proc = subprocess.run(
            [herdr_bin(), *args],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except FileNotFoundError as exc:
        raise HerdrUnavailable(f"herdr binary not found ({herdr_bin()})") from exc
    except subprocess.TimeoutExpired:
        return HerdrResult(False, {}, "timeout", f"herdr {' '.join(args[:2])} timed out")

    for stream in (proc.stdout, proc.stderr):
        payload = _last_json(stream)
        if payload is None:
            continue
        if "error" in payload:
            err = payload.get("error") or {}
            return HerdrResult(False, {}, str(err.get("code", "error")),
                               str(err.get("message", "")), stream)
        if "result" in payload:
            return HerdrResult(True, payload.get("result") or {}, raw=stream)
    text = (proc.stdout + proc.stderr).strip()
    if proc.returncode == 0:
        return HerdrResult(True, {}, raw=text)
    return HerdrResult(False, {}, f"exit_{proc.returncode}", text[:400], text)


def call_text(*args: str, timeout: float = TIMEOUT_S) -> Optional[str]:
    """For commands that print plain text (pane read). None on failure."""
    try:
        proc = subprocess.run([herdr_bin(), *args], capture_output=True,
                              text=True, timeout=timeout)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout


def _last_json(stream: str) -> Optional[dict]:
    for line in reversed(stream.strip().splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                return value
    return None


# --- typed helpers -----------------------------------------------------------

def agent_get(target: str) -> Optional[dict]:
    res = call("agent", "get", target)
    return res.result.get("agent") if res.ok else None


def pane_get(pane_id: str) -> Optional[dict]:
    res = call("pane", "get", pane_id)
    return res.result.get("pane") if res.ok else None


def describe(target: str) -> Optional[dict]:
    """Resolve an agent name or pane id to identity fields."""
    info = agent_get(target)
    if info is None and ":" in target:
        info = pane_get(target)
    if info is None:
        return None
    return {
        "pane": info.get("pane_id"),
        "terminal": info.get("terminal_id"),
        "workspace": info.get("workspace_id"),
        "agent": info.get("agent"),
        "name": info.get("name"),
        "status": info.get("agent_status"),
    }


def agent_prompt(target: str, text: str) -> HerdrResult:
    return call("agent", "prompt", target, text)


def pane_run(pane_id: str, text: str) -> HerdrResult:
    return call("pane", "run", pane_id, text)


def pane_read(pane_id: str, lines: int = 400) -> Optional[str]:
    return call_text("pane", "read", pane_id, "--source", "recent-unwrapped",
                     "--lines", str(lines))


def notify(title: str, body: str = "", sound: str = "request") -> None:
    args = ["notification", "show", title[:80]]
    if body:
        args += ["--body", body[:240]]
    if sound:
        args += ["--sound", sound]
    try:
        call(*args, timeout=5)
    except HerdrUnavailable:
        pass


def open_plugin_pane(plugin_id: str, entrypoint: str, placement: str = "split") -> HerdrResult:
    return call("plugin", "pane", "open", "--plugin", plugin_id,
                "--entrypoint", entrypoint, "--placement", placement)


def json_or_none(value: Any) -> Any:
    return value if value not in ("", None) else None
