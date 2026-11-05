"""The caller-role test (客役の自動試験, 2026-11-05, user: 「これらの検証をあなたが客のスタンスで多様なパターンを検証して
くれることはできないのですか？」). A caller AI talks in text with the ElevenAgents reception AI; the server checks each
conversation. Nothing here reaches ElevenLabs or OpenAI: the agent's WebSocket and the caller AI are played from a script,
and the reception service behind the tools is the real one."""
import datetime as dt
import itertools
import json
import pathlib
import socket
import struct
import tempfile
import threading
import time
import unittest

from prototype.autotest import callers as C
from prototype.autotest import checks, runner
from prototype.browser_lab import elevenagents as ea
from prototype.browser_lab.wsclient import WebSocket
from prototype.browser_lab.server import Lab
from prototype.concierge.confirmation import Status
from prototype.concierge.readings import digits_only
from prototype.reception.service import ReceptionService
from prototype.reception.store import Store
from prototype.tenants import current as current_tenant
from prototype.tests.test_browser_lab import Clock
from prototype.tests.test_elevenagents import KEY, ElevenHttp

ENV = {"ELEVENLABS_API_KEY": KEY, "OPENAI_API_KEY": "sk-test-openai"}
GOODBYE = "本日はお電話ありがとうございました。株式会社野田、AIオペレーターがお受けいたしました。失礼いたします。"
USAGE = {"input_tokens": 1000, "cached_tokens": 200, "output_tokens": 50}


def said(words: str) -> dict:
    return {"type": "agent_response", "agent_response_event": {"agent_response": words}}


class FakeAgent:
    """ElevenAgents in chat mode, played from a script. After each caller line it makes that step's tool calls (or says
    the step's words); each tool result's own words are what it says next, as the real agent is told to."""

    def __init__(self, first: str, steps: list[list]):
        self.queue = [{"type": "conversation_initiation_metadata",
                       "conversation_initiation_metadata_event": {"conversation_id": "conv_1"}},
                      {"type": "ping", "ping_event": {"event_id": 7}}, said(first)]
        self.steps, self.sent, self.closed, self.n, self.url = list(steps), [], False, 0, None

    def __call__(self, url):          # the runner's connect()
        self.url = url
        return self

    def send_json(self, obj):
        self.sent.append(obj)
        if obj["type"] == "user_message":
            for item in (self.steps.pop(0) if self.steps else []):
                if isinstance(item, str):
                    self.queue.append(said(item))
                    continue
                self.n += 1
                self.queue.append({"type": "client_tool_call", "client_tool_call": {
                    "tool_name": item[0], "tool_call_id": f"t{self.n}", "parameters": item[1], "expects_response": True}})
        elif obj["type"] == "client_tool_result":
            res = json.loads(obj["result"])
            words = res.get("say") or (res.get("read_back_now") or {}).get("say")
            if words:
                self.queue.append(said(words))

    def recv_json(self, timeout_s):
        return self.queue.pop(0) if self.queue else None   # nothing queued: the agent is quiet

    def close(self):
        self.closed = True


class FakeCaller:
    """The caller AI: its lines in order, then it hangs up."""

    def __init__(self, lines):
        self.lines, self.seen = list(lines), []

    def __call__(self, env, http, pattern, transcript, model):
        self.seen.append(list(transcript))
        return (self.lines.pop(0) if self.lines else C.HANG_UP), dict(USAGE)


PATTERN = {"id": "T01", "title": "試験用のふつうの依頼", "persona": "焼肉店の店長", "facts": {"用件": "ダクトの清掃"},
           "behavior": [], "expect": {"confirmed": ["callback_number", "preferred_datetime"], "number": "09012345678",
                                      "request_has": [["2基", "２基"]], "datetime": {"weekday": "火", "time": "15:00"},
                                      "asked": {"request_count": True}, "tools": ["request_summary"], "ended": True}}
CALLER_LINES = ["ダクトの清掃をお願いしたいです。", "来週の火曜日の午後3時でお願いします。", "はい。", "焼肉みどり天神店の田中です。",
                "2基です。", "090-1234-5678です。", "はい。", "ありません。"]
AGENT_STEPS = [[("save_fields", {"request": "ダクトの清掃"})],
               [("save_fields", {"preferred_datetime": "2026-11-10T15:00"})],
               [("confirm_field", {"field": "preferred_datetime", "value": "2026-11-10T15:00"})],
               [("save_fields", {"shop_name": "焼肉みどり天神店", "caller_name": "田中"})],
               [("save_fields", {"request": "ダクトの清掃（2基）"})],
               [("save_fields", {"callback_number": "09012345678"})],
               [("confirm_field", {"field": "callback_number", "value": "09012345678"}), ("request_summary", {"reason": "全部伺った"})],
               [GOODBYE, ("end_call", {"reason": "最後のあいさつをした"})]]


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = pathlib.Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def lab(self, env=None):
        env = dict(ENV if env is None else env)
        lab = Lab(env=env, http=ElevenHttp(), ledger_path=self.dir / "ledger.jsonl", results_dir=self.dir / "results",
                  service=ReceptionService(Store(":memory:"), env=env), clock=Clock(),
                  agent_state=self.dir / "elevenagents.json")
        lab.spawn = lambda fn: fn()
        lab.sleep = lambda s: None
        return lab

    def run_pattern(self, lab, pattern, lines, steps, **kw):
        agent = FakeAgent(ea.greeting(), steps)
        caller = FakeCaller(lines)
        tick = itertools.count(0, 0.25)
        res = runner.run_one(lab, pattern, connect=agent, ask=caller, clock=lambda: next(tick), **kw)
        return res, agent, caller


class ConversationTest(Base):
    def test_a_whole_conversation_runs_through_the_lab_and_passes_its_checks(self):
        lab = self.lab()
        res, agent, caller = self.run_pattern(lab, PATTERN, CALLER_LINES, AGENT_STEPS, llm="gpt-6-luna")
        self.assertTrue(res["ok"], [c for c in res["checks"] if not c["ok"]])
        self.assertEqual(res["reason"], "AIが電話を切った（end_call）")
        self.assertTrue(agent.closed)
        self.assertTrue(agent.url.startswith("wss://api.elevenlabs.io/"))
        # the agent: a text-only one of its own, so the voice test's agent is never changed by this test
        create = lab.http.of("POST", "/agents/create")[0]["body"]
        self.assertEqual(create["name"], ea.TEXT_AGENT_NAME)
        self.assertTrue(create["conversation_config"]["conversation"]["text_only"])
        self.assertEqual(create["conversation_config"]["agent"]["prompt"]["llm"], "gpt-6-luna")
        state = json.loads((self.dir / "elevenagents.json").read_text(encoding="utf-8"))
        self.assertIn("text_agent_id", state)
        self.assertNotIn("agent_id", state)
        # what went over the WebSocket: the caller's words as user messages, each tool's result to its call, a pong
        kinds = [m["type"] for m in agent.sent]
        self.assertEqual(kinds[0], "conversation_initiation_client_data")
        self.assertIn({"type": "pong", "event_id": 7}, agent.sent)
        self.assertEqual([m["text"] for m in agent.sent if m["type"] == "user_message"], CALLER_LINES)
        results = [m for m in agent.sent if m["type"] == "client_tool_result"]
        self.assertEqual([r["tool_call_id"] for r in results], [f"t{i + 1}" for i in range(len(results))])
        self.assertEqual([t["name"] for t in res["tools"]][-1], "end_call")
        # the caller AI saw the conversation so far, the AI's opening first
        self.assertEqual(caller.seen[0][0][0], "ai")
        self.assertEqual(caller.seen[1][-2], ("caller", CALLER_LINES[0]))
        # the record: a test call like any other, with ElevenLabs' conversation id and its own record of the cost
        view = lab.service.call_view(res["call_id"])
        fields = {f["name"]: f for f in view["fields"]}
        self.assertEqual(fields["callback_number"]["status"], Status.CONFIRMED.value)
        self.assertEqual(fields["preferred_datetime"]["status"], Status.CONFIRMED.value)
        row = lab.store.q1("SELECT * FROM lab_sessions WHERE id = ?", (res["session_id"],))
        self.assertEqual(row["vendor_session_id"], "conv_1")
        self.assertIn("客役の自動試験", row["end_reason"])
        self.assertTrue(lab.http.of("GET", "/convai/conversations/conv_1"))
        used = next(e for e in view["events"] if e["kind"] == "ai_used")
        self.assertIn("客役の自動試験 T01", json.dumps(used["data"], ensure_ascii=False))
        # the caller AI's cost: one conversation in the ledger, its reservation closed (OpenAI returned the usage)
        led = lab.ledger("openai_lab")
        totals = led.totals()
        self.assertEqual(totals["requests"], 1)
        self.assertEqual(led.open_reservations(), [])
        pin, pcached, pout = runner.DELEGATION_PRICES[C.CALLER_MODEL]
        one = (800 * pin + 200 * pcached + 50 * pout) / 1e6
        self.assertAlmostEqual(totals["est_cost_usd"], round(one, 6) * len(CALLER_LINES), places=5)

    def test_the_voice_agent_is_made_separately_after_the_text_one(self):
        lab = self.lab()
        self.run_pattern(lab, PATTERN, CALLER_LINES, AGENT_STEPS)
        lab.start("elevenagents")
        made = lab.http.of("POST", "/agents/create")
        self.assertEqual(len(made), 2)
        self.assertFalse(made[1]["body"]["conversation_config"]["conversation"]["text_only"])
        self.assertNotEqual(made[1]["body"]["name"], ea.TEXT_AGENT_NAME)
        state = json.loads((self.dir / "elevenagents.json").read_text(encoding="utf-8"))
        self.assertNotEqual(state["agent_id"], state["text_agent_id"])

    def test_text_only_is_refused_for_the_other_candidates(self):
        lab = self.lab()
        with self.assertRaises(ValueError):
            lab.start("gpt-live-1", text_only=True)

    def test_the_caller_hanging_up_ends_the_conversation(self):
        lab = self.lab()
        res, agent, _ = self.run_pattern(lab, PATTERN, ["ダクトの清掃をお願いしたいです。"], AGENT_STEPS)
        self.assertEqual(res["reason"], "客役が電話を切った")
        self.assertEqual(res["transcript"][-1], ("caller", C.HANG_UP))
        self.assertFalse(res["ok"])                    # the number was never confirmed
        failed = {c["name"] for c in res["checks"] if not c["ok"]}
        self.assertIn("折り返しの番号が正しい", failed)
        self.assertIn("最後のあいさつをして電話を切った（end_call）", failed)

    def test_the_callers_wish_to_stop_the_ai_stops_it(self):
        lab = self.lab()
        p = {"id": "T16", "title": "AIを断る", "persona": "x", "facts": {"x": "y"}, "behavior": [],
             "expect": {"ai_stopped": True}}
        res, agent, _ = self.run_pattern(lab, p, ["人と話したいので、AIは使わないでください"], [])
        self.assertEqual(res["reason"], "お客様の求めでAIが止まった")
        self.assertTrue(res["ok"], res["checks"])
        self.assertNotIn("user_message", [m["type"] for m in agent.sent])   # the words never reach the AI

    def test_a_lost_connection_is_reported_and_nothing_is_left_held_for_the_caller_ai(self):
        lab = self.lab()

        def refuse(url):
            raise ConnectionError("handshake refused")
        tick = itertools.count(0, 0.25)
        res = runner.run_one(lab, PATTERN, connect=refuse, ask=FakeCaller([]), clock=lambda: next(tick))
        self.assertFalse(res["ok"])
        self.assertEqual(res["checks"][0]["name"], "会話が最後まで進んだ")
        self.assertIn("handshake refused", res["checks"][0]["detail"])
        self.assertEqual(res["reason"], "接続が切れた")
        led = lab.ledger("openai_lab")
        self.assertEqual(led.open_reservations(), [])
        self.assertEqual(led.totals()["requests"], 0)
        row = lab.store.q1("SELECT status FROM lab_sessions WHERE id = ?", (res["session_id"],))
        self.assertEqual(row["status"], "ended")

    def test_a_caller_ai_that_fails_leaves_its_reservation_to_check(self):
        lab = self.lab()

        def broken(*a):
            raise RuntimeError("HTTP 500 from OpenAI")
        agent = FakeAgent("お電話ありがとうございます。", [])
        tick = itertools.count(0, 0.25)
        res = runner.run_one(lab, PATTERN, connect=agent, ask=broken, clock=lambda: next(tick))
        self.assertEqual(res["reason"], "途中で失敗")
        self.assertIn("HTTP 500", res["checks"][0]["detail"])
        self.assertEqual(len(lab.ledger("openai_lab").open_reservations()), 1)   # usage unknown: checked by hand

    def test_a_silent_agent_is_given_up(self):
        lab = self.lab()
        agent = FakeAgent("", [])
        agent.queue = []
        tick = itertools.count(0, 1.0)
        res = runner.run_one(lab, PATTERN, connect=agent, ask=FakeCaller([]), clock=lambda: next(tick))
        self.assertIn(f"{runner.SILENT_LIMIT_S}秒", res["reason"])


class ChecksTest(unittest.TestCase):
    STARTED = dt.datetime(2026, 11, 5, 12, 0)

    def ev(self, ai_lines, expect=None, fields=None, tools=(), stopped=False):
        call = {"fields": fields or [], "emergency": None}
        transcript = [("ai", t) for t in ai_lines]
        return {c["name"]: c for c in checks.evaluate({"expect": expect or {}}, call, transcript,
                                                         [{"name": n} for n in tools], stopped, self.STARTED)}

    def test_words_the_ai_must_not_use_english_and_raw_dates_are_caught(self):
        got = self.ev(["なるほど、了解です。", "Waiting for the customer's response.", "2026-11-10T15:00でよろしいですか。"])
        self.assertFalse(got["使わない言葉を言っていない"]["ok"])
        self.assertIn("なるほど", got["使わない言葉を言っていない"]["detail"])
        self.assertFalse(got["英語を話していない"]["ok"])
        self.assertFalse(got["日付をそのままの形（2026-11-10など）で読んでいない"]["ok"])
        clean = self.ev(["かしこまりました。11月10日、火曜日の、午後3時でございますね。"])
        self.assertTrue(all(c["ok"] for c in clean.values()), clean)

    def test_the_same_question_asked_again(self):
        q = "折り返しのお電話番号を教えていただけますでしょうか。"
        self.assertTrue(self.ev([q, q])["同じ質問を何度も繰り返していない"]["ok"])
        self.assertFalse(self.ev([q, q, q])["同じ質問を何度も繰り返していない"]["ok"])
        self.assertFalse(self.ev([q, q], {"ai_not_repeat_question": True})["同じ質問をもう一度していない"]["ok"])

    def test_the_wish_the_number_and_the_follow_ups(self):
        fields = [{"name": "preferred_datetime", "value": "2026-11-10T15:00頃", "status": "confirmed_by_caller"},
                  {"name": "callback_number", "value": "090-1234-5678", "status": "heard", "status_label": "聞き取り済み"}]
        got = self.ev(["清掃の対象は、何基ございますか。"], {
            "confirmed": ["callback_number", "preferred_datetime"], "number": "09012345678",
            "datetime": {"weekday": "火", "time": "15:00", "days_after": 5},
            "asked": {"request_count": True, "request_target": False}}, fields)
        self.assertTrue(got["希望日時が本人の「はい」で確認済み"]["ok"])
        self.assertFalse(got["折り返しの番号が本人の「はい」で確認済み"]["ok"])
        self.assertIn("聞き取り済み", got["折り返しの番号が本人の「はい」で確認済み"]["detail"])
        self.assertTrue(got["折り返しの番号が正しい"]["ok"])
        self.assertTrue(got["希望日時の曜日（火）"]["ok"])
        self.assertTrue(got["希望の時刻（15:00）"]["ok"])
        self.assertTrue(got["希望日時が通話の5日後"]["ok"])
        ups = {u.key: u.label for u in current_tenant().follow_ups}
        self.assertTrue(got[f"「{ups['request_count']}」を伺った"]["ok"])
        self.assertTrue(got[f"「{ups['request_target']}」を伺わなかった"]["ok"])
        none = self.ev([], {"datetime": {"none": True}}, [{"name": "preferred_datetime", "value": "", "status": "empty"}])
        self.assertTrue(none["希望日時なしで進んだ"]["ok"])


class PatternsTest(unittest.TestCase):
    def test_the_companys_patterns_load_and_agree_with_their_facts(self):
        patterns = C.load()
        self.assertEqual(len(patterns), 20)
        self.assertEqual(len({p["id"] for p in patterns}), 20)
        for p in patterns:
            n = p["expect"].get("number")
            if n:   # the number to check is one the caller knows
                self.assertTrue(any(n in digits_only(str(v)) for v in p["facts"].values()), p["id"])
            for key in p["expect"].get("asked", {}):
                self.assertIn(key, {u.key for u in current_tenant().follow_ups}, p["id"])

    def test_a_broken_file_says_what_to_fix(self):
        with tempfile.TemporaryDirectory() as d:
            f = pathlib.Path(d) / "x.callers.json"
            f.write_text(json.dumps({"callers": [{"id": "A", "title": "t", "persona": "p", "facts": {"a": "b"},
                                                  "expect": {"numbr": "1"}}]}), encoding="utf-8")
            with self.assertRaisesRegex(C.CallerError, "numbr"):
                C.load(f)
            f.write_text(json.dumps({"callers": [{"id": "A", "title": "t", "persona": "p", "facts": {"a": "b"}}] * 2}),
                         encoding="utf-8")
            with self.assertRaisesRegex(C.CallerError, "重なって"):
                C.load(f)
            with self.assertRaisesRegex(C.CallerError, "ありません"):
                C.load(pathlib.Path(d) / "none.json")

    def test_the_caller_ai_is_asked_with_the_conversation_so_far(self):
        sent = []

        def http(method, url, headers, body=None):
            sent.append((method, url, headers, body))
            return 200, {"output": [{"type": "reasoning"}, {"type": "message", "content": [
                {"type": "output_text", "text": " 焼肉ほのか博多店の田中です。 "}]}],
                "usage": {"input_tokens": 900, "output_tokens": 40, "input_tokens_details": {"cached_tokens": 300}}}
        p = C.load()[0]
        text, usage = C.caller_turn({"OPENAI_API_KEY": "sk-o"}, http, p, [("ai", "お電話ありがとうございます。"),
                                                                         ("caller", "清掃をお願いします。")])
        self.assertEqual(text, "焼肉ほのか博多店の田中です。")
        self.assertEqual(usage, {"input_tokens": 900, "cached_tokens": 300, "output_tokens": 40})
        method, url, headers, body = sent[0]
        self.assertEqual((method, url), ("POST", C.RESPONSES_URL))
        self.assertEqual(headers["Authorization"], "Bearer sk-o")
        self.assertEqual(body["model"], C.CALLER_MODEL)
        self.assertFalse(body["store"])
        self.assertEqual(body["input"], [{"role": "user", "content": "お電話ありがとうございます。"},
                                         {"role": "assistant", "content": "清掃をお願いします。"}])
        self.assertIn(current_tenant().company_name, body["instructions"])
        self.assertIn("090-1234-5678", body["instructions"])     # what the caller knows
        self.assertIn(C.HANG_UP, body["instructions"])
        _, _ = C.caller_turn({"OPENAI_API_KEY": "sk-o"}, http, p, [])
        self.assertEqual(sent[1][3]["input"][0]["content"], "（電話がつながりました）")


class RunnerTest(unittest.TestCase):
    def wait(self, r):
        for _ in range(200):
            if r.status()["status"] != "running":
                return r.status()
            time.sleep(0.01)
        self.fail("the run did not finish")

    def test_runs_the_chosen_patterns_in_the_background_and_reports_them(self):
        seen = []

        def one(lab, p, llm):
            seen.append((p["id"], llm))
            return {"id": p["id"], "title": p["title"], "ok": p["id"] == "P01", "call_id": f"c{p['id']}", "reason": "end",
                    "checks": [{"name": "番号", "ok": p["id"] == "P01", "detail": "保存：なし"}],
                    "transcript": [("ai", "お電話ありがとうございます。"), ("caller", "清掃です。")], "tools": [{"name": "save_fields"}]}
        lab = type("L", (), {"env": dict(ENV)})()
        r = runner.Runner(lab, run_one=one)
        self.assertEqual(r.status(), {"status": "idle"})
        r.start(["P01", "P03"], "gpt-6-luna")
        st = self.wait(r)
        self.assertEqual(seen, [("P01", "gpt-6-luna"), ("P03", "gpt-6-luna")])
        self.assertEqual((st["status"], st["total"], len(st["results"])), ("done", 2, 2))
        text = runner.report_text(st)
        self.assertIn("2件中 1件 合格", text)
        self.assertIn("■ P03", text)
        self.assertIn("× 番号（保存：なし）", text)
        self.assertIn("AI：お電話ありがとうございます。", text)
        self.assertIn("業務処理：save_fields", text)

    def test_what_is_refused_before_anything_is_paid(self):
        lab = type("L", (), {"env": {"ELEVENLABS_API_KEY": KEY}})()
        r = runner.Runner(lab, run_one=lambda *a: self.fail("must not run"))
        with self.assertRaisesRegex(ValueError, "OPENAI_API_KEY"):
            r.start(None, None)
        lab.env["OPENAI_API_KEY"] = "sk"
        with self.assertRaisesRegex(ValueError, "P99"):
            r.start(["P99"], None)
        self.assertEqual(r.status(), {"status": "idle"})

    def test_a_pattern_that_cannot_start_stops_the_run(self):
        def one(lab, p, llm):
            raise RuntimeError("ElevenLabs: HTTP 401")
        lab = type("L", (), {"env": dict(ENV)})()
        r = runner.Runner(lab, run_one=one)
        r.start(None, None)
        st = self.wait(r)
        self.assertEqual(st["status"], "stopped")
        self.assertEqual(len(st["results"]), 1)
        self.assertIn("HTTP 401", st["results"][0]["checks"][0]["detail"])

    def test_stop_waits_for_the_conversation_then_ends_the_run(self):
        r = None

        def one(lab, p, llm):
            r.stop()
            return {"id": p["id"], "title": p["title"], "ok": True, "call_id": None, "reason": "", "checks": [],
                    "transcript": [], "tools": []}
        lab = type("L", (), {"env": dict(ENV)})()
        r = runner.Runner(lab, run_one=one)
        r.start(None, None)
        st = self.wait(r)
        self.assertEqual((st["status"], len(st["results"])), ("stopped", 1))
        self.assertIn("ended_at", st)
        with self.assertRaisesRegex(ValueError, "いま動いています"):
            r.run["status"] = "running"
            r.start(None, None)


class WebSocketClientTest(unittest.TestCase):
    """The standard-library WebSocket the test talks to the agent with, against a small local server: a ping is
    answered, a message in two frames is put together, the client's frames are masked, a close is reported."""

    def test_talks_text_frames_with_a_server(self):
        srv = socket.socket()
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        port, seen = srv.getsockname()[1], {}

        def frame(op, data, fin=True):
            return bytes([(0x80 if fin else 0) | op, len(data)]) + data

        def read_frame(conn):
            b1, b2 = conn.recv(1)[0], conn.recv(1)[0]
            n = b2 & 0x7F
            if n == 126:
                n = struct.unpack(">H", conn.recv(2))[0]
            mask = conn.recv(4)
            data = b""
            while len(data) < n:
                data += conn.recv(n - len(data))
            return b1 & 0x0F, bool(b2 & 0x80), bytes(b ^ mask[i % 4] for i, b in enumerate(data))

        def serve():
            conn, _ = srv.accept()
            head = b""
            while b"\r\n\r\n" not in head:
                head += conn.recv(1)
            seen["request"] = head.decode()
            conn.sendall(b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n\r\n")
            conn.sendall(frame(0x9, b"hi"))
            seen["pong"] = read_frame(conn)
            msg = json.dumps({"type": "agent_response", "agent_response_event": {"agent_response": "お電話ありがとうございます。"}},
                             ensure_ascii=False).encode()
            conn.sendall(frame(0x1, msg[:10], fin=False) + frame(0x0, msg[10:]))
            op, masked, data = read_frame(conn)
            seen["client"] = (op, masked, json.loads(data))
            conn.sendall(frame(0x8, struct.pack(">H", 1000) + "おわり".encode()))
            conn.close()
        t = threading.Thread(target=serve, daemon=True)
        t.start()
        ws = WebSocket(f"ws://127.0.0.1:{port}/v1/convai/conversation?agent_id=a&token=t", timeout_s=3)
        try:
            got = ws.recv_json(3)
            self.assertEqual(got["agent_response_event"]["agent_response"], "お電話ありがとうございます。")
            self.assertEqual(seen["pong"], (0xA, True, b"hi"))
            ws.send_json({"type": "user_message", "text": "清掃をお願いします。"})
            with self.assertRaisesRegex(ConnectionError, "1000"):
                ws.recv_json(3)
            with self.assertRaises(ConnectionError):
                ws.recv_json(0.1)
        finally:
            ws.close()
            t.join(3)
            srv.close()
        self.assertIn("GET /v1/convai/conversation?agent_id=a&token=t HTTP/1.1", seen["request"])
        self.assertEqual(seen["client"], (0x1, True, {"type": "user_message", "text": "清掃をお願いします。"}))

    def test_nothing_in_time_is_none_and_a_refused_upgrade_is_an_error(self):
        srv = socket.socket()
        srv.bind(("127.0.0.1", 0))
        srv.listen(2)
        port = srv.getsockname()[1]

        def serve():
            for answer in (b"HTTP/1.1 101 Switching Protocols\r\n\r\n", b"HTTP/1.1 403 Forbidden\r\n\r\n"):
                conn, _ = srv.accept()
                head = b""
                while b"\r\n\r\n" not in head:
                    head += conn.recv(1)
                conn.sendall(answer)
                if b"101" in answer:
                    conn.recv(64)       # wait until the client closes
                conn.close()
        t = threading.Thread(target=serve, daemon=True)
        t.start()
        try:
            ws = WebSocket(f"ws://127.0.0.1:{port}/", timeout_s=3)
            self.assertIsNone(ws.recv_json(0.1))
            ws.close()
            with self.assertRaisesRegex(ConnectionError, "403"):
                WebSocket(f"ws://127.0.0.1:{port}/", timeout_s=3)
        finally:
            t.join(3)
            srv.close()


if __name__ == "__main__":
    unittest.main()
