import asyncio
import unittest

from prototype.engines.realtime_runner import RealtimeRunner
from prototype.tests.fake_realtime import FakeRealtime


def _speech(ms: int) -> tuple[bytes, float]:
    return b"\x80" * (8 * ms), 0.0  # loud-ish mu-law bytes; the fake ignores content


class RunnerWithFakeServerTest(unittest.TestCase):
    """Exercises the harness only. Nothing here measures a vendor."""

    def test_barge_in_truncates_at_played_position(self):
        async def scenario():
            fake = FakeRealtime(response_ms=1500)
            runner = RealtimeRunner(fake, voice="marin", idle_timeout_s=5)
            turns = [
                {"text": "first", "timing": {"type": "immediate"}},
                {"text": "interrupt", "timing": {"type": "during_ai_speech", "offset_ms": 400}},
            ]
            result = await runner.run(turns, lambda turn: _speech(300))
            await fake.close()
            return fake, result

        fake, result = asyncio.run(scenario())
        truncates = fake.truncates()
        self.assertEqual(len(truncates), 1)
        played = truncates[0]["audio_end_ms"]
        self.assertTrue(350 <= played <= 800, played)  # offset 400 ms + fake VAD 60 ms + scheduling slack
        barge = [e for e in result["events"] if e["kind"] == "barge_in"]
        self.assertEqual(len(barge), 1)
        self.assertTrue(barge[0]["received_ms"] > played)
        self.assertTrue(result["ai_items"][0]["truncated"])

    def test_latency_is_logged_for_each_ai_turn(self):
        async def scenario():
            fake = FakeRealtime(response_ms=500)
            runner = RealtimeRunner(fake, voice="cedar", idle_timeout_s=5)
            turns = [{"text": "a", "timing": {"type": "immediate"}},
                     {"text": "b", "timing": {"type": "after_ai_end", "delay_ms": 100}}]
            result = await runner.run(turns, lambda turn: _speech(300))
            await fake.close()
            return fake, result

        fake, result = asyncio.run(scenario())
        starts = [e for e in result["events"] if e["kind"] == "ai_audio_start"]
        self.assertEqual(len(starts), 2)
        for e in starts:
            # fake: think 150 ms after its VAD end; generous slack for the event loop
            self.assertTrue(100 <= e["latency_from_vad_end_ms"] <= 600, e)
        self.assertEqual(fake.truncates(), [])


if __name__ == "__main__":
    unittest.main()
