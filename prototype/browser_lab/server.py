"""Local server for the browser conversation lab (standard library only).

python3 -m prototype.browser_lab [--port 8765] [--db PATH]   then open http://127.0.0.1:8765
The admin app (python3 -m prototype.admin) serves the same lab at /lab/ behind its login.

Runs on the tester's own PC (the microphone is in their browser). Listens on 127.0.0.1 only and rejects
other Host headers and cross-site POSTs. Keys come from environment variables and are never sent to the page.

Business logic: every session is a call in the common ReceptionService (source "browser_lab"). Its settings
snapshot (FAQ, voice, storage rules) is taken at session start; the vendor gets instructions built from it;
every tool call goes to ReceptionService.tool (the same confirmation guard and records as the demo and the
future phone path).

Spend control per session: before credentials are minted, the ledger reserves
MAX_SESSION_MIN x the candidate's upper per-minute price. When the page ends the session, the measured
length (rounded up to the vendor's billing increment) at the upper price is recorded as an ESTIMATE and
the reservation is closed; reconcile with the vendor console afterwards (python3 -m prototype.engines.budget).
A session that never ends keeps its full reservation.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import mimetypes
import os
import pathlib
import threading
import time
import uuid
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from ..engines.budget import DEFAULT_LEDGER, BudgetExceeded, UsageLedger
from ..reception.service import ReceptionService
from ..reception.store import DEFAULT_DB, Store
from . import vendors
from .config import (CANDIDATES, LAB_LIMITS, LAB_LIMITS_BY_STAGE, LAB_RULES, LAB_STAGES, LOGIC_PATHS,
                     MAX_SESSION_MIN, public_candidates)

STATIC = pathlib.Path(__file__).resolve().parent / "static"
RESULTS = pathlib.Path(__file__).resolve().parents[1] / "results" / "browser-lab"
PATH_TAG = "browser_lab"


class Lab:
    """State shared by request handlers: sessions, ledgers, injectable env and HTTP for tests."""

    def __init__(self, env: dict | None = None, http=vendors.http_json, ledger_path: pathlib.Path = DEFAULT_LEDGER,
                 results_dir: pathlib.Path = RESULTS, service: ReceptionService | None = None) -> None:
        self.env = dict(os.environ if env is None else env)
        self.http, self.ledger_path, self.results_dir = http, pathlib.Path(ledger_path), pathlib.Path(results_dir)
        self.service = service or ReceptionService(Store(":memory:"), env=self.env)
        self.stage = self.env.get("LAB_STAGE", "connection")
        if self.stage not in LAB_LIMITS_BY_STAGE:
            raise ValueError(f"LAB_STAGE must be one of {', '.join(LAB_LIMITS_BY_STAGE)}")
        self.sessions: dict[str, dict] = {}
        self.ended: dict[str, dict] = {}   # kept so the saved result can include the server-side record
        self.lock = threading.Lock()

    def ledger(self, vendor: str) -> UsageLedger:
        return UsageLedger(vendor, LAB_LIMITS_BY_STAGE[self.stage][vendor], self.ledger_path)

    # --- sessions ------------------------------------------------------------------------

    def start(self, candidate: str) -> dict:
        cfg = CANDIDATES.get(candidate)
        if cfg is None:
            raise ValueError(f"unknown candidate {candidate}")
        missing = [k for k in cfg["env"] if not self.env.get(k)]
        if missing:
            raise ValueError(f"missing environment variables: {', '.join(missing)} (nothing was sent)")
        sid = uuid.uuid4().hex[:12]
        call = self.service.start_call(dialed="browser-lab", source="browser_lab", force_ai=True)
        instructions = self.service.instructions(call["id"], preamble=LAB_RULES)
        voice = call["voice"]["voice"] if call["voice"]["candidate"] == candidate else cfg.get("voice")
        rid = None
        if cfg["vendor"]:
            est = MAX_SESSION_MIN * cfg["upper_usd_per_min"]
            rid = self.ledger(cfg["vendor"]).reserve(f"browser_session:{candidate}", PATH_TAG, est, sid,
                                                     audio_seconds=MAX_SESSION_MIN * 60)
        try:
            creds = vendors.mint(cfg, self.env, self.http, instructions=instructions, voice=voice)
        except Exception:
            if rid:  # minting a token is not billed: release the reservation with a zero record
                led = self.ledger(cfg["vendor"])
                led.record("mint_failed", PATH_TAG, 0.0, sid, reservation=rid)
                led.close(rid, note="credential minting failed; no voice session started")
            self.service.end_call(call["id"], "接続情報の発行に失敗")
            raise
        with self.lock:
            self.sessions[sid] = {"candidate": candidate, "cfg": cfg, "rid": rid, "started": time.time(),
                                  "call_id": call["id"], "instructions": instructions, "voice": voice,
                                  "sdp_done": False}
        return {"session_id": sid, "candidate": candidate, "adapter": cfg["adapter"], "verified": cfg["verified"],
                "logic_path": LOGIC_PATHS[cfg["logic_path"]], "call_id": call["id"],
                "config_version": call["config_version"], "faq_codes": call["faq_codes"], "voice": voice,
                "max_session_min": MAX_SESSION_MIN, "credentials": creds}

    def sdp(self, sid: str, offer: str) -> dict:
        s = self._session(sid)
        if s["cfg"]["adapter"] != "gpt-live":
            raise ValueError("SDP exchange is only for GPT-Live")
        with self.lock:   # one vendor session per reservation
            if s["sdp_done"]:
                raise ValueError("this session already has a connection")
            s["sdp_done"] = True
        return vendors.exchange_live_sdp(s["cfg"], self.env, offer, self.http, instructions=s["instructions"],
                                         voice=s["voice"])

    def end(self, sid: str, duration_s: float, reason: str = "") -> dict:
        with self.lock:
            s = self.sessions.pop(sid, None)
            if s is not None:
                self.ended[sid] = s
        if s is None:
            raise ValueError(f"unknown session {sid}")
        self.service.end_call(s["call_id"], reason or "試験者が終了")
        cfg = s["cfg"]
        if not s["rid"]:
            return {"estimated_usd": 0.0}
        # never bill less than the time the server saw the session open (the page's number is not trusted alone)
        inc = cfg["increment_s"]
        seen_s = time.time() - s["started"]
        billed_s = math.ceil(max(0.0, duration_s, seen_s) / inc) * inc
        est = billed_s / 60 * cfg["upper_usd_per_min"]
        led = self.ledger(cfg["vendor"])
        led.record("browser_session", PATH_TAG, est, sid, audio_seconds=billed_s, reservation=s["rid"],
                   estimated=True, basis=f"{billed_s}s x upper ${cfg['upper_usd_per_min']}/min; reconcile with the "
                                         "vendor console", reason=reason, candidate=s["candidate"])
        led.close(s["rid"], note="estimated from measured duration")
        return {"estimated_usd": round(est, 4), "billed_s": billed_s, "ledger_totals": led.totals()}

    def tool(self, sid: str, name: str, args: dict, caller_utterance: str = "", delay_ms: int = 0) -> dict:
        s = self._session(sid)
        if delay_ms:
            time.sleep(min(delay_ms, 15000) / 1000)
        return self.service.tool(s["call_id"], name, args or {}, caller_utterance)

    def consent(self, sid: str, kind: str) -> dict:
        """Tester-simulated refusal etc. The page must close the vendor connection when stop_ai is true."""
        s = self._session(sid)
        if kind not in ("ai_refused", "recording_refused"):
            raise ValueError("kind must be ai_refused or recording_refused")
        res = self.service.consent_event(s["call_id"], kind)
        return {"actions": res["actions"], "stop_ai": not self.service.ai_allowed(s["call_id"])}

    def save_results(self, payload: dict) -> str:
        self.results_dir.mkdir(parents=True, exist_ok=True)
        ts = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        cand = "".join(ch for ch in str(payload.get("candidate", "unknown")) if ch.isalnum() or ch in "-._")[:40]
        path = self.results_dir / f"{ts}-{cand}-{uuid.uuid4().hex[:6]}.json"
        sid = payload.get("session_id")
        with self.lock:
            s = self.sessions.get(sid) or self.ended.get(sid)
        if s is not None:  # the record the server kept is what counts for the business-logic comparison
            view = self.service.call_view(s["call_id"])
            tool_events = [e for e in view["events"] if e["kind"] == "tool"]
            payload["server_call_id"] = s["call_id"]
            payload["server_tool_calls"] = tool_events
            payload["server_fields"] = self.service.fields(s["call_id"])
            payload["server_summary"] = view["summaries"][-1]["text"] if view["summaries"] else None
            comp = payload.setdefault("comparison", {})
            comp["business_logic_status"] = ("評価対象（サーバーが業務処理を受け取った）" if tool_events else
                                             "未評価（この会話では業務処理がサーバーに届かなかった）")
        payload["path"] = PATH_TAG
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2))
        return str(path)

    def _session(self, sid: str) -> dict:
        with self.lock:
            s = self.sessions.get(sid)
        if s is None:
            raise ValueError(f"unknown session {sid}")
        return s

    def ledger_summary(self) -> dict:
        return {v: self.ledger(v).totals() for v in LAB_LIMITS}


# --- request handling shared with the admin app (/lab/...) -------------------------------------------------

def api_get(lab: Lab, path: str, csrf: str = "") -> dict | None:
    path = path.split("?")[0]
    if path == "/api/candidates":
        return {"candidates": public_candidates(lab.env), "stage": lab.stage, "stage_label": LAB_STAGES[lab.stage]}
    if path == "/api/ledger":
        return lab.ledger_summary()
    if path == "/api/csrf":
        return {"csrf": csrf}
    return None


def api_post(lab: Lab, path: str, body: dict) -> tuple[int, dict]:
    """Returns (status, payload). Maps the lab's errors to HTTP statuses."""
    try:
        if path == "/api/session/start":
            return 200, lab.start(body.get("candidate", ""))
        if path == "/api/session/sdp":
            return 200, lab.sdp(body.get("session_id", ""), body.get("sdp", ""))
        if path == "/api/session/end":
            return 200, lab.end(body.get("session_id", ""), float(body.get("duration_s", 0)), body.get("reason", ""))
        if path == "/api/tool":
            return 200, lab.tool(body.get("session_id", ""), body.get("name", ""), body.get("args") or {},
                                 body.get("caller_utterance", ""), int(body.get("delay_ms") or 0))
        if path == "/api/consent":
            return 200, lab.consent(body.get("session_id", ""), body.get("kind", ""))
        if path == "/api/results":
            return 200, {"saved": lab.save_results(body)}
        return HTTPStatus.NOT_FOUND, {"error": "not found"}
    except BudgetExceeded as exc:
        return HTTPStatus.PAYMENT_REQUIRED, {"error": f"budget: {exc}"}
    except (ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
        return HTTPStatus.BAD_REQUEST, {"error": str(exc)}
    except vendors.VendorError as exc:
        return HTTPStatus.BAD_GATEWAY, {"error": str(exc)}


def static_file(rel: str) -> tuple[bytes, str] | None:
    rel = "index.html" if rel in ("", "/") else rel.split("?")[0].lstrip("/")
    target = (STATIC / rel).resolve()
    if not target.is_file() or STATIC not in target.parents:  # no path traversal out of static/
        return None
    ctype = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
    if target.suffix == ".js":
        ctype = "text/javascript"
    return target.read_bytes(), ctype + ("; charset=utf-8" if ctype.startswith("text/") else "")


def make_handler(lab: Lab, port_holder: dict):
    class Handler(BaseHTTPRequestHandler):
        server_version = "BrowserLab/1"

        def log_message(self, fmt, *args):  # keep the console quiet; errors are returned as JSON
            pass

        def _host_ok(self) -> bool:
            host = (self.headers.get("Host") or "").split(":")[0]
            return host in ("127.0.0.1", "localhost")

        def _origin_ok(self) -> bool:
            """Refuse cross-site POSTs (a page elsewhere in the tester's browser could otherwise start sessions)."""
            origin = self.headers.get("Origin")
            if origin and origin not in (f"http://127.0.0.1:{port_holder.get('port')}",
                                         f"http://localhost:{port_holder.get('port')}"):
                return False
            return (self.headers.get("Content-Type") or "").startswith("application/json")

        def _json(self, status: int, obj) -> None:
            body = json.dumps(obj, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _body(self) -> dict:
            n = int(self.headers.get("Content-Length") or 0)
            if n > 2_000_000:
                raise ValueError("request too large")
            return json.loads(self.rfile.read(n) or b"{}")

        def do_GET(self):
            if not self._host_ok():
                return self._json(HTTPStatus.FORBIDDEN, {"error": "host not allowed"})
            got = api_get(lab, self.path)
            if got is not None:
                return self._json(200, got)
            found = static_file(self.path)
            if found is None:
                return self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})
            data, ctype = found
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def do_POST(self):
            if not self._host_ok():
                return self._json(HTTPStatus.FORBIDDEN, {"error": "host not allowed"})
            if not self._origin_ok():
                return self._json(HTTPStatus.FORBIDDEN, {"error": "cross-site or non-JSON request refused"})
            try:
                body = self._body()
            except (ValueError, json.JSONDecodeError) as exc:
                return self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            status, payload = api_post(lab, self.path, body)
            return self._json(status, payload)

    return Handler


def serve(port: int = 8765, lab: Lab | None = None) -> ThreadingHTTPServer:
    lab = lab or Lab()
    holder: dict = {}
    httpd = ThreadingHTTPServer(("127.0.0.1", port), make_handler(lab, holder))
    holder["port"] = httpd.server_address[1]
    return httpd


def main() -> int:
    ap = argparse.ArgumentParser(description="Browser conversation lab (local only)")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--results-dir", default=str(RESULTS))
    ap.add_argument("--ledger", default=str(DEFAULT_LEDGER))
    ap.add_argument("--db", default=str(DEFAULT_DB), help="reception database shared with the admin app")
    args = ap.parse_args()
    lab = Lab(ledger_path=pathlib.Path(args.ledger), results_dir=pathlib.Path(args.results_dir))
    lab.service = ReceptionService(Store(args.db), env=lab.env)
    httpd = serve(args.port, lab)
    print(f"browser lab: http://127.0.0.1:{httpd.server_address[1]}  (Ctrl+C to stop)", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0
