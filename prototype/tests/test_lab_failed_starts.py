"""GPT-Live starts that created nothing at OpenAI (2026-10-08: two HTTP 400s used up both connection checks)."""
import json
import pathlib
import tempfile
import unittest

from prototype.admin.server import AdminApp
from prototype.browser_lab import config, vendors
from prototype.browser_lab.server import Lab, SessionLimit
from prototype.reception.service import ReceptionService
from prototype.reception.store import Store

LIVE_OK = {"transport": {"sdp": "v=0 answer"}, "session": {"id": "ls_1"}}


class Http:
    """Answers /v1/live/sessions with the queued results (a dict, or a VendorError to raise)."""

    def __init__(self, *results):
        self.results, self.calls = list(results), []

    def __call__(self, method, url, headers, body=None):
        self.calls.append({"url": url, "body": body})
        r = self.results.pop(0)
        if isinstance(r, Exception):
            raise r
        return 201, r


def http_400():
    return vendors.VendorError('https://api.openai.com/v1/live/sessions: HTTP 400 {"error": {"message": "bad"}}', 400)


class FailedStartTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = pathlib.Path(self.tmp.name)
        self.svc = ReceptionService(Store(self.dir / "app.db"))

    def tearDown(self):
        self.svc.store.close()
        self.tmp.cleanup()

    def lab(self, *results):
        return Lab(env={"OPENAI_API_KEY": "k"}, http=Http(*results), ledger_path=self.dir / "l.jsonl",
                   results_dir=self.dir / "r", service=self.svc, live_close=lambda *a: {"closed": True})

    def row(self, lab, sid):
        return lab.store.q1("SELECT * FROM lab_sessions WHERE id = ?", (sid,))

    def test_http_400_is_not_a_started_session_and_releases_the_reservation(self):
        lab = self.lab(http_400())
        sid = lab.start("gpt-live-1")["session_id"]
        with self.assertRaises(vendors.VendorError):
            lab.sdp(sid, "v=0 offer")
        row = self.row(lab, sid)
        self.assertEqual(row["status"], "vendor_rejected")
        self.assertIn("HTTP 400", row["end_reason"])                 # the vendor's error text is kept
        self.assertIn("bad", row["end_reason"])
        self.assertEqual(lab.end(sid, 3, "開始の失敗")["already"], "vendor_rejected")
        c = lab.counts("gpt-live-1")
        self.assertEqual((c["sessions"], c["mint_failures"]), (0, 1))
        t = lab.ledger("openai_lab").totals()
        self.assertEqual((t["open_reservations"], t["requests"], t["est_cost_usd"]), (0, 0, 0.0))

    def test_unknown_outcome_stays_counted(self):
        lab = self.lab(vendors.VendorError("https://api.openai.com/v1/live/sessions: URLError"),
                       vendors.VendorError("https://api.openai.com/v1/live/sessions: HTTP 503 busy", 503))
        for _ in range(2):
            sid = lab.start("gpt-live-1")["session_id"]
            with self.assertRaises(vendors.VendorError):
                lab.sdp(sid, "v=0 offer")
            lab.end(sid, 3, "開始の失敗")
            self.assertEqual(self.row(lab, sid)["status"], "connect_failed")
            self.assertIn("結果が不明", self.row(lab, sid)["end_reason"])
        self.assertEqual(lab.counts("gpt-live-1")["sessions"], 2)
        self.assertEqual(lab.ledger("openai_lab").totals()["open_reservations"], 2)   # may have been billed

    def test_certificate_failure_on_this_pc_is_not_counted(self):
        """2026-10-08 on a Mac: python.org Python without "Install Certificates.command" -> URLError."""
        import ssl
        import urllib.error
        cert = urllib.error.URLError(ssl.SSLCertVerificationError(1, "[SSL: CERTIFICATE_VERIFY_FAILED] certificate "
                                                                     "verify failed: unable to get local issuer"))
        err = vendors.network_error("https://api.openai.com/v1/live/sessions", cert)
        self.assertTrue(err.unreached and err.certificate)
        self.assertIn("CERTIFICATE_VERIFY_FAILED", str(err))              # the reason is shown, not just "URLError"
        lab = self.lab(err)
        sid = lab.start("gpt-live-1")["session_id"]
        with self.assertRaises(vendors.VendorError) as shown:
            lab.sdp(sid, "v=0 offer")
        self.assertIn("Install Certificates.command", str(shown.exception))   # what the page shows
        row = self.row(lab, sid)
        self.assertEqual(row["status"], "not_sent")
        self.assertIn("Install Certificates.command", row["end_reason"])
        self.assertEqual(lab.end(sid, 3, "開始の失敗")["already"], "not_sent")
        c = lab.counts("gpt-live-1")
        self.assertEqual((c["sessions"], c["mint_failures"]), (0, 0))
        t = lab.ledger("openai_lab").totals()
        self.assertEqual((t["open_reservations"], t["requests"], t["est_cost_usd"]), (0, 0, 0.0))

    def test_network_error_kinds(self):
        import socket
        import urllib.error
        url = "https://api.openai.com/v1/live/sessions"
        name = vendors.network_error(url, urllib.error.URLError(socket.gaierror(8, "nodename nor servname")))
        self.assertTrue(name.unreached and not name.certificate)
        self.assertIn("nodename", str(name))
        self.assertTrue(vendors.network_error(url, urllib.error.URLError(ConnectionRefusedError(61, "refused")))
                        .unreached)
        for maybe_sent in (TimeoutError("timed out"), ConnectionResetError(54, "reset"),
                           urllib.error.URLError(TimeoutError("timed out"))):
            e = vendors.network_error(url, maybe_sent)
            self.assertFalse(e.unreached, maybe_sent)                    # may have reached OpenAI: still counted
            self.assertIsNone(e.status)
        self.assertEqual(str(vendors.network_error(url, TimeoutError())), f"{url}: TimeoutError")

    def test_failure_before_anything_was_sent_is_not_counted(self):
        lab = self.lab()
        sid = lab.start("gpt-live-1")["session_id"]
        out = lab.end(sid, 1, "開始の失敗")                              # e.g. microphone or ICE error on the page
        self.assertTrue(out["not_sent"])
        self.assertEqual(self.row(lab, sid)["status"], "not_sent")
        self.assertEqual(lab.counts("gpt-live-1")["sessions"], 0)
        self.assertEqual(lab.http.calls, [])
        self.assertEqual(lab.ledger("openai_lab").totals()["open_reservations"], 0)

    def test_refusals_do_not_block_the_next_start(self):
        lab = self.lab(http_400(), http_400(), LIVE_OK)
        for _ in range(2):
            sid = lab.start("gpt-live-1")["session_id"]
            with self.assertRaises(vendors.VendorError):
                lab.sdp(sid, "v=0 offer")
        self.assertEqual(lab.counts("gpt-live-1")["mint_failures"], 2)   # recorded, no limit (2026-10-08)
        from unittest import mock
        from prototype.browser_lab import server as labsrv
        with mock.patch.object(labsrv, "MAX_MINT_FAILURES", 2), self.assertRaises(SessionLimit):
            lab.start("gpt-live-1")                                    # the gate still works if a limit is set
        sid = lab.start("gpt-live-1")["session_id"]
        self.assertEqual(lab.sdp(sid, "v=0 offer")["sdp"], "v=0 answer")
        self.assertEqual(lab.counts("gpt-live-1")["sessions"], 1)

    def test_the_users_two_failed_starts_can_be_released_after_checking_usage(self):
        # the state on the user's PC: two GPT-Live starts recorded as connect_failed by the earlier version
        lab = self.lab(LIVE_OK)
        ids = []
        for _ in range(2):
            sid = lab.start("gpt-live-1")["session_id"]
            with lab.lock:
                lab.sessions[sid]["sdp_done"] = True    # the earlier version had sent the offer
            lab.end(sid, 3, "開始の失敗")
            ids.append(sid)
        from unittest import mock
        with mock.patch.dict(config.SESSIONS["connection"], {"gpt-live-1": 2}), self.assertRaises(SessionLimit):
            lab.start("gpt-live-1")                                  # under the limit of that time
        with self.assertRaises(ValueError):                         # a note on what was checked is required
            lab.release_session(ids[0], "")
        for sid in ids:
            lab.release_session(sid, "10/08 OpenAIの利用画面で0件・$0を確認", "admin")
            self.assertEqual(self.row(lab, sid)["status"], "released_by_staff")
        self.assertEqual(lab.counts("gpt-live-1")["sessions"], 0)
        t = lab.ledger("openai_lab").totals()
        self.assertEqual((t["open_reservations"], t["requests"], t["est_cost_usd"]), (0, 0, 0.0))   # estimates cancelled
        sid = lab.start("gpt-live-1")["session_id"]                  # the ledger cap allows it again
        lab.sdp(sid, "v=0 offer")
        with self.assertRaises(ValueError):                         # a session that exists cannot be released
            lab.release_session(sid, "x")
        audit = lab.store.q("SELECT * FROM audit_log WHERE action = 'lab_session_release'")
        self.assertEqual(len(audit), 2)

    def test_gpt_live_tools_are_strict_and_the_offer_must_not_be_empty(self):
        lab = self.lab(LIVE_OK)
        sid = lab.start("gpt-live-1")["session_id"]
        with self.assertRaises(ValueError):
            lab.sdp(sid, "  ")
        self.assertEqual(lab.http.calls, [])
        lab.sdp(sid, "v=0 offer")
        tools = lab.http.calls[-1]["body"]["session"]["delegation"]["responses"]["tools"]
        for t in tools:
            p = t["parameters"]
            self.assertTrue(t["strict"])
            self.assertIs(p["additionalProperties"], False)
            self.assertEqual(sorted(p["required"]), sorted(p["properties"]))
        self.assertNotIn("audio", {k for k in lab.http.calls[-1]["body"]["session"]["audio"]["output"]} - {"voice"})


class AdminReleaseTest(unittest.TestCase):
    def test_admin_screen_lists_releasable_rows_and_releases_them(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        d = pathlib.Path(tmp.name)
        app = AdminApp(Store(d / "a.db"), env={"OPENAI_API_KEY": "k"}, ledger_path=d / "l.jsonl", results_dir=d / "r")
        self.addCleanup(app.store.close)
        user = {"user": "admin", "role": "admin", "csrf": "x"}
        sid = app.lab.start("gpt-live-1")["session_id"]
        with app.lab.lock:
            app.lab.sessions[sid]["sdp_done"] = True
        app.lab.end(sid, 2, "開始の失敗")
        got = app.get("/api/ledger", {}, user)
        row = next(r for r in got["sessions"] if r["id"] == sid)
        self.assertTrue(row["releasable"])
        self.assertIn("数える", row["status_label"])
        self.assertEqual(got["counts"]["gpt-live-1"]["sessions"], 1)
        out = app.post("/api/lab/release", {"session_id": sid, "note": "利用画面で0件"}, user)
        self.assertEqual(out["counts"]["sessions"], 0)
        self.assertEqual(app.post("/api/lab/reset_failures", {"candidate": "gpt-live-1"}, user)["cleared"], 0)
        self.assertNotIn('"k"', json.dumps(got))                       # no key value in what the page receives


if __name__ == "__main__":
    unittest.main()

