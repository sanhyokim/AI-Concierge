"""Regression tests for linking late, missing, duplicate and reordered usage to the right turn."""
import json
import pathlib
import tempfile
import unittest

from prototype.engines.accounting import TurnAccounting
from prototype.engines.budget import Limits, UsageLedger

USAGE = {"input_token_details": {"text_tokens": 100}, "output_token_details": {"audio_tokens": 20}}
TR = {"type": "tokens", "input_tokens": 30, "output_tokens": 5}


class TurnAccountingTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ledger = UsageLedger("openai", Limits(100, 1000, 10.0), pathlib.Path(self.tmp.name) / "l.jsonl")
        self.acct = TurnAccounting(self.ledger, "openai_api_direct", "t")

    def tearDown(self):
        self.tmp.cleanup()

    def _turn(self, n, item, est=1.0):
        rid = self.acct.open_turn(f"turn:{n}", est)
        self.acct.speech_started(item)
        self.acct.committed(item)
        return rid

    def _open(self):
        return {r["rid"] for r in self.ledger.open_reservations()}

    def test_late_missing_usage_holds_its_own_turn_after_barge_in(self):
        r1 = self._turn(1, "u1")
        self.acct.transcription("u1", TR, 0.001)
        self.acct.response_created("resp1")
        r2 = self._turn(2, "u2")                      # barge-in: turn 2 starts while resp1 is playing
        self.acct.transcription("u2", TR, 0.001)
        self.acct.response_done("resp1", None, 0.0)   # turn 1's done arrives late, usage missing
        self.acct.response_created("resp2")
        self.acct.response_done("resp2", USAGE, 0.01)
        out = self.acct.settle(clean=True)
        self.assertEqual(out["held"], [r1])
        self.assertEqual(out["closed"], [r2])
        self.assertEqual(self._open(), {r1})

    def test_reverse_order_completion_events(self):
        r1 = self._turn(1, "u1")
        self.acct.response_created("resp1")
        r2 = self._turn(2, "u2")
        self.acct.response_created("resp2")
        self.acct.response_done("resp2", USAGE, 0.02)  # turn 2 finishes first
        self.acct.transcription("u2", TR, 0.001)
        self.acct.response_done("resp1", USAGE, 0.03)
        self.acct.transcription("u1", TR, 0.001)
        out = self.acct.settle(clean=True)
        self.assertEqual(out["closed"], [r1, r2])
        linked = {}
        for line in self.ledger.path.read_text(encoding="utf-8").splitlines():
            rec = json.loads(line)
            if rec.get("kind") == "usage":
                linked.setdefault(rec["reservation"], 0.0)
                linked[rec["reservation"]] += rec["est_cost_usd"]
        self.assertAlmostEqual(linked[r1], 0.031)
        self.assertAlmostEqual(linked[r2], 0.021)

    def test_response_ok_but_transcription_missing_keeps_the_turn_held(self):
        r1 = self._turn(1, "u1")
        self.acct.response_created("resp1")
        self.acct.response_done("resp1", USAGE, 0.02)
        self.acct.transcription("u1", None, 0.0)       # failed / no usage
        self.assertEqual(self.acct.settle(clean=True)["held"], [r1])

    def test_transcription_never_arriving_keeps_the_turn_held(self):
        r1 = self._turn(1, "u1")
        self.acct.response_created("resp1")
        self.acct.response_done("resp1", USAGE, 0.02)
        self.assertEqual(self.acct.pending_transcriptions(), 1)
        self.assertEqual(self.acct.settle(clean=False)["held"], [r1])

    def test_duplicate_events_count_once(self):
        self._turn(1, "u1")
        self.acct.transcription("u1", TR, 0.001)
        self.assertEqual(self.acct.transcription("u1", TR, 0.001), "duplicate")
        self.acct.response_created("resp1")
        self.acct.response_done("resp1", USAGE, 0.02)
        self.assertEqual(self.acct.response_done("resp1", USAGE, 0.02), "duplicate")
        self.acct.settle(clean=True)
        t = self.ledger.totals()
        self.assertEqual(t["requests"], 2)
        self.assertAlmostEqual(t["est_cost_usd"], 0.021)

    def test_unmatched_usage_is_kept_and_unattributed_missing_holds_everything(self):
        r1 = self._turn(1, "u1")
        self.acct.transcription("u1", TR, 0.001)
        self.acct.response_created("resp1")
        self.acct.response_done("resp1", USAGE, 0.02)
        self.assertEqual(self.acct.response_done("ghost", USAGE, 0.05), "unmatched")
        lines = [json.loads(line) for line in self.ledger.path.read_text(encoding="utf-8").splitlines()]
        ghost = [r for r in lines if r.get("units", {}).get("unmatched")]
        self.assertEqual(len(ghost), 1)
        self.assertNotIn("reservation", ghost[0])  # kept in the totals, not linked to any turn
        self.assertAlmostEqual(ghost[0]["est_cost_usd"], 0.05)
        self.assertEqual(self.acct.response_done("ghost2", None, 0.0), "unmatched_missing")
        self.assertEqual(self.acct.settle(clean=True)["held"], [r1])

    def test_actual_above_the_reservation_is_flagged(self):
        r1 = self._turn(1, "u1", est=0.01)
        self.acct.response_created("resp1")
        self.acct.response_done("resp1", USAGE, 0.02)
        self.assertEqual(self.acct.exceeded, [r1])

    def test_tool_follow_up_responses_stay_with_the_turn(self):
        r1 = self._turn(1, "u1")
        for rid in ("a", "b", "c"):
            self.acct.response_created(rid)
        self.assertEqual(self.acct.responses_for(r1), 3)


if __name__ == "__main__":
    unittest.main()
