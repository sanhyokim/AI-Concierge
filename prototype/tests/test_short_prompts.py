"""The short conversation prompt (2026-10-14). ChatGPT's voice sounded more fluent than the lab on the same model;
the lab gave the voice model about 4,700 characters. OpenAI's GPT-Live guide: keep the conversation prompt short
and put detailed workflows in the backend. Google's Live API guide: no multi-page prompts. GPT-Live's voice model
now gets the role, the speaking style and when to ask its backend; the backend model gets the steps, the dates
and the FAQ. Gemini (no backend model) gets the short prompt with short tool rules; the FAQ comes from lookup_faq."""
import json
import pathlib
import tempfile
import unittest

from prototype.browser_lab import config
from prototype.browser_lab.config import LAB_PERSONA
from prototype.browser_lab.server import Lab
from prototype.concierge.prompts import VOICE_RULES
from prototype.reception.service import ReceptionService
from prototype.reception.store import Store
from prototype.tests.test_browser_lab import FakeHttp
from prototype.tests.test_manners import FORBIDDEN

ROOT = pathlib.Path(__file__).resolve().parents[2]

TOOL_NAMES = ("save_field", "save_fields", "request_readback", "confirm_field", "lookup_faq", "flag_emergency",
              "offer_line_number", "end_call")


def service():
    return ReceptionService(Store(":memory:"), env={})


class PromptTextTest(unittest.TestCase):
    def setUp(self):
        self.svc = service()
        self.cid = self.svc.start_call(at="2026-10-14T13:00", caller_id="092-504-2185")["id"]
        self.full = self.svc.instructions(self.cid, preamble=config.LAB_RULES)

    def test_gpt_live_voice_model_gets_a_short_prompt_without_tools_faq_or_digits(self):
        voice = self.svc.voice_instructions(self.cid, LAB_PERSONA, backend=True)
        self.assertLess(len(voice), 2000)
        self.assertLess(len(voice), len(self.full) / 2)
        for words in ("0は「ゼロ」と言います", "ほかにご用件はございますでしょうか。", "失礼いたします。」と言い、裏方に",
                      "【裏方への頼み方】", "「そのまま読む文：」は、一字一句変えずに読みます", "「次にすること：」は声に出さず",
                      "回線の番号が届いています", "【今日】2026年10月14日（水）"):
            self.assertIn(words, voice)
        for name in TOOL_NAMES:   # the voice model cannot call tools; its backend does
            self.assertNotIn(name, voice)
        self.assertNotIn("点検は無料です。", voice)
        self.assertNotIn("【日付の早見表】", voice)
        self.assertNotIn("504", voice)

    def test_gpt_live_backend_gets_the_steps_dates_and_faq(self):
        backend = self.svc.backend_instructions(self.cid)
        for words in ("「そのまま読む文：」", "save_field", "save_fields", "read_back_later", "lookup_faq",
                      "flag_emergency", "end_call", "offer_line_number を呼び", "【日付の早見表】",
                      "- 10/16（金）：明後日・今週の金曜", "点検は無料です。"):
            self.assertIn(words, backend)
        self.assertNotIn("504", backend)   # the number itself comes only from the tool

    def test_gemini_gets_short_tool_rules_and_the_dates_but_not_the_faq(self):
        text = self.svc.voice_instructions(self.cid, LAB_PERSONA, backend=False)
        self.assertLess(len(text), 2600)
        for words in ("0は「ゼロ」と言います", "失礼いたします。」と言い、end_call を呼びます", "必ず lookup_faq を呼び",
                      "read_back_now.say を一字一句そのまま読みます", "まず offer_line_number を呼び",
                      "- 10/16（金）：明後日・今週の金曜", "save_field には、2026-10-16"):
            self.assertIn(words, text)
        self.assertNotIn("点検は無料です。", text)
        self.assertNotIn("裏方", text)

    def test_a_withheld_number_is_said_in_every_prompt(self):
        cid = self.svc.start_call(at="2026-10-14T13:00")["id"]
        self.assertIn("この電話は非通知です", self.svc.voice_instructions(cid, LAB_PERSONA, backend=True))
        self.assertIn("offer_line_number は呼びません", self.svc.voice_instructions(cid, LAB_PERSONA, backend=False))
        self.assertIn("offer_line_number は呼びません", self.svc.backend_instructions(cid))

    def test_the_example_phrases_avoid_the_forbidden_words(self):
        rules = VOICE_RULES.split("\n")
        start = next(i for i, line in enumerate(rules) if line.startswith("- 使わない言葉"))
        listed = {start, start + 1} | {i for i, line in enumerate(rules) if line.startswith("- 数字の0")}
        for i, line in enumerate(rules):
            if i not in listed:
                for word in FORBIDDEN:
                    self.assertNotIn(word, line, line)


class LabPromptTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = pathlib.Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def lab(self, env, responses):
        return Lab(env=env, http=FakeHttp(responses), ledger_path=self.dir / "ledger.jsonl",
                   results_dir=self.dir / "results")

    def test_gpt_live_sends_the_short_prompt_to_the_voice_and_the_steps_to_the_backend(self):
        lab = self.lab({"OPENAI_API_KEY": "k"}, {"live/sessions": {"transport": {"sdp": "answer"}}})
        s = lab.start("gpt-live-1")
        lab.sdp(s["session_id"], "offer")
        body = lab.http.calls[-1]["body"]["session"]
        self.assertEqual(body["instructions"], lab.service.voice_instructions(s["call_id"], LAB_PERSONA, backend=True))
        self.assertEqual(body["delegation"]["responses"]["instructions"], lab.service.backend_instructions(s["call_id"]))
        self.assertIn("短い指示：声のモデルへ", s["applied"]["instructions"])
        self.assertIn("裏方のモデルへ", s["applied"]["instructions"])

    def test_gemini_setup_carries_the_short_prompt(self):
        lab = self.lab({"GEMINI_API_KEY": "secret-gemini-key"}, {"auth_tokens": {"name": "auth_tokens/abc"}})
        s = lab.start("gemini-3.8-live")
        text = s["credentials"]["setup"]["setup"]["systemInstruction"]["parts"][0]["text"]
        self.assertEqual(text, lab.service.voice_instructions(s["call_id"], LAB_PERSONA, backend=False))
        self.assertIn("FAQは lookup_faq で渡す", s["applied"]["instructions"])
        self.assertNotIn("secret-gemini-key", json.dumps(s))

    def test_held_candidates_keep_the_full_prompt(self):
        self.assertNotIn("prompt_style", config.CANDIDATES["gpt-realtime-2.1"])
        self.assertNotIn("prompt_style", config.CANDIDATES["elevenagents"])


if __name__ == "__main__":
    unittest.main()


class OpeningTest(unittest.TestCase):
    """2026-10-20: the caller had to speak first (GPT-Live did not greet on its own; A-01 was only logged)."""

    def test_the_page_plays_the_recorded_opening_and_the_models_do_not_greet(self):
        from prototype.browser_lab import config, vendors
        from prototype.concierge.prompts import OPENING_ANNOUNCEMENT, OPENING_NOTE
        creds = vendors.mint(config.CANDIDATES["gpt-live-1"], {"OPENAI_API_KEY": "k"})
        self.assertTrue(creds["opening"].startswith(OPENING_ANNOUNCEMENT))
        self.assertTrue(creds["opening"].endswith("どのようなご用件でしょうか。"))
        self.assertIn("録音いたします", creds["opening"])                 # A-01 (spec 4-2)
        self.assertIn(OPENING_NOTE, config.LAB_PERSONA)                     # the AI knows the opening was played
        static = ROOT / "prototype" / "browser_lab" / "static"
        for name in ("gpt-live.js", "gemini-live.js"):
            self.assertNotIn("あいさつをしてください", (static / "adapters" / name).read_text(encoding="utf-8"))
        self.assertIn("playOpening(st.session.credentials.opening)", (static / "lab.js").read_text(encoding="utf-8"))
