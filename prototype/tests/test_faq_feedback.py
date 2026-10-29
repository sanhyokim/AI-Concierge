"""The questions the FAQ could not answer, and the way back into the FAQ (2026-10-29).

User: 「答えられなかった質問はフィードバックで回答に追加できる流れが必要かもね」「あと類似の質問も答えられる必要性も
あります」「FAQで回答できなかった内容をいれて、管理者が回答を記入したら使えるようにするとか？」. In the Grok test
(call 20261029-134104-08f64f) 「福岡市は対応してますか?」 got 「担当の者が確認のうえ…」 though FAQ-06 says where the
company works: the keywords (地域・エリア…) did not match the words.
"""
import json
import sqlite3
import tempfile
import pathlib
import unittest

from prototype.concierge.prompts import ASK_FOR, for_speech
from prototype.reception import settings as S
from prototype.reception.faq import faq_lookup, similarity
from prototype.reception.service import ReceptionService
from prototype.reception.store import Store

AREA = "福岡を中心に、九州・中国・四国で対応しています。"


def call(svc=None):
    svc = svc or ReceptionService(Store(":memory:"))
    return svc, svc.start_call(at="2026-10-29T13:41")["id"]


def ask(svc, cid, said, question=None):
    svc.caller_utterance(cid, said)
    return svc.tool(cid, "lookup_faq", {"question": question or said})


class SimilarQuestionTest(unittest.TestCase):
    def test_no_matching_words_hands_the_ai_the_faq_to_choose_from(self):
        svc, cid = call()
        res = ask(svc, cid, "福岡市は対応してますか?", "福岡市の排煙ダクト・厨房換気設備の清掃は対応していますか？")
        self.assertFalse(res["found"])
        self.assertNotIn("answer", res)   # the AI must not read the fallback before it looked at the candidates
        self.assertIn(("FAQ-06", AREA), [(c["faq"], c["answer"]) for c in res["candidates"]])
        self.assertEqual(res["if_none"], S.FALLBACK_ANSWER)
        self.assertIn("candidates", res["how"])

    def test_the_ai_reading_a_candidate_answer_closes_the_question_for_the_staff(self):
        svc, cid = call()
        ask(svc, cid, "福岡市は対応してますか?", "福岡市でも清掃に来てもらえますか")
        self.assertEqual(len(svc.call_view(cid)["open_questions"]), 1)
        svc.ai_utterance(cid, "はい。" + for_speech(AREA))
        self.assertEqual(svc.call_view(cid)["open_questions"], [])
        miss = svc.faq_misses()[0]
        self.assertEqual((miss["status"], miss["faq_code"], miss["question"]), ("ai_answered", "FAQ-06", "福岡市は対応してますか?"))

    def test_the_fallback_keeps_the_question_open(self):
        svc, cid = call()
        ask(svc, cid, "駐車場はありますか")
        svc.ai_utterance(cid, for_speech(S.FALLBACK_ANSWER))
        self.assertEqual(svc.call_view(cid)["open_questions"], ["駐車場はありますか"])
        self.assertEqual(svc.faq_misses()[0]["status"], "open")

    def test_a_similar_question_added_by_the_administrator_matches_next_time(self):
        svc, cid = call()
        ask(svc, cid, "福岡市は対応してますか?")
        svc.ai_utterance(cid, AREA)
        res = svc.handle_faq_miss(svc.faq_misses()[0]["id"], {"action": "add_to"}, "admin")   # the AI's choice is right
        self.assertEqual((res["status"], res["faq_code"]), ("added", "FAQ-06"))
        _, cid2 = call(svc)
        res = ask(svc, cid2, "福岡市は対応していますか", "福岡市も対応していますか")
        self.assertEqual((res["found"], res["faq"]), (True, "FAQ-06"))
        self.assertEqual(svc.call_view(cid2)["open_questions"], [])

    def test_an_answer_written_by_the_administrator_becomes_an_faq(self):
        svc, cid = call()
        ask(svc, cid, "駐車場はありますか")
        res = svc.handle_faq_miss(svc.faq_misses()[0]["id"],
                                  {"action": "answer", "answer": "作業の車は、お店の前に止めさせていただきます。"}, "admin")
        self.assertEqual(res["status"], "new_faq")
        new = svc.store.q1("SELECT * FROM faqs WHERE code = ?", (res["faq_code"],))
        self.assertEqual((new["question"], new["approval"]), ("駐車場はありますか", "未承認（試作で追加）"))
        _, cid2 = call(svc)
        self.assertEqual(ask(svc, cid2, "駐車場はありますか？")["faq"], res["faq_code"])

    def test_the_question_can_be_reworded_and_the_callers_words_stay_as_a_similar_question(self):
        svc, cid = call()
        ask(svc, cid, "あのー車ってどこに止めたらいいですかね")
        res = svc.handle_faq_miss(svc.faq_misses()[0]["id"],
                                  {"action": "answer", "question": "駐車場はありますか", "answer": "お店の前に止めます。"}, "admin")
        new = svc.store.q1("SELECT * FROM faqs WHERE code = ?", (res["faq_code"],))
        self.assertEqual(new["examples"], "あのー車ってどこに止めたらいいですかね")

    def test_dismiss_and_errors(self):
        svc, cid = call()
        ask(svc, cid, "社長さんはいますか")
        mid = svc.faq_misses()[0]["id"]
        with self.assertRaises(ValueError):
            svc.handle_faq_miss(mid, {"action": "add_to"}, "admin")   # nothing chosen
        with self.assertRaises(ValueError):
            svc.handle_faq_miss(mid, {"action": "answer", "answer": ""}, "admin")
        self.assertEqual(svc.faq_misses()[0]["status"], "open")       # the failed answer changed nothing
        self.assertEqual(svc.handle_faq_miss(mid, {"action": "dismiss"}, "admin")["status"], "dismissed")
        self.assertEqual(svc.status()["faq_misses_open"], 0)

    def test_no_transcript_no_list(self):
        svc = ReceptionService(Store(":memory:"))
        _, cfg, _ = svc.config()
        svc.save_config({**cfg, "store_transcript": False}, "admin")
        _, cid = call(svc)
        ask(svc, cid, "駐車場はありますか")
        self.assertEqual(svc.faq_misses(), [])
        self.assertEqual(len(svc.call_view(cid)["open_questions"]), 1)   # the staff still sees that one is open

    def test_keywords_still_come_first_and_unlike_questions_do_not_match(self):
        faqs = [{"code": c, "question": q, "answer": a, "keywords": k, "examples": ""} for c, q, a, k in S.FAQ_SEEDS]
        faqs[5]["examples"] = "福岡市は対応してますか"
        self.assertEqual(faq_lookup(faqs, "点検は無料ですか")["faq"], "FAQ-01")
        self.assertEqual(faq_lookup(faqs, "福岡市は対応していますか？")["faq"], "FAQ-06")
        # the same ending is not the same question: these go to the AI to judge by meaning
        self.assertNotEqual(faq_lookup(faqs, "夜中は対応してますか").get("faq"), "FAQ-06")
        self.assertLess(similarity("北九州市は対応してますか", "福岡市は対応してますか"), 0.8)

    def test_gpt_lives_backend_sees_the_similar_questions(self):
        svc, cid = call()
        ask(svc, cid, "福岡市は対応してますか?")
        svc.ai_utterance(cid, AREA)
        svc.handle_faq_miss(svc.faq_misses()[0]["id"], {"action": "add_to"}, "admin")
        _, cid2 = call(svc)
        self.assertIn("（似た質問：福岡市は対応してますか?）", svc.backend_instructions(cid2))


class OldDatabaseTest(unittest.TestCase):
    def test_an_old_database_gets_the_column_and_its_open_questions_listed(self):
        with tempfile.TemporaryDirectory() as d:
            path = pathlib.Path(d) / "old.db"
            svc = ReceptionService(Store(path))
            cid = svc.start_call(at="2026-10-20T10:00")["id"]
            svc.store.x("UPDATE calls SET open_questions = ? WHERE id = ?", (json.dumps(["駐車場はありますか"]), cid))
            svc.store.x("DROP TABLE faq_misses")
            svc.store.x("DELETE FROM meta WHERE key = 'faq_misses_from_calls'")
            svc.store.close()
            db = sqlite3.connect(path)   # a faqs table from before the similar questions
            db.execute("CREATE TABLE f2 AS SELECT id, code, question, answer, keywords, enabled, approval, generated, "
                       "position, updated_at, updated_by FROM faqs")
            db.execute("DROP TABLE faqs")
            db.execute("ALTER TABLE f2 RENAME TO faqs")
            db.commit()
            db.close()
            svc = ReceptionService(Store(path))
            self.assertEqual([m["question"] for m in svc.faq_misses()], ["駐車場はありますか"])
            self.assertEqual(svc.faqs()[0]["examples"], "")
            ReceptionService(svc.store)   # started again: listed once
            self.assertEqual(len(svc.faq_misses()), 1)
            svc.store.close()


class NextQuestionTest(unittest.TestCase):
    """2026-10-29 (Grok): handed nothing after 「焼肉田中です」, it said 「お名前は、田中様でよろしいでしょうか」."""

    def test_after_the_shop_the_name_is_asked_in_the_servers_words(self):
        svc, cid = call()
        svc.tool(cid, "save_field", {"field": "request", "value": "ダクトの清掃、3基"})
        svc.ai_utterance(cid, ASK_FOR["preferred_datetime"])
        res = svc.tool(cid, "save_field", {"field": "shop_name", "value": "焼肉田中"})
        self.assertEqual(res["say"], for_speech(ASK_FOR["caller_name"]))

    def test_a_name_saved_alone_is_greeted_and_the_number_asked(self):
        svc, cid = call()
        svc.tool(cid, "save_fields", {"request": "ダクトの清掃、3基", "shop_name": "焼肉田中"})
        svc.ai_utterance(cid, ASK_FOR["preferred_datetime"])
        res = svc.tool(cid, "save_field", {"field": "caller_name", "value": "小林"})
        self.assertTrue(res["say"].startswith("小林様でいらっしゃいますね。いつもお世話になっております。"))
        self.assertIn("お電話番号", res["say"])

    def test_a_vague_wish_is_kept_as_said(self):
        svc, cid = call()
        res = svc.tool(cid, "save_field", {"field": "preferred_datetime", "value": "来週ぐらい"})
        self.assertEqual(res["read_back_now"]["say"], "来週ぐらいでございますね。")
        rules = svc.voice_instructions(cid, "", backend=False)
        self.assertIn("「来週ぐらい」も", rules)


if __name__ == "__main__":
    unittest.main()
