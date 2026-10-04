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

    def req(self, method, path, body=None, host=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        headers = {"Content-Type": "application/json"}
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
