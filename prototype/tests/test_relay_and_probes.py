import asyncio
import os
import subprocess
import sys
import unittest
import xml.etree.ElementTree as ET

from prototype.engines.relay_app import (RelaySession, ScriptedBrain, Segment, build_tokens, truncate_history,
                                         twiml_connect, twiml_dtmf_entry)

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class RelayPureFunctionsTest(unittest.TestCase):
    def test_twiml_is_well_formed(self):
        root = ET.fromstring(twiml_connect("wss://example.invalid/relay", "Kazuha-Neural", "/after",
                                           "株式会社野田、AI受付です。"))
        relay = root.find("Connect/ConversationRelay")
        self.assertEqual(relay.get("ttsProvider"), "Amazon")
        self.assertEqual(relay.get("voice"), "Kazuha-Neural")
        self.assertEqual(relay.get("dtmfDetection"), "true")
        gather = ET.fromstring(twiml_dtmf_entry("/dtmf", "https://example.invalid/clips")).find("Gather")
        self.assertEqual(gather.get("input"), "dtmf")

    def test_slow_segment_is_one_token(self):
        msgs = build_tokens([Segment("お電話番号を確認します。"), Segment("ゼロキュウゼロ、イチニー", slow=True),
                             Segment("。よろしいでしょうか。")], "80%")
        self.assertEqual(len(msgs), 3)
        self.assertTrue(msgs[1]["token"].startswith('<prosody rate="80%">'))
        self.assertTrue(msgs[1]["token"].endswith("</prosody>"))
        self.assertEqual([m["last"] for m in msgs], [False, False, True])

    def test_interrupt_truncates_assistant_history(self):
        history = [{"role": "user", "content": "a"}, {"role": "assistant", "content": "全文の案内です。正式な金額は…"}]
        truncate_history(history, "全文の案内です。")
        self.assertEqual(history[-1]["content"], "全文の案内です。")
        self.assertTrue(history[-1]["interrupted"])

    def test_refusal_hands_off_with_end(self):
        session = RelaySession(ScriptedBrain([[Segment("こんにちは")]]))
        asyncio.run(session.on_message({"type": "setup", "from": "+819012345678"}))
        session.flow.on_human_request()
        out = session.refuse_ai(via_human_request=True)
        self.assertEqual(out[0]["type"], "end")
        self.assertEqual(session.flow.consent.ai_processing, "refused")


class ProbesWithoutCredentialsTest(unittest.TestCase):
    """Vendor probes must stop before contacting anything when credentials are absent."""

    def _run(self, module: str) -> subprocess.CompletedProcess:
        env = {k: v for k, v in os.environ.items()
               if k not in ("OPENAI_API_KEY", "CONCIERGE_POLLY_ACCESS_KEY_ID", "CONCIERGE_POLLY_SECRET_ACCESS_KEY")}
        return subprocess.run([sys.executable, "-m", module, "--out", "/nonexistent-should-not-be-created"],
                              cwd=ROOT, env=env, capture_output=True, text=True, encoding="utf-8", timeout=60)

    def test_openai_probe_exits(self):
        r = self._run("prototype.engines.realtime_probe")
        self.assertEqual(r.returncode, 2, r.stderr)
        self.assertIn("nothing was sent", r.stderr)
        self.assertFalse(os.path.exists("/nonexistent-should-not-be-created"))

    def test_polly_probe_exits(self):
        r = self._run("prototype.engines.polly_probe")
        self.assertEqual(r.returncode, 2, r.stderr)
        self.assertIn("nothing was sent", r.stderr)

    def test_dialog_runner_exits(self):
        env = {k: v for k, v in os.environ.items() if k != "OPENAI_API_KEY"}
        r = subprocess.run([sys.executable, "-m", "prototype.engines.run_dialog", "--scenario", "R-03"],
                           cwd=ROOT, env=env, capture_output=True, text=True, encoding="utf-8", timeout=60)
        self.assertEqual(r.returncode, 2, r.stderr)
        self.assertIn("nothing was sent", r.stderr)


if __name__ == "__main__":
    unittest.main()
