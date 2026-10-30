"""Telephone manners (2026-10-13, browser test C1): 0 is read as ゼロ, saving a number starts its read-back in the
same tool call, the AI ends the call with closing words and end_call, and the fixed phrases avoid the words the
instructions forbid."""
import unittest

from prototype.browser_lab import config as lab
from prototype.concierge.prompts import CLOSING, GREETING, SPEECH_MANNERS
from prototype.engines.tools import ToolHandler
from prototype.reception import settings as S
from prototype.reception.fake_agent import ASK
from prototype.reception.service import ReceptionService
from prototype.reception.store import Store

# words the instructions tell the AI not to use; the phrases this app writes itself must not use them either
FORBIDDEN = ("マル", "〇", "もしもし", "さようでございますか", "承って", "少々確認", "確認しましょう", "了解", "なるほど", "よろしかった", "になります",
             "頂戴", "お声が遠い", "AI受付です", "伺えますか")


def service():
    return ReceptionService(Store(":memory:"), env={})


class ReadingTest(unittest.TestCase):
    def test_saving_a_number_returns_the_words_to_read_back(self):
        h = ToolHandler()
        r = h.handle("save_field", {"field": "callback_number", "value": "080-5644-2050"})
        now = r["read_back_now"]
        self.assertEqual(now["read_this_clearly"], "ゼロハチゼロの、ゴーロクヨンヨンの、ニーゼロゴーゼロ")
        self.assertEqual(now["say"], "復唱いたします。ゼロハチゼロの、ゴーロクヨンヨンの、ニーゼロゴーゼロ。"
                                     "こちらでお間違いないでしょうか。")
        self.assertEqual(r["status"], "awaiting_confirmation")
        self.assertNotIn("read_back_now", h.handle("save_field", {"field": "shop_name", "value": "焼肉太郎博多店"}))

    def test_the_spoken_read_back_and_a_yes_confirm(self):
        h = ToolHandler()
        now = h.handle("save_field", {"field": "callback_number", "value": "08056442050"})["read_back_now"]
        self.assertEqual(h.ai_said(now["say"]), {})
        h.caller_said("はい")
        self.assertEqual(h.confirm_from_reply(), "callback_number")

    def test_saving_the_same_value_again_keeps_the_confirmation(self):
        # the model often sends every value again at the end; that must not undo the caller's 「はい」
        h = ToolHandler()
        now = h.handle("save_field", {"field": "callback_number", "value": "080-5644-2050"})["read_back_now"]
        h.ai_said(now["say"])
        h.caller_said("はい、合っています")
        h.confirm_from_reply()
        r = h.handle("save_field", {"field": "callback_number", "value": "08056442050"})
        self.assertEqual(r, {"ok": True, "status": "confirmed_by_caller"})
        r = h.handle("save_field", {"field": "callback_number", "value": "080-5644-2051"})   # a real change: read again
        self.assertEqual(r["status"], "awaiting_confirmation")
        self.assertTrue(r["read_back_now"]["read_this_clearly"].endswith("ニーゼロゴーイチ"))


class ServiceTest(unittest.TestCase):
    def test_save_fields_reads_back_one_item_and_leaves_the_other_for_later(self):
        svc = service()
        cid = svc.start_call(at="2026-10-13T14:17")["id"]
        res = svc.tool(cid, "save_fields", {"shop_name": "焼肉太郎博多店", "caller_name": "田中",
                                            "callback_number": "080-5644-2050", "request": "無煙ロースター3基の清掃",
                                            "preferred_datetime": "2026-10-15"})
        self.assertEqual(res["read_back_now"]["field"], "callback_number")
        self.assertEqual(res["read_back_later"]["fields"], ["preferred_datetime"])
        svc.ai_utterance(cid, res["read_back_now"]["say"])
        self.assertEqual(svc.caller_utterance(cid, "はい", "u1")["confirmed"], "callback_number")
        date = svc.tool(cid, "request_readback", {"field": "preferred_datetime"})
        self.assertEqual(date["say"], "10月15日、木曜日でございますね。お時間のご希望はございますか。")   # 2026-10-24
        svc.ai_utterance(cid, date["say"])
        self.assertEqual(svc.caller_utterance(cid, "はい、大丈夫です", "u2")["confirmed"], "preferred_datetime")

    def test_the_old_order_read_back_before_saving_is_not_confirmed(self):
        # 2026-10-13: the AI read the number back on its own (as 〇八〇…) before any save; the 「はい」 confirmed nothing
        svc = service()
        cid = svc.start_call(at="2026-10-13T14:17")["id"]
        svc.ai_utterance(cid, "はい、〇八〇五六四四二〇五〇ですね。この番号でお間違いないですか?")
        self.assertNotIn("confirmed", svc.caller_utterance(cid, "はい", "u1"))

    def test_end_call_asks_the_page_to_hang_up_after_the_wait(self):
        svc = service()
        cid = svc.start_call(at="2026-10-13T14:17")["id"]
        res = svc.tool(cid, "end_call", {"reason": "受付が終わった"})
        self.assertEqual((res["ok"], res["hangup"], res["hangup_wait_ms"]), (True, True, int(S.HANGUP_WAIT_S * 1000)))
        self.assertTrue(2000 <= res["hangup_wait_ms"] <= 3000)   # spec 3-6: 2-3 s recommended, undecided

    def test_the_instructions_carry_the_manners_and_the_closing(self):
        svc = service()
        cid = svc.start_call(at="2026-10-13T14:17")["id"]
        text = svc.instructions(cid, preamble=lab.LAB_RULES)
        for words in ("0は「ゼロ」", "「の」を挟んで", "確認いたしますので、少々お待ちいただけますでしょうか。",
                      "ほかにご用件はございますでしょうか。", "失礼いたします。」と言い、end_call を呼びます",
                      "read_back_now.say"):
            self.assertIn(words, text)
        self.assertIn(SPEECH_MANNERS, lab.LAB_INSTRUCTIONS)


class FixedPhrasesTest(unittest.TestCase):
    def test_fixed_phrases_avoid_the_forbidden_words(self):
        h = ToolHandler()
        say = h.handle("save_field", {"field": "callback_number", "value": "090-1020-3040"})["read_back_now"]["say"]
        for text in (GREETING, CLOSING, S.FALLBACK_ANSWER, say, *ASK.values()):
            for word in FORBIDDEN:
                self.assertNotIn(word, text, text)

    def test_greeting_names_the_company_and_the_ai(self):
        self.assertTrue(GREETING.startswith("お電話ありがとうございます。株式会社野田、AIオペレーターでございます。"))
        self.assertIn("AIオペレーター", GREETING)   # 2026-10-23: the user's wording
        self.assertEqual(lab.GREETING, GREETING)


if __name__ == "__main__":
    unittest.main()


class SummaryTest(unittest.TestCase):
    """2026-10-22 (user): every AI closes with the same correct Japanese, made by the server (request_summary)."""

    def test_the_summary_asks_for_what_is_missing_first(self):
        svc = service()
        cid = svc.start_call(at="2026-10-21T13:00")["id"]
        svc.tool(cid, "save_fields", {"request": "無煙ロースター3基の清掃"})
        res = svc.tool(cid, "request_summary", {})
        self.assertFalse(res["ok"])
        self.assertEqual(res["missing"], ["店舗名", "お名前", "折り返し先"])
        self.assertIn("店舗名とお名前を", res["say"])

    def test_the_summary_and_the_closing_words(self):
        svc = service()
        cid = svc.start_call(at="2026-10-21T13:00")["id"]
        res = svc.tool(cid, "save_fields", {"shop_name": "焼肉ほのか博多店", "caller_name": "田中",
                                            "request": "無煙ロースター3基の清掃", "callback_number": "080-5644-2050",
                                            "preferred_datetime": "2026-10-23T15:00"})
        self.assertEqual(res["do_not_ask_again"], ["店舗名", "お名前", "折り返し先", "用件", "希望日時"])
        self.assertNotIn("still_to_ask", res)
        say = svc.tool(cid, "request_summary", {})["say"]
        self.assertTrue(say.startswith("それでは、ご用件を確認いたします。焼肉ほのか博多店の田中様。ご用件は、無煙ロースター3基の清掃。"
                                       "作業のご希望のにちじは、10月23日、金曜日の、午後3時。"), say)
        self.assertIn("担当のものにもうしつたえ", say)
        self.assertTrue(say.endswith("ほかにご用件はございますでしょうか。"))
        for word in FORBIDDEN + ("担当の者", "担当の方", "0805"):
            self.assertNotIn(word, say)

    def test_a_question_back_is_not_a_yes(self):
        from prototype.concierge.confirmation import Reply, classify_reply
        # 2026-10-21 (Eleven Realtime): this confirmed the date before any time was given
        self.assertEqual(classify_reply("の三時ぐらいで大丈夫ですか？"), Reply.UNCLEAR)
        self.assertEqual(classify_reply("それでいいですか？"), Reply.UNCLEAR)
        for yes in ("はい", "大丈夫です", "はい、それで大丈夫ですか？", "はい。それでお願いします。"):
            self.assertEqual(classify_reply(yes), Reply.AFFIRMATIVE, yes)
