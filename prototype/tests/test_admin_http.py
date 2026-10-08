"""Admin app over HTTP: login, CSRF and origin checks, persistence across a restart, no key exposure."""
import http.client
import json
import pathlib
import tempfile
import threading
import unittest

from prototype.admin.server import AdminApp, serve
from prototype.reception.store import Store

FAKE_KEY = "sk-test-FAKE-LONG-LIVED-KEY-1111111111"


class AdminHttpTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = pathlib.Path(self.tmp.name) / "app.db"
        self.bodies: list[str] = []
        self.start()

    def start(self):
        self.app = AdminApp(Store(self.db), env={"OPENAI_API_KEY": FAKE_KEY},
                            ledger_path=pathlib.Path(self.tmp.name) / "ledger.jsonl",
                            results_dir=pathlib.Path(self.tmp.name) / "results")
        self.app.auth.ensure_admin("pw-for-tests-1")
        self.httpd = serve(self.app, "127.0.0.1", 0, log_requests=False)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.app.store.close()

    def tearDown(self):
        self.stop()
        self.tmp.cleanup()

    def req(self, method, path, body=None, cookie=None, csrf=None, host=None, origin=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        headers = {"Host": host or f"127.0.0.1:{self.port}"}
        if body is not None:
            headers["Content-Type"] = "application/json"
        if cookie:
            headers["Cookie"] = cookie
        if csrf:
            headers["X-CSRF-Token"] = csrf
        if origin:
            headers["Origin"] = origin
        conn.request(method, path, json.dumps(body) if body is not None else None, headers)
        r = conn.getresponse()
        data = r.read().decode()
        self.bodies.append(data)
        return r.status, data, r

    def login(self, password="pw-for-tests-1"):
        status, data, r = self.req("POST", "/api/login", {"username": "admin", "password": password})
        if status != 200:
            return status, None, None
        cookie = r.getheader("Set-Cookie").split(";")[0]
        self.assertIn("HttpOnly", r.getheader("Set-Cookie"))
        self.assertIn("SameSite=Strict", r.getheader("Set-Cookie"))
        return status, cookie, json.loads(data)["csrf"]

    def test_nothing_without_login(self):
        for path in ("/api/config", "/api/calls", "/api/faqs", "/api/targets", "/api/audit", "/lab/api/candidates"):
            self.assertIn(self.req("GET", path)[0], (401, 302), path)
        status, _, r = self.req("GET", "/")
        self.assertEqual((status, r.getheader("Location")), (302, "/login"))
        self.assertEqual(self.req("POST", "/api/config", {"config": {}})[0], 401)
        self.assertEqual(self.req("POST", "/api/demo/start", {})[0], 401)
        self.assertEqual(self.req("GET", "/login")[0], 200)

    def test_wrong_password_and_lockout(self):
        for _ in range(5):
            self.assertEqual(self.login("wrong")[0], 401)
        self.assertEqual(self.login()[0], 429)   # locked for a while even with the right password

    def test_csrf_origin_host_and_static_traversal(self):
        _, cookie, csrf = self.login()
        cfg = json.loads(self.req("GET", "/api/config", cookie=cookie)[1])["config"]
        self.assertEqual(self.req("POST", "/api/config", {"config": cfg}, cookie=cookie)[0], 403)
        self.assertEqual(self.req("POST", "/api/config", {"config": cfg}, cookie=cookie, csrf=csrf,
                                  origin="http://evil.example")[0], 403)
        self.assertEqual(self.req("POST", "/api/config", {"config": cfg}, cookie=cookie, csrf=csrf,
                                  origin=f"http://127.0.0.1:{self.port}")[0], 200)
        self.assertEqual(self.req("GET", "/api/config", cookie=cookie, host="evil.example")[0], 403)
        self.assertEqual(self.req("GET", "/static/../server.py")[0], 404)
        status, _, r = self.req("GET", "/", cookie=cookie)
        self.assertEqual(status, 200)
        self.assertIn("frame-ancestors 'none'", r.getheader("Content-Security-Policy"))

    def test_settings_and_records_survive_a_restart(self):
        _, cookie, csrf = self.login()
        cfg = json.loads(self.req("GET", "/api/config", cookie=cookie)[1])["config"]
        cfg["ai_failure_action"] = "dtmf"
        self.assertEqual(self.req("POST", "/api/config", {"config": cfg, "note": "t"}, cookie=cookie, csrf=csrf)[0], 200)
        call = json.loads(self.req("POST", "/api/demo/start", {"at": "2026-10-05T10:00"}, cookie=cookie, csrf=csrf)[1])["call"]
        self.assertEqual(call["route"], "ai")   # every call that reaches the app goes to the AI, at any hour
        self.req("POST", "/api/demo/say", {"call_id": call["id"], "text": "折り返しは090-1234-5678です。"}, cookie=cookie, csrf=csrf)
        self.req("POST", "/api/demo/end", {"call_id": call["id"]}, cookie=cookie, csrf=csrf)
        self.stop()
        self.start()
        self.assertEqual(self.req("GET", "/api/config", cookie=cookie)[0], 200)   # the session lives in the database
        got = json.loads(self.req("GET", "/api/config", cookie=cookie)[1])
        self.assertEqual(got["config"]["ai_failure_action"], "dtmf")
        view = json.loads(self.req("GET", f"/api/calls/{call['id']}", cookie=cookie)[1])
        self.assertEqual(next(f for f in view["fields"] if f["name"] == "callback_number")["value"], "09012345678")
        self.assertEqual(len(view["notifications"]), 2)

    def test_key_never_appears(self):
        _, cookie, csrf = self.login()
        for path in ("/api/voices", "/api/status", "/lab/api/candidates", "/api/config", "/api/targets"):
            self.assertEqual(self.req("GET", path, cookie=cookie)[0], 200)
        cands = json.loads(self.bodies[-3])["candidates"]
        self.assertTrue(next(c for c in cands if c["id"] == "gpt-live-1")["ready"])
        self.assertNotIn("gpt-realtime-2.1", [c["id"] for c in cands])                      # held: not offered
        self.assertFalse(any(FAKE_KEY in b for b in self.bodies))
        dump = "\n".join(str(r) for r in self.app.store.q("SELECT * FROM audit_log"))
        self.assertNotIn(FAKE_KEY, dump)


@unittest.skipUnless(__import__("shutil").which("node") and not __import__("os").environ.get("NO_BROWSER_SMOKE"),
                     "node not available")
class AdminBrowserTest(unittest.TestCase):
    """Operates the screens in Chromium at PC and smartphone widths (prototype/tests/admin_smoke.cjs)."""

    def test_screens_end_to_end(self):
        import subprocess
        script = pathlib.Path(__file__).with_name("admin_smoke.cjs")
        proc = subprocess.run(["node", str(script)], cwd=pathlib.Path(__file__).resolve().parents[2],
                              capture_output=True, text=True, encoding="utf-8", timeout=400)
        if "Cannot find module 'playwright'" in proc.stderr:
            self.skipTest("playwright not installed for node")
        self.assertEqual(proc.returncode, 0, proc.stdout[-4000:] + proc.stderr[-4000:])


if __name__ == "__main__":
    unittest.main()
