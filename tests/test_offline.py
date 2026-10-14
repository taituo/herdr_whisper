"""Offline tests for the loop logic (no Herdr, no LLM).

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

from whisper_lib import core, herdr, judge  # noqa: E402
from whisper_lib.store import DEFAULT_CONFIG, Store, fold  # noqa: E402

CFG = {**DEFAULT_CONFIG, "notify": False, "settle_grace_s": 5}


class FakeHerdr:
    def __init__(self):
        self.status = {"w1:p1": "idle", "w1:p2": "idle", "w1:p9": "idle"}
        self.agents = {"w1:p1": "claude", "w1:p2": "codex", "w1:p9": None}
        self.prompts = []

    def install(self, test):
        patches = {
            "describe": lambda t: None if t not in self.status else {
                "pane": t, "terminal": "term_" + t, "workspace": "w1",
                "agent": self.agents[t], "name": None, "status": self.status[t]},
            "agent_prompt": self._prompt,
            "pane_read": lambda p, n=150: "output",
            "notify": lambda *a, **k: None,
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


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.store = Store(self.dir)
        self.h = FakeHerdr()
        self.h.install(self)
        self.verdict = judge.Verdict("forward", "the answer is 42", "answered")
        original = judge.run
        judge.run = lambda prompt, cmd, timeout: self.verdict
        self.addCleanup(setattr, judge, "run", original)

    def prompt(self, sender="w1:p1", target="w1:p2", text="question?", wait=False):
        os.environ["HERDR_PANE_ID"] = sender
        self.addCleanup(os.environ.pop, "HERDR_PANE_ID", None)
        argv = ["agent", "prompt", target, text] + (["--wait"] if wait else [])
        mid = core._record_prompt(argv, self.store)
        if wait:
            self.store.append({"t": "wait", "id": mid, "result": "ok"})
        return mid

    def state(self, mid):
        return self.store.state()[mid]

    def turn(self, pane="w1:p2"):
        core.on_status(self.store, CFG, pane, "working")
        for mid in core.on_status(self.store, CFG, pane, "idle"):
            core.run_judge(self.store, CFG, mid)


class ParseTest(unittest.TestCase):
    def test_prompt_args(self):
        p = core.parse_prompt_args
        self.assertEqual(p(["rev", "hi", "--wait", "--timeout", "5"]),
                         {"target": "rev", "text": "hi", "wait": True})
        self.assertEqual(p(["--until", "idle", "rev", "hi"])["target"], "rev")
        self.assertEqual(p(["rev", "--", "--not-a-flag"])["text"], "--not-a-flag")
        self.assertIsNone(p(["rev"]))
        self.assertIsNone(p(["rev", "hi", "--weird"]))

    def test_judge_parse(self):
        v = judge.parse('ok\n```json\n{"action": "forward", "message": "x", "reason": "r"}\n```')
        self.assertEqual((v.action, v.message), ("forward", "x"))
        self.assertEqual(judge.parse('{"action": "forward"}').action, "human")  # no message
        self.assertEqual(judge.parse("I think resend").action, "human")
        self.assertTrue(judge.parse("nope").error)


class LoopTest(Base):
    def test_not_waiting_gets_forwarded(self):
        mid = self.prompt()
        self.turn()
        self.assertEqual(self.state(mid)["state"], "forwarded")
        target, text = self.h.prompts[-1]
        self.assertEqual(target, "w1:p1")
        self.assertIn("the answer is 42", text)

    def test_waiting_sender_is_left_alone(self):
        mid = self.prompt(wait=True)
        self.turn()
        self.assertEqual(self.state(mid)["state"], "returned")
        self.assertEqual(self.h.prompts, [])

    def test_wait_still_running_counts_as_waiting(self):
        os.environ["HERDR_PANE_ID"] = "w1:p1"
        mid = core._record_prompt(["agent", "prompt", "w1:p2", "q", "--wait"], self.store)
        self.turn()
        self.assertEqual(self.state(mid)["state"], "returned")

    def test_direct_reply_skips_judge(self):
        mid = self.prompt()
        core.on_status(self.store, CFG, "w1:p2", "working")
        self.prompt(sender="w1:p2", target="w1:p1", text="answer")
        core.on_status(self.store, CFG, "w1:p2", "idle")
        self.assertEqual(self.state(mid)["state"], "replied")

    def test_non_agent_sender(self):
        mid = self.prompt(sender="w1:p9")
        self.turn()
        self.assertEqual(self.state(mid)["state"], "returned")

    def test_resend_until_limit_then_escalate(self):
        self.verdict = judge.Verdict("resend", "", "ignored")
        mid = self.prompt()
        for _ in range(4):
            self.turn()
        st = [h[1] for h in self.state(mid)["history"]]
        self.assertEqual(st.count("resent"), CFG["max_pushes"])
        self.assertEqual(st[-1], "escalated")

    def test_judge_error_escalates(self):
        self.verdict = judge.Verdict("human", error="judge command not found: llm")
        mid = self.prompt()
        self.turn()
        self.assertEqual(self.state(mid)["state"], "escalated")
        self.assertIn("not found", self.state(mid)["history"][-1][2])

    def test_dry_run_never_types(self):
        cfg = {**CFG, "push": False}
        mid = self.prompt()
        core.on_status(self.store, cfg, "w1:p2", "working")
        for m in core.on_status(self.store, cfg, "w1:p2", "idle"):
            core.run_judge(self.store, cfg, m)
        self.assertEqual(self.state(mid)["state"], "escalated")
        self.assertEqual(self.h.prompts, [])

    def test_blocked_sender_escalates_forward(self):
        mid = self.prompt()
        self.h.status["w1:p1"] = "blocked"
        self.turn()
        self.assertEqual(self.state(mid)["state"], "escalated")

    def test_forward_rate_limit(self):
        cfg = {**CFG, "max_forwards_per_pair_per_hour": 2}
        ids = []
        for i in range(3):
            ids.append(self.prompt(text=f"q{i}"))
            core.on_status(self.store, cfg, "w1:p2", "working")
            for m in core.on_status(self.store, cfg, "w1:p2", "idle"):
                core.run_judge(self.store, cfg, m)
        self.assertEqual([self.state(i)["state"] for i in ids], ["forwarded", "forwarded", "escalated"])

    def test_busy_at_send_folded_turn_judged_by_sweep(self):
        self.h.status["w1:p2"] = "working"
        mid = self.prompt()
        self.h.status["w1:p2"] = "idle"
        self.assertEqual(core.on_status(self.store, CFG, "w1:p2", "idle"), [])
        self.assertEqual(self.state(mid)["state"], "delivered")
        core.sweep(self.store, CFG, now=time.time() + 30)
        self.assertEqual(self.state(mid)["state"], "forwarded")

    def test_seen_flip_is_not_a_settle(self):
        mid = self.prompt()
        core.on_status(self.store, CFG, "w1:p2", "idle")
        self.assertEqual(self.state(mid)["state"], "delivered")


class LogTest(Base):
    def test_replay_deterministic_and_torn_line_safe(self):
        for i in range(3):
            self.prompt(text=f"q{i}")
        with open(self.store.log_path, "a") as fh:
            fh.write('{"t":"state","id":"tor')
        a = self.store.state()
        (self.dir / "snapshot.json").unlink()
        b = Store(self.dir).state()
        self.assertEqual(a, b)
        replay = {}
        for line in self.store.log_path.read_text().splitlines()[:-1]:
            fold(replay, json.loads(line))
        self.assertEqual(replay, b)
        later = self.prompt(text="after crash")
        self.assertIn(later, Store(self.dir).state())


if __name__ == "__main__":
    unittest.main()
