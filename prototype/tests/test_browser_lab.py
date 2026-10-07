import http.client
import json
import os
import pathlib
import shutil
import subprocess
import tempfile
import threading
import unittest

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
        self.assertEqual(t["open_reservations"], 0)
        self.assertAlmostEqual(t["est_cost_usd"], 0.12)

    def test_mint_failure_releases_the_reservation(self):
        lab = self.lab({"CARTESIA_API_KEY": "k", "CARTESIA_AGENT_ID": "agent"},
                       {"access-token": vendors.VendorError("HTTP 401")})
        with self.assertRaises(vendors.VendorError):
            lab.start("cartesia-agents")
        t = lab.ledger("cartesia_lab").totals()
        self.assertEqual((t["open_reservations"], t["est_cost_usd"]), (0, 0.0))

    def test_unfinished_session_keeps_its_full_reservation(self):
        lab = self.lab({"OPENAI_API_KEY": "k"}, {"client_secrets": {"value": "ek_123"}})
        lab.start("gpt-realtime-2.1")
        self.assertAlmostEqual(lab.ledger("openai_lab").totals()["est_cost_usd"], config.MAX_SESSION_MIN * 0.30)

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
        lab = self.lab({"ELEVENLABS_API_KEY": "xi", "ELEVENLABS_AGENT_ID": "ag", "OPENAI_API_KEY": "sk"},
                       {"get-signed-url": {"signed_url": "wss://signed"}, "client_secrets": {"value": "ek_1"}})
        e = lab.start("elevenagents")["credentials"]
        self.assertEqual(e["signed_url"], "wss://signed")
        self.assertEqual(lab.http.calls[-1]["headers"]["xi-api-key"], "xi")
        r = lab.start("gpt-realtime-2.1")["credentials"]
        self.assertEqual(r["ephemeral_key"], "ek_1")
        session = lab.http.calls[-1]["body"]["session"]
        self.assertEqual(session["max_output_tokens"], 1200)
        self.assertNotIn("sk", json.dumps(r))

    def test_tools_use_the_confirmation_guard_and_results_are_saved(self):
        lab = self.lab({})
        s = lab.start("fake")
        sid = s["session_id"]
        lab.tool(sid, "save_field", {"field": "callback_number", "value": "090-1234-5678"})
        lab.tool(sid, "request_readback", {"field": "callback_number"})
        r = lab.tool(sid, "confirm_field", {"field": "callback_number", "value": "09012345678"},
                     caller_utterance="はい、違います", delay_ms=10)
        self.assertFalse(r["ok"])                        # 「はい、違います」 is not a confirmation
        path = lab.save_results({"candidate": "fake", "session_id": sid, "judgments": {}})
        saved = json.loads(pathlib.Path(path).read_text())
        self.assertEqual(saved["path"], "browser_lab")
        self.assertEqual(len(saved["server_tool_calls"]), 3)


class LabBusinessLogicTest(unittest.TestCase):
    """The lab's tool calls go to the common reception service; vendor bodies follow the call's snapshot."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = pathlib.Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def lab(self, env, responses=None):
        return Lab(env=env, http=FakeHttp(responses or {}), ledger_path=self.dir / "ledger.jsonl",
                   results_dir=self.dir / "results")

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
        lab = self.lab({"CARTESIA_API_KEY": "k", "CARTESIA_AGENT_ID": "agent_1"}, {"access-token": {"token": "tok"}})
        creds = lab.start("cartesia-agents")["credentials"]
        self.assertEqual(creds["ws_url"], "wss://api.cartesia.ai/v1/agents/websocket/agent_1")
        self.assertEqual(creds["start"], {"type": "session_create", "audio": {"input_format": "pcm_16000"}})

    def test_tool_calls_use_the_snapshot_and_the_guard_and_refusal_is_final(self):
        lab = self.lab({})
        s = lab.start("fake")
        sid = s["session_id"]
        self.assertTrue(lab.tool(sid, "lookup_faq", {"question": "点検は無料ですか"})["found"])
        lab.tool(sid, "save_field", {"field": "callback_number", "value": "090-1234-5678"})
        lab.tool(sid, "request_readback", {"field": "callback_number"})
        self.assertFalse(lab.tool(sid, "confirm_field", {"field": "callback_number", "value": "09012345678"},
                                  caller_utterance="はい、違います")["ok"])
        res = lab.consent(sid, "ai_refused")
        self.assertTrue(res["stop_ai"])
        self.assertEqual(lab.tool(sid, "save_field", {"field": "request", "value": "x"})["reason"], "ai_refused")
        lab.end(sid, 30)
        saved = json.loads(pathlib.Path(lab.save_results({"candidate": "fake", "session_id": sid})).read_text())
        self.assertTrue(saved["comparison"]["business_logic_status"].startswith("評価対象"))
        self.assertEqual(saved["server_fields"]["callback_number"]["status"], "awaiting_confirmation")
        view = lab.service.call_view(s["call_id"])
        self.assertEqual((view["source"], view["notifications"]), ("browser_lab", []))   # the lab notifies no one

    def test_no_tool_calls_means_business_logic_not_evaluated(self):
        lab = self.lab({})
        sid = lab.start("fake")["session_id"]
        lab.end(sid, 5)
        saved = json.loads(pathlib.Path(lab.save_results({"candidate": "fake", "session_id": sid})).read_text())
        self.assertTrue(saved["comparison"]["business_logic_status"].startswith("未評価"))

    def test_connection_stage_allows_only_the_first_short_sessions(self):
        from prototype.engines.budget import BudgetExceeded
        lab = self.lab({"GEMINI_API_KEY": "k"}, {"auth_tokens": {"name": "auth_tokens/abc"}})
        self.assertEqual(lab.stage, "connection")
        for _ in range(2):
            lab.end(lab.start("gemini-3.8-live")["session_id"], 60)
        with self.assertRaises(BudgetExceeded):          # the detailed comparison needs LAB_STAGE=detailed
            lab.start("gemini-3.8-live")
        detailed = self.lab({"GEMINI_API_KEY": "k", "LAB_STAGE": "detailed"}, {"auth_tokens": {"name": "auth_tokens/abc"}})
        self.assertIn("session_id", detailed.start("gemini-3.8-live"))   # same ledger, higher cumulative cap

    def test_billing_never_below_the_time_the_server_saw(self):
        lab = self.lab({"GEMINI_API_KEY": "k"}, {"auth_tokens": {"name": "auth_tokens/abc"}})
        sid = lab.start("gemini-3.8-live")["session_id"]
        lab.sessions[sid]["started"] -= 125          # the session has been open for about 2 minutes
        res = lab.end(sid, 1)                       # the page reports 1 s
        self.assertEqual(res["billed_s"], 180)      # rounded up to the 60 s increment


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
                              capture_output=True, text=True, timeout=120)
        if "Cannot find module 'playwright'" in proc.stderr:
            self.skipTest("playwright not installed for node")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)


if __name__ == "__main__":
    unittest.main()
