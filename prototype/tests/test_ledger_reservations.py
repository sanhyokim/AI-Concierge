import asyncio
import json
import pathlib
import tempfile
import unittest

from prototype.engines import budget
from prototype.engines.budget import (Limits, UsageLedger, openai_cost, openai_usage_breakdown, reconcile,
                                      transcription_cost, transcription_upper_bound)
from prototype.engines.realtime_runner import RealtimeRunner
from prototype.tests.fake_realtime import FakeRealtime

USAGE = {"input_token_details": {"text_tokens": 1500, "audio_tokens": 300,
                                 "cached_tokens_details": {"text_tokens": 1000, "audio_tokens": 100}},
         "output_token_details": {"text_tokens": 20, "audio_tokens": 400}}


class ReservationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ledger = UsageLedger("openai", Limits(100, 1000, 1.0), pathlib.Path(self.tmp.name) / "l.jsonl")

    def tearDown(self):
        self.tmp.cleanup()

    def test_unsettled_reservation_counts_at_the_estimate(self):
        self.ledger.reserve("op", "openai_api_direct", 0.30, "r1", audio_seconds=30)  # e.g. timed out
        t = self.ledger.totals()
        self.assertEqual((t["requests"], t["open_reservations"]), (1, 1))
        self.assertAlmostEqual(t["est_cost_usd"], 0.30)
        self.assertAlmostEqual(t["held_usd"], 0.30)

    def test_settled_reservation_counts_at_the_actual_amount(self):
        rid = self.ledger.reserve("op", "openai_api_direct", 0.30, "r1")
        self.ledger.record("op", "openai_api_direct", 0.02, "r1", reservation=rid)
        self.ledger.close(rid)
        t = self.ledger.totals()
        self.assertEqual((t["requests"], t["open_reservations"]), (1, 0))
        self.assertAlmostEqual(t["est_cost_usd"], 0.02)

    def test_partial_usage_on_an_open_reservation_keeps_the_larger_amount(self):
        rid = self.ledger.reserve("turn", "openai_api_direct", 0.30, "r1")
        self.ledger.record("resp", "openai_api_direct", 0.05, "r1", reservation=rid)  # then the run died
        self.assertAlmostEqual(self.ledger.totals()["est_cost_usd"], 0.30)
        self.ledger.record("resp", "openai_api_direct", 0.40, "r1", reservation=rid)
        self.assertAlmostEqual(self.ledger.totals()["est_cost_usd"], 0.45)

    def test_held_amounts_block_new_requests(self):
        self.ledger.reserve("op", "openai_api_direct", 0.90, "r1")
        with self.assertRaises(budget.BudgetExceeded):
            self.ledger.reserve("op", "openai_api_direct", 0.20, "r2")

    def test_reconcile_settles_with_the_console_amount(self):
        rid = self.ledger.reserve("op", "openai_api_direct", 0.30, "r1")
        self.assertEqual(reconcile(self.ledger, rid, 0.04, "console"), "adjusted")
        t = self.ledger.totals()
        self.assertEqual(t["open_reservations"], 0)
        self.assertAlmostEqual(t["est_cost_usd"], 0.04)
        self.assertEqual(t["requests"], 1)  # the adjustment is not a request
        with self.assertRaises(KeyError):
            reconcile(self.ledger, "nope", 0.04)

    def test_reconcile_after_partial_usage_sets_the_total_not_adds_it(self):
        rid = self.ledger.reserve("op", "openai_api_direct", 0.30, "r1")
        self.ledger.record("op", "openai_api_direct", 0.03, "r1", reservation=rid)
        reconcile(self.ledger, rid, 0.05, "console total")
        self.assertAlmostEqual(self.ledger.totals()["est_cost_usd"], 0.05)  # was 0.08 before the fix
        adj = [json.loads(line) for line in self.ledger.path.read_text(encoding="utf-8").splitlines()
               if json.loads(line).get("kind") == "adjustment"][0]
        self.assertEqual((adj["previous_linked_usd"], adj["console_total_usd"]), (0.03, 0.05))

    def test_reconcile_below_the_recorded_usage(self):
        rid = self.ledger.reserve("op", "openai_api_direct", 0.30, "r1")
        self.ledger.record("op", "openai_api_direct", 0.07, "r1", reservation=rid)
        self.ledger.close(rid)
        reconcile(self.ledger, rid, 0.05, "console lower")
        self.assertAlmostEqual(self.ledger.totals()["est_cost_usd"], 0.05)

    def test_repeated_reconcile_never_increases(self):
        rid = self.ledger.reserve("op", "openai_api_direct", 0.30, "r1")
        self.ledger.record("op", "openai_api_direct", 0.03, "r1", reservation=rid)
        reconcile(self.ledger, rid, 0.05)
        self.assertEqual(reconcile(self.ledger, rid, 0.05), "unchanged")
        with self.assertRaises(ValueError):
            reconcile(self.ledger, rid, 0.06)
        self.assertAlmostEqual(self.ledger.totals()["est_cost_usd"], 0.05)

    def test_old_lines_without_kind_are_read_as_usage(self):
        self.ledger.path.write_text(json.dumps({"vendor": "openai", "operation": "x", "est_cost_usd": 0.1,
                                                "audio_seconds": 1.0}) + "\n", encoding="utf-8")
        self.assertEqual(self.ledger.totals()["requests"], 1)


class UsagePricingTest(unittest.TestCase):
    def test_breakdown_separates_cached_and_uncached_input(self):
        b = openai_usage_breakdown(USAGE)
        self.assertEqual(b, {"text_in_uncached": 500, "text_in_cached": 1000, "audio_in_uncached": 200,
                             "audio_in_cached": 100, "text_out": 20, "audio_out": 400})
        p = budget.PRICES["openai_rt21"]
        expected = (500 * p["text_in"] + 1000 * p["cached_text_in"] + 200 * p["audio_in"]
                    + 100 * p["cached_audio_in"] + 20 * p["text_out"] + 400 * p["audio_out"]) / 1e6
        self.assertAlmostEqual(openai_cost(USAGE), expected)

    def test_transcription_cost_tokens_and_duration(self):
        self.assertAlmostEqual(transcription_cost({"type": "tokens", "input_tokens": 1000, "output_tokens": 100}),
                               (1000 * 2.50 + 100 * 10.00) / 1e6)
        self.assertAlmostEqual(transcription_cost({"type": "duration", "seconds": 60}), 0.006)
        self.assertEqual(transcription_cost(None), 0.0)
        self.assertGreater(transcription_upper_bound(60), 0.006)  # pessimistic against the official estimate


def _run_dialog(fake, ledger):
    async def scenario():
        runner = RealtimeRunner(fake, "marin", idle_timeout_s=5, ledger=ledger, run_id="t")
        try:
            return await runner.run([{"text": "a", "timing": {"type": "immediate"}}],
                                    lambda turn: (b"\x80" * 2400, 0.0))
        finally:
            await fake.close()
    return asyncio.run(scenario())


class RunnerReservationTest(unittest.TestCase):
    def test_missing_response_usage_keeps_the_turn_held(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = UsageLedger("openai", Limits(100, 1000, 5.0), pathlib.Path(tmp) / "l.jsonl")
            result = _run_dialog(FakeRealtime(response_ms=500, usage=False), ledger)
            t = ledger.totals()
            self.assertEqual(t["open_reservations"], 1)
            self.assertGreater(t["held_usd"], 0.1)  # the turn's estimate, not zero
            self.assertTrue(any(e["kind"] == "response_done" and e["accounting"] == "missing"
                                for e in result["events"]))

    def test_turn_reservation_includes_transcription(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = UsageLedger("openai", Limits(100, 1000, 5.0), pathlib.Path(tmp) / "l.jsonl")
            _run_dialog(FakeRealtime(response_ms=500), ledger)
            res = [json.loads(line) for line in ledger.path.read_text(encoding="utf-8").splitlines()
                   if json.loads(line).get("kind") == "reservation"][0]
            self.assertGreater(res["est_cost_usd"], 3 * budget.openai_response_bound())


class ProbeTimeoutTest(unittest.TestCase):
    def test_timed_out_request_stays_in_the_totals(self):
        from prototype.engines import realtime_probe

        class SilentTransport:
            async def send(self, event):
                pass

            async def recv(self):
                await asyncio.sleep(10)

        with tempfile.TemporaryDirectory() as tmp:
            ledger = UsageLedger("openai", Limits(100, 1000, 5.0, request_timeout_s=0.1), pathlib.Path(tmp) / "l.jsonl")
            ctx = realtime_probe.Ctx("gpt-realtime-2.1", "k", ledger, "t")
            with self.assertRaises(asyncio.TimeoutError):
                asyncio.run(realtime_probe._speak(ctx, SilentTransport(), "読んで", "op"))
            t = ledger.totals()
            self.assertEqual((t["requests"], t["open_reservations"]), (1, 1))
            self.assertAlmostEqual(t["est_cost_usd"], budget.openai_response_bound())

    def test_polly_failure_after_sending_stays_held(self):
        from prototype.engines import polly_probe

        class BrokenPolly:
            def synthesize_speech(self, **kw):
                raise TimeoutError("read timeout")

        with tempfile.TemporaryDirectory() as tmp:
            ledger = UsageLedger("aws_polly", Limits(10, 100, 1.0), pathlib.Path(tmp) / "l.jsonl")
            with self.assertRaises(TimeoutError):
                polly_probe._synthesize(BrokenPolly(), ledger, "t", "op", "neural", Text="テスト", TextType="text",
                                        VoiceId="Kazuha", OutputFormat="pcm", SampleRate="8000")
            self.assertEqual(ledger.totals()["open_reservations"], 1)


class RepeatSpreadTest(unittest.TestCase):
    def test_n_vs_n_ratios_per_segment(self):
        from prototype.engines.realtime_probe import repeat_spread

        def run(rep, post_rate, voice="marin"):
            segs = {name: {"articulation_rate": 8.0} for name in ("pre", "target")}
            segs["post"] = {"articulation_rate": post_rate}
            return {"voice": voice, "method": "split", "script": "S-01", "rep": rep, "N": {"measure": {"segments": segs}}}

        out = repeat_spread([run(1, 8.0), run(2, 8.4), run(1, 8.0, "cedar"), run(2, 7.6, "cedar"),
                             {"method": "instr", "rep": 1}])
        self.assertEqual(len(out["pairs"]), 2)
        self.assertEqual((out["post_min"], out["post_max"]), (0.95, 1.05))
        self.assertEqual(out["pairs"][0]["ratio_vs_rep1"]["pre"], 1.0)


if __name__ == "__main__":
    unittest.main()
