"""The Grok C5 test of 2026-10-30 (record 20261030-094736) and the user's notes on it.

「「株式会社野田のAIオペレーターでございます。」の「株式会社野田、AIオペレーター」に変更」「突然英語を言い出した」
"""
import unittest
from unittest import mock

from prototype.concierge.prompts import GREETING, OPENING_ANNOUNCEMENT, OPENING_NOTE, VOICE_RULES
from prototype.reception import service as service_mod
from prototype.reception.service import ReceptionService
from prototype.reception.store import Store


class OpeningTest(unittest.TestCase):
    def test_the_company_and_the_ai_operator_are_said_apart(self):
        for text in (OPENING_ANNOUNCEMENT, GREETING, OPENING_NOTE):
            self.assertIn("株式会社野田、AIオペレーターでございます", text)
            self.assertNotIn("野田のAIオペレーター", text)


class ElevenSayOnlyTest(unittest.TestCase):
    # record 20261030-105638 (v1 Max): 「システムのセリフとイレブンラボが勝手に考えたセリフが混在してるように感じる」
    def test_only_eleven_is_told_to_speak_only_the_says(self):
        svc = ReceptionService(Store(":memory:"))
        cid = svc.start_call(at="2026-10-30T10:56")["id"]
        plain = svc.voice_instructions(cid, "", backend=False)
        strict = svc.voice_instructions(cid, "", backend=False, say_only=True)
        self.assertNotIn("話す言葉は結果の say だけ", plain)
        self.assertIn("話す言葉は結果の say だけ", strict)
        self.assertIn("呼ぶ前には何も話しません", strict)

    def test_eleven_gets_no_list_of_questions_and_no_calm_tone(self):
        # record 20261030-111750 (v1 Mini): 「要件を言ってるのに、どうのような要件ですかっておかしくない？」
        # 「声のトーンが突然暗くなる」
        svc = ReceptionService(Store(":memory:"))
        cid = svc.start_call(at="2026-10-30T11:17")["id"]
        plain = svc.voice_instructions(cid, "", backend=False)
        strict = svc.voice_instructions(cid, "", backend=False, say_only=True)
        self.assertIn("どのようなご用件でしょうか", plain)
        self.assertNotIn("どのようなご用件でしょうか", strict)
        self.assertNotIn("落ち着いた声", strict)
        self.assertIn("暗い調子にしません", strict)
        self.assertIn("自分で質問を選びません", strict)
        self.assertIn("【守ること】", strict)


class NoEnglishTest(unittest.TestCase):
    def test_a_refusal_that_only_waits_for_the_caller_asks_for_no_new_answer(self):
        # 109.15 s: confirm_field reached the server before the caller's 「はい」 (0.15 s after the wait); the AI
        # then said 「Waiting for the customer's response.」
        svc = ReceptionService(Store(":memory:"))
        cid = svc.start_call(at="2026-10-30T09:47")["id"]
        svc.caller_utterance(cid, "08056442050")
        say = svc.tool(cid, "save_field", {"field": "callback_number", "value": "08056442050"})["read_back_now"]["say"]
        svc.ai_utterance(cid, say)
        with mock.patch.object(service_mod, "CONFIRM_WAIT_S", 0.05):
            res = svc.tool(cid, "confirm_field", {"field": "callback_number", "value": "08056442050"})
        self.assertFalse(res["ok"])
        self.assertTrue(res["wait_silently"])
        svc.caller_utterance(cid, "はい。")
        res = svc.tool(cid, "confirm_field", {"field": "callback_number", "value": "08056442050"})
        self.assertTrue(res["ok"])
        self.assertNotIn("wait_silently", res)

    def test_the_wait_for_the_reply_is_longer(self):
        self.assertGreaterEqual(service_mod.CONFIRM_WAIT_S, 4.0)

    def test_the_voice_model_is_told_to_speak_only_japanese(self):
        self.assertIn("日本語だけで話します", VOICE_RULES)


if __name__ == "__main__":
    unittest.main()
