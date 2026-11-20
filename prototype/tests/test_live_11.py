"""2026-11-19: GPT-Live-1.1 reached the API; the lab's GPT-Live candidate uses it (GPT_LIVE_MODEL goes back)."""
import unittest

from prototype.browser_lab import config, vendors


class LiveModelTest(unittest.TestCase):
    def setUp(self):
        self.cfg = config.CANDIDATES["gpt-live-1"]

    def test_the_lab_uses_gpt_live_1_1(self):
        self.assertEqual(vendors.live_model(self.cfg, {}), "gpt-live-1.1")
        self.assertIn("1.1", self.cfg["name"])

    def test_the_old_model_can_be_chosen_again(self):
        self.assertEqual(vendors.live_model(self.cfg, {"GPT_LIVE_MODEL": "gpt-live-1"}), "gpt-live-1")

    def test_the_session_asks_for_that_model(self):
        sent = {}

        def http(method, url, headers, body):
            sent.update(body)
            return 200, {"transport": {"sdp": "v=0"}, "session": {"id": "s1"}}

        vendors.exchange_live_sdp(self.cfg, {"OPENAI_API_KEY": "k"}, "v=0", http=http, voice="harema")
        self.assertEqual(sent["session"]["model"], "gpt-live-1.1")
        self.assertEqual(sent["session"]["audio"]["output"]["voice"], "harema")


if __name__ == "__main__":
    unittest.main()
