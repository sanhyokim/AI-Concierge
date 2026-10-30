"""ReceptionService: the business logic every voice adapter uses (demo, browser lab, phone relay).

- Settings are versioned. At call start the effective settings (route decision, FAQ, voice, storage rules)
  are frozen into the call's snapshot; later edits apply from the next call only.
- Intake fields go through the existing ToolHandler / FieldState guard: a value becomes
  ``confirmed_by_caller`` only after a read-back and an explicit affirmative reply.
- Consent and refusal go through the existing CallFlow; its actions are applied here. Once AI processing
  is refused (or the call is on the push-button path, or routed to a person) no caller content is
  forwarded to the AI and AI tools are refused, whatever the adapter asks.
- end_call writes the rule-based summary and the notification outbox rows in one transaction.
"""
from __future__ import annotations

import csv
import io
import json
import os
import re
import threading
import unicodedata
import uuid
from dataclasses import asdict

from ..concierge.call_flow import CallFlow, Consent
from ..concierge.confirmation import FieldState, Status
from ..concierge.prompts import ASK_FOR, BASE_INSTRUCTIONS, CLOSING_WORDS, SPEECH_MANNERS, for_speech, voice_rules
from ..concierge.readings import digits_only
from ..engines.tools import READBACK_FIELDS, ToolHandler
from . import settings as S
from .faq import (BACKEND_RULES, DELEGATION_POLICY, DIRECT_TOOL_RULES, SAY_ONLY_ASKING, SAY_ONLY_RULES, SAY_ONLY_TONE,
                  TOOL_RULES, backend_line_block,
                  contains_share, date_block, faq_block, faq_lookup, line_block, today_line, voice_line_hint)
from .speech_consent import detect as detect_spoken_consent
from .notify import Outbox
from .routing import Decision, decide
from .store import Store, dumps, iso, now_jst, parse_at
from .summary import build_summary, line_entry, notification_body

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



# Follow-up questions on the request, user 2026-10-26: 「清掃の対象がなんなのかを必ず確認する（…「フードの清掃」というと
# 厨房フードか焼肉屋の上引きフードかわからない）。また焼肉屋だと対象のものが何基あるか必ず確認する。」
_CLEAR_HOOD = ("厨房", "キッチン", "調理", "焼肉", "上引き", "うわびき", "上びき", "ロースター", "レンジ")
_YAKINIKU = ("ロースター", "上引き", "うわびき", "上びき", "焼肉", "無煙")
# 2026-10-29 (Grok, 「点検」 at 「焼肉ほのか」): 「清掃の対象は、何基ございますか」 for an inspection. The count is asked
# for a cleaning or a named object only.
_COUNT_JOB = ("清掃", "掃除", "洗浄", "ロースター", "フード", "ダクト", "上引き", "うわびき", "上びき")
_COUNT_SAID = re.compile(r"(\d+|[一二三四五六七八九十何数]+)\s*(基|台|卓|つ|個)")
FOLLOW_UPS = {
    "request_target": lambda req, shop: bool(req) and "フード" in req and not any(w in req for w in _CLEAR_HOOD),
    "request_count": lambda req, shop: (bool(req) and any(w in req + shop for w in _YAKINIKU)
                                        and any(w in req for w in _COUNT_JOB)
                                        and not _COUNT_SAID.search(unicodedata.normalize("NFKC", req))),
}
FOLLOW_UP_LABELS = {"request_target": "清掃の対象（厨房のフードか、焼肉の上引きフードか）", "request_count": "対象の数（何基か）"}


def say_only_rules(rules: str) -> str:
    """Eleven Realtime (2026-10-30): no list of questions to pick from, and no 「落ち着いた声」 (it turned the tone dark)."""
    head, rest = rules.split("【伺うこと】", 1)
    rest = rest[rest.index("【守ること】"):]
    head = "\n".join(SAY_ONLY_TONE if line.startswith("- 落ち着いた声") else line for line in head.split("\n"))
    return head + SAY_ONLY_ASKING + rest


def _label(field: str) -> str:
    return FOLLOW_UP_LABELS.get(field) or S.FIELD_LABELS[field]

EMERGENCY_LABELS = {"obvious": "明白な危険", "ambiguous": "曖昧（要注意）", "undetermined": "判断できない（要注意）"}


def fmt_at(value: str | None) -> str:
    """2026-10-29T15:00:54+09:00 → 2026-10-29 15:00:54 (Japan time, as on the screens)."""
    return (value or "").replace("T", " ").replace("+09:00", "")


class HandledConflict(Exception):
    """The handling state changed after the screen read it (someone else took or changed the call first)."""


FAQ_READ_SHARE = 0.6   # this much of a candidate FAQ answer in the AI's words: the AI read that answer
CONFIRM_WAIT_S = 4.0   # a reply transcript may arrive after the model's confirm_field call (Gemini: 2026-10-20;
                       # 2026-10-30 Grok: the 「はい」 came 0.15 s after a 2.5 s wait)


class CallSession:
    def __init__(self, tools: ToolHandler, flow: CallFlow) -> None:
        self.tools, self.flow, self.lock = tools, flow, threading.RLock()
        self.new_utterance = threading.Condition(self.lock)
        self.date_asked = False   # the AI has asked for the wished date: a caller with no wish is not asked again
        self.asked: set[str] = set()   # follow-up questions on the request the AI has asked (request_target/count)
        self.faq_choice: dict | None = None   # a question no FAQ matched, and the FAQ handed to the AI to choose from


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
                                 (code, q, a, kw, None, i, now, "system"))
            # the questions the FAQ could not answer, from calls made before the list existed (2026-10-29)
            if not self.store.q1("SELECT value FROM meta WHERE key = 'faq_misses_from_calls'"):
                for c in self.store.q("SELECT id, started_at, open_questions, snapshot FROM calls ORDER BY started_at"):
                    if not json.loads(c["snapshot"])["config"].get("store_transcript", True):
                        continue
                    for q in json.loads(c["open_questions"] or "[]"):
                        self.store.x("INSERT INTO faq_misses(call_id, at, question, ai_question) VALUES(?, ?, ?, ?)",
                                     (c["id"], c["started_at"], q, q))
                self.store.x("INSERT INTO meta(key, value) VALUES('faq_misses_from_calls', ?)", (now,))
            # FAQ-04 used to be generated from the schedule: give it its own (editable) answer
            self.store.x("UPDATE faqs SET answer = ?, generated = NULL WHERE generated = 'hours'", (S.HOURS_ANSWER,))
            if not self.store.q1("SELECT id FROM notify_targets LIMIT 1"):
                for name, ch, sim, en in (("担当者A（架空）", "simulation", "success", 1),
                                          ("担当者B（架空）", "simulation", "fail_500_once", 1),
                                          ("LINE公式アカウント（未接続・架空）", "line", "success", 0)):
                    self.store.x("INSERT INTO notify_targets(name, channel, address, enabled, simulate, created_at, "
                                 "updated_at) VALUES(?, ?, '', ?, ?, ?, ?)", (name, ch, en, sim, now, now))

    # --- settings --------------------------------------------------------------------------------------
    def config(self) -> tuple[int, dict, dict]:
        row = self.store.q1("SELECT * FROM config_versions ORDER BY id DESC LIMIT 1")
        # validated on read too: a version saved before the schedule was removed loses its mode and schedule
        return row["id"], S.validate_config(json.loads(row["data"])), {"created_at": row["created_at"], "created_by": row["created_by"],
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
            self.store.audit(user, "config_save", f"v{vid}", {"ai_failure_action": cfg["ai_failure_action"],
                                                              "note": note[:200]})
        return vid

    def config_history(self, limit: int = 20) -> list[dict]:
        return self.store.q("SELECT id, created_at, created_by, note FROM config_versions ORDER BY id DESC LIMIT ?",
                            (limit,))

    # --- FAQ -------------------------------------------------------------------------------------------
    def faqs(self) -> list[dict]:
        _, cfg, _ = self.config()
        return self.store.q("SELECT * FROM faqs ORDER BY position, id")

    def save_faq(self, data: dict, user: str | None, fid: int | None = None) -> int:
        q, a = str(data.get("question", "")).strip(), str(data.get("answer", "")).strip()
        kw = ",".join(k.strip() for k in str(data.get("keywords", "")).replace("、", ",").split(",") if k.strip())
        enabled = 1 if data.get("enabled", True) else 0
        examples = "\n".join(e.strip() for e in str(data.get("examples", "")).splitlines() if e.strip())
        now = iso(self.clock())
        with self.store.tx():
            if fid is None:
                if not q or not a:
                    raise ValueError("質問と回答を入力してください")
                pos = (self.store.q1("SELECT MAX(position) AS m FROM faqs")["m"] or 0) + 1
                code = f"FAQ-U{pos}"
                fid = self.store.x("INSERT INTO faqs(code, question, answer, keywords, enabled, approval, position, "
                                   "updated_at, updated_by, examples) VALUES(?, ?, ?, ?, ?, '未承認（試作で追加）', ?, ?, ?, ?)",
                                   (code, q, a, kw, enabled, pos, now, user, examples)).lastrowid
                self.store.audit(user, "faq_add", str(fid), {"question": q})
            else:
                row = self.store.q1("SELECT * FROM faqs WHERE id = ?", (fid,))
                if not row:
                    raise ValueError("FAQが見つかりません")
                q, a = q or row["question"], a or row["answer"]
                kw = kw if "keywords" in data else row["keywords"]
                examples = examples if "examples" in data else row["examples"]
                self.store.x("UPDATE faqs SET question = ?, answer = ?, keywords = ?, enabled = ?, updated_at = ?, "
                             "updated_by = ?, examples = ? WHERE id = ?", (q, a, kw, enabled, now, user, examples, fid))
                self.store.audit(user, "faq_update", str(fid), {"enabled": bool(enabled)})
        return fid

    def snapshot_faqs(self, cfg: dict) -> list[dict]:
        out = []
        for r in self.store.q("SELECT * FROM faqs WHERE enabled = 1 ORDER BY position, id"):
            out.append({"code": r["code"], "question": r["question"], "answer": r["answer"], "keywords": r["keywords"],
                        "examples": r["examples"]})
        return out

    # --- the questions the FAQ could not answer (2026-10-29) -----------------------------------------------
    # User: 「答えられなかった質問はフィードバックで回答に追加できる流れが必要かもね」「FAQで回答できなかった内容を
    # いれて、管理者が回答を記入したら使えるようにするとか？」. Each question no FAQ answered is listed on the FAQ
    # screen; an administrator writes an answer (a new FAQ) or adds it to an FAQ as a similar question.
    MISS_LABELS = {"open": "未回答", "ai_answered": "AIが近いFAQで答えた（確認待ち）", "new_faq": "新しいFAQにした",
                   "added": "FAQに似た質問として足した", "dismissed": "FAQにしない"}

    def faq_misses(self, limit: int = 200) -> list[dict]:
        rows = self.store.q("SELECT * FROM faq_misses ORDER BY CASE WHEN status IN ('open', 'ai_answered') THEN 0 "
                            "ELSE 1 END, id DESC LIMIT ?", (limit,))
        for r in rows:
            r["status_label"] = self.MISS_LABELS.get(r["status"], r["status"])
        return rows

    def handle_faq_miss(self, mid: int, data: dict, user: str | None) -> dict:
        row = self.store.q1("SELECT * FROM faq_misses WHERE id = ?", (mid,))
        if not row:
            raise ValueError("その質問が見つかりません")
        action, now = data.get("action"), iso(self.clock())
        with self.store.tx():
            if action == "answer":            # a new FAQ: the caller's question and the administrator's answer
                question = str(data.get("question") or row["question"]).strip()
                fid = self.save_faq({"question": question, "answer": data.get("answer", ""),
                                     "keywords": data.get("keywords", ""), "examples": row["question"]
                                     if row["question"] != question else ""}, user)
                status, code = "new_faq", self.store.q1("SELECT code FROM faqs WHERE id = ?", (fid,))["code"]
            elif action == "add_to":          # the same question as an FAQ already there, asked another way
                faq = (self.store.q1("SELECT * FROM faqs WHERE id = ?", (int(data.get("faq_id") or 0),))
                       or self.store.q1("SELECT * FROM faqs WHERE code = ?", (row["faq_code"] or "",)))
                if not faq:
                    raise ValueError("足す先のFAQを選んでください")
                have = [e for e in (faq["examples"] or "").splitlines() if e.strip()]
                if row["question"] not in have and row["question"] != faq["question"]:
                    have.append(row["question"])
                self.save_faq({"question": faq["question"], "answer": faq["answer"], "keywords": faq["keywords"],
                               "enabled": bool(faq["enabled"]), "examples": "\n".join(have)}, user, faq["id"])
                status, code = "added", faq["code"]
            elif action == "dismiss":
                status, code = "dismissed", row["faq_code"]
            else:
                raise ValueError("操作が正しくありません")
            self.store.x("UPDATE faq_misses SET status = ?, faq_code = ?, handled_by = ?, handled_at = ? WHERE id = ?",
                         (status, code, user, now, mid))
            self.store.audit(user, "faq_miss_" + action, str(mid), {"faq": code})
        return {"ok": True, "status": status, "faq_code": code,
                "note": "次の会話から使われます" if status in ("new_faq", "added") else ""}

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
            d = Decision("ai", "browser_lab", "ブラウザー会話試験（AIで会話）", iso(t))
        else:
            d = decide(cfg, t, ai_available)
        voice = next(v for v in cfg["voices"] if v["id"] == cfg["active_voice"])
        snapshot = {"config_version": vid, "config": cfg, "faqs": self.snapshot_faqs(cfg), "voice": voice,
                    "decision": d.as_dict()}
        call_id = f"{t:%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:6]}"
        caller = digits_only(caller_id or "") or None
        flow = CallFlow(caller_id=caller, ai_available=ai_available)
        state = {"ai": "ai_conversation", "forward": "forwarded_to_staff", "dtmf": "dtmf_entry"}[d.route]
        with self.store.tx():
            self.store.x("INSERT INTO calls(id, source, started_at, dialed, caller_id, route, route_rule, route_reason, "
                         "config_version, snapshot, flow, state) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                         (call_id, source, iso(t), dialed, caller, d.route, d.rule, d.reason, vid, dumps(snapshot),
                          dumps(_flow_dump(flow)), state))
            self._event(call_id, "route", {"route": d.route, "label": d.as_dict()["route_label"], "reason": d.reason,
                                           "config_version": vid, "voice": voice})
            if d.route == "forward":
                self.store.x("UPDATE calls SET ended_at = ?, end_reason = ? WHERE id = ?",
                             (iso(t), "AIが止まっていたため、担当者の携帯へ転送した", call_id))
                self._event(call_id, "forward_to_staff",
                            {"text": "AIが使えないため、担当者の携帯へ転送する。AIの会話は始めない（デモ：電話はかけない）"})
        sess = CallSession(ToolHandler(), flow)
        with self._lock:
            self._live[call_id] = sess
        if d.route == "ai":
            # the AI says these in its own voice, not a recording (user decision 2026-10-20)
            self._event(call_id, "say_ai", {"clip": "A-01", "intent": "冒頭の案内（AIの受付であること・録音の告知）"})
            if source != "browser_lab":
                self._event(call_id, "say_ai", {"clip": "A-04", "intent": "担当者が出られないための案内"
                                                                      "（ただいま担当者が電話に出られないため、AIがご用件を伺います）"})
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
        call = self._call(call_id)
        snap = json.loads(call["snapshot"])
        at = parse_at(snap["decision"]["at_jst"])
        return (preamble.rstrip() + "\n" + SPEECH_MANNERS + "\n" + TOOL_RULES + "\n" + line_block(bool(call["caller_id"]))
                + "\n" + date_block(at) + "\n" + faq_block(snap["faqs"]))

    def voice_instructions(self, call_id: str, preamble: str, backend: bool, say_only: bool = False) -> str:
        """The short conversation prompt (2026-10-14): role, speaking style, what to ask, guardrails, the end.
        backend=True (GPT-Live): when to hand work to the backend model, which gets backend_instructions().
        backend=False (Gemini): short tool rules and the date table; the FAQ comes only from lookup_faq.
        say_only (Eleven Realtime, 2026-10-30): the model speaks only the says the tools return."""
        call = self._call(call_id)
        at = parse_at(json.loads(call["snapshot"])["decision"]["at_jst"])
        has_line = bool(call["caller_id"])
        if backend:
            parts = [voice_rules(end="裏方に、最後のあいさつを言い終えたことを伝えます",
                                 summary="裏方にまとめを頼み、返ってきた文を一字一句そのまま読みます"), DELEGATION_POLICY,
                     voice_line_hint(has_line), today_line(at) + "。日付や曜日は自分で計算せず、裏方に確かめます。"]
        else:
            rules = voice_rules(end="end_call を呼びます",
                                summary="request_summary を呼び、返ってきた say を一字一句そのまま読みます")
            if say_only:
                rules = say_only_rules(rules)
            parts = [rules, DIRECT_TOOL_RULES, line_block(has_line),
                     date_block(at, brief=True)] + ([SAY_ONLY_RULES] if say_only else [])
        # the words the voice models misread are given in kana (2026-10-20: 「承りました」 said 「しょうりました」)
        return for_speech("\n".join(part.rstrip() for part in [preamble, *parts]))

    def backend_instructions(self, call_id: str) -> str:
        """GPT-Live's backend (delegation) model: the procedures, the tools, the dates and this call's FAQ."""
        call = self._call(call_id)
        snap = json.loads(call["snapshot"])
        at = parse_at(snap["decision"]["at_jst"])
        return "\n".join([BACKEND_RULES.rstrip(), backend_line_block(bool(call["caller_id"])), date_block(at),
                          faq_block(snap["faqs"])])

    def snapshot(self, call_id: str) -> dict:
        return json.loads(self._call(call_id)["snapshot"])

    def set_call_ai(self, call_id: str, candidate: str, name: str, voice: str | None, note: str = "") -> None:
        """The browser lab: which AI took this call and the voice it really used. The snapshot's voice is the
        admin's active voice at the start, which names another AI when the lab tests a different candidate
        (2026-10-20: a Gemini call showed GPT-Live's harema)."""
        with self.store.tx():
            snap = self.snapshot(call_id)
            known = next((v for v in snap["config"]["voices"] if v["candidate"] == candidate and v["voice"] == voice),
                         None)
            snap["ai"] = {"candidate": candidate, "name": name}
            snap["voice"] = known or {"id": None, "candidate": candidate, "voice": voice or "",
                                      "label": voice or "AIの会社の既定の声"}
            self.store.x("UPDATE calls SET snapshot = ? WHERE id = ?", (dumps(snap), call_id))
            self._event(call_id, "ai_used", {"candidate": candidate, "name": name, "voice": snap["voice"]["label"],
                                        **({"note": note} if note else {})})

    def note_ai_usage(self, call_id: str, data: dict) -> None:
        """The AI vendor's own usage report for the call, priced (the browser lab, Gemini)."""
        self._event(call_id, "ai_usage", data)

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
            else:
                confirmed = sess.tools.confirm_from_reply()
                if confirmed:   # the server saw the AI read the value back and this reply affirm it
                    self._event(call_id, "confirmed_by_reply", {"field": confirmed, "seq": sess.tools.utterance_seq})
                    self._persist(call_id, sess)
                    out["confirmed"] = confirmed
            return out

    def ai_interrupted(self, call_id: str, source: str, unplayed_ms: int | None = None) -> dict:
        """The vendor reported that the AI's speech was cut off (not a device-level observation). unplayed_ms: the
        AI audio not yet played at that moment, when the page knows it."""
        sess = self._session(call_id)
        with sess.lock:
            invalidated = sess.tools.readback_interrupted(source, unplayed_ms)
            self._event(call_id, "vendor_interrupted", {"source": source, "invalidated": invalidated,
                                                        **({"unplayed_ms": unplayed_ms}
                                                           if unplayed_ms is not None else {})})
            self._persist(call_id, sess)
            return {"invalidated": invalidated}

    def ai_utterance(self, call_id: str, text: str) -> dict:
        """What the AI said (its own transcript of one turn). Kept in the call's history, and checked against a
        pending read-back: a different number or date than the saved value blocks the confirmation."""
        sess = self._session(call_id)
        with sess.lock:
            call = self._call(call_id)
            if self._ai_block_reason(call, sess):
                return {"recorded": False}
            store = json.loads(call["snapshot"])["config"]["store_transcript"]
            self._event(call_id, "ai", {"text": text if store else "（文字起こしを保存しない設定）"})
            if "日時" in text or "にちじ" in text:
                sess.date_asked = True
            if "何基" in text or "なんき" in text:
                sess.asked.add("request_count")
            if "厨房" in text and ("上引き" in text or "うわびき" in text):
                sess.asked.add("request_target")
            if sess.faq_choice:
                self._faq_answered(call, sess, text)
            wrong = sess.tools.ai_said(text)
            for name, said in wrong.items():
                self._event(call_id, "read_back_mismatch", {"field": name, "said": said})
            self._persist(call_id, sess)
            return {"recorded": True, "read_back_mismatch": wrong}

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
                question = str(args.get("question", ""))
                said = sess.tools.last_caller_utterance
                res = faq_lookup(snap["faqs"], question, said)
                sess.faq_choice = None
                if not res["found"]:
                    mid = self._add_open_question(call, question[:200], snap, said[:200])
                    if res.get("candidates"):
                        sess.faq_choice = {"miss_id": mid, "question": question[:200],
                                           "answers": [(c["faq"], c["answer"]) for c in res["candidates"]]}
            elif name == "flag_emergency":
                res = self._emergency(call, args)
            elif name == "save_fields":
                res, logged_args = self._save_fields(sess, snap, args, call)
            elif name == "offer_line_number":
                res = self._offer_line_number(call, sess, snap)
            elif name == "request_summary":
                res = self._summary(sess, snap)
            elif name == "end_call":
                # the AI said its last words; the page (or the phone side) hangs up after the caller had the chance
                res = {"ok": True, "hangup": True, "hangup_wait_ms": int(S.HANGUP_WAIT_S * 1000),
                       "note": "これ以上は話さないでください。お客様が先に切るのを待ってから、こちらで切ります。"}
            elif name in ("save_field", "request_readback", "confirm_field"):
                field = args.get("field", "")
                if field not in S.FIELD_KEYS:
                    res = {"ok": False, "reason": f"unknown field {field}"}
                elif not snap["config"]["fields"][field]["store"]:
                    res = {"ok": False, "reason": "field_not_storable",
                           "say": "この項目は、設定により伺いません。次の質問へ進んでください。"}
                    logged_args = {"field": field, "value": "（保存しない設定のため記録しない）"}
                else:
                    had_name = self._has(sess, "caller_name")
                    res = sess.tools.handle(name, args)
                    if name == "save_field" and field in ("request", "shop_name", "caller_name") \
                            and "read_back_now" not in res:
                        if field == "caller_name" and not had_name and self._has(sess, "caller_name"):
                            res.update(self._greet_and_go_on(sess, self._still_to_ask(sess, snap), call["caller_id"]))
                        else:
                            res.update(self._request_followup(sess, snap, call["caller_id"]))
                    # 2026-10-29 (Grok): after a confirmation it made up 「次は、お名前です。」: the next question too
                    if name == "confirm_field" and res.get("ok") and not self._readback_open(sess):
                        res.update(self._request_followup(sess, snap, call["caller_id"]))
            else:
                res = {"ok": False, "reason": f"unknown tool {name}"}
            # 2026-10-30 (Grok): the AI answered this refusal aloud in English (「Waiting for the customer's
            # response.」). The page sends it without asking for a new answer; the caller's reply starts the next one.
            if name == "confirm_field" and not res.get("ok") and str(res.get("reason", "")).startswith("no caller reply"):
                res["wait_silently"] = True
            self._event(call_id, "tool", {"name": name, "args": logged_args, "result": res})
            self._persist(call_id, sess)
            return res

    def _save_fields(self, sess: CallSession, snap: dict, args: dict, call: dict | None = None) -> tuple[dict, dict]:
        """Several heard values in one tool call (an empty string = not heard). One call instead of one per
        value: each tool call is a pause before the AI speaks again (2026-10-13: six calls in a row)."""
        saved, skipped, logged = {}, [], {}
        had_name = self._has(sess, "caller_name")
        for field in S.FIELD_KEYS:
            value = str(args.get(field) or "").strip()
            if not value:
                continue
            if not snap["config"]["fields"][field]["store"]:
                skipped.append(field)
                logged[field] = "（保存しない設定のため記録しない）"
                continue
            saved[field] = sess.tools.save(field, value)
            logged[field] = value
        res = {"ok": True, "saved": saved}
        # 2026-10-21 (Eleven Realtime): the shop and the name were saved, then asked for again
        left = self._still_to_ask(sess, snap)
        if left:
            res["still_to_ask"] = [_label(f) + ("（ご希望がなければ不要）" if f == "preferred_datetime" else "")
                                   for f in left]
        res["do_not_ask_again"] = [S.FIELD_LABELS[f] for f in S.FIELD_KEYS if f in saved]
        if skipped:
            res["not_stored_by_setting"] = skipped
        # the number or the date is read back at once, in the same tool call; one item per reply, so a second
        # one waits for request_readback (2026-10-13: the AI read the number back before saving it, with 〇)
        pending = [f for f in READBACK_FIELDS if f in saved and sess.tools.needs_readback(f)]
        if pending:
            res["read_back_now"] = sess.tools.start_readback(pending[0])
        if pending[1:]:
            res["read_back_later"] = {"fields": pending[1:],
                                      "how": "お客様が「はい」と答えた後で、request_readback を呼んで復唱する"}
        if "caller_name" in saved and not had_name and not pending:
            res.update(self._greet_and_go_on(sess, left, (call or {}).get("caller_id")))
        elif not pending:
            res.update(self._request_followup(sess, snap, (call or {}).get("caller_id")))
        return res, logged

    @staticmethod
    def _readback_open(sess: CallSession) -> bool:
        """A number or a date still to be read back or answered: the next question waits for it."""
        fields = sess.tools.store.fields
        return any(sess.tools.needs_readback(f) or (f in fields and fields[f].status == Status.AWAITING)
                   for f in READBACK_FIELDS)

    @staticmethod
    def _has(sess: CallSession, field: str) -> bool:
        state = sess.tools.store.fields.get(field)
        return bool(state and state.value)

    def _request_followup(self, sess: CallSession, snap: dict, line_number: str | None = None) -> dict:
        """What to ask next, in the server's words: which hood and how many (FOLLOW_UPS), else the next item.
        2026-10-29 (Grok): handed nothing after the shop 「焼肉田中」, it made up 「お名前は、田中様でよろしいでしょうか」."""
        left = self._still_to_ask(sess, snap)
        if not left:
            return {}
        if left[0] not in FOLLOW_UPS:
            if left[0] == "callback_number" and line_number:
                return {"then": "offer_line_number を呼ぶ"}
            if left[0] == "shop_name" and self._has(sess, "caller_name"):
                return {"say": for_speech("恐れ入りますが、店舗名をお伺いしてもよろしいでしょうか。")}
            return {"say": for_speech(ASK_FOR[left[0]])}
        return {"say": for_speech(ASK_FOR[left[0]]),
                "how": ("お答えをご用件に足し、save_field で request を保存し直します"
                        "（例：「焼肉の上引きフードの清掃、3基」）。分からないと言われたら、そのまま次へ進みます")}

    @staticmethod
    def _greet_and_go_on(sess: CallSession, left: list[str], line_number: str | None) -> dict:
        """The name was just given: 「田中様でいらっしゃいますね。いつもお世話になっております。」 and, without waiting,
        the next question. 2026-10-26 (user): callers mostly say nothing to this greeting, so the AI waited in
        silence (Grok: 10 s until the caller said 「はい」); 「続けて…そのまましゃべっていいと思う」."""
        name = sess.tools.store.fields["caller_name"].value
        greeting = f"{name}様でいらっしゃいますね。いつもお世話になっております。"
        how = "お客様のお返事を待たずに、一度の返答で続けて言う"
        if not left:
            return {"say": for_speech(greeting), "then": "お返事を待たずに、request_summary を呼ぶ"}
        if left[0] == "callback_number" and line_number:
            return {"say": for_speech(greeting), "then": "お返事を待たずに、offer_line_number を呼ぶ"}
        if left[0] == "shop_name":   # the name came first: ask only for the shop
            return {"say": for_speech(greeting + "恐れ入りますが、店舗名をお伺いしてもよろしいでしょうか。"), "how": how}
        return {"say": for_speech(greeting + ASK_FOR[left[0]]), "how": how}

    @staticmethod
    def _still_to_ask(sess: CallSession, snap: dict, required_only: bool = False) -> list[str]:
        """The items not heard yet, in the order the AI asks them (VOICE_RULES 【伺うこと】). User decision
        2026-10-23: the request and the date first, the name and the number after (「日時の要件より先に電話番号を
        聞くのは失礼な気がする」). The date is asked once but not required: a caller may have no wish."""
        order = ("request", "preferred_datetime", "shop_name", "caller_name", "callback_number")
        fields = sess.tools.store.fields
        left = [f for f in order if snap["config"]["fields"][f]["store"]
                and not (f == "preferred_datetime" and (required_only or sess.date_asked))
                and not (fields.get(f) and fields[f].value)]
        # the follow-ups on the request come right after it, each asked once (the caller may not know)
        val = lambda f: (fields[f].value or "") if fields.get(f) else ""
        extra = [k for k, needed in FOLLOW_UPS.items() if k not in sess.asked and needed(val("request"), val("shop_name"))]
        return [f for f in left if f == "request"] + extra + [f for f in left if f != "request"]

    def _summary(self, sess: CallSession, snap: dict) -> dict:
        """The words that close the intake, made here so every AI says the same correct Japanese (2026-10-22,
        user: 「GeminiとElevenLabsが設計上同じならAIのセリフや対応も統一できますよね？」; Gemini had said
        「承りました内容は、…の田中様より、…のご希望でございます」)."""
        if not any(self._has(sess, f) for f in S.FIELD_KEYS):
            # 2026-10-29 (C3): the caller only asked whether the inspection is free and said 「以上です」; the AI was handed
            # 「どのようなご用件でしょうか」 again. A call with questions only ends with the goodbye.
            return {"ok": True, "nothing_to_take": True,
                    "then": "うけたまわる用件はありません。お客様が終えようとしていれば、最後のあいさつを言って end_call を呼ぶ"}
        left = self._still_to_ask(sess, snap, required_only=True)
        if left:
            return {"ok": False, "missing": [_label(f) for f in left], "say": for_speech(ASK_FOR[left[0]])}
        fields = sess.tools.store.fields
        val = lambda f: (fields[f].value if fields.get(f) and fields[f].value else "")
        who = "の".join(v for v in (val("shop_name"), f"{val('caller_name')}様" if val("caller_name") else "") if v)
        parts = ["それでは、ご用件を確認いたします。"]
        if who:
            parts.append(f"{who}。")
        parts.append(f"ご用件は、{val('request')}。")
        if val("preferred_datetime"):
            parts.append(f"作業のご希望の日時は、{sess.tools.reading('preferred_datetime')}。")
        parts.append(CLOSING_WORDS)
        return {"ok": True, "say": for_speech("".join(parts)),
                "then": "お客様のお返事を待つ。ほかにご用件がなければ、最後のあいさつを言って end_call を呼ぶ"}

    def _offer_line_number(self, call: dict, sess: CallSession, snap: dict) -> dict:
        """Ask whether to call back on the number the line delivered (spec 2: the first way to get the callback
        number). The number is saved as the callback number (source: the line) and read back; only the caller's
        clear 「はい」 to that read-back confirms it, as for a number said aloud (user request 2026-10-14)."""
        if not snap["config"]["fields"]["callback_number"]["store"]:
            return {"ok": False, "reason": "field_not_storable", "say": "この項目は、設定により伺いません。次の質問へ進んでください。"}
        caller = call["caller_id"]
        if not caller:
            return {"ok": False, "reason": "no_line_number",
                    "note": "非通知か、回線の番号が届いていません。折り返し先のお電話番号を口頭で伺ってください。"}
        state = sess.tools.store.fields.get("callback_number")
        if state and state.status == Status.CONFIRMED:
            return {"ok": True, "already_confirmed": True, "note": "折り返し先は確認済みです。伺い直さないでください。"}
        sess.tools.save("callback_number", caller, source="line")
        now = sess.tools.start_readback("callback_number")
        now["say"] = (f"折り返しのお電話は、いまおかけいただいているお電話番号、{now['read_this_clearly']}で、"
                      "よろしいでしょうか。")
        return {"ok": True, "read_back_now": now,
                "if_no": "別の番号をおっしゃったら save_field で保存し、返ってきた say で復唱します"}

    def _add_open_question(self, call: dict, question: str, snap: dict, said: str = "") -> int | None:
        """A question for the staff (the summary's 確認事項) and, with the transcript kept, for the FAQ screen. The
        caller's own words are listed there when we have them: they are what the next caller may say."""
        qs = json.loads(call["open_questions"])
        keep = snap["config"]["store_transcript"]
        qs.append(question if keep else "（FAQにない質問。文字起こしを保存しない設定）")
        self.store.x("UPDATE calls SET open_questions = ? WHERE id = ?", (dumps(qs), call["id"]))
        if not keep:
            return None
        return self.store.x("INSERT INTO faq_misses(call_id, at, question, ai_question) VALUES(?, ?, ?, ?)",
                            (call["id"], iso(self.clock()), (said or question).strip(), question)).lastrowid

    def _faq_answered(self, call: dict, sess: CallSession, text: str) -> None:
        """No FAQ matched the words, and the AI chose one of the candidates by meaning: which one, if it read its
        answer out. The question is then no longer open for the staff; it waits on the FAQ screen for an
        administrator to confirm the choice (and add the words to that FAQ)."""
        choice = sess.faq_choice
        best = max(((contains_share(text, for_speech(a)), code) for code, a in choice["answers"]), default=(0, None))
        if best[0] < FAQ_READ_SHARE:
            return
        sess.faq_choice = None
        qs = json.loads(call["open_questions"])
        if choice["question"] in qs:
            qs.remove(choice["question"])
            self.store.x("UPDATE calls SET open_questions = ? WHERE id = ?", (dumps(qs), call["id"]))
        if choice["miss_id"]:
            self.store.x("UPDATE faq_misses SET status = 'ai_answered', faq_code = ? WHERE id = ? AND status = 'open'",
                         (best[1], choice["miss_id"]))
        self._event(call["id"], "faq_chosen_by_ai", {"faq": best[1], "question": choice["question"]})

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
            if call["route"] == "forward":
                raise ValueError("担当者へ転送した通話では、AIを使っていません")
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
                             corrected, caller_id=call["caller_id"])
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
            raise ValueError("この受付には要約がありません（担当者へ転送した通話、または通話中）")
        kinds = json.loads(call["notify_kinds"]) + ([f"emergency_{call['emergency']}"] if call["emergency"] else [])
        items = json.loads(latest["items"])
        summ = {"items": items, "kinds": kinds, "line": line_entry(call["caller_id"], items)}
        res = self._enqueue(call, latest["version"], summ)
        self.store.audit(user, "notification_reinject", call_id, {"version": latest["version"], **res})
        return res

    def correct_field(self, call_id: str, field: str, value: str, user: str | None, reason: str) -> dict:
        call = self._call(call_id)
        if not call["ended_at"]:
            raise ValueError("通話中の受付は補正できません。通話の終了後に補正してください")
        if call["route"] == "forward":
            raise ValueError("担当者へ転送した通話には受付項目がありません")
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

    def set_handled(self, call_id: str, status: str, user: str | None, version: int | None) -> dict:
        """Change the handling state, only if nobody changed it since the screen read it (version).

        The person who sets 対応中 becomes the one in charge (担当). 未対応 clears it; 対応済み and 対応不要
        keep whoever was in charge (or record the person, if nobody was). Choosing 対応中 again on a call
        someone else has takes it over: the screen shows who has it before anyone can choose that.
        """
        if status not in ("未対応", "対応中", "対応済み", "対応不要"):
            raise ValueError("対応状態が正しくありません")
        if isinstance(version, bool) or not isinstance(version, int):
            raise ValueError("画面の版がありません。画面を読み直してください")
        with self.store.tx():
            call = self._call(call_id)
            if call["handled_version"] != version:
                who = f"（{call['handled_by']}）" if call["handled_by"] else ""
                raise HandledConflict(f"ほかの画面{who}が先に対応状態を「{call['handled_status']}」に変えました。"
                                      "画面を読み直しました。もう一度選んでください")
            by = None if status == "未対応" else user if status == "対応中" else (call["handled_by"] or user)
            now = iso(self.clock())
            cur = self.store.x("UPDATE calls SET handled_status = ?, handled_by = ?, handled_at = ?, "
                               "handled_version = handled_version + 1 WHERE id = ? AND handled_version = ?",
                               (status, by, now, call_id, version))
            if cur.rowcount != 1:   # cannot happen inside the lock; kept so a future change cannot skip the check
                raise HandledConflict("対応状態が先に変わりました。画面を読み直してください")
            self.store.audit(user, "handled_status", call_id,
                             {"status": status, "by": by, "from": call["handled_status"],
                              "from_by": call["handled_by"]})
        return {"handled_status": status, "handled_by": by, "handled_at": now, "handled_version": version + 1}

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
            "config_version": call["config_version"], "voice": snap["voice"], "ai": snap.get("ai"),
            "snapshot_failure_action": S.FAILURE_LABELS.get(snap["config"].get("ai_failure_action"), ""),
            "faq_codes": [f["code"] for f in snap["faqs"]],
            "state": flow["state"] if not call["ended_at"] else "ended", "consent": flow["consent"],
            "ai_allowed": self._ai_block_reason(call, self._session(call_id)) is None if not call["ended_at"] else False,
            "emergency": call["emergency"], "open_questions": json.loads(call["open_questions"]),
            "notify_kinds": json.loads(call["notify_kinds"]), "handled_status": call["handled_status"],
            "handled_by": call["handled_by"], "handled_at": call["handled_at"],
            "handled_version": call["handled_version"],
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
                        "handled_by": c["handled_by"],
                        "name": fv("caller_name"), "shop": fv("shop_name"), "number": fv("callback_number"),
                        "request": fv("request"), "datetime": fv("preferred_datetime"),
                        "summary": summ["text"] if summ else "", "summary_version": summ["version"] if summ else None,
                        "notifications": {n["status"]: n["n"] for n in notes}})
        return out

    # 2026-10-29 (user): 「受付一覧に記録のエクスポート機能がほしい」. One row per call, for Excel: UTF-8 with a BOM
    # (Excel on Japanese Windows reads a CSV without it as Shift_JIS) and CRLF line ends.
    EXPORT_COLUMNS = ("受付ID", "着信日時", "終了日時", "種類", "経路", "AI", "回線の番号", "店舗名", "店舗名の確認", "お名前",
                      "お名前の確認", "折り返し先", "折り返し先の確認", "用件", "用件の確認", "希望日時", "希望日時の確認",
                      "確認事項", "緊急", "対応", "担当", "要約", "会話")

    def export_calls_csv(self) -> str:
        from .summary import STATUS_LABELS, display_value
        out = io.StringIO()
        w = csv.writer(out, lineterminator="\r\n")
        w.writerow(self.EXPORT_COLUMNS)
        # a cell that starts like a formula is written as text, so a caller's words never run in Excel
        safe = lambda v: "'" + v if isinstance(v, str) and v[:1] in ("=", "+", "-", "@") else ("" if v is None else v)
        for c in self.store.q("SELECT * FROM calls ORDER BY started_at DESC, rowid DESC"):
            fields, snap = self._fields(c["id"]), json.loads(c["snapshot"])
            row = [c["id"], fmt_at(c["started_at"]), fmt_at(c["ended_at"]), SOURCES.get(c["source"], c["source"]),
                   snap["decision"]["route_label"], (snap.get("ai") or {}).get("name", ""),
                   c["caller_id"] or "非通知・不明"]
            for name in ("shop_name", "caller_name", "callback_number", "request", "preferred_datetime"):
                f = fields.get(name)
                has = bool(f and f["value"])
                row += [display_value(name, f["value"]) if has else "",
                        STATUS_LABELS.get(f["status"], f["status"]) if has else "未取得"]
            summ = self.store.q1("SELECT text FROM summaries WHERE call_id = ? ORDER BY version DESC LIMIT 1", (c["id"],))
            talk = [("お客様：" if e["kind"] == "caller" else "AI：") + (json.loads(e["data"] or "{}").get("text") or "")
                    for e in self.store.q("SELECT kind, data FROM call_events WHERE call_id = ? AND kind IN ('caller', "
                                          "'ai') ORDER BY id", (c["id"],))]
            row += ["／".join(json.loads(c["open_questions"] or "[]")), EMERGENCY_LABELS.get(c["emergency"], ""),
                    c["handled_status"], c["handled_by"] or "", summ["text"] if summ else "", "\n".join(talk)]
            w.writerow([safe(v) for v in row])
        return "\ufeff" + out.getvalue()

    def status(self) -> dict:
        vid, cfg, meta = self.config()
        now = self.clock()
        return {"now": iso(now), "config_version": vid, "config_meta": meta,
                "ai_failure_action": cfg["ai_failure_action"],
                "ai_failure_label": S.FAILURE_LABELS[cfg["ai_failure_action"]], "provisional": cfg["provisional"],
                "retention_unset": cfg["retention_days"] is None, "notification_problems": self.outbox.problems(),
                "line_connected": False, "phone_line_connected": False,
                "faq_misses_open": self.store.q1("SELECT COUNT(*) AS n FROM faq_misses WHERE status IN ('open', "
                                                 "'ai_answered')")["n"]}
