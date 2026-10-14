"""Offline tests for the tracker logic (no Herdr needed).

    python3 -m unittest discover -s tests -p 'test_*.py'
"""
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from whisper_lib import core, herdr  # noqa: E402
from whisper_lib.store import Store, fold  # noqa: E402

CFG = {**__import__("whisper_lib.store", fromlist=["DEFAULT_CONFIG"]).DEFAULT_CONFIG,
       "notify": False, "settle_grace_s": 5}


class FakeHerdr:
    """Stands in for the Herdr CLI: panes with an agent status."""

    def __init__(self):
        self.status = {"w1:p1": "idle", "w1:p2": "idle"}
        self.prompts = []
        self.screens = {}

    def install(self, test):
        patches = {
            "describe": lambda t: None if t not in self.status else {
                "pane": t, "terminal": "term_" + t, "workspace": "w1", "agent": "claude",
                "name": None, "status": self.status[t]},
            "agent_prompt": self._prompt,
            "pane_read": lambda p, n=400: self.screens.get(p, ""),
            "notify": lambda *a, **k: None,
            "pane_get": lambda p: {"terminal_id": "term_" + p} if p in self.status else None,
        }
        for name, fn in patches.items():
            original = getattr(herdr, name)
            setattr(herdr, name, fn)
            test.addCleanup(setattr, herdr, name, original)

    def _prompt(self, target, text):
        if self.status.get(target) == "blocked":
            return herdr.HerdrResult(False, {}, "agent_blocked", "blocked")
        self.prompts.append((target, text))
        return herdr.HerdrResult(True, {})


class TrackerTest(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.store = Store(self.dir)
        self.h = FakeHerdr()
        self.h.install(self)
        os.environ["HERDR_PANE_ID"] = "w1:p1"
        self.addCleanup(os.environ.pop, "HERDR_PANE_ID", None)

    def send(self, **kw):
        return core.send("w1:p2", "question?", store=self.store, cfg=CFG, **kw)

    def test_busy_at_send_and_merged_turn_is_judged_by_sweep(self):
        self.h.status["w1:p2"] = "working"
        m = self.send(expect_reply=True)
        self.assertEqual(m["to"]["status_at_send"], "working")
        # The busy turn ends; this settle must not be taken as our answer.
        self.h.status["w1:p2"] = "idle"
        core.on_status(self.store, CFG, "w1:p2", "idle")
        self.assertEqual(self.store.state()[m["id"]]["state"], "delivered")
        # No second turn ever comes (the agent folded the message into the
        # busy one). After the grace period the sweep judges it.
        core.sweep(self.store, CFG, now=time.time() + 1)
        self.assertEqual(self.store.state()[m["id"]]["state"], "delivered")
        core.sweep(self.store, CFG, now=time.time() + 30)
        self.assertEqual(self.store.state()[m["id"]]["state"], "unanswered")

    def test_raw_reply_token_counts(self):
        m = self.send(expect_reply=True)
        core.on_status(self.store, CFG, "w1:p2", "working")
        self.h.screens["w1:p1"] = f"... answer re:{m['id']} 42"
        core.on_status(self.store, CFG, "w1:p2", "done")
        self.assertEqual(self.store.state()[m["id"]]["state"], "replied")

    def test_other_ids_do_not_count_as_reply(self):
        m = self.send(expect_reply=True)
        core.on_status(self.store, CFG, "w1:p2", "working")
        self.h.screens["w1:p1"] = f"re:{m['id']}0 and re:abcdef"
        core.on_status(self.store, CFG, "w1:p2", "idle")
        self.assertEqual(self.store.state()[m["id"]]["state"], "unanswered")

    def test_active_mode_nudges_then_gives_up(self):
        cfg = {**CFG, "mode": "active", "max_nudges": 1}
        m = core.send("w1:p2", "q", expect_reply=True, store=self.store, cfg=cfg)
        for _ in range(2):
            core.on_status(self.store, cfg, "w1:p2", "working")
            core.on_status(self.store, cfg, "w1:p2", "idle")
        states = [h[1] for h in self.store.state()[m["id"]]["history"]]
        self.assertEqual(states, ["queued", "delivered", "working", "nudged", "working", "unanswered"])
        self.assertIn("still waiting for your reply", self.h.prompts[-1][1])

    def test_done_to_idle_seen_flip_is_not_a_settle(self):
        m = self.send(expect_reply=True)
        core.on_status(self.store, CFG, "w1:p2", "idle")  # no working seen yet
        self.assertEqual(self.store.state()[m["id"]]["state"], "delivered")

    def test_blocked_recipient_refused_at_send(self):
        self.h.status["w1:p2"] = "blocked"
        m = self.send(expect_reply=True)
        self.assertEqual(m["state"], "failed")
        self.assertIn("agent_blocked", m["history"][-1][2])

    def test_reply_closes_question(self):
        m = self.send(expect_reply=True)
        os.environ["HERDR_PANE_ID"] = "w1:p2"
        r = core.reply(m["id"], "42", store=self.store, cfg=CFG)
        self.assertEqual(r["re"], m["id"])
        self.assertEqual(self.store.state()[m["id"]]["state"], "replied")
        self.assertEqual(self.h.prompts[-1][0], "w1:p1")
        self.assertIn(f"re:{m['id']}", self.h.prompts[-1][1])

    def test_move_remaps_pane(self):
        m = self.send(expect_reply=True)
        self.h.status["w2:p1"] = "idle"
        # The moved pane keeps its terminal id.
        herdr.pane_get = lambda p: {"terminal_id": "term_w1:p2"}
        core.on_moved(self.store, "w2:p1")
        self.assertEqual(self.store.state()[m["id"]]["to"]["pane"], "w2:p1")

    def test_log_replay_is_deterministic_and_survives_torn_line(self):
        for _ in range(3):
            self.send(expect_reply=True)
        with open(self.store.log_path, "a") as fh:
            fh.write('{"t":"state","id":"tor')  # crash mid-write
        a = self.store.state()
        (self.dir / "snapshot.json").unlink()
        b = Store(self.dir).state()
        self.assertEqual(a, b)
        replay = {}
        for line in self.store.log_path.read_text().splitlines()[:-1]:
            fold(replay, json.loads(line))
        self.assertEqual(replay, b)
        # Writing after the crash keeps the new record intact.
        later = self.send(expect_reply=True)
        self.assertIn(later["id"], Store(self.dir).state())


if __name__ == "__main__":
    unittest.main()
