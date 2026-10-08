"""ReceptionService: the business logic every voice adapter uses (demo, browser lab, phone relay).

- Settings are versioned. At call start the effective settings (mode decision, FAQ, voice, storage rules)
  are frozen into the call's snapshot; later edits apply from the next call only.
- Intake fields go through the existing ToolHandler / FieldState guard: a value becomes
  ``confirmed_by_caller`` only after a read-back and an explicit affirmative reply.
- Consent and refusal go through the existing CallFlow; its actions are applied here. Once AI processing
  is refused (or the call is on the push-button path, or routed to a person) no caller content is
  forwarded to the AI and AI tools are refused, whatever the adapter asks.
- end_call writes the rule-based summary and the notification outbox rows in one transaction.
"""
from __future__ import annotations

import json
import os
import threading
import uuid
from dataclasses import asdict

from ..concierge.call_flow import CallFlow, Consent
from ..concierge.confirmation import FieldState, Status
from ..concierge.prompts import BASE_INSTRUCTIONS
from ..concierge.readings import digits_only
from ..engines.tools import ToolHandler
from . import settings as S
from .faq import TOOL_RULES, date_block, faq_block, faq_lookup
from .speech_consent import detect as detect_spoken_consent
from .notify import Outbox
from .routing import Decision, decide
from .store import Store, dumps, iso, now_jst, parse_at
from .summary import build_summary, notification_body

AI_STATES = ("ai_conversation", "human_request_pending")
EMERGENCY_ORDER = ("undetermined", "ambiguous", "obvious")
EMERGENCY_PLAY = {"obvious": ("E-01", "折り返し先だけを短く伺い、全員へ「緊急」で通知した"),
                  "ambiguous": ("E-02", "通常か異常かを一度だけ選んでもらう。曖昧なら undetermined を呼ぶ"),
                  "undetermined": ("E-03", "安全とは断定せず受付を続ける。「要注意」で通知した")}
ACTION_LABELS = {"stop_ai_stream": "AIへの音声送信を停止", "stop_recording": "録音を停止",
                 "mark_for_deletion": "削除", "save_field": "項目を保存", "notify": "通知の種類を記録",
                 "end_call": "通話を終了", "say_ai": "AIが話す（意図）", "play": "録音済みの案内を流す",
                 "gather_dtmf": "プッシュボタンの入力を待つ"}
SOURCES = {"demo": "架空の着信テスト", "browser_lab": "ブラウザー会話試験", "relay": "電話経路（未接続）"}


CONFIRM_WAIT_S = 1.5   # a reply transcript may arrive slightly after the model's confirm_field call


class CallSession:
    def __init__(self, tools: ToolHandler, flow: CallFlow) -> None:
        self.tools, self.flow, self.lock = tools, flow, threading.RLock()
        self.new_utterance = threading.Condition(self.lock)


def _flow_dump(f: CallFlow) -> dict:
    return {"caller_id": f.caller_id, "voicemail_enabled": f.voicemail_enabled, "consent": asdict(f.consent),
            "state": f.state, "intake_mode": f.intake_mode, "ai_available": f.ai_available,
            "human_request_asked": f.human_request_asked, "retries": f.retries, "pending_number": f.pending_number}


def _flow_load(d: dict) -> CallFlow:
    d = dict(d)
    d["consent"] = Consent(**d["consent"])
    return CallFlow(**d)


class ReceptionService:
    def __init__(self, store: Store, env: dict | None = None, clock=now_jst) -> None:
        self.store, self.clock = store, clock
        self.env = dict(os.environ if env is None else env)
        self.outbox = Outbox(store, clock)
        self._live: dict[str, CallSession] = {}
        self._lock = threading.RLock()
        self._seed()

    # --- seed ------------------------------------------------------------------------------------------
    def _seed(self) -> None:
        now = iso(self.clock())
        with self.store.tx():
            if not self.store.q1("SELECT id FROM config_versions LIMIT 1"):
                self.store.x("INSERT INTO config_versions(created_at, created_by, note, data) VALUES(?, ?, ?, ?)",
                             (now, "system", "初期値（暫定・架空。本番の決定ではない）",
                              dumps(S.validate_config(S.DEFAULT_CONFIG))))
            if not self.store.q1("SELECT id FROM faqs LIMIT 1"):
                for i, (code, q, a, kw) in enumerate(S.FAQ_SEEDS):
                    self.store.x("INSERT INTO faqs(code, question, answer, keywords, enabled, approval, generated, "
                                 "position, updated_at, updated_by) VALUES(?, ?, ?, ?, 1, '未承認（推奨案）', ?, ?, ?, ?)",
                                 (code, q, a, kw, "hours" if code == "FAQ-04" else None, i, now, "system"))
            if not self.store.q1("SELECT id FROM notify_targets LIMIT 1"):
                for name, ch, sim, en in (("担当者A（架空）", "simulation", "success", 1),
                                          ("担当者B（架空）", "simulation", "fail_500_once", 1),
                                          ("LINE公式アカウント（未接続・架空）", "line", "success", 0)):
                    self.store.x("INSERT INTO notify_targets(name, channel, address, enabled, simulate, created_at, "
                                 "updated_at) VALUES(?, ?, '', ?, ?, ?, ?)", (name, ch, en, sim, now, now))

    # --- settings --------------------------------------------------------------------------------------
    def config(self) -> tuple[int, dict, dict]:
        row = self.store.q1("SELECT * FROM config_versions ORDER BY id DESC LIMIT 1")
        return row["id"], json.loads(row["data"]), {"created_at": row["created_at"], "created_by": row["created_by"],
                                                     "note": row["note"]}

    def save_config(self, data: dict, user: str | None, note: str = "") -> int:
        from ..browser_lab.config import CANDIDATES, voice_applies
        cfg = S.validate_config(data)
        active = next(v for v in cfg["voices"] if v["id"] == cfg["active_voice"])
        if active["candidate"] in CANDIDATES and not voice_applies(active["candidate"], self.env):
            raise ValueError(f"{CANDIDATES[active['candidate']]['name']} の声は、業者のエージェント設定で固定です。"
                             "この画面で選んでも反映されないため、使う声にはできません（記録としての登録はできます）")
        with self.store.tx():
            vid = self.store.x("INSERT INTO config_versions(created_at, created_by, note, data) VALUES(?, ?, ?, ?)",
                               (iso(self.clock()), user, note[:200], dumps(cfg))).lastrowid
            self.store.audit(user, "config_save", f"v{vid}", {"mode": cfg["mode"], "note": note[:200]})
        return vid

    def config_history(self, limit: int = 20) -> list[dict]:
        return self.store.q("SELECT id, created_at, created_by, note FROM config_versions ORDER BY id DESC LIMIT ?",
                            (limit,))

    def check_route(self, at=None, ai_available: bool = True) -> dict:
        vid, cfg, _ = self.config()
        d = decide(cfg, parse_at(at, self.clock()), ai_available).as_dict()
        d["config_version"] = vid
        return d

    # --- FAQ -------------------------------------------------------------------------------------------
    def faqs(self) -> list[dict]:
        _, cfg, _ = self.config()
        rows = self.store.q("SELECT * FROM faqs ORDER BY position, id")
        for r in rows:
            if r["generated"] == "hours":
                r["answer"] = S.hours_answer(cfg)
        return rows

    def save_faq(self, data: dict, user: str | None, fid: int | None = None) -> int:
        q, a = str(data.get("question", "")).strip(), str(data.get("answer", "")).strip()
        kw = ",".join(k.strip() for k in str(data.get("keywords", "")).replace("、", ",").split(",") if k.strip())
        enabled = 1 if data.get("enabled", True) else 0
        now = iso(self.clock())
        with self.store.tx():
            if fid is None:
                if not q or not a:
                    raise ValueError("質問と回答を入力してください")
                pos = (self.store.q1("SELECT MAX(position) AS m FROM faqs")["m"] or 0) + 1
                code = f"FAQ-U{pos}"
                fid = self.store.x("INSERT INTO faqs(code, question, answer, keywords, enabled, approval, position, "
                                   "updated_at, updated_by) VALUES(?, ?, ?, ?, ?, '未承認（試作で追加）', ?, ?, ?)",
                                   (code, q, a, kw, enabled, pos, now, user)).lastrowid
                self.store.audit(user, "faq_add", str(fid), {"question": q})
            else:
                row = self.store.q1("SELECT * FROM faqs WHERE id = ?", (fid,))
                if not row:
                    raise ValueError("FAQが見つかりません")
                q, a = q or row["question"], a or row["answer"]
                if row["generated"]:
                    a = row["answer"]   # generated from the schedule; edit the schedule instead
                kw = kw if "keywords" in data else row["keywords"]
                self.store.x("UPDATE faqs SET question = ?, answer = ?, keywords = ?, enabled = ?, updated_at = ?, "
                             "updated_by = ? WHERE id = ?", (q, a, kw, enabled, now, user, fid))
                self.store.audit(user, "faq_update", str(fid), {"enabled": bool(enabled)})
        return fid

    def snapshot_faqs(self, cfg: dict) -> list[dict]:
        out = []
        for r in self.store.q("SELECT * FROM faqs WHERE enabled = 1 ORDER BY position, id"):
            answer = S.hours_answer(cfg) if r["generated"] == "hours" else r["answer"]
            out.append({"code": r["code"], "question": r["question"], "answer": answer, "keywords": r["keywords"]})
        return out

    # --- voices ----------------------------------------------------------------------------------------
    def voice_catalog(self) -> list[dict]:
        from ..browser_lab.config import APPLY_LABELS, CANDIDATES, apply_modes, released, voice_applies
        out = []
        for cid, cand in CANDIDATES.items():
            status, label = S.voice_status(cid, self.env, CANDIDATES)
            cat = S.VOICE_CATALOG.get(cid, {"voices": [], "how": ""})
            out.append({"candidate": cid, "name": cand["name"], "voices": cat["voices"], "how": cat["how"],
                        "status": status, "status_label": label, "keys_present": status != "no_key",
                        "voice_applies": voice_applies(cid, self.env),
                        "apply_label": APPLY_LABELS[apply_modes(cid, self.env)["voice"]],
                        "hold": cand.get("hold") if cid not in released(self.env) else None})
        return out

    # --- calls -----------------------------------------------------------------------------------------
    def start_call(self, *, dialed: str = "0120-77-3408", caller_id: str | None = None, at=None,
                   ai_available: bool = True, source: str = "demo", force_ai: bool = False) -> dict:
        if source not in SOURCES:
            raise ValueError(f"unknown source {source}")
        vid, cfg, _ = self.config()
        t = parse_at(at, self.clock())
        if force_ai:   # the browser lab always talks to the AI; the routing rule is not under test there
            d = Decision("ai", "browser_lab", "ブラウザー会話試験（経路の判定を使わずにAIで会話）", iso(t), False)
        else:
            d = decide(cfg, t, ai_available)
        voice = next(v for v in cfg["voices"] if v["id"] == cfg["active_voice"])
        snapshot = {"config_version": vid, "config": cfg, "faqs": self.snapshot_faqs(cfg), "voice": voice,
                    "decision": d.as_dict()}
        call_id = f"{t:%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:6]}"
        caller = digits_only(caller_id or "") or None
        flow = CallFlow(caller_id=caller, ai_available=ai_available)
        state = {"ai": "ai_conversation", "normal": "handed_to_normal", "dtmf": "dtmf_entry"}[d.route]
        with self.store.tx():
            self.store.x("INSERT INTO calls(id, source, started_at, dialed, caller_id, route, route_rule, route_reason, "
                         "config_version, snapshot, flow, state) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                         (call_id, source, iso(t), dialed, caller, d.route, d.rule, d.reason, vid, dumps(snapshot),
                          dumps(_flow_dump(flow)), state))
            self._event(call_id, "route", {"route": d.route, "label": d.as_dict()["route_label"], "reason": d.reason,
                                           "config_version": vid, "voice": voice})
            if d.route == "normal":
                self.store.x("UPDATE calls SET ended_at = ?, end_reason = ? WHERE id = ?",
                             (iso(t), "普通受電へ渡した", call_id))
                self._event(call_id, "handoff_normal",
                            {"text": "普通受電へ渡す判定。AIの会話は始めない（デモ：会社の電話機は鳴らさない）"})
        sess = CallSession(ToolHandler(), flow)
        with self._lock:
            self._live[call_id] = sess
        if d.route == "ai":
            self._event(call_id, "play", {"clip": "A-01", "text": "冒頭の案内（AIの受付であること・録音の告知）"})
            if source != "browser_lab" and not d.within_staff_hours:
                self._event(call_id, "play", {"clip": "A-04", "text": "時間外の補足（担当者の受付時間外）"})
        elif d.route == "dtmf":
            with sess.lock:
                self._apply(call_id, sess, flow.on_ai_failure())
                self._persist(call_id, sess)
        return self.call_view(call_id)

    def _call(self, call_id: str) -> dict:
        row = self.store.q1("SELECT * FROM calls WHERE id = ?", (call_id,))
        if not row:
            raise ValueError(f"通話が見つかりません：{call_id}")
        return row

    def _session(self, call_id: str) -> CallSession:
        with self._lock:
            sess = self._live.get(call_id)
            if sess is not None:
                return sess
            call = self._call(call_id)   # rebuilt from the database after a restart
            tools = ToolHandler()
            for r in self.store.q("SELECT * FROM call_fields WHERE call_id = ?", (call_id,)):
                tools.store.fields[r["name"]] = FieldState(r["name"], r["value"], Status(r["status"]), r["source"],
                                                           r["read_back_value"], json.loads(r["history"]))
            sess = CallSession(tools, _flow_load(json.loads(call["flow"])))
            self._live[call_id] = sess
            return sess

    def _event(self, call_id: str, kind: str, data=None) -> None:
        self.store.x("INSERT INTO call_events(call_id, at, kind, data) VALUES(?, ?, ?, ?)",
                     (call_id, iso(self.clock()), kind, dumps(data) if data is not None else None))

    def _persist(self, call_id: str, sess: CallSession) -> None:
        now = iso(self.clock())
        with self.store.tx():
            for name, f in sess.tools.store.fields.items():
                self.store.x("INSERT INTO call_fields(call_id, name, value, status, source, read_back_value, history, "
                             "updated_at) VALUES(?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(call_id, name) DO UPDATE SET "
                             "value = excluded.value, status = excluded.status, source = excluded.source, "
                             "read_back_value = excluded.read_back_value, history = excluded.history, "
                             "updated_at = excluded.updated_at",
                             (call_id, name, f.value, f.status.value, f.source, f.read_back_value, dumps(f.history), now))
            self.store.x("UPDATE calls SET flow = ? WHERE id = ?", (dumps(_flow_dump(sess.flow)), call_id))

    def _ai_block_reason(self, call: dict, sess: CallSession) -> str | None:
        if call["route"] != "ai":
            return "ai_not_used_for_this_call"
        if call["ended_at"]:
            return "call_ended"
        if sess.flow.consent.ai_processing == "refused":
            return "ai_refused"
        if not sess.flow.ai_available:
            return "ai_failure"
        if sess.flow.state not in AI_STATES:
            return f"ai_path_stopped:{sess.flow.state}"
        return None

    def ai_allowed(self, call_id: str) -> bool:
        return self._ai_block_reason(self._call(call_id), self._session(call_id)) is None

    def instructions(self, call_id: str, preamble: str = BASE_INSTRUCTIONS) -> str:
        snap = json.loads(self._call(call_id)["snapshot"])
        at = parse_at(snap["decision"]["at_jst"])
        return (preamble.rstrip() + "\n" + TOOL_RULES + "\n" + date_block(at) + "\n" + faq_block(snap["faqs"]))

    def snapshot(self, call_id: str) -> dict:
        return json.loads(self._call(call_id)["snapshot"])

    def caller_utterance(self, call_id: str, text: str, utterance_id: str | int | None = None) -> dict:
        """A final caller transcript event. forward=False means: do not send it (or anything after it) to the AI.

        This is the only way a caller utterance is registered for the confirmation guard. A repeated
        utterance_id is a duplicate: it is acknowledged and otherwise ignored."""
        sess = self._session(call_id)
        with sess.lock:
            call = self._call(call_id)
            block = self._ai_block_reason(call, sess)
            if block:
                self._event(call_id, "ai_send_blocked", {"reason": block})   # the content itself is not stored
                return {"forward": False, "stop_ai": True, "reason": block}
            if not sess.tools.caller_said(text, utterance_id):
                return {"forward": True, "stop_ai": False, "duplicate": True}
            store = json.loads(call["snapshot"])["config"]["store_transcript"]
            self._event(call_id, "caller", {"text": text if store else "（文字起こしを保存しない設定）",
                                            "seq": sess.tools.utterance_seq})
            sess.new_utterance.notify_all()
            heard = detect_spoken_consent(text)
            out: dict = {"forward": True, "stop_ai": False, "human_request": heard.human_request}
            if heard.recording_refused and sess.flow.consent.recording != "refused":
                # recording only: stop and delete the recording; the AI conversation continues
                out["actions"] = self.consent_event(call_id, "recording_refused", source="speech")["actions"]
                out["recording_stopped"] = True
            if heard.ai_refused:
                # stop sending to the AI at once; the transcript before the refusal (this one included) is deleted
                acts = self.consent_event(call_id, "ai_refused", source="speech")["actions"]
                out = {"forward": False, "stop_ai": True, "reason": "ai_refused_by_speech",
                       "recording_stopped": out.get("recording_stopped", False),
                       "actions": out.get("actions", []) + acts}
            return out

    def ai_interrupted(self, call_id: str, source: str) -> dict:
        """The vendor reported that the AI's speech was cut off (not a device-level observation)."""
        sess = self._session(call_id)
        with sess.lock:
            invalidated = sess.tools.readback_interrupted(source)
            self._event(call_id, "vendor_interrupted", {"source": source, "invalidated": invalidated})
            self._persist(call_id, sess)
            return {"invalidated": invalidated}

    def ai_utterance(self, call_id: str, text: str) -> None:
        sess = self._session(call_id)
        with sess.lock:
            call = self._call(call_id)
            if self._ai_block_reason(call, sess):
                return
            store = json.loads(call["snapshot"])["config"]["store_transcript"]
            self._event(call_id, "ai", {"text": text if store else "（文字起こしを保存しない設定）"})

    def tool(self, call_id: str, name: str, args: dict, caller_utterance: str = "") -> dict:
        """caller_utterance (text an adapter attaches to a tool call) is ignored on purpose: only transcript
        events registered through caller_utterance() count as what the caller said."""
        sess = self._session(call_id)
        with sess.lock:
            call = self._call(call_id)
            block = self._ai_block_reason(call, sess)
            if block:
                self._event(call_id, "ai_blocked", {"tool": name, "reason": block})
                return {"ok": False, "reason": block}
            if name == "confirm_field" and sess.tools.awaiting_reply(str((args or {}).get("field", ""))):
                sess.new_utterance.wait_for(lambda: not sess.tools.awaiting_reply(str(args.get("field", ""))),
                                            timeout=CONFIRM_WAIT_S)
            snap = json.loads(call["snapshot"])
            args = dict(args or {})
            logged_args = args
            if name == "lookup_faq":
                res = faq_lookup(snap["faqs"], str(args.get("question", "")))
                if not res["found"]:
                    self._add_open_question(call, str(args.get("question", ""))[:200], snap)
            elif name == "flag_emergency":
                res = self._emergency(call, args)
            elif name in ("save_field", "request_readback", "confirm_field"):
                field = args.get("field", "")
                if field not in S.FIELD_KEYS:
                    res = {"ok": False, "reason": f"unknown field {field}"}
                elif not snap["config"]["fields"][field]["store"]:
                    res = {"ok": False, "reason": "field_not_storable",
                           "say": "この項目は、設定により伺いません。次の質問へ進んでください。"}
                    logged_args = {"field": field, "value": "（保存しない設定のため記録しない）"}
                else:
                    res = sess.tools.handle(name, args)
            else:
                res = {"ok": False, "reason": f"unknown tool {name}"}
            self._event(call_id, "tool", {"name": name, "args": logged_args, "result": res})
            self._persist(call_id, sess)
            return res

    def _add_open_question(self, call: dict, question: str, snap: dict) -> None:
        qs = json.loads(call["open_questions"])
        text = question if snap["config"]["store_transcript"] else "（FAQにない質問。文字起こしを保存しない設定）"
        qs.append(text)
        self.store.x("UPDATE calls SET open_questions = ? WHERE id = ?", (dumps(qs), call["id"]))

    def _emergency(self, call: dict, args: dict) -> dict:
        level = args.get("level")
        if level not in EMERGENCY_ORDER:
            return {"ok": False, "reason": "level must be obvious, ambiguous or undetermined"}
        prev = call["emergency"]
        if prev is None or EMERGENCY_ORDER.index(level) > EMERGENCY_ORDER.index(prev):
            self.store.x("UPDATE calls SET emergency = ? WHERE id = ?", (level, call["id"]))
        clip, next_step = EMERGENCY_PLAY[level]
        self._event(call["id"], "play", {"clip": clip, "text": "緊急の案内（文言は未承認）"})
        notified = 0
        if level in ("obvious", "undetermined"):   # 'ambiguous' asks once first (E-02)
            kind = f"emergency_{level}"
            label = "緊急" if level == "obvious" else "要注意"
            body = (f"【{label}】（デモ・架空のデータ）受付 {call['id']} で{label}の判定。通話の終了を待たずに通知。"
                    "詳細は管理画面で確認（ログインが必要）")
            notified = self.outbox.enqueue(call["id"], 0, kind, lambda prefix: body, suffix=f":{kind}")["created"]
        return {"ok": True, "play": clip, "next": next_step, "notified": notified}

    # --- consent, refusal, push buttons -----------------------------------------------------------------
    def consent_event(self, call_id: str, kind: str, value=None, source: str = "operator") -> dict:
        sess = self._session(call_id)
        with sess.lock:
            call = self._call(call_id)
            if call["route"] == "normal":
                raise ValueError("普通受電の通話では、AIを使っていません")
            if call["ended_at"]:
                raise ValueError("通話は終わっています")
            f = sess.flow
            handlers = {"recording_refused": f.on_recording_refused, "ai_refused": f.on_ai_refused,
                        "human_request": f.on_human_request, "ai_failure": f.on_ai_failure,
                        "human_request_answer": lambda: f.on_human_request_answer(bool(value))}
            if kind not in handlers:
                raise ValueError(f"unknown consent event {kind}")
            self._event(call_id, "consent", {"kind": kind, "value": value, "source": source})
            applied = self._apply(call_id, sess, handlers[kind]())
            self._persist(call_id, sess)
        return {"actions": applied, "call": self.call_view(call_id)}

    def dtmf(self, call_id: str, digits: str | None) -> dict:
        sess = self._session(call_id)
        with sess.lock:
            if self._call(call_id)["ended_at"]:
                raise ValueError("通話は終わっています")
            self._event(call_id, "dtmf", {"digits": digits})
            applied = self._apply(call_id, sess, sess.flow.on_dtmf(digits))
            self._persist(call_id, sess)
        return {"actions": applied, "call": self.call_view(call_id)}

    def _apply(self, call_id: str, sess: CallSession, actions: list[tuple]) -> list[dict]:
        out, end_reason = [], None
        for a in actions:
            kind = a[0]
            out.append({"action": kind, "label": ACTION_LABELS.get(kind, kind),
                        "args": [x for x in a[1:] if not isinstance(x, dict)]})
            if kind == "mark_for_deletion" and a[1] == "transcript_before_refusal":
                n = self.store.x("DELETE FROM call_events WHERE call_id = ? AND kind IN ('caller', 'ai')",
                                 (call_id,)).rowcount
                self._event(call_id, "transcript_deleted", {"count": n, "why": "AIの拒否より前の文字起こしを削除"})
            elif kind == "mark_for_deletion":
                self._event(call_id, "recording_deleted", {"why": "録音の拒否より前の録音を削除（デモでは録音はない）"})
            elif kind == "save_field":
                _, name, value, source, status = a
                f = sess.tools.store.get(name)
                f.hear(value, source=source)
                if status == "confirmed_by_caller":   # read back with recorded digit clips and answered with "1"
                    f.read_back(value)
                    f.status = Status.CONFIRMED
                    f._log("confirmed_by_keypad", None)
                self._event(call_id, "field_saved", {"field": name, "source": source, "status": f.status.value})
            elif kind == "notify":
                call = self._call(call_id)
                kinds = json.loads(call["notify_kinds"])
                if a[1] not in kinds:
                    kinds.append(a[1])
                self.store.x("UPDATE calls SET notify_kinds = ? WHERE id = ?", (dumps(kinds), call_id))
            elif kind == "end_call":
                end_reason = a[1]
            elif kind == "stop_ai_stream":
                self._event(call_id, "ai_send_stopped", {"text": "AIへの音声送信を停止。この通話では再開しない"})
            elif kind == "stop_recording":
                self._event(call_id, "recording_stopped", {})
            elif kind == "play":
                self._event(call_id, "play", {"clip": a[1], "text": "録音済みの案内（AIを使わない）"})
            elif kind == "say_ai":
                self._event(call_id, "say_ai", {"intent": a[1]})
            elif kind == "gather_dtmf":
                self._event(call_id, "gather_dtmf", {"spec": a[1]})
            else:
                self._event(call_id, kind, {"args": list(a[1:])})
        if end_reason:
            self._persist(call_id, sess)
            self.end_call(call_id, end_reason)
        return out

    # --- end, summary, notifications ---------------------------------------------------------------------
    def fields(self, call_id: str) -> dict[str, dict]:
        """name -> {value, status} of the intake fields of a call (what an adapter may show back)."""
        return {n: {"value": r["value"], "status": r["status"]} for n, r in self._fields(call_id).items()}

    def _fields(self, call_id: str) -> dict[str, dict]:
        return {r["name"]: r for r in self.store.q("SELECT * FROM call_fields WHERE call_id = ?", (call_id,))}

    def _summarize(self, call: dict, version: int, reason: str, corrected: bool) -> dict:
        snap = json.loads(call["snapshot"])
        kinds = json.loads(call["notify_kinds"]) + ([f"emergency_{call['emergency']}"] if call["emergency"] else [])
        summ = build_summary(self._fields(call["id"]), snap["config"], json.loads(call["open_questions"]), kinds,
                             corrected)
        self.store.x("INSERT INTO summaries(call_id, version, text, items, review, reason, created_at) "
                     "VALUES(?, ?, ?, ?, ?, ?, ?)", (call["id"], version, summ["text"], dumps(summ["items"]),
                                                     dumps(summ["review"]), reason, iso(self.clock())))
        summ["kinds"] = kinds
        return summ

    def _enqueue(self, call: dict, version: int, summ: dict) -> dict:
        oq = json.loads(call["open_questions"])
        return self.outbox.enqueue(call["id"], version, "intake",
                                   lambda prefix: notification_body(summ, call["id"], summ["kinds"], oq, prefix))

    def end_call(self, call_id: str, reason: str = "通話の終了") -> dict:
        sess = self._session(call_id)
        with sess.lock:
            call = self._call(call_id)
            if call["ended_at"] is None:
                with self.store.tx():
                    self.store.x("UPDATE calls SET ended_at = ?, end_reason = ? WHERE id = ?",
                                 (iso(self.clock()), reason, call_id))
                    self._event(call_id, "call_ended", {"reason": reason})
                    call = self._call(call_id)
                    if call["route"] in ("ai", "dtmf"):
                        summ = self._summarize(call, 1, "通話の終了", False)
                        if call["source"] != "browser_lab":   # the lab checks the record; it notifies no one
                            self._enqueue(call, 1, summ)
        return self.call_view(call_id)

    def renotify(self, call_id: str, user: str | None) -> dict:
        """Re-inject the notification event of the latest summary. Existing rows are reused, never duplicated."""
        call = self._call(call_id)
        latest = self.store.q1("SELECT * FROM summaries WHERE call_id = ? ORDER BY version DESC LIMIT 1", (call_id,))
        if not latest:
            raise ValueError("この受付には要約がありません（普通受電、または通話中）")
        kinds = json.loads(call["notify_kinds"]) + ([f"emergency_{call['emergency']}"] if call["emergency"] else [])
        summ = {"items": json.loads(latest["items"]), "kinds": kinds}
        res = self._enqueue(call, latest["version"], summ)
        self.store.audit(user, "notification_reinject", call_id, {"version": latest["version"], **res})
        return res

    def correct_field(self, call_id: str, field: str, value: str, user: str | None, reason: str) -> dict:
        call = self._call(call_id)
        if not call["ended_at"]:
            raise ValueError("通話中の受付は補正できません。通話の終了後に補正してください")
        if call["route"] == "normal":
            raise ValueError("普通受電の通話には受付項目がありません")
        if field not in S.FIELD_KEYS:
            raise ValueError("補正できない項目です")
        snap = json.loads(call["snapshot"])
        if not snap["config"]["fields"][field]["store"]:
            raise ValueError("この項目は、保存しない設定でした")
        reason = str(reason or "").strip()
        if not reason:
            raise ValueError("補正の理由を入力してください")
        value = digits_only(value) if field == "callback_number" else str(value).strip()
        if not value:
            raise ValueError("補正後の値を入力してください")
        before = self._fields(call_id).get(field)
        now = iso(self.clock())
        with self.store.tx():
            hist = json.loads(before["history"]) if before else []
            hist.append({"event": "staff_correction", "value": value, "status": "corrected_by_staff", "by": user,
                         "at": now, "before": before["value"] if before else None, "reason": reason})
            self.store.x("INSERT INTO call_fields(call_id, name, value, status, source, read_back_value, history, "
                         "updated_at) VALUES(?, ?, ?, 'corrected_by_staff', 'staff', NULL, ?, ?) "
                         "ON CONFLICT(call_id, name) DO UPDATE SET value = excluded.value, status = excluded.status, "
                         "source = excluded.source, read_back_value = NULL, history = excluded.history, "
                         "updated_at = excluded.updated_at", (call_id, field, value, dumps(hist), now))
            self.store.x("INSERT INTO corrections(call_id, field, before_value, before_status, after_value, after_status, "
                         "reason, by_user, at) VALUES(?, ?, ?, ?, ?, 'corrected_by_staff', ?, ?, ?)",
                         (call_id, field, before["value"] if before else None,
                          before["status"] if before else "empty", value, reason, user, now))
            version = self.store.q1("SELECT MAX(version) AS v FROM summaries WHERE call_id = ?", (call_id,))["v"] or 0
            summ = self._summarize(call, version + 1, f"担当者の補正（{S.FIELD_LABELS[field]}）", True)
            if call["source"] != "browser_lab":
                self._enqueue(call, version + 1, summ)
            self.store.audit(user, "field_correct", call_id, {"field": field, "reason": reason})
        with self._lock:
            self._live.pop(call_id, None)
        return self.call_view(call_id)

    def set_handled(self, call_id: str, status: str, user: str | None) -> None:
        if status not in ("未対応", "対応中", "対応済み", "対応不要"):
            raise ValueError("対応状態が正しくありません")
        self._call(call_id)
        with self.store.tx():
            self.store.x("UPDATE calls SET handled_status = ? WHERE id = ?", (status, call_id))
            self.store.audit(user, "handled_status", call_id, {"status": status})

    # --- views -----------------------------------------------------------------------------------------
    def call_view(self, call_id: str) -> dict:
        from .summary import STATUS_LABELS, display_value
        call = self._call(call_id)
        snap = json.loads(call["snapshot"])
        fields = self._fields(call_id)
        flist = []
        for name in S.FIELD_KEYS:
            f = fields.get(name)
            st = f["status"] if f and f["value"] else "empty"
            flist.append({"name": name, "label": S.FIELD_LABELS[name], "value": f["value"] if f else None,
                          "display": display_value(name, f["value"] if f else None), "status": st,
                          "status_label": STATUS_LABELS.get(st, st), "source": f["source"] if f else None,
                          "history": json.loads(f["history"]) if f else [],
                          "storable": snap["config"]["fields"][name]["store"]})
        events = self.store.q("SELECT id, at, kind, data FROM call_events WHERE call_id = ? ORDER BY id", (call_id,))
        for e in events:
            e["data"] = json.loads(e["data"]) if e["data"] else None
        summaries = self.store.q("SELECT * FROM summaries WHERE call_id = ? ORDER BY version", (call_id,))
        for s in summaries:
            s["items"], s["review"] = json.loads(s["items"]), json.loads(s["review"])
        flow = json.loads(call["flow"])
        return {
            "id": call_id, "source": call["source"], "source_label": SOURCES.get(call["source"], call["source"]),
            "started_at": call["started_at"], "ended_at": call["ended_at"], "end_reason": call["end_reason"],
            "dialed": call["dialed"], "caller_id": call["caller_id"], "route": call["route"],
            "route_label": snap["decision"]["route_label"], "route_reason": call["route_reason"],
            "config_version": call["config_version"], "voice": snap["voice"],
            "snapshot_mode": S.MODE_LABELS[snap["config"]["mode"]], "faq_codes": [f["code"] for f in snap["faqs"]],
            "state": flow["state"] if not call["ended_at"] else "ended", "consent": flow["consent"],
            "ai_allowed": self._ai_block_reason(call, self._session(call_id)) is None if not call["ended_at"] else False,
            "emergency": call["emergency"], "open_questions": json.loads(call["open_questions"]),
            "notify_kinds": json.loads(call["notify_kinds"]), "handled_status": call["handled_status"],
            "fields": flist, "events": events, "summaries": summaries,
            "corrections": self.store.q("SELECT * FROM corrections WHERE call_id = ? ORDER BY id", (call_id,)),
            "notifications": self.outbox.for_call(call_id),
        }

    def list_calls(self, limit: int = 200) -> list[dict]:
        rows = self.store.q("SELECT * FROM calls ORDER BY started_at DESC, rowid DESC LIMIT ?", (limit,))
        out = []
        from .summary import STATUS_LABELS, display_value
        for c in rows:
            fields = self._fields(c["id"])
            def fv(n):
                f = fields.get(n)
                return {"display": display_value(n, f["value"]) if f and f["value"] else "",
                        "status_label": STATUS_LABELS.get(f["status"], "") if f and f["value"] else ""}
            summ = self.store.q1("SELECT text, version FROM summaries WHERE call_id = ? ORDER BY version DESC LIMIT 1",
                                 (c["id"],))
            notes = self.store.q("SELECT status, COUNT(*) AS n FROM notifications WHERE call_id = ? GROUP BY status",
                                 (c["id"],))
            snap = json.loads(c["snapshot"])
            out.append({"id": c["id"], "source_label": SOURCES.get(c["source"], c["source"]),
                        "started_at": c["started_at"], "ended_at": c["ended_at"], "dialed": c["dialed"],
                        "route": c["route"], "route_label": snap["decision"]["route_label"],
                        "emergency": c["emergency"], "handled_status": c["handled_status"],
                        "name": fv("caller_name"), "shop": fv("shop_name"), "number": fv("callback_number"),
                        "request": fv("request"), "datetime": fv("preferred_datetime"),
                        "summary": summ["text"] if summ else "", "summary_version": summ["version"] if summ else None,
                        "notifications": {n["status"]: n["n"] for n in notes}})
        return out

    def status(self) -> dict:
        vid, cfg, meta = self.config()
        now = self.clock()
        return {"now": iso(now), "config_version": vid, "config_meta": meta, "mode": cfg["mode"],
                "mode_label": S.MODE_LABELS[cfg["mode"]], "decision_now": decide(cfg, now).as_dict(),
                "manual_mode_warning": cfg["mode"] != "schedule", "provisional": cfg["provisional"],
                "retention_unset": cfg["retention_days"] is None, "notification_problems": self.outbox.problems(),
                "line_connected": False, "phone_line_connected": False}
