"""The order of the questions, user decision 2026-10-23: 「要件や日程を確認してからお名前や連絡先を聞いたほうがいい。
お客様が名乗ればそれはそれでOK」. Every AI asks the request, then the wished date, then the shop and the name, then the
number to call back."""
import unittest

from prototype.concierge.prompts import ASK_FOR, voice_rules
from prototype.reception.fake_agent import RuleAgent
from prototype.reception.service import ReceptionService
from prototype.reception.store import Store


def service():
    return ReceptionService(Store(":memory:"), env={})


class OrderTest(unittest.TestCase):
    def test_the_voice_instructions_list_the_request_and_the_date_before_the_name_and_the_number(self):
        rules = voice_rules(end="", summary="")
        at = [rules.index(ASK_FOR[f]) for f in ("request", "preferred_datetime", "shop_name", "callback_number")]
        self.assertEqual(at, sorted(at))

    def test_the_server_lists_the_date_next_until_the_ai_has_asked_it(self):
        svc = service()
        cid = svc.start_call(at="2026-10-23T13:00")["id"]
        res = svc.tool(cid, "save_fields", {"request": "無煙ロースター3基の清掃"})
        self.assertEqual(res["still_to_ask"], ["希望日時（ご希望がなければ不要）", "店舗名", "お名前", "折り返し先"])
        svc.ai_utterance(cid, ASK_FOR["preferred_datetime"])
        svc.caller_utterance(cid, "特にありません", "u1")
        res = svc.tool(cid, "save_fields", {"shop_name": "焼肉ほのか博多店"})
        self.assertEqual(res["still_to_ask"], ["お名前", "折り返し先"])   # no wish: not asked again

    def test_the_offline_agent_asks_the_date_before_the_name_and_the_number(self):
        svc = service()
        cid = svc.start_call(at="2026-10-23T13:00")["id"]
        agent = RuleAgent(svc, cid)
        agent.greeting()
        said = " ".join(agent.hear("無煙ロースターの清掃をお願いしたいです。")["say"])
        self.assertIn("作業にお伺いするご希望の日時", said)
        said = " ".join(agent.hear("特にありません。")["say"])
        self.assertIn("店舗名", said)
        said = " ".join(agent.hear("焼肉ほのか博多店の田中です。")["say"])
        self.assertIn("お電話番号", said)

    def test_a_caller_who_gives_the_name_first_is_not_asked_it_again(self):
        svc = service()
        cid = svc.start_call(at="2026-10-23T13:00")["id"]
        agent = RuleAgent(svc, cid)
        agent.greeting()
        said = " ".join(agent.hear("焼肉ほのか博多店の田中です。")["say"])
        self.assertIn("ご用件", said)
        said = " ".join(agent.hear("無煙ロースターの清掃をお願いしたいです。")["say"])
        self.assertIn("作業にお伺いするご希望の日時", said)
        said = " ".join(agent.hear("特にありません。")["say"])
        self.assertNotIn("店舗名", said)
        self.assertIn("お電話番号", said)


if __name__ == "__main__":
    unittest.main()
