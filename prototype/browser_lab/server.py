"""Local server for the browser conversation lab (standard library only).

python3 -m prototype.browser_lab [--port 8765]   then open http://127.0.0.1:8765

Runs on the tester's own PC (the microphone is in their browser). Listens on 127.0.0.1 only and rejects
other Host headers. Keys come from environment variables and are never sent to the page.

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
from ..engines.tools import ToolHandler
from . import vendors
from .config import CANDIDATES, LAB_LIMITS, MAX_SESSION_MIN, public_candidates

STATIC = pathlib.Path(__file__).resolve().parent / "static"
RESULTS = pathlib.Path(__file__).resolve().parents[1] / "results" / "browser-lab"
PATH_TAG = "browser_lab"


class Lab:
    """State shared by request handlers: sessions, ledgers, injectable env and HTTP for tests."""

    def __init__(self, env: dict | None = None, http=vendors.http_json, ledger_path: pathlib.Path = DEFAULT_LEDGER,
                 results_dir: pathlib.Path = RESULTS) -> None:
        self.env = dict(os.environ if env is None else env)
        self.http, self.ledger_path, self.results_dir = http, pathlib.Path(ledger_path), pathlib.Path(results_dir)
        self.sessions: dict[str, dict] = {}
        self.ended: dict[str, dict] = {}   # kept so the saved result can include the server-side tool record
        self.lock = threading.Lock()

    def ledger(self, vendor: str) -> UsageLedger:
        return UsageLedger(vendor, LAB_LIMITS[vendor], self.ledger_path)

    # --- sessions ------------------------------------------------------------------------

    def start(self, candidate: str) -> dict:
        cfg = CANDIDATES.get(candidate)
        if cfg is None:
            raise ValueError(f"unknown candidate {candidate}")
        missing = [k for k in cfg["env"] if not self.env.get(k)]
        if missing:
            raise ValueError(f"missing environment variables: {', '.join(missing)} (nothing was sent)")
        sid = uuid.uuid4().hex[:12]
        rid = None
        if cfg["vendor"]:
            est = MAX_SESSION_MIN * cfg["upper_usd_per_min"]
            rid = self.ledger(cfg["vendor"]).reserve(f"browser_session:{candidate}", PATH_TAG, est, sid,
                                                     audio_seconds=MAX_SESSION_MIN * 60)
        try:
            creds = vendors.mint(cfg, self.env, self.http)
        except Exception:
            if rid:  # minting a token is not billed: release the reservation with a zero record
                led = self.ledger(cfg["vendor"])
                led.record("mint_failed", PATH_TAG, 0.0, sid, reservation=rid)
                led.close(rid, note="credential minting failed; no voice session started")
            raise
        with self.lock:
            self.sessions[sid] = {"candidate": candidate, "cfg": cfg, "rid": rid, "started": time.time(),
                                  "tools": ToolHandler()}
        return {"session_id": sid, "candidate": candidate, "adapter": cfg["adapter"], "verified": cfg["verified"],
                "max_session_min": MAX_SESSION_MIN, "credentials": creds}

    def sdp(self, sid: str, offer: str) -> dict:
        s = self._session(sid)
        if s["cfg"]["adapter"] != "gpt-live":
            raise ValueError("SDP exchange is only for GPT-Live")
        return vendors.exchange_live_sdp(s["cfg"], self.env, offer, self.http)

    def end(self, sid: str, duration_s: float, reason: str = "") -> dict:
        with self.lock:
            s = self.sessions.pop(sid, None)
            if s is not None:
                self.ended[sid] = s
        if s is None:
            raise ValueError(f"unknown session {sid}")
        cfg = s["cfg"]
        if not s["rid"]:
            return {"estimated_usd": 0.0}
        inc = cfg["increment_s"]
        billed_s = math.ceil(max(0.0, duration_s) / inc) * inc
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
        handler: ToolHandler = s["tools"]
        if caller_utterance:
            handler.last_caller_utterance = caller_utterance
        t_ms = round((time.time() - s["started"]) * 1000)
        return handler.handle(name, args or {}, t_ms=t_ms)

    def save_results(self, payload: dict) -> str:
        self.results_dir.mkdir(parents=True, exist_ok=True)
        ts = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        cand = "".join(ch for ch in str(payload.get("candidate", "unknown")) if ch.isalnum() or ch in "-._")[:40]
        path = self.results_dir / f"{ts}-{cand}-{uuid.uuid4().hex[:6]}.json"
        sid = payload.get("session_id")
        with self.lock:
            s = self.sessions.get(sid) or self.ended.get(sid)
        if s is not None:  # the tool calls the server saw, as the record of what was saved
            payload["server_tool_calls"] = s["tools"].calls
            payload["server_fields"] = s["tools"].store.snapshot()
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


def make_handler(lab: Lab, port_holder: dict):
    class Handler(BaseHTTPRequestHandler):
        server_version = "BrowserLab/1"

        def log_message(self, fmt, *args):  # keep the console quiet; errors are returned as JSON
            pass

        def _host_ok(self) -> bool:
            host = (self.headers.get("Host") or "").split(":")[0]
            return host in ("127.0.0.1", "localhost")

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
            if self.path == "/api/candidates":
                return self._json(200, {"candidates": public_candidates(lab.env)})
            if self.path == "/api/ledger":
                return self._json(200, lab.ledger_summary())
            rel = "index.html" if self.path in ("/", "") else self.path.split("?")[0].lstrip("/")
            target = (STATIC / rel).resolve()
            if not target.is_file() or STATIC not in target.parents:  # no path traversal out of static/
                return self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})
            data = target.read_bytes()
            ctype = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
            if target.suffix == ".js":
                ctype = "text/javascript"
            self.send_response(200)
            self.send_header("Content-Type", ctype + ("; charset=utf-8" if ctype.startswith("text/") else ""))
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def do_POST(self):
            if not self._host_ok():
                return self._json(HTTPStatus.FORBIDDEN, {"error": "host not allowed"})
            try:
                body = self._body()
                if self.path == "/api/session/start":
                    return self._json(200, lab.start(body.get("candidate", "")))
                if self.path == "/api/session/sdp":
                    return self._json(200, lab.sdp(body.get("session_id", ""), body.get("sdp", "")))
                if self.path == "/api/session/end":
                    return self._json(200, lab.end(body.get("session_id", ""), float(body.get("duration_s", 0)),
                                                   body.get("reason", "")))
                if self.path == "/api/tool":
                    return self._json(200, lab.tool(body.get("session_id", ""), body.get("name", ""),
                                                    body.get("args") or {}, body.get("caller_utterance", ""),
                                                    int(body.get("delay_ms") or 0)))
                if self.path == "/api/results":
                    return self._json(200, {"saved": lab.save_results(body)})
                return self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})
            except BudgetExceeded as exc:
                return self._json(HTTPStatus.PAYMENT_REQUIRED, {"error": f"budget: {exc}"})
            except (ValueError, KeyError, json.JSONDecodeError) as exc:
                return self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            except vendors.VendorError as exc:
                return self._json(HTTPStatus.BAD_GATEWAY, {"error": str(exc)})

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
    args = ap.parse_args()
    httpd = serve(args.port, Lab(ledger_path=pathlib.Path(args.ledger), results_dir=pathlib.Path(args.results_dir)))
    print(f"browser lab: http://127.0.0.1:{httpd.server_address[1]}  (Ctrl+C to stop)", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0
