"""Local server for the browser conversation lab (standard library only).

python3 -m prototype.browser_lab [--port 8765] [--db PATH]   then open http://127.0.0.1:8765
The admin app (python3 -m prototype.admin) serves the same lab at /lab/ behind its login.

Runs on the tester's own PC (the microphone is in their browser). Listens on 127.0.0.1 only and rejects
other Host headers and cross-site POSTs. Keys come from environment variables and are never sent to the page.

Business logic: every session is a call in the common ReceptionService (source "browser_lab"). Its settings
snapshot (FAQ, voice, storage rules) is taken at session start; the vendor gets instructions built from it where
the vendor allows it; every tool call goes to ReceptionService.tool. Caller transcripts arrive as utterance events
(the only replies the confirmation guard accepts); spoken AI refusal stops the session.

Session control:
- starts per candidate and stage are counted in the database (lab_sessions) inside one transaction, so the plan's
  1-2 connection checks hold across restarts and concurrent starts; held candidates cannot start;
- before credentials are minted, the ledger reserves the voice maximum at the upper price (plus, for GPT-Live,
  the backend under the strict assumption). Ending a session records an ESTIMATE but keeps the reservation open
  until it is reconciled with the vendor console (python3 -m prototype.engines.budget --close ...);
- the page sends a heartbeat; the server expires sessions past the maximum length and tells the page to stop.
  For GPT-Live the watchdog also asks the vendor to close the session (sideband session.close). Whether a
  vendor connection really ends is not guaranteed where the vendor offers no server-side close.
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
from ..reception.store import DEFAULT_DB, Store, dumps, iso, now_jst
from . import vendors, wsclient
from .config import (APPLY_LABELS, CANDIDATES, DELEGATION_PRICES, LAB_LIMITS, LAB_LIMITS_BY_STAGE, LAB_RULES,
                     LAB_STAGES, LOGIC_PATHS, MAX_BACKEND_RESPONSES, MAX_MINT_FAILURES, MAX_RESPONSE_CREATES,
                     MAX_SESSION_MIN, MAX_TOOL_CALLS, SESSION_GRACE_S, SESSIONS, apply_modes, delegation_model,
                     public_candidates, released, session_reserve_usd)

STATIC = pathlib.Path(__file__).resolve().parent / "static"
RESULTS = pathlib.Path(__file__).resolve().parents[1] / "results" / "browser-lab"
PATH_TAG = "browser_lab"


COUNTED = ("starting", "active", "ended", "expired", "lost_on_restart", "connect_failed")
FAKE_SCRIPTS = {   # what the offline candidate "hears" at each end of caller speech (no speech recognition)
    "default": ["（模擬）焼肉ほのか博多店の田中です", "（模擬）点検は無料ですか"],
    "refusal": ["（模擬）点検は無料ですか", "人と話したいので、AIは使わないでください"],
    "recording": ["録音はしないでください", "（模擬）点検は無料ですか", "（模擬）ダクトの清掃をお願いします"],
}


class SessionLimit(ValueError):
    """A start refused by the plan's per-candidate count (not by the ledger)."""


class Lab:
    """State shared by request handlers: sessions, ledgers, injectable env, HTTP and sideband close for tests."""

    def __init__(self, env: dict | None = None, http=vendors.http_json, ledger_path: pathlib.Path = DEFAULT_LEDGER,
                 results_dir: pathlib.Path = RESULTS, service: ReceptionService | None = None,
                 live_close=wsclient.close_live_session, clock=time.time, recover: bool = True) -> None:
        self.env = dict(os.environ if env is None else env)
        self.http, self.ledger_path, self.results_dir = http, pathlib.Path(ledger_path), pathlib.Path(results_dir)
        self.service = service or ReceptionService(Store(":memory:"), env=self.env)
        self.store = self.service.store
        self.live_close, self.clock = live_close, clock
        self.stage = self.env.get("LAB_STAGE", "connection")
        if self.stage not in LAB_LIMITS_BY_STAGE:
            raise ValueError(f"LAB_STAGE must be one of {', '.join(LAB_LIMITS_BY_STAGE)}")
        self.sessions: dict[str, dict] = {}   # runtime data of sessions started by this process
        self.lock = threading.Lock()
        self._watchdog: threading.Thread | None = None
        if recover:   # this process serves the lab: sessions left active by an earlier process are lost
            self.store.x("UPDATE lab_sessions SET status = 'lost_on_restart', ended_at = ?, end_reason = "
                         "'サーバーの再起動で失われた（留保は照合まで残す）' WHERE status IN ('starting', 'active')",
                         (iso(now_jst()),))

    def ledger(self, vendor: str) -> UsageLedger:
        return UsageLedger(vendor, LAB_LIMITS_BY_STAGE[self.stage][vendor], self.ledger_path)

    def _row(self, sid: str) -> dict:
        row = self.store.q1("SELECT * FROM lab_sessions WHERE id = ?", (sid,))
        if not row:
            raise ValueError(f"unknown session {sid}")
        return row

    def _set(self, sid: str, **cols) -> None:
        sets = ", ".join(f"{k} = ?" for k in cols)
        self.store.x(f"UPDATE lab_sessions SET {sets} WHERE id = ?", (*cols.values(), sid))

    def counts(self, candidate: str) -> dict:
        q = "SELECT COUNT(*) AS c FROM lab_sessions WHERE candidate = ? AND stage = ? AND status "
        used = self.store.q1(q + f"IN ({','.join('?' * len(COUNTED))})", (candidate, self.stage, *COUNTED))["c"]
        failed = self.store.q1(q + "= 'mint_failed'", (candidate, self.stage))["c"]
        return {"sessions": used, "limit": SESSIONS[self.stage].get(candidate), "mint_failures": failed,
                "mint_failure_limit": MAX_MINT_FAILURES}

    def reset_mint_failures(self, candidate: str) -> int:
        return self.store.x("UPDATE lab_sessions SET status = 'mint_failed_reset' WHERE candidate = ? AND stage = ? "
                            "AND status = 'mint_failed'", (candidate, self.stage)).rowcount

    # --- sessions ------------------------------------------------------------------------

    def _applied(self, cfg: dict, candidate: str, call: dict) -> tuple[dict, str | None, dict | None]:
        """What the admin settings change in this vendor session, and the voice / overrides to send."""
        modes = apply_modes(candidate, self.env)
        snap = self.service.snapshot(call["id"])
        voices = snap["config"]["voices"]
        pick = snap["voice"] if snap["voice"]["candidate"] == candidate else \
            next((v for v in voices if v["candidate"] == candidate), None)
        voice = pick["voice"] if pick and modes["voice"] in ("session", "override") else None
        applied = {"instructions": APPLY_LABELS[modes["instructions"]] +
                   ("（FAQ " + ", ".join(call["faq_codes"]) + "）" if modes["instructions"] in ("session", "override")
                    else ""),
                   "voice": (f"{voice}（管理画面の設定）" if voice else
                             APPLY_LABELS[modes["voice"]] if modes["voice"] in ("agent_fixed", "n/a") else
                             "業者の既定の声（管理画面に、この候補の声が登録されていない）"),
                   "faq": "業務処理 lookup_faq で、会話の開始時のFAQを使う（全候補）"}
        overrides = None
        if cfg["adapter"] == "elevenlabs" and "override" in modes.values():
            overrides = {}
            if modes["instructions"] == "override":
                overrides["agent"] = {"prompt": {"prompt": None}, "firstMessage": None, "language": "ja"}
            if modes["voice"] == "override" and voice:
                overrides["tts"] = {"voiceId": voice}
        return applied, voice, overrides

    def start(self, candidate: str, fake_script: str | None = None) -> dict:
        cfg = CANDIDATES.get(candidate)
        if cfg is None:
            raise ValueError(f"unknown candidate {candidate}")
        if cfg.get("hold") and candidate not in released(self.env):
            raise SessionLimit(f"保留中の候補です：{cfg['hold']}（解除は利用者の承認の後、LAB_RELEASE_HOLD）")
        missing = [k for k in cfg["env"] if not self.env.get(k)]
        if missing:
            raise ValueError(f"missing environment variables: {', '.join(missing)} (nothing was sent)")
        if cfg.get("delegation_model_env") and delegation_model(cfg, self.env) not in DELEGATION_PRICES:
            raise ValueError(f"GPT-Live の裏方のモデル {delegation_model(cfg, self.env)} の単価が未登録です"
                             "（単価と実行の制限を決めてから使う）")
        sid = uuid.uuid4().hex[:12]
        now = self.clock()
        with self.store.tx():   # count and claim the slot atomically (restarts and concurrent starts included)
            if cfg["vendor"]:
                c = self.counts(candidate)
                if c["limit"] == 0:
                    raise SessionLimit(f"{cfg['name']}：この段階（{LAB_STAGES[self.stage]}）では開始の回数が決まっていない"
                                       "（0回）。回数と費用の上限を決めてから試す")
                if c["limit"] is not None and c["sessions"] >= c["limit"]:
                    raise SessionLimit(f"{cfg['name']}：この段階（{LAB_STAGES[self.stage]}）の開始は{c['limit']}回までで、"
                                       f"すでに{c['sessions']}回です")
                if c["mint_failures"] >= MAX_MINT_FAILURES:
                    raise SessionLimit(f"{cfg['name']}：接続情報の発行に{c['mint_failures']}回失敗しました。鍵・設定を確かめ、"
                                       f"python3 -m prototype.browser_lab --reset-mint-failures {candidate} で解除してから試す")
            self.store.x("INSERT INTO lab_sessions(id, candidate, stage, status, vendor, started_at, started_ts) "
                         "VALUES(?, ?, ?, 'starting', ?, ?, ?)", (sid, candidate, self.stage, cfg["vendor"],
                                                                  iso(now_jst()), now))
        rid = None
        if cfg["vendor"]:
            try:
                rid = self.ledger(cfg["vendor"]).reserve(f"browser_session:{candidate}", PATH_TAG,
                                                         session_reserve_usd(cfg, self.env), sid,
                                                         audio_seconds=MAX_SESSION_MIN * 60)
            except BudgetExceeded:
                self._set(sid, status="not_started", end_reason="台帳の上限で開始しなかった")
                raise
        call = self.service.start_call(dialed="browser-lab", source="browser_lab", force_ai=True)
        instructions = self.service.instructions(call["id"], preamble=LAB_RULES)
        applied, voice, overrides = self._applied(cfg, candidate, call)
        if overrides and "agent" in overrides:
            overrides["agent"]["prompt"]["prompt"] = instructions
            overrides["agent"]["firstMessage"] = vendors.GREETING
        try:
            creds = vendors.mint(cfg, self.env, self.http, instructions=instructions, voice=voice,
                                 overrides=overrides, script=FAKE_SCRIPTS.get(fake_script or "default"))
        except Exception:
            if rid:  # minting a token is not billed: release the reservation with a zero record
                led = self.ledger(cfg["vendor"])
                led.record("mint_failed", PATH_TAG, 0.0, sid, reservation=rid, counts_request=False)
                led.close(rid, note="credential minting failed; no voice session started")
            self._set(sid, status="mint_failed", call_id=call["id"], ended_at=iso(now_jst()),
                      end_reason="接続情報の発行に失敗")
            self.service.end_call(call["id"], "接続情報の発行に失敗")
            raise
        self._set(sid, status="active", rid=rid, call_id=call["id"], last_seen=iso(now_jst()))
        with self.lock:
            self.sessions[sid] = {"candidate": candidate, "cfg": cfg, "rid": rid, "started": now,
                                  "call_id": call["id"], "instructions": instructions, "voice": voice,
                                  "sdp_done": False}
        return {"session_id": sid, "candidate": candidate, "adapter": cfg["adapter"], "verified": cfg["verified"],
                "logic_path": LOGIC_PATHS[cfg["logic_path"]], "call_id": call["id"],
                "config_version": call["config_version"], "faq_codes": call["faq_codes"], "voice": voice,
                "applied": applied, "stage": self.stage, "counts": self.counts(candidate),
                "limits": {"tool_calls": MAX_TOOL_CALLS, "backend_responses": MAX_BACKEND_RESPONSES,
                           "response_creates": MAX_RESPONSE_CREATES},
                "max_session_min": MAX_SESSION_MIN, "credentials": creds}

    def _active(self, sid: str) -> tuple[dict, dict]:
        row = self._row(sid)
        if row["status"] != "active":
            raise ValueError(f"session {sid} is not active ({row['status']})")
        with self.lock:
            rt = self.sessions.get(sid)
        if rt is None:
            raise ValueError(f"session {sid} is not active in this server")
        return row, rt

    def sdp(self, sid: str, offer: str) -> dict:
        row, s = self._active(sid)
        if s["cfg"]["adapter"] != "gpt-live":
            raise ValueError("SDP exchange is only for GPT-Live")
        with self.lock:   # one vendor session per reservation
            if s["sdp_done"]:
                raise ValueError("this session already has a connection")
            s["sdp_done"] = True
        out = vendors.exchange_live_sdp(s["cfg"], self.env, offer, self.http, instructions=s["instructions"],
                                        voice=s["voice"])
        self._set(sid, vendor_session_id=out.get("vendor_session_id"))
        return out

    def _estimate(self, row: dict, rt: dict | None, duration_s: float, reason: str, final: bool = False) -> dict:
        cfg = CANDIDATES[row["candidate"]]
        inc = cfg["increment_s"]
        seen_s = self.clock() - (rt["started"] if rt else row["started_ts"])
        billed_s = math.ceil(max(0.0, duration_s, seen_s) / inc) * inc
        if final:
            billed_s = max(billed_s, MAX_SESSION_MIN * 60)
        est = billed_s / 60 * cfg["upper_usd_per_min"]
        led = self.ledger(cfg["vendor"])
        led.record("browser_session", PATH_TAG, est, row["id"], audio_seconds=billed_s, reservation=row["rid"],
                   estimated=True, basis=f"{billed_s}s x upper ${cfg['upper_usd_per_min']}/min; reconcile with the "
                                         "vendor console", reason=reason, candidate=row["candidate"])
        # the reservation stays open: the vendor's real usage is not known until it is reconciled
        return {"estimated_usd": round(est, 4), "billed_s": billed_s, "ledger_totals": led.totals(),
                "reconcile": "照合待ち（業者の利用画面と照合するまで、留保は減らさない）"}

    def end(self, sid: str, duration_s: float, reason: str = "", close_confirmed: bool = False,
            final_usage: dict | None = None) -> dict:
        row = self._row(sid)
        if row["status"] != "active":
            return {"already": row["status"], "reconcile": "照合待ち" if row["rid"] else None}
        with self.lock:
            rt = self.sessions.pop(sid, None)
        status = "connect_failed" if reason.startswith("開始の失敗") else "ended"
        usage = json.loads(row["usage"])
        if final_usage:
            usage.append({"kind": "session_closed", "data": final_usage})
        self._set(sid, status=status, ended_at=iso(now_jst()), end_reason=reason or "試験者が終了",
                  close_confirmed=1 if close_confirmed else row["close_confirmed"], usage=dumps(usage))
        self.service.end_call(row["call_id"], reason or "試験者が終了")
        if not row["rid"]:
            return {"estimated_usd": 0.0}
        return self._estimate(row, rt, duration_s, reason)

    def heartbeat(self, sid: str) -> dict:
        row = self._row(sid)
        if row["status"] == "active":
            self._set(sid, last_seen=iso(now_jst()))
        stop = row["status"] != "active" or bool(row["stop_reason"])
        return {"stop": stop, "reason": row["stop_reason"] or (row["status"] if row["status"] != "active" else "")}

    def _stop(self, sid: str, reason: str) -> None:
        self._set(sid, stop_reason=reason)

    def tool(self, sid: str, name: str, args: dict, caller_utterance: str = "", delay_ms: int = 0) -> dict:
        row, s = self._active(sid)
        if row["tool_calls"] + 1 > MAX_TOOL_CALLS:
            self._stop(sid, "tool_call_limit")
            return {"ok": False, "reason": "tool_call_limit", "stop": True}
        self._set(sid, tool_calls=row["tool_calls"] + 1)
        if delay_ms:
            time.sleep(min(delay_ms, 15000) / 1000)
        return self.service.tool(s["call_id"], name, args or {})   # attached text is never taken as a reply

    def utterance(self, sid: str, text: str, seq) -> dict:
        """A final caller transcript from the page (the only reply the confirmation guard accepts)."""
        row = self._row(sid)
        res = self.service.caller_utterance(row["call_id"], str(text)[:2000], f"lab:{sid}:{seq}")
        if res.get("stop_ai") and row["status"] == "active":
            self._stop(sid, res.get("reason") or "ai_stopped")
        return res

    def interrupted(self, sid: str, source: str) -> dict:
        row = self._row(sid)
        return self.service.ai_interrupted(row["call_id"], str(source)[:80])

    def vendor_event(self, sid: str, kind: str, data: dict | None = None) -> dict:
        """Counts and usage the page reports from the vendor's events (GPT-Live backend responses, closes)."""
        row = self._row(sid)
        data = data or {}
        usage = json.loads(row["usage"])
        if kind == "backend_response":
            n = row["backend_responses"] + 1
            self._set(sid, backend_responses=n)
            if n > MAX_BACKEND_RESPONSES:
                self._stop(sid, "backend_response_limit")
        elif kind == "response_create":
            n = row["response_creates"] + 1
            self._set(sid, response_creates=n)
            if n > MAX_RESPONSE_CREATES:
                self._stop(sid, "response_create_limit")
        elif kind == "backend_usage":
            cfg = CANDIDATES[row["candidate"]]
            model = delegation_model(cfg, self.env)
            pin, pcached, pout = DELEGATION_PRICES[model]
            tin, tcached, tout = (int(data.get("input_tokens") or 0), int(data.get("cached_tokens") or 0),
                                  int(data.get("output_tokens") or 0))
            usd = ((tin - tcached) * pin + tcached * pcached + tout * pout) / 1_000_000
            if row["rid"]:
                self.ledger(cfg["vendor"]).record("gpt_live_backend", PATH_TAG, usd, sid, reservation=row["rid"],
                                                  counts_request=False, model=model, input_tokens=tin, cached_tokens=tcached,
                                                  output_tokens=tout, reported_by_vendor=True)
            usage.append({"kind": "backend_usage", "model": model, "usd": round(usd, 6), "data": data})
            self._set(sid, usage=dumps(usage))
        elif kind == "session_closed":
            usage.append({"kind": "session_closed", "data": data})
            self._set(sid, close_confirmed=1, usage=dumps(usage))
        else:
            raise ValueError(f"unknown vendor event {kind}")
        row = self._row(sid)
        return {"stop": bool(row["stop_reason"]), "reason": row["stop_reason"] or ""}

    def consent(self, sid: str, kind: str) -> dict:
        """Tester-simulated refusal. Only an AI refusal closes the vendor connection."""
        row, s = self._active(sid)
        if kind not in ("ai_refused", "recording_refused"):
            raise ValueError("kind must be ai_refused or recording_refused")
        res = self.service.consent_event(s["call_id"], kind, source="tester")
        stop_ai = not self.service.ai_allowed(s["call_id"])
        if stop_ai:
            self._stop(sid, "ai_refused")
        return {"actions": res["actions"], "stop_ai": stop_ai}

    def sweep(self, now: float | None = None) -> list[str]:
        """Expire sessions past the maximum length (the page's timer is not trusted alone)."""
        now = self.clock() if now is None else now
        limit = MAX_SESSION_MIN * 60 + SESSION_GRACE_S
        expired = []
        for row in self.store.q("SELECT * FROM lab_sessions WHERE status = 'active' AND started_ts < ?",
                                (now - limit,)):
            with self.lock:
                rt = self.sessions.pop(row["id"], None)
            self._set(row["id"], status="expired", stop_reason="max_duration", ended_at=iso(now_jst()),
                      end_reason="最大時間を超えたため、サーバーが期限切れにした")
            self.service.end_call(row["call_id"], "最大時間（サーバーの見張り）")
            note = "業者側の終了は未確認"
            if row["candidate"] == "gpt-live-1" and row["vendor_session_id"] and self.env.get("OPENAI_API_KEY"):
                res = self.live_close(row["vendor_session_id"], self.env["OPENAI_API_KEY"])
                usage = json.loads(row["usage"]) + [{"kind": "sideband_close", "data": {
                    "closed": res.get("closed"), "error": res.get("error"), "event": res.get("event")}}]
                self._set(row["id"], close_confirmed=1 if res.get("closed") else 0, usage=dumps(usage))
                note = "業者側で終了を確認" if res.get("closed") else f"業者側の終了・使用量は未確認（{res.get('error')}）"
            self._set(row["id"], end_reason=f"最大時間を超えたため、サーバーが期限切れにした。{note}。留保は照合まで残す")
            if row["rid"]:
                self._estimate(row, rt, 0.0, "expired", final=True)
            expired.append(row["id"])
        return expired

    def start_watchdog(self, interval_s: float = 10.0) -> None:
        if self._watchdog:
            return

        def loop():
            while True:
                time.sleep(interval_s)
                try:
                    self.sweep()
                except Exception:   # noqa: BLE001 - keep watching
                    pass
        self._watchdog = threading.Thread(target=loop, daemon=True)
        self._watchdog.start()

    def save_results(self, payload: dict) -> str:
        self.results_dir.mkdir(parents=True, exist_ok=True)
        ts = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        cand = "".join(ch for ch in str(payload.get("candidate", "unknown")) if ch.isalnum() or ch in "-._")[:40]
        path = self.results_dir / f"{ts}-{cand}-{uuid.uuid4().hex[:6]}.json"
        sid = payload.get("session_id")
        row = self.store.q1("SELECT * FROM lab_sessions WHERE id = ?", (sid,)) if sid else None
        if row is not None:  # the record the server kept is what counts for the business-logic comparison
            view = self.service.call_view(row["call_id"])
            tool_events = [e for e in view["events"] if e["kind"] == "tool"]
            payload["server_call_id"] = row["call_id"]
            payload["server_tool_calls"] = tool_events
            payload["server_fields"] = self.service.fields(row["call_id"])
            payload["server_summary"] = view["summaries"][-1]["text"] if view["summaries"] else None
            payload["server_session"] = {k: row[k] for k in ("status", "stage", "tool_calls", "backend_responses",
                                                             "response_creates", "stop_reason", "close_confirmed")}
            payload["server_session"]["usage"] = json.loads(row["usage"])
            comp = payload.setdefault("comparison", {})
            comp["business_logic_status"] = ("評価対象（サーバーが業務処理を受け取った）" if tool_events else
                                             "未評価（この会話では業務処理がサーバーに届かなかった）")
        payload["path"] = PATH_TAG
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return str(path)

    def sessions_view(self) -> list[dict]:
        return self.store.q("SELECT id, candidate, stage, status, started_at, ended_at, end_reason, tool_calls, "
                            "backend_responses, stop_reason, close_confirmed FROM lab_sessions ORDER BY started_ts DESC "
                            "LIMIT 50")

    def ledger_summary(self) -> dict:
        out = {}
        for v in LAB_LIMITS_BY_STAGE[self.stage]:
            led = self.ledger(v)
            out[v] = {**led.totals(), "open": led.open_reservations(),
                      "cap_usd": LAB_LIMITS_BY_STAGE[self.stage][v].max_cost_usd}
        return out


# --- request handling shared with the admin app (/lab/...) -------------------------------------------------

def api_get(lab: Lab, path: str, csrf: str = "") -> dict | None:
    path = path.split("?")[0]
    if path == "/api/candidates":
        cands = public_candidates(lab.env)
        for c in cands:
            c["counts"] = lab.counts(c["id"])
        return {"candidates": cands, "stage": lab.stage, "stage_label": LAB_STAGES[lab.stage]}
    if path == "/api/sessions":
        return {"sessions": lab.sessions_view()}
    if path == "/api/ledger":
        return lab.ledger_summary()
    if path == "/api/csrf":
        return {"csrf": csrf}
    return None


def api_post(lab: Lab, path: str, body: dict) -> tuple[int, dict]:
    """Returns (status, payload). Maps the lab's errors to HTTP statuses."""
    sid = body.get("session_id", "")
    try:
        if path == "/api/session/start":
            return 200, lab.start(body.get("candidate", ""), body.get("fake_script"))
        if path == "/api/session/sdp":
            return 200, lab.sdp(sid, body.get("sdp", ""))
        if path == "/api/session/end":
            return 200, lab.end(sid, float(body.get("duration_s", 0)), body.get("reason", ""),
                                bool(body.get("close_confirmed")), body.get("final_usage"))
        if path == "/api/session/heartbeat":
            return 200, lab.heartbeat(sid)
        if path == "/api/tool":
            return 200, lab.tool(sid, body.get("name", ""), body.get("args") or {}, body.get("caller_utterance", ""),
                                 int(body.get("delay_ms") or 0))
        if path == "/api/utterance":
            return 200, lab.utterance(sid, body.get("text", ""), body.get("seq"))
        if path == "/api/interrupted":
            return 200, lab.interrupted(sid, body.get("source", "vendor"))
        if path == "/api/vendor_event":
            return 200, lab.vendor_event(sid, body.get("kind", ""), body.get("data"))
        if path == "/api/consent":
            return 200, lab.consent(sid, body.get("kind", ""))
        if path == "/api/results":
            return 200, {"saved": lab.save_results(body)}
        return HTTPStatus.NOT_FOUND, {"error": "not found"}
    except BudgetExceeded as exc:
        return HTTPStatus.PAYMENT_REQUIRED, {"error": f"budget: {exc}"}
    except SessionLimit as exc:
        return HTTPStatus.CONFLICT, {"error": str(exc)}
    except (ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
        return HTTPStatus.BAD_REQUEST, {"error": str(exc)}
    except vendors.VendorError as exc:
        return HTTPStatus.BAD_GATEWAY, {"error": str(exc)}


def static_file(rel: str) -> tuple[bytes, str] | None:
    rel = rel.split("?")[0]
    rel = "index.html" if rel in ("", "/") else rel.lstrip("/")
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
    ap.add_argument("--reset-mint-failures", metavar="CANDIDATE", help="clear the credential-minting failures of a "
                    "candidate in the current stage, then exit")
    args = ap.parse_args()
    service = ReceptionService(Store(args.db))
    lab = Lab(ledger_path=pathlib.Path(args.ledger), results_dir=pathlib.Path(args.results_dir), service=service,
              recover=not args.reset_mint_failures)   # the reset leaves a running lab's sessions alone
    if args.reset_mint_failures:
        print(f"reset {lab.reset_mint_failures(args.reset_mint_failures)} failure(s) for {args.reset_mint_failures}")
        return 0
    lab.start_watchdog()
    httpd = serve(args.port, lab)
    print(f"browser lab: http://127.0.0.1:{httpd.server_address[1]}  (Ctrl+C to stop)", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0
