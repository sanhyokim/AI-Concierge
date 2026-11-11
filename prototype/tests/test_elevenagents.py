"""ElevenAgents, the three-stage path (2026-11-03, user: 「「聞き取り → 考えるAI → 読み上げ」の3段の作りで一本化が
もっとも安定しそうですね。あなたの提案で進めましょう」, then 「OpenAI GPT-Live 1とElevenAgentsで検証します。他のモデルは
当面使いません」). The server makes the lab's tools and agent with the API, updates the agent for each call (prompt,
thinking AI, voice) and hands the page a signed URL. Nothing here reaches ElevenLabs: the HTTP is scripted."""
import json
import pathlib
import re
import tempfile
import unittest

from prototype.browser_lab import config, elevenagents as ea, vendors
from prototype.browser_lab.config import LAB_PERSONA_TEXT
from prototype.browser_lab.server import Lab, SessionLimit, api_get
from prototype.concierge.prompts import GOODBYE, OPENING_NOTE, for_speech
from prototype.reception.service import ReceptionService
from prototype.reception.store import Store
from prototype.reception.tooldefs import RECEPTION_TOOLS
from prototype.tests.test_browser_lab import Clock
from prototype.tests.test_manners import FORBIDDEN

KEY = "sk_secret_eleven_key"


class ElevenHttp:
    """ElevenLabs as scripted answers: (method, part of the URL) → a dict, or an HTTP status to fail with."""

    def __init__(self, fail=None):
        self.calls, self.fail, self.n = [], dict(fail or {}), 0

    def __call__(self, method, url, headers, body=None):
        self.calls.append({"method": method, "url": url, "headers": headers, "body": body})
        for (m, part), status in list(self.fail.items()):
            if m == method and part in url:
                if isinstance(status, list):            # fail only the first times
                    status = status.pop(0) if status else None
                    if status is None:
                        continue
                raise vendors.VendorError(f"{url}: HTTP {status} refused", status)
        if method == "POST" and url.endswith("/convai/tools"):
            self.n += 1
            return 200, {"id": f"tool_{self.n}", "tool_config": body["tool_config"]}
        if method == "PATCH" and "/convai/tools/" in url:
            return 200, {"id": url.rsplit("/", 1)[1]}
        if method == "POST" and url.endswith("/agents/create"):
            return 200, {"agent_id": f"agent_{len([c for c in self.calls if c['url'].endswith('/agents/create')])}"}
        if method == "PATCH" and "/convai/agents/" in url:
            return 200, {"agent_id": url.rsplit("/", 1)[1]}
        if "get-signed-url" in url:
            return 200, {"signed_url": "wss://api.elevenlabs.io/v1/convai/conversation?agent_id=a&token=t"}
        if "/convai/conversations/" in url:
            return 200, {"status": "done", "metadata": {"call_duration_secs": 95, "cost": 640,
                                                       "charging": {"call_charge": 600, "llm_charge": 40,
                                                                    "llm_price": 0.021, "tier": "starter"}}}
        raise AssertionError(f"unexpected {method} {url}")

    def of(self, method, part):
        return [c for c in self.calls if c["method"] == method and part in c["url"]]


class ElevenAgentsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = pathlib.Path(self.tmp.name)
        self.state = self.dir / "elevenagents.json"

    def tearDown(self):
        self.tmp.cleanup()

    def lab(self, http=None, env=None, service=None):
        env = {"ELEVENLABS_API_KEY": KEY, **(env or {})}
        lab = Lab(env=env, http=http or ElevenHttp(), ledger_path=self.dir / "ledger.jsonl",
                  results_dir=self.dir / "results", service=service or ReceptionService(Store(":memory:"), env=env),
                  clock=Clock(), agent_state=self.state)
        lab.spawn = lambda fn: fn()          # the usage record is fetched inline
        lab.sleep = lambda s: None
        return lab

    def test_first_call_makes_the_tools_and_the_agent_then_hands_a_signed_url(self):
        lab = self.lab()
        s = lab.start("elevenagents", caller_id="092-504-2185")
        http = lab.http
        made = http.of("POST", "/convai/tools")
        self.assertEqual(len(made), len(RECEPTION_TOOLS))
        for call, tool in zip(made, RECEPTION_TOOLS):
            conf = call["body"]["tool_config"]
            self.assertEqual((conf["type"], conf["name"]), ("client", tool["name"]))
            self.assertEqual(conf["pre_tool_speech"], "off")       # nothing said before the tool (2026-10-30)
            self.assertTrue(conf["expects_response"])
            for prop in conf["parameters"]["properties"].values():
                self.assertTrue(prop["description"])               # ElevenLabs asks for one per parameter
        level = next(c for c in made if c["body"]["tool_config"]["name"] == "flag_emergency")
        self.assertIn("obvious", level["body"]["tool_config"]["parameters"]["properties"]["level"]["description"])
        create = http.of("POST", "/agents/create")[0]["body"]
        agent = create["conversation_config"]["agent"]
        self.assertEqual(agent["first_message"], for_speech(ea.OPENING_ANNOUNCEMENT + ea.OPENING_QUESTION))
        self.assertEqual(agent["language"], "ja")
        self.assertTrue(agent["disable_first_message_interruptions"])
        prompt = agent["prompt"]
        self.assertEqual(prompt["llm"], "gpt-5.6-terra")           # the default thinking AI
        self.assertEqual(prompt["reasoning_effort"], "low")
        self.assertEqual(prompt["tool_ids"], [f"tool_{i + 1}" for i in range(len(RECEPTION_TOOLS))])
        self.assertIsNone(prompt["built_in_tools"]["end_call"])    # ours is the end_call the page handles
        self.assertEqual(prompt["prompt"], lab.service.text_instructions(s["call_id"], LAB_PERSONA_TEXT))
        self.assertEqual(create["conversation_config"]["tts"]["model_id"], "eleven_v4_turbo")
        self.assertEqual(create["conversation_config"]["asr"]["user_input_audio_format"], "pcm_16000")
        self.assertIn("client_tool_call", create["conversation_config"]["conversation"]["client_events"])
        self.assertTrue(create["platform_settings"]["auth"]["enable_auth"])   # only the signed URL gets in
        creds = s["credentials"]
        self.assertTrue(creds["signed_url"].startswith("wss://api.elevenlabs.io/"))
        self.assertEqual((creds["agent_id"], creds["llm"], creds["output_rate"], creds["input_rate"]),
                         ("agent_1", "gpt-5.6-terra", 24000, 16000))
        self.assertNotIn(KEY, json.dumps(s))                       # the key never reaches the page
        self.assertIn("考えるAI（gpt-5.6-terra）", s["applied"]["instructions"])
        saved = json.loads(self.state.read_text(encoding="utf-8"))
        self.assertEqual(saved["agent_id"], "agent_1")
        self.assertEqual(set(saved["tools"]), {t["name"] for t in RECEPTION_TOOLS})

    def test_later_calls_only_update_the_agent(self):
        lab = self.lab()
        lab.start("elevenagents")
        n = len(lab.http.calls)
        s = lab.start("elevenagents", llm="claude-sonnet-5-5")
        later = lab.http.calls[n:]
        self.assertEqual([c["method"] for c in later], ["PATCH", "GET"])
        self.assertTrue(later[0]["url"].endswith("/convai/agents/agent_1"))
        prompt = later[0]["body"]["conversation_config"]["agent"]["prompt"]
        self.assertEqual(prompt["llm"], "claude-sonnet-5-5")
        self.assertNotIn("reasoning_effort", prompt)               # only the OpenAI models get it
        self.assertEqual(s["llm"], "claude-sonnet-5-5")
        with self.assertRaises(ValueError):
            lab.start("elevenagents", llm="gemini-3.8-flash")      # not offered (too slow to answer)

    def test_a_changed_tool_is_updated_and_a_deleted_one_made_again(self):
        lab = self.lab()
        lab.start("elevenagents")
        state = json.loads(self.state.read_text(encoding="utf-8"))
        state["tools"]["lookup_faq"]["digest"] = "old"
        state["tools"]["end_call"]["digest"] = "old"
        self.state.write_text(json.dumps(state), encoding="utf-8")
        end_id = state["tools"]["end_call"]["id"]
        lab.http.fail[("PATCH", f"/convai/tools/{end_id}")] = 404  # deleted on ElevenLabs' screen
        lab.start("elevenagents")
        self.assertEqual(len(lab.http.of("PATCH", "/convai/tools/")), 2)
        remade = lab.http.of("POST", "/convai/tools")[-1]["body"]["tool_config"]["name"]
        self.assertEqual(remade, "end_call")
        again = json.loads(self.state.read_text(encoding="utf-8"))
        self.assertNotEqual(again["tools"]["end_call"]["id"], end_id)

    def test_a_deleted_agent_is_made_again(self):
        lab = self.lab()
        lab.start("elevenagents")
        lab.http.fail[("PATCH", "/convai/agents/agent_1")] = 404
        s = lab.start("elevenagents")
        self.assertEqual(s["credentials"]["agent_id"], "agent_2")
        self.assertEqual(json.loads(self.state.read_text(encoding="utf-8"))["agent_id"], "agent_2")

    def test_an_agent_id_given_on_the_pc_is_used_and_never_replaced(self):
        lab = self.lab(env={"ELEVENLABS_AGENT_ID": "mine"})
        s = lab.start("elevenagents")
        self.assertEqual(s["credentials"]["agent_id"], "mine")
        self.assertEqual(lab.http.of("POST", "/agents/create"), [])
        self.assertNotIn("agent_id", json.loads(self.state.read_text(encoding="utf-8")))

    def test_a_refused_setting_falls_back_to_the_minimal_agent_and_says_so(self):
        http = ElevenHttp(fail={("POST", "/agents/create"): [422]})
        s = self.lab(http).start("elevenagents")
        bodies = [c["body"] for c in http.of("POST", "/agents/create")]
        self.assertEqual(len(bodies), 2)
        minimal = bodies[1]["conversation_config"]
        self.assertNotIn("asr", minimal)
        self.assertNotIn("reasoning_effort", minimal["agent"]["prompt"])
        self.assertEqual(minimal["agent"]["first_message"], ea.greeting())
        self.assertIn("HTTP 422", s["credentials"]["agent_fallback"]["reason"])

    def test_a_key_without_the_agents_permission_is_explained(self):
        http = ElevenHttp(fail={("POST", "/convai/tools"): 401})
        lab = self.lab(http)
        with self.assertRaises(vendors.VendorError) as cm:
            lab.start("elevenagents")
        self.assertIn("ElevenAgents", str(cm.exception))
        self.assertIn("書き込み", str(cm.exception))
        self.assertNotIn(KEY, str(cm.exception))
        self.assertEqual(lab.ledger("elevenlabs_lab").totals()["held_usd"], 0.0)   # nothing reserved stays

    def test_the_voice_registered_for_eleven_realtime_is_used(self):
        svc = ReceptionService(Store(":memory:"), env={})
        _, cfg, _ = svc.config()
        cfg["voices"].append({"id": "v-jp", "candidate": "eleven-realtime-max", "voice": "MXKtCrra8fvlDUbfKUT1",
                              "label": "日本語の女性"})
        svc.save_config(cfg, "tester")
        lab = self.lab(service=svc)
        choices = lab.voice_choices("elevenagents")
        self.assertEqual([c["key"] for c in choices["choices"]], ["v-jp"])
        s = lab.start("elevenagents", voice_key="v-jp")
        tts = lab.http.of("POST", "/agents/create")[0]["body"]["conversation_config"]["tts"]
        self.assertEqual(tts["voice_id"], "MXKtCrra8fvlDUbfKUT1")
        self.assertEqual(s["voice"], "MXKtCrra8fvlDUbfKUT1")

    def test_elevenlabs_own_record_of_the_call_is_kept(self):
        lab = self.lab()
        s = lab.start("elevenagents")
        lab.vendor_event(s["session_id"], "vendor_session", {"conversation_id": "conv_123"})
        lab.end(s["session_id"], 95.0, "試験者が終了")
        got = lab.http.of("GET", "/convai/conversations/conv_123")
        self.assertEqual(len(got), 1)
        events = lab.service.call_view(s["call_id"])["events"]
        usage = next(e for e in events if e["kind"] == "ai_usage")["data"]
        self.assertEqual((usage["source"], usage["credits"], usage["llm_usd"]), ("elevenlabs", 640, 0.021))
        row = lab._row(s["session_id"])
        self.assertIn("vendor_record", row["usage"])

    def test_the_record_is_asked_again_while_elevenlabs_processes_it(self):
        http = ElevenHttp()
        answers = iter([{"status": "processing"}, {"status": "done", "metadata": {"call_duration_secs": 60}}])
        base = http.__call__

        def call(method, url, headers, body=None):
            if "/convai/conversations/" in url:
                http.calls.append({"method": method, "url": url, "headers": headers, "body": body})
                return 200, next(answers)
            return base(method, url, headers, body)
        lab = self.lab(call)
        s = lab.start("elevenagents")
        lab.vendor_event(s["session_id"], "vendor_session", {"conversation_id": "c9"})
        lab.end(s["session_id"], 60.0)
        self.assertEqual(len(http.of("GET", "/convai/conversations/c9")), 2)


class TextPromptTest(unittest.TestCase):
    """Fewer rules (2026-11-03): the read-backs and fixed lines come from the tools; the talk is the model's."""

    def setUp(self):
        self.svc = ReceptionService(Store(":memory:"), env={})
        self.cid = self.svc.start_call(at="2026-11-03T13:00", caller_id="092-504-2185")["id"]
        self.text = self.svc.text_instructions(self.cid, LAB_PERSONA_TEXT)

    def test_short_with_the_rules_that_matter(self):
        self.assertLess(len(self.text), 2500)   # 2026-11-11: the request saved as a short noun phrase
        for words in ("一字一句そのまま", "read_back_now.say", "confirm_field", "request_summary", "end_call",
                      "lookup_faq", "offer_line_number", "【日付の早見表】", "記号・箇条書き",
                      for_speech(GOODBYE)):
            self.assertIn(words, self.text)
        self.assertNotIn(for_speech(OPENING_NOTE.split("。")[0]), self.text)   # ElevenLabs says the opening
        self.assertNotIn("点検は無料です。", self.text)                       # the FAQ comes from lookup_faq
        self.assertNotIn("504", self.text)                                   # the number comes from the tool

    def test_the_words_it_is_given_to_say_use_none_of_the_forbidden_words(self):
        lines = self.text.split("\n")
        i = next(n for n, line in enumerate(lines) if "使わない言葉：" in line)
        rest = "\n".join(lines[:i] + lines[i + 2:])            # the list of words not to use, and its second line
        said = [q for q in re.findall(r"「([^」]*)」", rest) if q != "もしもし"]   # 「もしもし」 is the caller's
        self.assertIn(for_speech(GOODBYE), said)
        for word in FORBIDDEN:
            for q in said:
                self.assertNotIn(word, q, q)


class PageTest(unittest.TestCase):
    def test_the_page_offers_gpt_live_and_elevenagents_with_the_thinking_ais(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        d = pathlib.Path(tmp.name)
        lab = Lab(env={"OPENAI_API_KEY": "k", "ELEVENLABS_API_KEY": KEY}, ledger_path=d / "l.jsonl",
                  results_dir=d / "r", agent_state=d / "a.json")
        listed = {c["id"]: c for c in api_get(lab, "/api/candidates")["candidates"]}
        self.assertEqual(set(listed), {"fake", "gpt-live-1", "elevenagents"})   # user, 2026-11-03
        llms = listed["elevenagents"]["llms"]
        self.assertEqual(llms["default"], "gpt-5.6-terra")
        self.assertEqual([c["id"] for c in llms["choices"]], [c["id"] for c in ea.LLM_CHOICES])
        terra = next(c for c in llms["choices"] if c["id"] == "gpt-5.6-terra")
        luna = next(c for c in llms["choices"] if c["id"] == "gpt-6-luna")
        self.assertGreater(terra["call_yen"], luna["call_yen"])
        self.assertNotIn("llms", listed["gpt-live-1"])
        # the two cards are compared: GPT-Live's counts its backend model too (Terra: $0.15 + $0.075 for 3 minutes)
        from prototype.browser_lab import config
        from prototype.cost import cost_model as cm
        live_usd = 0.05 * 3 + config.backend_usd("gpt-6.1-terra", config.BACKEND_TYPICAL)
        self.assertEqual(listed["gpt-live-1"]["call_yen"], round(live_usd * (1 + cm.TAX) * cm.FX))
        self.assertNotIn(KEY, json.dumps(listed))

    def test_the_others_are_not_used_for_now(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        d = pathlib.Path(tmp.name)
        lab = Lab(env={"GEMINI_API_KEY": "g", "XAI_API_KEY": "x", "PHONIC_API_KEY": "p"}, ledger_path=d / "l.jsonl",
                  results_dir=d / "r", agent_state=d / "a.json")
        for cid in config.PAUSED:
            with self.assertRaises(SessionLimit) as cm:
                lab.start(cid)
            self.assertIn("当面使わない", str(cm.exception))
        self.assertEqual(lab.store.q1("SELECT COUNT(*) AS c FROM lab_sessions")["c"], 0)

    def test_the_cost_per_minute_matches_the_calculator_figures(self):
        # ElevenLabs' calculator (2026-09-19): GPT-5 $0.0119, Claude Haiku 4.5 $0.0086, GPT-5.5 $0.0446 per minute
        for pin, pout, per_min in ((1.25, 10.0, 0.0119), (1.0, 5.0, 0.0086), (5.0, 30.0, 0.0446)):
            est = (ea.TOKENS_PER_MIN[0] * pin + ea.TOKENS_PER_MIN[1] * pout) / 1e6
            self.assertAlmostEqual(est, per_min, delta=0.0002)


if __name__ == "__main__":
    unittest.main()
