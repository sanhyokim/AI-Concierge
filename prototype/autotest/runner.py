"""One caller-role conversation, and a run of several in the background (the test page's 「客役で試す」).

The conversation goes through the browser lab's own steps (Lab.start / tool / utterance / ai_utterance / end), so
it is counted in the ledger, kept as a test call with its summary, and ElevenLabs' own record of the cost is
fetched afterwards, as for a voice test. Only the page is replaced: this module talks to the agent's WebSocket in
text (user_message, agent_response, client_tool_call / client_tool_result) and asks the caller AI what to say.
"""
from __future__ import annotations

import datetime as dt
import json
import threading
import time
import uuid

from ..browser_lab.config import DELEGATION_PRICES
from ..browser_lab.server import PATH_TAG
from ..browser_lab.wsclient import WebSocket
from ..reception.store import now_jst
from . import callers as C
from .checks import evaluate

QUIET_S = 2.5          # after the AI's last words, this long with nothing more from it: the caller's turn
SILENT_LIMIT_S = 40    # nothing at all from the agent for this long: the conversation is given up
MAX_CALLER_TURNS = 24
MAX_CONVERSATION_S = 300
CALLER_RESERVE_USD = 0.05   # what one conversation's caller AI holds in the ledger (about 15 turns of a few
                            # thousand tokens each at gpt-6.1-terra's price)
COST_YEN = (15, 35)         # one conversation, yen with tax: the range below
COST_NOTE = ("1つの会話の費用の見込み：ElevenLabs の文字の1通ごとの料金（1通約$0.003）＋考えるAI＋客役のAI（OpenAI）で、"
             "約15〜35円。実際の額は、受付記録の「AIの料金」と ElevenLabs・OpenAI の利用画面で確かめる")


def run_one(lab, pattern: dict, llm: str | None = None, connect=WebSocket, ask=C.caller_turn,
            clock=time.monotonic, caller_model: str = C.CALLER_MODEL) -> dict:
    """One conversation. Never raises for a problem in the conversation: it is reported in the result."""
    started_at = now_jst()
    info = lab.start("elevenagents", llm=llm, text_only=True, note=f"客役の自動試験 {pattern['id']}「{pattern['title']}」")
    sid, call_id = info["session_id"], info["call_id"]
    transcript: list[tuple[str, str]] = []
    tools: list[dict] = []
    state = {"ai_stopped": False, "ended": False, "error": "", "reason": ""}
    t0 = clock()
    led = lab.ledger("openai_lab")
    rid = led.reserve("autotest_caller", PATH_TAG, CALLER_RESERVE_USD, sid)
    caller = {"turns": 0, "asking": False}   # asking: a request to the caller AI whose usage is not known
    ws = None
    try:
        ws = connect(info["credentials"]["signed_url"])
        ws.send_json({"type": "conversation_initiation_client_data"})
        ai_waiting = False         # the AI said something the caller has not answered yet
        last_event = clock()
        seq = 0
        while True:
            if clock() - t0 > MAX_CONVERSATION_S:
                state["reason"] = f"{MAX_CONVERSATION_S // 60}分を超えた"
                break
            ev = ws.recv_json(QUIET_S if ai_waiting else 5.0)
            if ev is None:
                if state["ended"]:
                    state["reason"] = "AIが電話を切った（end_call）"
                    break
                if not ai_waiting:
                    if clock() - last_event > SILENT_LIMIT_S:
                        state["reason"] = f"AIから{SILENT_LIMIT_S}秒、何も届かなかった"
                        break
                    continue
                # the caller's turn
                caller["asking"] = True
                said, usage = ask(lab.env, lab.http, pattern, transcript, caller_model)
                pin, pcached, pout = DELEGATION_PRICES.get(caller_model, (0.0, 0.0, 0.0))
                usd = ((usage["input_tokens"] - usage["cached_tokens"]) * pin + usage["cached_tokens"] * pcached
                       + usage["output_tokens"] * pout) / 1e6
                # one conversation counts once in the ledger; each turn adds its cost
                led.record("autotest_caller", PATH_TAG, usd, sid, reservation=rid, counts_request=not caller["turns"],
                           model=caller_model, **usage)
                caller["turns"] += 1
                caller["asking"] = False
                ai_waiting = False
                if not said or C.HANG_UP in said:
                    transcript.append(("caller", C.HANG_UP))
                    state["reason"] = "客役が電話を切った"
                    break
                transcript.append(("caller", said))
                seq += 1
                r = lab.utterance(sid, said, seq)
                if r.get("stop_ai"):
                    state["ai_stopped"] = True
                    state["reason"] = "お客様の求めでAIが止まった"
                    break
                ws.send_json({"type": "user_message", "text": said})
                last_event = clock()
                if seq >= MAX_CALLER_TURNS:
                    state["reason"] = f"客役が{MAX_CALLER_TURNS}回話しても終わらなかった"
                    break
                continue
            last_event = clock()
            t = ev.get("type", "")
            if t == "conversation_initiation_metadata":
                conv = (ev.get("conversation_initiation_metadata_event") or {}).get("conversation_id")
                if conv:
                    lab.vendor_event(sid, "vendor_session", {"conversation_id": conv})
            elif t == "ping":
                ws.send_json({"type": "pong", "event_id": (ev.get("ping_event") or {}).get("event_id")})
            elif t == "agent_response":
                words = ((ev.get("agent_response_event") or {}).get("agent_response") or "").strip()
                if words:
                    transcript.append(("ai", words))
                    lab.ai_utterance(sid, words)
                    ai_waiting = True
            elif t == "client_tool_call":
                call = ev.get("client_tool_call") or {}
                name, args = call.get("tool_name", ""), call.get("parameters") or {}
                res = lab.tool(sid, name, args)
                tools.append({"name": name, "args": args, "result": res})
                ai_waiting = False     # the AI is still working: its words after the tool are what the caller answers
                if call.get("expects_response", True) is not False:
                    ws.send_json({"type": "client_tool_result", "tool_call_id": call.get("tool_call_id"),
                                  "result": json.dumps(res, ensure_ascii=False), "is_error": False})
                if name == "end_call":
                    state["ended"] = True
                if res.get("stop"):
                    state["reason"] = f"試験の上限で止めた（{res.get('reason')}）"
                    break
            elif t in ("client_error", "error"):
                state["error"] = json.dumps(ev, ensure_ascii=False)[:300]
    except ConnectionError as exc:
        if not state["ended"]:
            state["error"] = f"ElevenLabs との接続が切れた：{exc}"[:300]
        state["reason"] = state["reason"] or ("AIが電話を切った（end_call）" if state["ended"] else "接続が切れた")
    except Exception as exc:   # noqa: BLE001 - one conversation's failure is reported, the run goes on
        state["error"] = f"{type(exc).__name__}: {exc}"[:300]
        state["reason"] = state["reason"] or "途中で失敗"
    finally:
        if ws is not None:
            ws.close()
        lab.end(sid, clock() - t0, f"客役の自動試験：{state['reason'] or '終了'}")
        # The caller AI's cost is known from the usage OpenAI returned with each answer: its reservation closes. Only a
        # request that failed on the way (sent, usage unknown) leaves it open, to check on OpenAI's usage page.
        if not caller["asking"]:
            if caller["turns"]:
                led.close(rid, "客役のAIの費用は、OpenAIが返した使用量で計算した")
            else:
                led.void(rid, "客役のAIは一度も呼ばなかった")
    view = lab.service.call_view(call_id)
    results = evaluate(pattern, view, transcript, tools, state["ai_stopped"], started_at.replace(tzinfo=None))
    if state["error"]:
        results.insert(0, {"name": "会話が最後まで進んだ", "ok": False, "detail": state["error"]})
    return {"id": pattern["id"], "title": pattern["title"], "call_id": call_id, "session_id": sid,
            "ok": all(r["ok"] for r in results), "checks": results, "transcript": transcript,
            "tools": [{"name": t["name"], "args": t["args"]} for t in tools], "reason": state["reason"],
            "seconds": round(clock() - t0, 1)}


class Runner:
    """Runs the chosen patterns one after another in a background thread; the page asks for the progress."""

    def __init__(self, lab, run_one=run_one) -> None:
        self.lab, self._run_one = lab, run_one
        self.lock = threading.Lock()
        self.run: dict | None = None
        self._stop = False

    def start(self, ids: list[str] | None, llm: str | None) -> dict:
        patterns = C.load()
        if ids:
            unknown = set(ids) - {p["id"] for p in patterns}
            if unknown:
                raise ValueError(f"知らないパターンです：{', '.join(sorted(unknown))}")
            patterns = [p for p in patterns if p["id"] in ids]
        missing = [k for k in ("ELEVENLABS_API_KEY", "OPENAI_API_KEY") if not self.lab.env.get(k)]
        if missing:
            raise ValueError("客役の自動試験には、ElevenLabs と OpenAI のAPIキーが要ります（足りないもの："
                             + "、".join(missing) + "）。起動の画面でキーを入れてください")
        with self.lock:
            if self.run and self.run["status"] == "running":
                raise ValueError("客役の自動試験は、いま動いています。終わるか止めてから始めてください")
            self._stop = False
            self.run = {"id": uuid.uuid4().hex[:8], "status": "running", "llm": llm or "",
                        "started_at": now_jst().isoformat(timespec="seconds"), "total": len(patterns),
                        "current": None, "results": []}
        threading.Thread(target=self._loop, args=(patterns, llm), daemon=True).start()
        return self.status()

    def stop(self) -> dict:
        with self.lock:
            self._stop = True
        return self.status()

    def _loop(self, patterns: list[dict], llm: str | None) -> None:
        for p in patterns:
            with self.lock:
                if self._stop:
                    break
                self.run["current"] = {"id": p["id"], "title": p["title"]}
            try:
                res = self._run_one(self.lab, p, llm)
            except Exception as exc:   # noqa: BLE001 - e.g. the agent could not be made: shown, then stop
                res = {"id": p["id"], "title": p["title"], "ok": False, "call_id": None, "transcript": [], "tools": [],
                       "checks": [{"name": "会話を始められた", "ok": False, "detail": str(exc)[:400]}],
                       "reason": "開始できなかった"}
                with self.lock:
                    self.run["results"].append(res)
                    self._stop = True
                break
            with self.lock:
                self.run["results"].append(res)
        with self.lock:
            self.run["current"] = None
            self.run["status"] = "stopped" if self._stop and len(self.run["results"]) < self.run["total"] else "done"
            self.run["ended_at"] = now_jst().isoformat(timespec="seconds")

    def status(self) -> dict:
        with self.lock:
            return json.loads(json.dumps(self.run)) if self.run else {"status": "idle"}


def report_text(run: dict) -> str:
    """The run as text, to send (「結果をテキストで保存」)."""
    if not run or run.get("status") == "idle":
        return "客役の自動試験の結果はまだありません。\n"
    ok = sum(1 for r in run["results"] if r["ok"])
    lines = [f"客役の自動試験 {run['started_at']}〜{run.get('ended_at', '')}（考えるAI {run.get('llm') or '既定'}）",
             f"結果：{len(run['results'])}件中 {ok}件 合格", ""]
    for r in run["results"]:
        lines.append(f"■ {r['id']} {r['title']}：{'合格' if r['ok'] else '要確認'}（{r.get('reason', '')}・受付 {r.get('call_id') or '-'}）")
        for c in r["checks"]:
            lines.append(f"  {'○' if c['ok'] else '×'} {c['name']}" + (f"（{c['detail']}）" if c.get("detail") and not c["ok"] else ""))
        lines.append("  会話：")
        for role, text in r["transcript"]:
            lines.append(f"    {'AI' if role == 'ai' else '客'}：{text}")
        if r.get("tools"):
            lines.append("  業務処理：" + "、".join(t["name"] for t in r["tools"]))
        lines.append("")
    return "\n".join(lines)


def pattern_list() -> dict:
    return {"patterns": [{"id": p["id"], "title": p["title"]} for p in C.load()], "cost_note": COST_NOTE,
            "cost_yen": list(COST_YEN), "caller_model": C.CALLER_MODEL}

