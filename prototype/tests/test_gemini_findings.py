"""The Gemini 3.8 Live tests of 2026-10-20: the call record named GPT-Live's voice, every early 「はい」 to a read-back
was refused, the end-of-call save_fields reset the number, the announcement needed a real recording, and the
caller's speech was transcribed in other languages."""
import base64
import pathlib
import tempfile
import unittest

from prototype.browser_lab.server import Lab, api_get, api_post
from prototype.engines.tools import ToolHandler
from prototype.reception.service import ReceptionService
from prototype.reception.store import Store
from prototype.tests.test_browser_lab import Clock, FakeHttp

NUM = {"field": "callback_number", "value": "08056442050"}


def read_back():
    h = ToolHandler()
    h.handle("save_field", NUM)          # the read-back starts in the same call
    h.ai_said("復唱いたします。ゼロハチゼロの、ゴーロクヨンヨンの、ニーゼロゴーゼロ。こちらでお間違いないでしょうか。")
    return h


class ReadBackTest(unittest.TestCase):
    def test_a_yes_that_cuts_only_the_closing_question_still_answers(self):
        h = read_back()
        self.assertEqual(h.readback_interrupted("gemini.serverContent.interrupted", unplayed_ms=1200), [])
        h.caller_said("はい、大丈夫です。", "u1")
        self.assertTrue(h.handle("confirm_field", NUM)["ok"])

    def test_a_cut_while_the_number_was_still_playing_invalidates(self):
        h = read_back()
        self.assertEqual(h.readback_interrupted("gemini.serverContent.interrupted", unplayed_ms=4500),
                         ["callback_number"])
        h.caller_said("はい", "u1")
        r = h.handle("confirm_field", NUM)
        self.assertFalse(r["ok"])
        self.assertIn("do not ask the caller to repeat", r["reason"])

    def test_unknown_unplayed_time_keeps_the_strict_rule(self):
        h = read_back()
        self.assertEqual(h.readback_interrupted("gemini.serverContent.interrupted", None), ["callback_number"])

    def test_the_ai_reading_it_again_by_itself_counts_as_a_new_read_back(self):
        h = read_back()
        h.readback_interrupted("gemini.serverContent.interrupted", unplayed_ms=5000)
        h.ai_said("念のため、もう一度復唱いたします。ゼロハチゼロの、ゴーロクヨンヨンの、ニーゼロゴーゼロ。こちらでお間違いないでしょうか。")
        h.caller_said("はい、大丈夫です。", "u1")
        self.assertTrue(h.handle("confirm_field", NUM)["ok"])

    def test_a_wrong_number_read_again_does_not_count(self):
        h = read_back()
        h.readback_interrupted("gemini.serverContent.interrupted", unplayed_ms=5000)
        h.ai_said("ゼロハチゼロの、ゴーロクヨンヨンの、ニーゼロゴーイチ。こちらでお間違いないでしょうか。")
        h.caller_said("はい", "u1")
        self.assertFalse(h.handle("confirm_field", NUM)["ok"])

    def test_saving_the_same_values_at_the_end_keeps_the_read_back_and_the_confirmation(self):
        h = read_back()
        h.handle("save_field", {"field": "preferred_datetime", "value": "2026-10-21T17:00"})
        self.assertEqual(h.save("callback_number", "080-5644-2050"), "awaiting_confirmation")
        h.caller_said("はい", "u1")
        self.assertTrue(h.handle("confirm_field", NUM)["ok"])
        self.assertEqual(h.save("callback_number", "08056442050"), "confirmed_by_caller")
        self.assertEqual(h.save("preferred_datetime", "2026-10-21T17:00"), "awaiting_confirmation")
        self.assertEqual(h.save("callback_number", "08056442051"), "corrected_unconfirmed")   # a real change


class LabTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = pathlib.Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def lab(self, env=None):
        env = env or {"GEMINI_API_KEY": "g"}
        return Lab(env=env, http=FakeHttp({"auth_tokens": {"name": "auth_tokens/abc"}}),
                   ledger_path=self.dir / "ledger.jsonl", results_dir=self.dir / "results",
                   service=ReceptionService(Store(self.dir / "lab.db"), env=env), clock=Clock(),
                   opening_dir=self.dir / "recordings")

    def test_the_call_record_names_the_ai_and_the_voice_really_used(self):
        lab = self.lab()
        s = lab.start("gemini-3.8-live")
        view = lab.service.call_view(s["call_id"])
        self.assertEqual(view["ai"]["name"], "Google Gemini 3.8 Live")
        self.assertEqual(view["voice"]["voice"], "Kore")              # not the admin's active GPT-Live voice
        self.assertTrue(any(e["kind"] == "ai_used" for e in view["events"]))

    def test_gemini_setup_hints_japanese_and_is_less_eager_to_cut_in(self):
        setup = self.lab().start("gemini-3.8-live")["credentials"]["setup"]["setup"]
        self.assertEqual(setup["inputAudioTranscription"], {"languageCodes": ["ja-JP"]})
        vad = setup["realtimeInputConfig"]["automaticActivityDetection"]
        self.assertEqual(vad["startOfSpeechSensitivity"], "START_SENSITIVITY_LOW")
        self.assertEqual(vad["endOfSpeechSensitivity"], "END_SENSITIVITY_LOW")

    def test_interruption_with_the_unplayed_time_reaches_the_guard(self):
        lab = self.lab()
        sid = lab.start("gemini-3.8-live")["session_id"]
        lab.tool(sid, "save_field", NUM)
        status, out = api_post(lab, "/api/interrupted", {"session_id": sid, "source": "gemini", "unplayed_ms": 900})
        self.assertEqual((status, out["invalidated"]), (200, []))
        _, out = api_post(lab, "/api/interrupted", {"session_id": sid, "source": "gemini", "unplayed_ms": "x"})
        self.assertEqual(out["invalidated"], ["callback_number"])     # not a number: the strict rule

    def test_the_opening_recording_is_kept_played_back_and_deleted(self):
        lab = self.lab()
        got = api_get(lab, "/api/opening")
        self.assertFalse(got["present"])
        self.assertIn("株式会社野田", got["text"])
        audio = b"OggS fake audio"
        status, out = api_post(lab, "/api/opening", {"mime": "audio/webm;codecs=opus", "seconds": 12.34,
                                                     "data": base64.b64encode(audio).decode()})
        self.assertEqual((status, out["present"], out["seconds"], out["source"]), (200, True, 12.3, "recorded"))
        self.assertEqual(base64.b64decode(api_get(lab, "/api/opening")["data"]), audio)
        self.assertTrue((self.dir / "recordings" / "A-01.webm").is_file())
        api_post(lab, "/api/opening", {"mime": "audio/mpeg", "source": "file", "data": base64.b64encode(b"ID3").decode()})
        self.assertEqual(sorted(p.name for p in (self.dir / "recordings").iterdir()), ["A-01.json", "A-01.mp3"])
        self.assertFalse(api_post(lab, "/api/opening", {"delete": True})[1]["present"])
        self.assertEqual(list((self.dir / "recordings").iterdir()), [])

    def test_the_opening_refuses_other_files(self):
        lab = self.lab()
        for body in ({"mime": "text/html", "data": "PGh0bWw+"}, {"mime": "audio/mpeg", "data": ""},
                     {"mime": "audio/mpeg", "data": "not base64!"},
                     {"mime": "audio/mpeg", "data": base64.b64encode(b"x" * 1_500_000).decode()}):
            status, _ = api_post(lab, "/api/opening", body)
            self.assertEqual(status, 400, body["mime"])
        self.assertFalse(api_get(lab, "/api/opening")["present"])


if __name__ == "__main__":
    unittest.main()
