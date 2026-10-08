"""Admin additions: the cost ledger with reconciliation, the simulated vendor interruption in the demo call,
and voices that only the vendor's agent settings decide."""
import json
import pathlib
import tempfile
import unittest

from prototype.admin.server import AdminApp
from prototype.reception.store import Store

USER = {"user": "admin", "role": "admin", "csrf": "x"}
SATURDAY = "2026-10-10T10:00"   # outside the provisional staff hours: the call goes to the AI


class AdminLedgerAndDemoTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = pathlib.Path(self.tmp.name)
        self.app = AdminApp(Store(d / "app.db"), env={}, ledger_path=d / "ledger.jsonl", results_dir=d / "results")

    def tearDown(self):
        self.app.store.close()
        self.tmp.cleanup()

    def post(self, path, body):
        return self.app.post(path, body, USER)

    def test_ledger_lists_open_reservations_and_reconciles_once(self):
        led = self.app.lab.ledger("google_lab")
        rid = led.reserve("browser_session:gemini-3.8-live", "browser_lab", 0.24, "s1", audio_seconds=240)
        led.record("browser_session", "browser_lab", 0.06, "s1", audio_seconds=60, reservation=rid, estimated=True)
        got = self.app.get("/api/ledger", {}, USER)
        self.assertEqual([r["rid"] for r in got["ledgers"]["google_lab"]["open"]], [rid])
        self.assertIn("上限ではありません", got["note"])
        with self.assertRaises(ValueError):                      # a note is required
            self.post("/api/ledger/reconcile", {"vendor": "google_lab", "rid": rid, "actual_usd": "0.05"})
        with self.assertRaises(ValueError):
            self.post("/api/ledger/reconcile", {"vendor": "google_lab", "rid": rid, "actual_usd": "abc", "note": "n"})
        with self.assertRaises(ValueError):                      # unknown reservation
            self.post("/api/ledger/reconcile", {"vendor": "google_lab", "rid": "nope", "actual_usd": 0.05, "note": "n"})
        res = self.post("/api/ledger/reconcile", {"vendor": "google_lab", "rid": rid, "actual_usd": "0.05",
                                                  "note": "10/09 利用画面"})
        self.assertEqual(res["result"], "adjusted")
        t = res["ledgers"]["google_lab"]
        self.assertEqual(t["open_reservations"], 0)
        self.assertAlmostEqual(t["est_cost_usd"], 0.05)          # the console amount, not estimate + amount
        with self.assertRaises(ValueError):                      # a different amount later is refused
            self.post("/api/ledger/reconcile", {"vendor": "google_lab", "rid": rid, "actual_usd": 0.07, "note": "n"})
        audit = self.app.store.q("SELECT * FROM audit_log WHERE action = 'ledger_reconcile'")
        self.assertEqual(len(audit), 1)

    def test_demo_interruption_invalidates_an_unanswered_readback_only(self):
        call = self.post("/api/demo/start", {"dialed": "0120-77-3408", "at": SATURDAY})["call"]
        self.assertEqual(call["route"], "ai")
        cid = call["id"]
        r = self.post("/api/demo/say", {"call_id": cid, "text": "折り返しは090-1234-5678です。"})
        self.assertIn("復唱", "".join(r["result"]["say"]))
        out = self.post("/api/demo/interrupted", {"call_id": cid})
        self.assertEqual(out["invalidated"], ["callback_number"])
        r = self.post("/api/demo/say", {"call_id": cid, "text": "はい、合っています。"})
        said = "".join(r["result"]["say"])
        self.assertIn("もう一度復唱", said)                        # not confirmed: the agent reads it again
        number = next(f for f in r["call"]["fields"] if f["name"] == "callback_number")
        self.assertEqual(number["status"], "awaiting_confirmation")
        r = self.post("/api/demo/say", {"call_id": cid, "text": "はい、合っています。"})
        number = next(f for f in r["call"]["fields"] if f["name"] == "callback_number")
        self.assertEqual(number["status"], "confirmed_by_caller")
        # an interruption notice after the reply invalidates nothing
        self.assertEqual(self.post("/api/demo/interrupted", {"call_id": cid})["invalidated"], [])
        kinds = [e["kind"] for e in r["call"]["events"]]
        self.assertIn("vendor_interrupted", kinds)

    def test_voice_screen_knows_which_voices_reach_the_vendor(self):
        cat = {c["candidate"]: c for c in self.app.get("/api/voices", {}, USER)["catalog"]}
        self.assertTrue(cat["gpt-live-1"]["voice_applies"])
        self.assertTrue(cat["gemini-3.8-live"]["voice_applies"])
        self.assertFalse(cat["cartesia-agents"]["voice_applies"])
        self.assertFalse(cat["elevenagents"]["voice_applies"])   # no ELEVENLABS_OVERRIDES on this machine
        self.assertTrue(cat["gpt-realtime-2.1"]["hold"])
        self.assertIsNone(cat["gpt-live-1"]["hold"])
        _, cfg, _ = self.app.service.config()
        cfg["voices"].append({"id": "c1", "candidate": "cartesia-agents", "voice": "abc", "label": "記録"})
        with self.assertRaises(ValueError):
            self.post("/api/config", {"config": {**cfg, "active_voice": "c1"}})
        self.assertIn("version", self.post("/api/config", {"config": cfg, "note": "記録だけ"}))

    def test_demo_shows_consent_and_recording_events(self):
        cid = self.post("/api/demo/start", {"dialed": "0120-77-3408", "at": SATURDAY})["call"]["id"]
        r = self.post("/api/demo/say", {"call_id": cid, "text": "録音はしないでください"})
        self.assertTrue(r["result"]["recording_stopped"])
        kinds = [e["kind"] for e in r["call"]["events"]]
        for k in ("consent", "recording_stopped", "recording_deleted"):
            self.assertIn(k, kinds)
        self.assertTrue(r["call"]["ai_allowed"])                 # the AI conversation continues
        consent = next(e for e in r["call"]["events"] if e["kind"] == "consent")
        self.assertEqual(consent["data"]["source"], "speech")
        self.assertEqual(json.loads(json.dumps(r["call"]["consent"]))["recording"], "refused")


if __name__ == "__main__":
    unittest.main()
