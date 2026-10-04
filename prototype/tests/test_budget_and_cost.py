import asyncio
import io
import json
import pathlib
import tempfile
import unittest

from prototype.cost import cost_model
from prototype.engines.budget import (BudgetExceeded, Limits, UsageLedger, async_with_retries, openai_cost,
                                      polly_cost, with_retries)
from prototype.engines.realtime_runner import RealtimeRunner
from prototype.tests.fake_realtime import FakeRealtime


class LedgerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = pathlib.Path(self.tmp.name) / "ledger.jsonl"

    def tearDown(self):
        self.tmp.cleanup()

    def test_cost_limit_blocks_before_sending_and_persists_across_instances(self):
        lim = Limits(max_requests=100, max_audio_seconds=1000, max_cost_usd=0.10)
        UsageLedger("openai", lim, self.path).record("r", "openai_api_direct", 0.08, "run1")
        second = UsageLedger("openai", lim, self.path)  # a new run reads the same ledger
        with self.assertRaises(BudgetExceeded):
            second.check(0.03)
        second.check(0.01)

    def test_request_and_audio_limits(self):
        lim = Limits(max_requests=1, max_audio_seconds=10, max_cost_usd=10)
        ledger = UsageLedger("aws_polly", lim, self.path)
        with self.assertRaises(BudgetExceeded):
            ledger.check(0.0, audio_seconds=11)
        ledger.record("r", "polly_direct", 0.0, "run1", audio_seconds=1)
        with self.assertRaises(BudgetExceeded):
            ledger.check(0.0)

    def test_ledger_lines_have_required_fields(self):
        UsageLedger("openai", Limits(10, 10, 10), self.path).record("op", "openai_api_direct", 0.01, "run9",
                                                                    audio_seconds=2.5, usage={"x": 1})
        rec = json.loads(self.path.read_text().splitlines()[0])
        for key in ("ts", "vendor", "operation", "path", "run_id", "audio_seconds", "est_cost_usd", "units"):
            self.assertIn(key, rec)


class RetryTest(unittest.TestCase):
    def test_one_retry_for_network_errors_only(self):
        calls = {"n": 0}

        def flaky():
            calls["n"] += 1
            raise ConnectionError("down")

        with self.assertRaises(ConnectionError):
            with_retries(flaky, max_retries=1)
        self.assertEqual(calls["n"], 2)

        calls["n"] = 0

        def bad_request():
            calls["n"] += 1
            raise ValueError("400")

        with self.assertRaises(ValueError):
            with_retries(bad_request, max_retries=1)
        self.assertEqual(calls["n"], 1)

    def test_async_retry_then_success(self):
        calls = {"n": 0}

        async def once_flaky():
            calls["n"] += 1
            if calls["n"] == 1:
                raise TimeoutError()
            return "ok"

        async def run():
            return await async_with_retries(once_flaky, max_retries=1)

        self.assertEqual(asyncio.run(run()), "ok")


class CostTest(unittest.TestCase):
    def test_openai_cost_from_usage(self):
        usage = {"input_token_details": {"text_tokens": 1000, "audio_tokens": 600,
                                         "cached_tokens_details": {"text_tokens": 1000, "audio_tokens": 0}},
                 "output_token_details": {"text_tokens": 0, "audio_tokens": 1200}}
        expected = (1000 * 0.40 + 600 * 32 + 1200 * 64) / 1_000_000
        self.assertAlmostEqual(openai_cost(usage), expected)
        self.assertEqual(openai_cost({}), 0.0)

    def test_polly_cost(self):
        self.assertAlmostEqual(polly_cost(1_000_000), 16.0)

    def test_unconfirmed_prices_stay_symbolic(self):
        total, _ = cost_model.scenario_total("B-Haiku", cost_model.SCENARIOS["中"])
        for symbol in ("F", "V_stt", "V_tts", "U_ntt", "S_srv"):
            self.assertIn(symbol, total.terms)
            self.assertGreater(total.terms[symbol], 0)
        a_total, _ = cost_model.scenario_total("A-2.1", cost_model.SCENARIOS["中"])
        self.assertIn("V_asr", a_total.terms)
        self.assertNotIn("V_stt", a_total.terms)
        text = cost_model.render()
        self.assertIn("0円にせず", text)
        self.assertIn("Polly単体の費用で、案B全体", text)

    def test_hand_check_low_scenario_plan_a(self):
        # 60 AI calls + 90 human calls + Twilio number, plus JPY items (voicewarp 550, extra number 110, LINE 0)
        ai = sum(cost_model.ai_call(cost_model.CONFIGS["A-2.1"]).values(), cost_model.Money()).usd
        human = (0.0100 + 0.0746) * 3
        usd = 60 * ai + 90 * human + 4.75
        expected = usd * 1.1 * 150 + 550 + 110
        total, _ = cost_model.scenario_total("A-2.1", cost_model.SCENARIOS["少"])
        self.assertAlmostEqual(total.jpy_total(), expected, places=6)
        self.assertEqual(total.terms["F"], 450)


class RunnerUsageTest(unittest.TestCase):
    def test_runner_records_usage_to_ledger(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = UsageLedger("openai", Limits(100, 1000, 5.0), pathlib.Path(tmp) / "l.jsonl")

            async def scenario():
                fake = FakeRealtime(response_ms=500)
                runner = RealtimeRunner(fake, "marin", idle_timeout_s=5, ledger=ledger, run_id="t")
                result = await runner.run([{"text": "a", "timing": {"type": "immediate"}}],
                                          lambda turn: (b"\x80" * 2400, 0.0))
                await fake.close()
                return result

            result = asyncio.run(scenario())
            self.assertGreater(result["est_cost_usd"], 0)
            self.assertEqual(ledger.totals()["requests"], 1)

    def test_runner_stops_before_a_turn_that_would_exceed_the_budget(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = UsageLedger("openai", Limits(100, 1000, 0.05), pathlib.Path(tmp) / "l.jsonl")

            async def scenario():
                fake = FakeRealtime(response_ms=500)
                runner = RealtimeRunner(fake, "marin", idle_timeout_s=5, ledger=ledger, run_id="t")
                try:
                    with self.assertRaises(BudgetExceeded):
                        await runner.run([{"text": "a", "timing": {"type": "immediate"}}],
                                         lambda turn: (b"\x80" * 2400, 0.0))
                finally:
                    await fake.close()
                return fake

            fake = asyncio.run(scenario())
            self.assertFalse(any(e["type"] == "input_audio_buffer.append" for e in fake.client_events))


class FakePolly:
    """Stands in for boto3's Polly client; returns PCM silence and two speech marks."""

    def __init__(self):
        self.calls = []

    def synthesize_speech(self, **kw):
        self.calls.append(kw)
        if kw["OutputFormat"] == "json":
            marks = [{"time": 1200, "type": "ssml", "value": "target_start"},
                     {"time": 3400, "type": "ssml", "value": "target_end"}]
            body = "\n".join(json.dumps(m) for m in marks).encode()
        else:
            body = b"\x00\x00" * 8000 * 5
        return {"AudioStream": io.BytesIO(body), "RequestCharacters": len(kw["Text"])}


class PollyProbeOfflineTest(unittest.TestCase):
    def test_render_records_both_requests_and_marks_path(self):
        from prototype.engines import polly_probe
        script = json.loads(polly_probe.SCENARIOS.read_text())["slowdown"][0]
        with tempfile.TemporaryDirectory() as tmp:
            ledger = UsageLedger("aws_polly", Limits(10, 100, 1.0), pathlib.Path(tmp) / "l.jsonl")
            fake = FakePolly()
            res = polly_probe.render(fake, "Kazuha-Neural", script, "80%", pathlib.Path(tmp), "D", ledger, "t")
            self.assertEqual(res["path"], "polly_direct")
            self.assertEqual(len(fake.calls), 2)
            self.assertEqual(fake.calls[0]["VoiceId"], "Kazuha")
            self.assertEqual(fake.calls[0]["Engine"], "neural")
            self.assertEqual(ledger.totals()["requests"], 2)
            self.assertEqual(res["segments"][1]["start_s"], 1.2)


class CallerAudioOfflineTest(unittest.TestCase):
    def test_caller_turns_use_non_candidate_voice_and_skip_existing_files(self):
        from prototype.engines import make_caller_audio
        scenarios = json.loads(make_caller_audio.SCENARIOS.read_text())["dialog"][:2]
        turns = sum(len(s["turns"]) for s in scenarios)
        with tempfile.TemporaryDirectory() as tmp:
            ledger = UsageLedger("aws_polly", Limits(50, 100, 1.0), pathlib.Path(tmp) / "l.jsonl")
            fake = FakePolly()
            written = make_caller_audio.make(fake, scenarios, pathlib.Path(tmp) / "audio", ledger, "t")
            self.assertEqual(len(written), turns)
            self.assertTrue(all(c["VoiceId"] == "Mizuki" and c["Engine"] == "standard" for c in fake.calls))
            again = make_caller_audio.make(fake, scenarios, pathlib.Path(tmp) / "audio", ledger, "t")
            self.assertEqual(again, [])
            self.assertEqual(ledger.totals()["requests"], turns)


if __name__ == "__main__":
    unittest.main()
