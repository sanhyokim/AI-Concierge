import http.client
import json
import os
import pathlib
import shutil
import subprocess
import tempfile
import threading
import unittest
from unittest import mock

from prototype.browser_lab import config, vendors
from prototype.browser_lab.server import Lab, serve


class FakeHttp:
    def __init__(self, responses):
        self.responses, self.calls = responses, []

    def __call__(self, method, url, headers, body=None):
        self.calls.append({"method": method, "url": url, "headers": headers, "body": body})
        for key, resp in self.responses.items():
            if key in url:
                if isinstance(resp, Exception):
                    raise resp
                return 200, resp
        raise AssertionError(f"unexpected url {url}")


def limit(n=2, candidates=("gpt-live-1", "gemini-3.8-live")):
    """Put a count limit back for a test: the mechanism stays (held candidates use 0) though the user removed the
    limits for the tested candidates (2026-10-08)."""
    return mock.patch.dict(config.SESSIONS["connection"], {c: n for c in candidates})


class LabTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ledger = pathlib.Path(self.tmp.name) / "ledger.jsonl"
        self.results = pathlib.Path(self.tmp.name) / "results"

    def tearDown(self):
        self.tmp.cleanup()

    def lab(self, env, responses=None):
        return Lab(env=env, http=FakeHttp(responses or {}), ledger_path=self.ledger, results_dir=self.results)

    def test_candidates_never_expose_keys_and_missing_keys_send_nothing(self):
        lab = self.lab({"GEMINI_API_KEY": "secret-key"})
        listed = {c["id"]: c for c in config.public_candidates(lab.env)}
        self.assertTrue(listed["fake"]["ready"])
        self.assertTrue(listed["gemini-3.8-live"]["ready"])
        self.assertFalse(listed["elevenagents"]["ready"])
        self.assertNotIn("secret-key", json.dumps(listed))
        with self.assertRaises(ValueError):
            lab.start("elevenagents")
        self.assertFalse(self.ledger.exists())
        self.assertEqual(lab.http.calls, [])

    def test_session_reserves_mints_without_key_and_settles_an_estimate(self):
        lab = self.lab({"GEMINI_API_KEY": "secret-key"}, {"auth_tokens": {"name": "auth_tokens/abc"}})
        s = lab.start("gemini-3.8-live")
        self.assertNotIn("secret-key", json.dumps(s))
        self.assertEqual(s["credentials"]["token"], "auth_tokens/abc")
        self.assertEqual(lab.http.calls[0]["headers"]["x-goog-api-key"], "secret-key")
        led = lab.ledger("google_lab")
        self.assertAlmostEqual(led.totals()["held_usd"], config.MAX_SESSION_MIN * 0.06)
        out = lab.end(s["session_id"], 61.0)
        self.assertEqual(out["billed_s"], 120)               # rounded up to whole minutes
        t = led.totals()
        self.assertEqual(t["open_reservations"], 1)          # an estimate does not release the reservation
        self.assertAlmostEqual(t["est_cost_usd"], config.MAX_SESSION_MIN * 0.06)
        from prototype.engines.budget import reconcile
        rid = led.open_reservations()[0]["rid"]
        reconcile(led, rid, 0.03, "利用画面で確認（試験）")
        t = led.totals()
        self.assertEqual(t["open_reservations"], 0)
        self.assertAlmostEqual(t["est_cost_usd"], 0.03)

    def test_mint_failure_releases_the_reservation(self):
        lab = self.lab({"GEMINI_API_KEY": "k"}, {"auth_tokens": vendors.VendorError("HTTP 401")})
        with self.assertRaises(vendors.VendorError):
            lab.start("gemini-3.8-live")
        t = lab.ledger("google_lab").totals()
        self.assertEqual((t["open_reservations"], t["est_cost_usd"]), (0, 0.0))
        self.assertEqual(lab.counts("gemini-3.8-live")["mint_failures"], 1)
        self.assertEqual(lab.counts("gemini-3.8-live")["sessions"], 0)   # not counted as a started session

    def test_unfinished_session_keeps_its_full_reservation(self):
        lab = self.lab({"OPENAI_API_KEY": "k"}, {"live/sessions": {}})
        lab.start("gpt-live-1")
        self.assertAlmostEqual(lab.ledger("openai_lab").totals()["est_cost_usd"],
                               config.session_reserve_usd(config.CANDIDATES["gpt-live-1"], {}))

    def test_held_candidates_cannot_start(self):
        lab = self.lab({"OPENAI_API_KEY": "k", "ELEVENLABS_API_KEY": "x", "ELEVENLABS_AGENT_ID": "a"})
        for cid in ("gpt-realtime-2.1", "elevenagents"):
            with self.assertRaises(ValueError) as cm:
                lab.start(cid)
            self.assertIn("保留", str(cm.exception))
        self.assertEqual(lab.http.calls, [])
        listed = {c["id"]: c for c in config.public_candidates(lab.env)}
        self.assertFalse(listed["gpt-realtime-2.1"]["ready"])
        self.assertTrue(listed["gpt-realtime-2.1"]["hold"])

    def test_gpt_live_offer_is_exchanged_server_side(self):
        lab = self.lab({"OPENAI_API_KEY": "secret"},
                       {"live/sessions": {"session": {"id": "s1"}, "transport": {"sdp": "v=0 answer"}}})
        s = lab.start("gpt-live-1")
        ans = lab.sdp(s["session_id"], "v=0 offer")
        self.assertEqual(ans["sdp"], "v=0 answer")
        body = lab.http.calls[-1]["body"]
        self.assertEqual(body["transport"], {"type": "webrtc", "sdp": "v=0 offer"})
        self.assertEqual(body["session"]["model"], "gpt-live-1")
        self.assertNotIn("secret", json.dumps(ans))

    def test_elevenlabs_and_realtime_credentials(self):
        # held candidates: the minting code is checked directly, without starting a lab session
        http = FakeHttp({"get-signed-url": {"signed_url": "wss://signed"}, "client_secrets": {"value": "ek_1"}})
        env = {"ELEVENLABS_API_KEY": "xi", "ELEVENLABS_AGENT_ID": "ag", "OPENAI_API_KEY": "sk"}
        e = vendors.mint(config.CANDIDATES["elevenagents"], env, http)
        self.assertEqual(e["signed_url"], "wss://signed")
        self.assertNotIn("overrides", e)                     # no overrides unless allowed
        self.assertEqual(http.calls[-1]["headers"]["xi-api-key"], "xi")
        r = vendors.mint(config.CANDIDATES["gpt-realtime-2.1"], env, http)
        self.assertEqual(r["ephemeral_key"], "ek_1")
        body = http.calls[-1]["body"]
        self.assertEqual(body["session"]["max_output_tokens"], 1200)
        self.assertEqual(body["expires_after"], {"anchor": "created_at", "seconds": 60})
        self.assertNotIn("sk", json.dumps(r))

    def test_tools_use_the_confirmation_guard_and_results_are_saved(self):
        lab = self.lab({})
        s = lab.start("fake")
        sid = s["session_id"]
        lab.tool(sid, "save_field", {"field": "callback_number", "value": "090-1234-5678"})
        lab.tool(sid, "request_readback", {"field": "callback_number"})
        lab.utterance(sid, "はい、違います", 1)
        r = lab.tool(sid, "confirm_field", {"field": "callback_number", "value": "09012345678"}, delay_ms=10)
        self.assertFalse(r["ok"])                        # 「はい、違います」 is not a confirmation
        path = lab.save_results({"candidate": "fake", "session_id": sid, "judgments": {}})
        saved = json.loads(pathlib.Path(path).read_text(encoding="utf-8"))
        self.assertEqual(saved["path"], "browser_lab")
        self.assertEqual(len(saved["server_tool_calls"]), 3)


class LabBusinessLogicTest(unittest.TestCase):
    """The lab's tool calls go to the common reception service; vendor bodies follow the call's snapshot."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = pathlib.Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def lab(self, env, responses=None, **kw):
        return Lab(env=env, http=FakeHttp(responses or {}), ledger_path=self.dir / "ledger.jsonl",
                   results_dir=self.dir / "results", **kw)

    def test_gpt_live_uses_responses_delegation_with_the_reception_tools(self):
        lab = self.lab({"OPENAI_API_KEY": "secret-key"},
                       {"live/sessions": {"transport": {"sdp": "answer"}, "session": {"id": "ls_1"}}})
        faq = next(f for f in lab.service.faqs() if f["code"] == "FAQ-02")
        lab.service.save_faq({**faq, "enabled": False}, "tester", faq["id"])
        s = lab.start("gpt-live-1")
        self.assertEqual(lab.sdp(s["session_id"], "offer")["sdp"], "answer")
        body = lab.http.calls[-1]["body"]["session"]
        self.assertEqual(body["delegation"]["type"], "responses")
        names = [t["name"] for t in body["delegation"]["responses"]["tools"]]
        self.assertEqual(names, ["save_field", "request_readback", "confirm_field", "lookup_faq", "flag_emergency"])
        self.assertEqual(body["delegation"]["responses"]["model"], "gpt-6-luna")
        self.assertIn("点検は無料です。", body["instructions"])            # FAQ from the admin settings
        self.assertNotIn("2万5千円", body["instructions"])                  # disabled FAQ is not given
        self.assertNotIn("secret-key", json.dumps(body))
        with self.assertRaises(ValueError):                                 # one vendor session per reservation
            lab.sdp(s["session_id"], "offer")

    def test_cartesia_uses_the_agent_websocket_with_client_tools(self):
        http = FakeHttp({"access-token": {"token": "tok"}})
        creds = vendors.mint(config.CANDIDATES["cartesia-agents"], {"CARTESIA_API_KEY": "k", "CARTESIA_AGENT_ID":
                                                                   "agent_1"}, http)
        self.assertEqual(creds["ws_url"], "wss://api.cartesia.ai/v1/agents/websocket/agent_1")
        self.assertEqual(creds["start"], {"type": "session_create", "audio": {"input_format": "pcm_16000"}})
        self.assertEqual(http.calls[-1]["body"]["expires_in"], 60)

    def test_tool_calls_use_the_snapshot_and_the_guard_and_refusal_is_final(self):
        lab = self.lab({})
        s = lab.start("fake")
        sid = s["session_id"]
        self.assertTrue(lab.tool(sid, "lookup_faq", {"question": "点検は無料ですか"})["found"])
        lab.tool(sid, "save_field", {"field": "callback_number", "value": "090-1234-5678"})
        lab.tool(sid, "request_readback", {"field": "callback_number"})
        lab.utterance(sid, "はい、違います", 1)
        self.assertFalse(lab.tool(sid, "confirm_field", {"field": "callback_number", "value": "09012345678"})["ok"])
        res = lab.consent(sid, "ai_refused")
        self.assertTrue(res["stop_ai"])
        self.assertEqual(lab.tool(sid, "save_field", {"field": "request", "value": "x"})["reason"], "ai_refused")
        self.assertTrue(lab.heartbeat(sid)["stop"])           # the page is told to close the connection
        lab.end(sid, 30)
        saved_path = pathlib.Path(lab.save_results({"candidate": "fake", "session_id": sid}))
        saved = json.loads(saved_path.read_text(encoding="utf-8"))
        self.assertTrue(saved["comparison"]["business_logic_status"].startswith("評価対象"))
        self.assertEqual(saved["server_fields"]["callback_number"]["status"], "awaiting_confirmation")
        view = lab.service.call_view(s["call_id"])
        self.assertEqual((view["source"], view["notifications"]), ("browser_lab", []))   # the lab notifies no one

    def test_no_tool_calls_means_business_logic_not_evaluated(self):
        lab = self.lab({})
        sid = lab.start("fake")["session_id"]
        lab.end(sid, 5)
        saved_path = pathlib.Path(lab.save_results({"candidate": "fake", "session_id": sid}))
        saved = json.loads(saved_path.read_text(encoding="utf-8"))
        self.assertTrue(saved["comparison"]["business_logic_status"].startswith("未評価"))

    def test_tested_candidates_have_no_count_limit_but_every_start_is_recorded(self):
        from prototype.browser_lab.server import SessionLimit
        lab = self.lab({"GEMINI_API_KEY": "k", "OPENAI_API_KEY": "k"}, {"auth_tokens": {"name": "auth_tokens/abc"}})
        for _ in range(5):                                  # user decision 2026-10-08: no count or cost cap
            lab.end(lab.start("gemini-3.8-live")["session_id"], 60)
        c = lab.counts("gemini-3.8-live")
        self.assertEqual((c["sessions"], c["limit"]), (5, None))
        t = lab.ledger("google_lab").totals()
        self.assertEqual((t["requests"], t["open_reservations"]), (5, 5))   # still recorded for reconciliation
        with self.assertRaises(SessionLimit):               # held candidates still cannot start
            lab.start("gpt-realtime-2.1")
        self.assertEqual(config.MAX_SESSION_MIN, 5.0)       # test calls stop at 5 minutes

    def test_billing_never_below_the_time_the_server_saw(self):
        lab = self.lab({"GEMINI_API_KEY": "k"}, {"auth_tokens": {"name": "auth_tokens/abc"}})
        sid = lab.start("gemini-3.8-live")["session_id"]
        lab.sessions[sid]["started"] -= 125          # the session has been open for about 2 minutes
        res = lab.end(sid, 1)                       # the page reports 1 s
        self.assertEqual(res["billed_s"], 180)      # rounded up to the 60 s increment


class Clock:
    def __init__(self, t=1_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


class LabSessionControlTest(unittest.TestCase):
    """Review fc81edd item 3 and the plan's session control: counts, expiry, GPT-Live limits, settings reflection."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = pathlib.Path(self.tmp.name)
        self.closes = []

    def tearDown(self):
        self.tmp.cleanup()

    def service(self, env=None):
        from prototype.reception.service import ReceptionService
        from prototype.reception.store import Store
        return ReceptionService(Store(self.dir / "lab.db"), env=env or {})

    def close_ok(self, vendor_sid, key):
        self.closes.append((vendor_sid, key))
        return {"closed": True, "event": {"type": "session.closed"}, "error": None}

    def lab(self, env, responses=None, service=None, clock=None, live_close=None):
        return Lab(env=env, http=FakeHttp(responses or {}), ledger_path=self.dir / "ledger.jsonl",
                   results_dir=self.dir / "results", service=service or self.service(env),
                   live_close=live_close or self.close_ok, clock=clock or Clock())

    LIVE = {"live/sessions": {"transport": {"sdp": "answer"}, "session": {"id": "ls_1"}}}

    def test_review_item3_a_count_limit_is_enforced_before_anything_is_sent(self):
        from prototype.browser_lab.server import SessionLimit
        lab = self.lab({"OPENAI_API_KEY": "k"}, self.LIVE)
        with limit(2):
            started = [lab.start("gpt-live-1")["session_id"] for _ in range(2)]
            with self.assertRaises(SessionLimit):
                lab.start("gpt-live-1")
        self.assertEqual(len(started), 2)
        self.assertEqual(lab.ledger("openai_lab").totals()["requests"], 2)   # no third reservation
        self.assertIn("session_id", lab.start("gpt-live-1"))    # without a limit (the user's decision) it starts

    def test_concurrent_starts_never_exceed_the_count(self):
        from prototype.browser_lab.server import SessionLimit
        lab = self.lab({"GEMINI_API_KEY": "k"}, {"auth_tokens": {"name": "auth_tokens/abc"}})
        ok, refused, barrier = [], [], threading.Barrier(6)

        def go():
            barrier.wait()
            try:
                ok.append(lab.start("gemini-3.8-live")["session_id"])
            except SessionLimit:
                refused.append(1)
        with limit(2):
            threads = [threading.Thread(target=go) for _ in range(6)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
        self.assertEqual((len(ok), len(refused)), (2, 4))
        self.assertEqual(lab.counts("gemini-3.8-live")["sessions"], 2)

    def test_counts_survive_a_restart_and_lost_sessions_keep_their_reservation(self):
        from prototype.browser_lab.server import SessionLimit
        env = {"GEMINI_API_KEY": "k"}
        resp = {"auth_tokens": {"name": "auth_tokens/abc"}}
        first = self.lab(env, resp)
        sid = first.start("gemini-3.8-live")["session_id"]           # still active when the server stops
        first.store.db.close()
        again = self.lab(env, resp)                                  # same database, new process state
        row = again.store.q1("SELECT status FROM lab_sessions WHERE id = ?", (sid,))
        self.assertEqual(row["status"], "lost_on_restart")
        self.assertEqual(again.counts("gemini-3.8-live")["sessions"], 1)   # counted
        self.assertEqual(again.ledger("google_lab").totals()["open_reservations"], 1)   # not released
        self.assertTrue(again.heartbeat(sid)["stop"])
        with limit(2):
            again.start("gemini-3.8-live")
            with self.assertRaises(SessionLimit):
                again.start("gemini-3.8-live")

    def test_mint_failures_are_recorded_but_do_not_block(self):
        """The user removed the limits (2026-10-08): failed starts are recorded, the next start is allowed."""
        lab = self.lab({"GEMINI_API_KEY": "k"}, {"auth_tokens": vendors.VendorError("HTTP 401")})
        for _ in range(3):
            with self.assertRaises(vendors.VendorError):
                lab.start("gemini-3.8-live")
        self.assertEqual(lab.counts("gemini-3.8-live")["mint_failures"], 3)
        self.assertEqual(lab.counts("gemini-3.8-live")["sessions"], 0)
        lab.http.responses["auth_tokens"] = {"name": "auth_tokens/ok"}
        self.assertIn("session_id", lab.start("gemini-3.8-live"))
        self.assertEqual(lab.counts("gemini-3.8-live")["sessions"], 1)

    def test_watchdog_expires_a_session_and_asks_gpt_live_to_close(self):
        clock = Clock()
        lab = self.lab({"OPENAI_API_KEY": "sk-test"}, self.LIVE, clock=clock)
        s = lab.start("gpt-live-1")
        sid = s["session_id"]
        lab.sdp(sid, "offer")
        clock.t += config.MAX_SESSION_MIN * 60 - 1
        self.assertEqual(lab.sweep(), [])                            # still within the maximum
        clock.t += 1 + config.SESSION_GRACE_S + 1
        self.assertEqual(lab.sweep(), [sid])
        self.assertEqual(self.closes, [("ls_1", "sk-test")])        # sideband session.close
        row = lab.store.q1("SELECT * FROM lab_sessions WHERE id = ?", (sid,))
        self.assertEqual((row["status"], row["close_confirmed"]), ("expired", 1))
        self.assertTrue(lab.heartbeat(sid)["stop"])
        with self.assertRaises(ValueError):                          # no business processing after expiry
            lab.tool(sid, "save_field", {"field": "request", "value": "x"})
        t = lab.ledger("openai_lab").totals()
        self.assertEqual(t["open_reservations"], 1)                  # kept until reconciled
        self.assertAlmostEqual(t["est_cost_usd"], config.session_reserve_usd(config.CANDIDATES["gpt-live-1"], {}))

    def test_close_not_confirmed_is_recorded_and_the_reservation_stays(self):
        clock = Clock()
        lab = self.lab({"OPENAI_API_KEY": "sk-test"}, self.LIVE, clock=clock,
                       live_close=lambda vsid, key: {"closed": False, "event": None, "error": "timeout"})
        sid = lab.start("gpt-live-1")["session_id"]
        lab.sdp(sid, "offer")
        clock.t += config.MAX_SESSION_MIN * 60 + config.SESSION_GRACE_S + 5
        lab.sweep()
        row = lab.store.q1("SELECT * FROM lab_sessions WHERE id = ?", (sid,))
        self.assertEqual(row["close_confirmed"], 0)
        self.assertIn("未確認", row["end_reason"])
        self.assertEqual(lab.ledger("openai_lab").totals()["open_reservations"], 1)

    def test_page_end_without_session_closed_keeps_close_unconfirmed(self):
        lab = self.lab({"OPENAI_API_KEY": "k"}, self.LIVE)
        sid = lab.start("gpt-live-1")["session_id"]
        lab.end(sid, 30, "試験者が終了")
        row = lab.store.q1("SELECT * FROM lab_sessions WHERE id = ?", (sid,))
        self.assertEqual(row["close_confirmed"], 0)
        sid2 = lab.start("gpt-live-1")["session_id"]
        lab.end(sid2, 30, "試験者が終了", close_confirmed=True, final_usage={"input_tokens": 10})
        row = lab.store.q1("SELECT * FROM lab_sessions WHERE id = ?", (sid2,))
        self.assertEqual(row["close_confirmed"], 1)
        self.assertEqual(lab.ledger("openai_lab").totals()["open_reservations"], 2)   # both wait for reconcile

    def test_gpt_live_backend_responses_are_capped_and_usage_is_recorded(self):
        lab = self.lab({"OPENAI_API_KEY": "k"}, self.LIVE)
        sid = lab.start("gpt-live-1")["session_id"]
        for _ in range(config.MAX_BACKEND_RESPONSES):
            self.assertFalse(lab.vendor_event(sid, "backend_response")["stop"])
        self.assertEqual(lab.vendor_event(sid, "backend_response"),
                         {"stop": True, "reason": "backend_response_limit"})
        self.assertTrue(lab.heartbeat(sid)["stop"])
        lab.vendor_event(sid, "backend_usage", {"input_tokens": 10_000, "cached_tokens": 2_000,
                                                "output_tokens": 400})
        led = lab.ledger("openai_lab")
        rows = [r for r in led._lines() if r.get("operation") == "gpt_live_backend"]
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0]["units"]["reported_by_vendor"])
        self.assertAlmostEqual(rows[0]["est_cost_usd"], (8_000 * 0.10 + 2_000 * 0.01 + 400 * 0.50) / 1e6)
        self.assertEqual(led.totals()["requests"], 1)               # still one session, not two requests
        lab.end(sid, 60)
        self.assertIn("session_id", lab.start("gpt-live-1"))        # the second planned session can start

    def test_tool_calls_are_capped_separately_from_backend_responses(self):
        lab = self.lab({"OPENAI_API_KEY": "k"}, self.LIVE)
        sid = lab.start("gpt-live-1")["session_id"]
        for _ in range(config.MAX_TOOL_CALLS):
            lab.tool(sid, "lookup_faq", {"question": "点検は無料ですか"})
        self.assertEqual(lab.tool(sid, "lookup_faq", {"question": "x"})["reason"], "tool_call_limit")
        self.assertTrue(lab.heartbeat(sid)["stop"])
        row = lab.store.q1("SELECT * FROM lab_sessions WHERE id = ?", (sid,))
        self.assertEqual(row["backend_responses"], 0)                # counted separately

    def test_spoken_ai_refusal_stops_the_session_but_recording_refusal_does_not(self):
        lab = self.lab({})
        sid = lab.start("fake", fake_script="recording")["session_id"]
        r = lab.utterance(sid, "録音はしないでください", 1)
        self.assertEqual((r["forward"], r["stop_ai"], r.get("recording_stopped")), (True, False, True))
        self.assertFalse(lab.heartbeat(sid)["stop"])                 # the connection stays open
        self.assertTrue(lab.tool(sid, "lookup_faq", {"question": "点検は無料ですか"})["found"])
        r = lab.utterance(sid, "人と話したいので、AIは使わないでください", 2)
        self.assertEqual((r["forward"], r["stop_ai"]), (False, True))
        self.assertEqual(lab.heartbeat(sid), {"stop": True, "reason": "ai_refused_by_speech"})
        self.assertEqual(lab.tool(sid, "save_field", {"field": "request", "value": "x"})["reason"], "ai_refused")

    def test_settings_reach_session_candidates_and_the_next_session_only(self):
        env = {"OPENAI_API_KEY": "k", "GEMINI_API_KEY": "g"}
        lab = self.lab(env, {**self.LIVE, "auth_tokens": {"name": "auth_tokens/abc"}})
        g1 = lab.start("gemini-3.8-live")
        setup1 = g1["credentials"]["setup"]["setup"]
        self.assertEqual(setup1["speechConfig"]["voiceConfig"]["prebuiltVoiceConfig"]["voiceName"], "Kore")
        self.assertIn("Kore（管理画面の設定）", g1["applied"]["voice"])
        self.assertIn("点検は無料です。", setup1["systemInstruction"]["parts"][0]["text"])
        # change the voice and disable a FAQ while g1 is still running
        _, cfg, _ = lab.service.config()
        cfg["voices"] = [v if v["candidate"] != "gemini-3.8-live" else {**v, "voice": "Aoede"} for v in cfg["voices"]]
        lab.service.save_config(cfg, "tester")
        faq = next(f for f in lab.service.faqs() if f["code"] == "FAQ-02")
        lab.service.save_faq({**faq, "enabled": False}, "tester", faq["id"])
        g2 = lab.start("gemini-3.8-live")
        setup2 = g2["credentials"]["setup"]["setup"]
        self.assertEqual(setup2["speechConfig"]["voiceConfig"]["prebuiltVoiceConfig"]["voiceName"], "Aoede")
        self.assertNotIn("FAQ-02", g2["faq_codes"])
        self.assertIn("FAQ-02", g1["faq_codes"])                     # the running call keeps its snapshot
        self.assertTrue(lab.tool(g1["session_id"], "lookup_faq", {"question": "ロースターの清掃の料金"})["found"])
        self.assertFalse(lab.tool(g2["session_id"], "lookup_faq", {"question": "ロースターの清掃の料金"})["found"])
        live = lab.start("gpt-live-1")
        lab.sdp(live["session_id"], "offer")
        self.assertEqual(lab.http.calls[-1]["body"]["session"]["audio"]["output"]["voice"], "marin")

    def test_agent_fixed_settings_are_labelled_and_overrides_need_permission(self):
        from prototype.browser_lab.server import SessionLimit
        env = {"LAB_RELEASE_HOLD": "elevenagents,cartesia-agents", "ELEVENLABS_API_KEY": "xi",
               "ELEVENLABS_AGENT_ID": "ag", "CARTESIA_API_KEY": "c", "CARTESIA_AGENT_ID": "a1"}
        resp = {"get-signed-url": {"signed_url": "wss://signed"}, "access-token": {"token": "tok"}}
        lab = self.lab(env, resp)
        with self.assertRaises(SessionLimit) as cm:                  # releasing the hold alone starts nothing
            lab.start("cartesia-agents")
        self.assertIn("0回", str(cm.exception))
        # after the user approves counts and caps for them (patched here)
        from prototype.engines.budget import Limits
        approved = Limits(max_requests=4, max_audio_seconds=16 * 60, max_cost_usd=2.0)
        patch_counts = mock.patch.dict(config.SESSIONS["connection"], {"cartesia-agents": 2, "elevenagents": 2})
        patch_caps = mock.patch.dict(config.LAB_LIMITS_BY_STAGE["connection"],
                                     {"cartesia_lab": approved, "elevenlabs_lab": approved})
        patch_counts.start(), patch_caps.start()
        self.addCleanup(patch_counts.stop)
        self.addCleanup(patch_caps.stop)
        c = lab.start("cartesia-agents")
        self.assertEqual(c["applied"]["voice"], config.APPLY_LABELS["agent_fixed"])
        self.assertEqual(c["applied"]["instructions"], config.APPLY_LABELS["agent_fixed"])
        e = lab.start("elevenagents")
        self.assertNotIn("overrides", e["credentials"])
        self.assertEqual(e["applied"]["instructions"], config.APPLY_LABELS["agent_fixed"])
        lab2 = self.lab({**env, "ELEVENLABS_OVERRIDES": "prompt"}, resp, service=lab.service)
        e2 = lab2.start("elevenagents")
        ov = e2["credentials"]["overrides"]
        self.assertIn("点検は無料です。", ov["agent"]["prompt"]["prompt"])
        self.assertNotIn("tts", ov)                                  # the voice was not allowed
        self.assertEqual(e2["applied"]["voice"], config.APPLY_LABELS["agent_fixed"])
        listed = {x["id"]: x for x in config.public_candidates(env)}
        self.assertEqual(listed["cartesia-agents"]["applies"]["voice"], config.APPLY_LABELS["agent_fixed"])

    def test_admin_cannot_make_an_agent_fixed_voice_the_active_voice(self):
        svc = self.service()
        _, cfg, _ = svc.config()
        cfg["voices"].append({"id": "c1", "candidate": "cartesia-agents", "voice": "voice-abc", "label": "記録"})
        svc.save_config(cfg, "tester")                               # recording the id is allowed
        with self.assertRaises(ValueError) as cm:
            svc.save_config({**cfg, "active_voice": "c1"}, "tester")
        self.assertIn("固定", str(cm.exception))


class HttpLayerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        lab = Lab(env={}, ledger_path=pathlib.Path(self.tmp.name) / "l.jsonl",
                  results_dir=pathlib.Path(self.tmp.name) / "r")
        self.httpd = serve(0, lab)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.tmp.cleanup()

    def req(self, method, path, body=None, host=None, extra=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        headers = {"Content-Type": "application/json", **(extra or {})}
        if host:
            headers["Host"] = host
        c.request(method, path, json.dumps(body) if body is not None else None, headers)
        r = c.getresponse()
        return r.status, r.read()

    def test_routes_host_check_and_no_traversal(self):
        status, body = self.req("GET", "/api/candidates")
        self.assertEqual(status, 200)
        self.assertIn("fake", body.decode())
        self.assertEqual(self.req("GET", "/", host="evil.example")[0], 403)
        self.assertEqual(self.req("GET", "/../server.py")[0], 404)
        self.assertEqual(self.req("GET", "/")[0], 200)
        self.assertEqual(self.req("POST", "/api/session/start", {"candidate": "gemini-3.8-live"})[0], 400)
        # a page on another site cannot start sessions (no reservation, no credential)
        self.assertEqual(self.req("POST", "/api/session/start", {"candidate": "fake"},
                                  extra={"Origin": "http://evil.example"})[0], 403)


@unittest.skipUnless(shutil.which("node") and not os.environ.get("NO_BROWSER_SMOKE"), "node not available")
class BrowserSmokeTest(unittest.TestCase):
    def test_page_with_fake_mic_and_offline_adapter(self):
        script = pathlib.Path(__file__).with_name("browser_lab_smoke.cjs")
        proc = subprocess.run(["node", str(script)], cwd=pathlib.Path(__file__).resolve().parents[2],
                              capture_output=True, text=True, encoding="utf-8", timeout=120)
        if "Cannot find module 'playwright'" in proc.stderr:
            self.skipTest("playwright not installed for node")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)


if __name__ == "__main__":
    unittest.main()
