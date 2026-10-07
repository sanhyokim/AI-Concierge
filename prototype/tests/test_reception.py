"""Reception core: routing rule, settings snapshot, confirmation through the service, consent, summary,
corrections and the notification outbox. Fake time and no vendor."""
import datetime as dt
import json
import unittest

from prototype.reception import settings as S
from prototype.reception.fake_agent import RuleAgent
from prototype.reception.notify import Outbox, RETRY_WINDOW
from prototype.reception.routing import decide
from prototype.reception.service import ReceptionService
from prototype.reception.store import JST, Store


class Clock:
    def __init__(self, t="2026-10-05T18:00:00+09:00"):
        self.t = dt.datetime.fromisoformat(t)

    def __call__(self):
        return self.t

    def advance(self, **kw):
        self.t += dt.timedelta(**kw)


def service(clock=None):
    return ReceptionService(Store(":memory:"), env={}, clock=clock or Clock())


class RoutingTest(unittest.TestCase):
    def setUp(self):
        self.cfg = S.validate_config(S.DEFAULT_CONFIG)   # Mon-Fri 09:00-17:00 normal, otherwise AI

    def test_jst_boundaries(self):
        cases = {"2026-10-05T08:59:59": "ai", "2026-10-05T09:00:00": "normal", "2026-10-05T16:59:59": "normal",
                 "2026-10-05T17:00:00": "ai", "2026-10-04T23:59:59": "ai", "2026-10-05T00:00:00": "ai",
                 "2026-10-10T10:00:00": "ai"}   # Sunday night -> Monday, Saturday daytime
        for at, want in cases.items():
            self.assertEqual(decide(self.cfg, at).route, want, at)

    def test_utc_input_is_judged_in_japan_time(self):
        self.assertEqual(decide(self.cfg, "2026-10-05T00:00:00Z").route, "normal")    # 09:00 JST
        self.assertEqual(decide(self.cfg, "2026-10-04T23:59:59Z").route, "ai")        # 08:59:59 JST
        self.assertEqual(decide(self.cfg, dt.datetime(2026, 10, 5, 8, 0, tzinfo=dt.timezone.utc)).route, "ai")  # 17:00

    def test_manual_modes_ignore_the_schedule(self):
        for mode, want in (("always_ai", "ai"), ("always_normal", "normal")):
            cfg = S.validate_config({**S.DEFAULT_CONFIG, "mode": mode})
            for at in ("2026-10-05T10:00", "2026-10-05T20:00", "2026-10-11T10:00"):
                d = decide(cfg, at)
                self.assertEqual((d.route, d.rule), (want, f"manual_{mode}"), at)

    def test_special_day_beats_weekly_band_and_ai_failure_is_forced(self):
        cfg = S.validate_config({**S.DEFAULT_CONFIG, "schedule": {**S.DEFAULT_CONFIG["schedule"],
                                 "special_days": [{"date": "2026-10-05", "mode": "ai", "note": "臨時休業（架空）"}]}})
        self.assertEqual(decide(cfg, "2026-10-05T10:00").rule, "special_day")
        self.assertEqual(decide(cfg, "2026-10-05T10:00").route, "ai")
        self.assertEqual(decide(cfg, "2026-10-05T10:00", ai_available=False).route, "normal")
        dtmf = S.validate_config({**S.DEFAULT_CONFIG, "ai_failure_action": "dtmf"})
        self.assertEqual(decide(dtmf, "2026-10-05T20:00", ai_available=False).route, "dtmf")
        self.assertEqual(decide(dtmf, "2026-10-05T10:00", ai_available=False).route, "normal")   # not an AI slot

    def test_validation_rejects_overlaps_and_bad_times(self):
        bad = [{"mon": [{"start": "09:00", "end": "12:00", "mode": "normal"}, {"start": "11:00", "end": "13:00", "mode": "ai"}]},
               {"mon": [{"start": "17:00", "end": "09:00", "mode": "normal"}]},
               {"mon": [{"start": "9:00", "end": "17:00", "mode": "normal"}]}]
        for weekly in bad:
            with self.assertRaises(ValueError):
                S.validate_config({**S.DEFAULT_CONFIG, "schedule": {"weekly": weekly, "outside": "ai"}})
        ok = S.validate_config({**S.DEFAULT_CONFIG, "schedule": {"weekly": {"fri": [{"start": "20:00", "end": "24:00", "mode": "ai"}]},
                                                                  "outside": "normal"}})
        self.assertEqual(decide(ok, "2026-10-09T23:59:59").route, "ai")
        self.assertEqual(decide(ok, "2026-10-10T00:00:00").route, "normal")

    def test_hours_faq_follows_the_mode(self):
        self.assertIn("AIがご用件を伺い", S.hours_answer(self.cfg))
        normal_outside = S.validate_config({**S.DEFAULT_CONFIG, "schedule": {**S.DEFAULT_CONFIG["schedule"], "outside": "normal"}})
        self.assertNotIn("AI", S.hours_answer(normal_outside))
        self.assertIn("9時から17時", S.hours_answer(normal_outside))


class ServiceTest(unittest.TestCase):
    def test_settings_survive_reopening_the_database(self):
        import tempfile, pathlib
        with tempfile.TemporaryDirectory() as d:
            path = pathlib.Path(d) / "r.db"
            svc = ReceptionService(Store(path), env={}, clock=Clock())
            svc.save_config({**svc.config()[1], "mode": "always_ai"}, "tester")
            svc.store.close()
            again = ReceptionService(Store(path), env={}, clock=Clock())
            self.assertEqual(again.config()[1]["mode"], "always_ai")
            self.assertEqual(len(again.faqs()), len(S.FAQ_SEEDS))   # seeds are not duplicated

    def test_mid_call_changes_apply_from_the_next_call(self):
        svc = service()
        a = svc.start_call(at="2026-10-05T18:00")
        faq01 = next(f for f in svc.faqs() if f["code"] == "FAQ-01")
        svc.save_faq({**faq01, "enabled": False}, "tester", faq01["id"])
        cfg = svc.config()[1]
        svc.save_config({**cfg, "mode": "always_normal", "active_voice": "v2"}, "tester")
        self.assertTrue(svc.tool(a["id"], "lookup_faq", {"question": "点検は無料ですか"})["found"])   # snapshot FAQ
        self.assertEqual(svc.call_view(a["id"])["voice"]["id"], "v1")
        self.assertTrue(svc.ai_allowed(a["id"]))                                                       # route kept
        b = svc.start_call(at="2026-10-05T18:00")
        self.assertEqual(b["route"], "normal")
        svc.save_config({**svc.config()[1], "mode": "schedule"}, "tester")
        c = svc.start_call(at="2026-10-05T18:00")
        self.assertEqual(c["voice"]["id"], "v2")
        res = svc.tool(c["id"], "lookup_faq", {"question": "点検は無料ですか"})
        self.assertFalse(res["found"])
        self.assertEqual(svc.call_view(c["id"])["open_questions"], ["点検は無料ですか"])
        self.assertNotIn("点検は無料です。", svc.instructions(c["id"]))      # the vendor prompt follows the snapshot too
        self.assertIn("点検は無料です。", svc.instructions(a["id"]))

    def test_corrected_number_matches_in_record_summary_and_notification(self):
        svc = service()
        call = svc.start_call(at="2026-10-05T18:00")
        agent = RuleAgent(svc, call["id"])
        for u in ["焼肉ほのか博多店の田中です。無煙ロースター8台の清掃をお願いしたいです。", "折り返しは090-1234-5678です。",
                  "はい、違います。090-1234-5679です。", "はい、合っています。"]:
            agent.hear(u)
        tools = [e["data"] for e in svc.call_view(call["id"])["events"] if e["kind"] == "tool"]
        self.assertTrue(any(t["name"] == "confirm_field" and not t["result"]["ok"] for t in tools))   # guard said no
        view = svc.end_call(call["id"])
        num = next(f for f in view["fields"] if f["name"] == "callback_number")
        self.assertEqual((num["value"], num["status"]), ("09012345679", "confirmed_by_caller"))
        self.assertIn("090-1234-5679（本人確認済み）", view["summaries"][0]["text"])
        bodies = [n["body"] for n in view["notifications"]]
        self.assertEqual(len(bodies), 2)
        for b in bodies:
            self.assertIn("090-1234-5679（本人確認済み）", b)
            self.assertNotIn("5678", b)

    def test_unconfirmed_value_is_never_presented_as_confirmed(self):
        svc = service()
        call = svc.start_call(at="2026-10-05T18:00")
        svc.tool(call["id"], "save_field", {"field": "callback_number", "value": "090-1234-5678"})
        svc.tool(call["id"], "request_readback", {"field": "callback_number"})
        svc.caller_utterance(call["id"], "はい、違います")
        self.assertFalse(svc.tool(call["id"], "confirm_field", {"field": "callback_number", "value": "09012345678"})["ok"])
        view = svc.end_call(call["id"])
        self.assertIn("復唱済み・返事待ち（未確認）", view["summaries"][0]["text"])
        self.assertNotIn("本人確認済み", view["summaries"][0]["text"])
        self.assertIn("未確認：折り返し先", view["notifications"][0]["body"])

    def test_normal_route_never_reaches_the_ai(self):
        svc = service()
        call = svc.start_call(at="2026-10-05T10:00")
        self.assertEqual(call["route"], "normal")
        self.assertFalse(svc.caller_utterance(call["id"], "090-1234-5678")["forward"])
        self.assertEqual(svc.tool(call["id"], "save_field", {"field": "request", "value": "x"})["reason"],
                         "ai_not_used_for_this_call")
        view = svc.call_view(call["id"])
        self.assertFalse(any(e["kind"] in ("caller", "tool") and "090" in json.dumps(e["data"]) for e in view["events"]))
        self.assertEqual(view["summaries"], [])
        self.assertEqual(view["notifications"], [])
        with self.assertRaises(ValueError):
            svc.consent_event(call["id"], "ai_refused")

    def test_ai_refusal_stops_sending_deletes_transcript_and_is_not_resumed(self):
        svc = service()
        call = svc.start_call(at="2026-10-05T18:00", caller_id="090-1111-2222")
        cid = call["id"]
        svc.caller_utterance(cid, "焼肉ほのか博多店です")
        svc.tool(cid, "save_field", {"field": "shop_name", "value": "焼肉ほのか博多店"})
        svc.consent_event(cid, "ai_refused")
        kinds = [e["kind"] for e in svc.call_view(cid)["events"]]
        self.assertNotIn("caller", kinds)                       # transcript before the refusal deleted
        self.assertIn("transcript_deleted", kinds)
        self.assertFalse(svc.caller_utterance(cid, "やっぱり話します")["forward"])
        self.assertEqual(svc.tool(cid, "save_field", {"field": "request", "value": "x"})["reason"], "ai_refused")
        svc.consent_event(cid, "human_request_answer", True)  # cannot re-open the AI: the flow is not waiting for it
        self.assertFalse(svc.ai_allowed(cid))
        svc.dtmf(cid, "09012345678")
        view = svc.dtmf(cid, "1")["call"]
        self.assertTrue(view["ended_at"])
        num = next(f for f in view["fields"] if f["name"] == "callback_number")
        self.assertEqual((num["status"], num["source"]), ("confirmed_by_caller", "dtmf"))
        self.assertIn("AI拒否", view["summaries"][0]["text"])
        self.assertEqual(view["consent"]["ai_processing"], "refused")

    def test_recording_refusal_keeps_ai_but_human_request_is_asked_once(self):
        svc = service()
        cid = svc.start_call(at="2026-10-05T18:00")["id"]
        acts = [a["action"] for a in svc.consent_event(cid, "recording_refused")["actions"]]
        self.assertIn("stop_recording", acts)
        self.assertTrue(svc.ai_allowed(cid))
        svc.consent_event(cid, "human_request")
        svc.consent_event(cid, "human_request_answer", True)
        self.assertTrue(svc.ai_allowed(cid))
        self.assertEqual(svc.call_view(cid)["consent"]["recording"], "refused")

    def test_fields_that_may_not_be_stored_are_not_stored_summarised_or_notified(self):
        svc = service()
        cfg = svc.config()[1]
        cfg["fields"]["caller_name"] = {"store": False, "summary": True, "notify": True}
        cfg["fields"]["callback_number"]["notify"] = False
        cfg["store_transcript"] = False
        svc.save_config(cfg, "tester")
        cid = svc.start_call(at="2026-10-05T18:00")["id"]
        svc.caller_utterance(cid, "田中です。090-1234-5678です")
        self.assertEqual(svc.tool(cid, "save_field", {"field": "caller_name", "value": "田中"})["reason"], "field_not_storable")
        svc.tool(cid, "save_field", {"field": "callback_number", "value": "090-1234-5678"})
        view = svc.end_call(cid)
        dump = json.dumps(view, ensure_ascii=False)
        self.assertNotIn("田中", dump)                       # neither the field, the event log nor the transcript
        self.assertNotIn("お名前", view["summaries"][0]["text"])
        self.assertIn("090-1234-5678", view["summaries"][0]["text"])
        self.assertNotIn("090-1234-5678", view["notifications"][0]["body"])

    def test_staff_correction_keeps_history_and_issues_a_correction_notice(self):
        svc = service()
        cid = svc.start_call(at="2026-10-05T18:00")["id"]
        svc.tool(cid, "save_field", {"field": "callback_number", "value": "090-1234-5678"})
        with self.assertRaises(ValueError):
            svc.correct_field(cid, "callback_number", "090-1234-5679", "staff", "番号違い")   # still on the call
        svc.end_call(cid)
        svc.outbox.process()
        with self.assertRaises(ValueError):
            svc.correct_field(cid, "callback_number", "090-1234-5679", "staff", "")             # reason required
        view = svc.correct_field(cid, "callback_number", "090-1234-5679", "staff", "折り返して確認（架空）")
        c = view["corrections"][0]
        self.assertEqual((c["before_value"], c["before_status"], c["after_value"]), ("09012345678", "heard", "09012345679"))
        self.assertEqual([s["version"] for s in view["summaries"]], [1, 2])
        self.assertIn("担当者が補正", view["summaries"][1]["text"])
        v2 = [n for n in view["notifications"] if n["version"] == 2]
        accepted_target = next(n["target_id"] for n in view["notifications"] if n["version"] == 1 and n["status"] == "accepted")
        retry_target = next(n["target_id"] for n in view["notifications"] if n["version"] == 1 and n["status"] == "superseded")
        self.assertTrue(next(n for n in v2 if n["target_id"] == accepted_target)["body"].startswith("【訂正】先ほどの通知を訂正します"))
        self.assertTrue(next(n for n in v2 if n["target_id"] == retry_target)["body"].startswith("【訂正済み】"))

    def test_emergency_notifies_at_once(self):
        svc = service()
        cid = svc.start_call(at="2026-10-05T18:00")["id"]
        res = svc.tool(cid, "flag_emergency", {"level": "obvious"})
        self.assertEqual((res["play"], res["notified"]), ("E-01", 2))
        self.assertEqual(svc.tool(cid, "flag_emergency", {"level": "obvious"})["notified"], 0)   # no duplicate
        self.assertEqual({n["kind"] for n in svc.call_view(cid)["notifications"]}, {"emergency_obvious"})


class OutboxTest(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.svc = service(self.clock)
        self.ob: Outbox = self.svc.outbox

    def call_with_summary(self):
        cid = self.svc.start_call(at="2026-10-05T18:00")["id"]
        self.svc.tool(cid, "save_field", {"field": "request", "value": "清掃"})
        self.svc.end_call(cid)
        return cid

    def statuses(self, cid):
        return {n["target_name"]: n["status"] for n in self.svc.call_view(cid)["notifications"] if n["version"] == 1}

    def test_per_target_results_and_no_duplicates_on_reinjection(self):
        cid = self.call_with_summary()
        self.assertEqual(self.svc.renotify(cid, "tester")["created"], 0)
        self.ob.process()
        self.assertEqual(self.statuses(cid), {"担当者A（架空）": "accepted", "担当者B（架空）": "retrying"})
        sent_once = self.svc.store.q1("SELECT COUNT(*) AS c FROM notification_attempts")["c"]
        self.svc.renotify(cid, "tester")
        self.ob.process()                      # B waits for its retry time; A is never sent again
        self.assertEqual(self.svc.store.q1("SELECT COUNT(*) AS c FROM notification_attempts")["c"], sent_once)
        self.clock.advance(minutes=2)
        self.ob.process()
        self.assertEqual(set(self.statuses(cid).values()), {"accepted"})
        keys = self.svc.store.q("SELECT DISTINCT a.retry_key FROM notification_attempts a JOIN notifications n "
                                "ON n.id = a.notification_id WHERE n.target_id = 2")
        self.assertEqual(len(keys), 1)          # the retry used the same key
        self.assertEqual(self.svc.store.q1("SELECT COUNT(*) AS c FROM notifications WHERE call_id = ?", (cid,))["c"], 2)

    def set_sim(self, tid, mode):
        t = next(x for x in self.ob.targets() if x["id"] == tid)
        self.ob.save_target({**t, "simulate": mode, "enabled": True}, "tester", tid)

    def test_unknown_after_24h_then_people_decide(self):
        self.set_sim(2, "fail_500_always")
        cid = self.call_with_summary()
        for _ in range(12):
            self.ob.process(force_due=True)
            self.clock.advance(hours=3)
        n = next(x for x in self.svc.call_view(cid)["notifications"] if x["target_id"] == 2)
        self.assertEqual(n["status"], "unknown")
        self.assertGreaterEqual(dt.datetime.fromisoformat(n["updated_at"]) - dt.datetime.fromisoformat(n["first_attempt_at"]),
                                RETRY_WINDOW)
        new_id = self.ob.resend(n["id"], "tester")
        resent = self.svc.store.q1("SELECT * FROM notifications WHERE id = ?", (new_id,))
        self.assertTrue(resent["body"].startswith("【再送】"))
        self.assertNotEqual(resent["retry_key"], n["retry_key"])

    def test_monthly_limit_stops_the_channel_until_a_person_resumes(self):
        self.set_sim(1, "monthly_limit_429")
        cid = self.call_with_summary()
        self.ob.process()
        self.assertEqual(self.statuses(cid)["担当者A（架空）"], "channel_stopped")
        self.assertTrue(self.ob.channel_stopped("simulation"))
        cid2 = self.call_with_summary()
        self.ob.process(force_due=True)
        self.assertEqual(set(self.statuses(cid2).values()), {"pending"})   # nothing goes out while stopped
        self.set_sim(1, "success")
        self.ob.resume_channel("simulation", "tester")
        self.ob.process(force_due=True)
        self.assertEqual(self.statuses(cid2)["担当者A（架空）"], "accepted")

    def test_rate_limit_and_bad_request_stop_for_a_person(self):
        self.set_sim(1, "rate_limit_429")
        self.set_sim(2, "bad_request_400")
        cid = self.call_with_summary()
        for _ in range(4):
            self.ob.process(force_due=True)
        self.assertEqual(set(self.statuses(cid).values()), {"failed_stopped"})
        self.assertEqual(self.svc.status()["notification_problems"]["counts"]["failed_stopped"], 2)

    def test_line_is_never_sent(self):
        line = next(t for t in self.ob.targets() if t["channel"] == "line")
        self.ob.save_target({**line, "enabled": True}, "tester", line["id"])
        cid = self.call_with_summary()
        self.ob.process()
        n = next(x for x in self.svc.call_view(cid)["notifications"] if x["channel"] == "line")
        self.assertEqual(n["status"], "blocked_unapproved")
        self.assertEqual(n["line_request_preview"]["headers"]["X-Line-Retry-Key"], n["retry_key"])
        self.assertEqual(n["attempts"], 0)


if __name__ == "__main__":
    unittest.main()
