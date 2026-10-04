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
        self.assertNotIn("V_stt", total.terms)  # Relay's built-in transcription is included in $0.07/min
        for symbol in ("F", "B_fwd", "B_num", "V_tts", "U_ntt", "S_srv"):
            self.assertIn(symbol, total.terms)
            self.assertGreater(total.terms[symbol], 0)
        a_total, _ = cost_model.scenario_total("A-2.1", cost_model.SCENARIOS["中"])
        self.assertNotIn("V_asr", a_total.terms)  # gpt-4o-transcribe price is confirmed now
        self.assertNotIn("V_stt", a_total.terms)
        text = cost_model.render()
        self.assertIn("既知の単価と使用量の仮定に基づく概算", text)
        self.assertIn("未確認の費用（F、B_fwd、B_num", text)
        self.assertIn("Polly単体の費用で、案B全体", text)
        self.assertNotIn("確定分", text)

    def test_hand_check_low_scenario_plan_a(self):
        # 60 AI calls + 90 human calls (two Twilio legs each) + Twilio number + LINE 0.
        # Twilio legs are billed 3.5 min (3 min + average rounding 0.5); hikari prices are NOT included.
        billed = 3.5
        ai = sum(cost_model.ai_call(cost_model.CONFIGS["A-2.1"]).values(), cost_model.Money()).usd
        ai_expected = (0.0100 + 0.0025 + 0.0044) * billed + cost_model.plan_a_model("openai_rt21").usd \
            + cost_model.summary("anthropic_haiku45").usd + 0.006 * 3 * 0.40  # transcription of caller speech
        self.assertAlmostEqual(ai, ai_expected, places=9)
        human = (0.0100 + 0.0746) * billed
        expected = (60 * ai + 90 * human + 4.75) * 1.1 * 150
        total, _ = cost_model.scenario_total("A-2.1", cost_model.SCENARIOS["少"])
        self.assertAlmostEqual(total.jpy_total(), expected, places=6)
        self.assertEqual(total.terms["F"], 150)
        self.assertEqual((total.terms["B_fwd"], total.terms["B_num"]), (1, 1))

    def test_cache_not_established_costs_more_and_matches_hand_check(self):
        for key, cfg in cost_model.CONFIGS.items():
            with_cache = sum(cost_model.ai_call(cfg).values(), cost_model.Money()).usd
            without = sum(cost_model.ai_call(cfg, cache=False).values(), cost_model.Money()).usd
            self.assertGreater(without, with_cache, key)
        t = cost_model.plan_a_tokens()
        p = cost_model.PRICES["openai_rt21"]
        diff = (t["cached_text"] * (p["text_in"] - p["cached_text_in"])
                + t["cached_audio"] * (p["audio_in"] - p["cached_audio_in"])) / 1e6
        self.assertAlmostEqual(cost_model.plan_a_model("openai_rt21", cache=False).usd
                               - cost_model.plan_a_model("openai_rt21").usd, diff, places=9)
        h = cost_model.PRICES["anthropic_haiku45"]
        b_no = (cost_model.B_UNCACHED_IN + cost_model.B_CACHE_READ + cost_model.B_CACHE_WRITE) * h["in"] / 1e6 \
            + cost_model.B_OUT * h["out"] / 1e6
        self.assertAlmostEqual(cost_model.plan_b_model("anthropic_haiku45", cache=False).usd, b_no, places=9)
        text = cost_model.render()
        self.assertIn("キャッシュが成立しない場合", text)
        self.assertIn("4,096トークン", text)

    def test_measurement_estimates_include_transcription_and_both_cache_cases(self):
        from prototype.cost import measurement_budget as mb
        est = mb.estimate()
        self.assertGreater(est["T3"]["usd_no_cache"], est["T3"]["usd"])
        self.assertGreater(est["T3"]["transcription_usd_per_dialog"], 0)
        self.assertGreater(est["T3"]["requests"], est["T3"]["runs"] * mb.DIALOG_RESPONSES)  # + transcriptions
        self.assertLess(est["total_usd_no_cache"], 20)  # still inside the proposed (unapproved) budget
        phone = mb.phone_stage_estimate()
        self.assertEqual(phone["calls_per_candidate"], 100)
        b = phone["candidates"]["B-Haiku"]
        self.assertIn("V_tts", b["symbols"])
        self.assertNotIn("V_stt", b["symbols"])
        self.assertNotIn("F", b["symbols"])
        a = phone["candidates"]["A-2.1"]
        self.assertGreater(a["usd_no_cache"], a["usd"])

    def test_hikari_prices_only_in_conditional_reference(self):
        total, _ = cost_model.scenario_total("A-2.1", cost_model.SCENARIOS["少"])
        self.assertEqual(total.jpy, 0)  # no JPY item: LINE is free at this volume, NTT items are symbols
        ref = cost_model.hikari_reference(total)
        self.assertAlmostEqual(ref, 150 * 1.5 * 11.55 + 550 + 110, places=6)
        self.assertAlmostEqual(cost_model.hikari_reference(total, from_2027_04=True), 150 * 1.5 * 13.2 + 550 + 110,
                               places=6)

    def test_rounding_allowance_per_twilio_leg(self):
        cfg = cost_model.CONFIGS["B-Haiku"]
        hi = sum(cost_model.ai_call(cfg).values(), cost_model.Money()).usd
        lo = sum(cost_model.ai_call(cfg, round_allow=False).values(), cost_model.Money()).usd
        self.assertAlmostEqual(hi - lo, 0.5 * (0.0100 + 0.0025 + 0.07), places=9)  # inbound, recording, Relay
        h_hi = sum(cost_model.human_call().values(), cost_model.Money()).usd
        h_lo = sum(cost_model.human_call(round_allow=False).values(), cost_model.Money()).usd
        self.assertAlmostEqual(h_hi - h_lo, 0.5 * (0.0100 + 0.0746), places=9)  # two legs, each rounded
        self.assertAlmostEqual(cost_model.ntt_units(3.0), 1.5)
        self.assertAlmostEqual(cost_model.ntt_units(3.0, round_allow=False), 1.0)


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
            totals = ledger.totals()
            self.assertEqual(totals["requests"], 2)  # one response + one input transcription
            self.assertEqual(totals["open_reservations"], 0)  # clean finish settles the turn reservation
            self.assertEqual(totals["tokens"]["text_in_cached"], 1000)
            self.assertEqual(totals["tokens"]["text_in_uncached"], 500)
            self.assertGreater(totals["transcription_usd"], 0)

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
