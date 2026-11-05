"""The company settings (design memo 2026-11-05, Boss): everything that changes from one company to the next is read
from one file per company, and the instructions are built from it.

- Noda's file gives the same words as before the move (the order, the questions, the opening and closing lines).
- A file with a mistake is refused with a message that says what to fix.
- With another company's file, no word of Noda's (its name, trade, examples) reaches anything the AI is told, and
  that company's order and questions are used.
"""
import ast
import json
import os
import pathlib
import re
import subprocess
import sys
import unittest

from prototype import tenants
from prototype.concierge.prompts import ASK_FOR, GOODBYE, OPENING_ANNOUNCEMENT, VOICE_RULES
from prototype.tenants import TenantError, from_dict

ROOT = pathlib.Path(__file__).resolve().parents[2]
FIXTURE = pathlib.Path(__file__).resolve().parent / "fixtures" / "tenant_midori.json"
NODA_WORDS = re.compile(r"野田|焼肉|ロースター|排煙|ダクト|フード|厨房|ほのか|上引き|うわびき|田中|何基")


def noda_data() -> dict:
    return json.loads((ROOT / "prototype" / "tenants" / "noda.json").read_text(encoding="utf-8"))


class NodaSettingsTest(unittest.TestCase):
    def test_the_default_company_is_noda(self):
        t = tenants.load("noda")
        self.assertEqual(t.company_name, "株式会社野田")
        # the user's order (2026-10-23): the request and the date first, then the shop and the name, then the number
        self.assertEqual(t.order, ("request", "preferred_datetime", "shop_name", "caller_name", "callback_number"))
        self.assertEqual(t.ask_names(), ["ご用件", "ご希望の日時", "店舗名とお名前", "折り返しのお電話番号"])

    def test_the_decided_words_are_unchanged(self):
        self.assertEqual(ASK_FOR["preferred_datetime"], "作業にお伺いするご希望の日時はございますか。")   # 2026-10-30
        self.assertTrue(OPENING_ANNOUNCEMENT.startswith("お電話ありがとうございます。株式会社野田、AIオペレーターでございます。"))
        self.assertEqual(GOODBYE, "お電話ありがとうございました。株式会社野田、AIオペレーターがお受けいたしました。失礼いたします。")
        self.assertIn("1. ご用件：「どのようなご用件でしょうか。」", VOICE_RULES)
        self.assertIn("3. 店舗名とお名前：「恐れ入りますが、店舗名とお名前をお伺いしてもよろしいでしょうか。」", VOICE_RULES)

    def test_fill_replaces_only_the_names_it_knows(self):
        t = tenants.load("noda")
        self.assertEqual(t.fill("{company}は{end}で{summary}"), "株式会社野田は{end}で{summary}")


class CheckingTest(unittest.TestCase):
    def assertRefused(self, data, words):
        with self.assertRaises(TenantError) as cm:
            from_dict(data)
        self.assertIn(words, str(cm.exception))

    def test_noda_file_is_accepted(self):
        from_dict(noda_data())

    def test_a_missing_item_is_refused(self):
        d = noda_data()
        d["intake_fields"] = [f for f in d["intake_fields"] if f["key"] != "callback_number"]
        self.assertRefused(d, "intake_fields")

    def test_an_item_without_its_question_is_refused(self):
        d = noda_data()
        del d["intake_fields"][0]["question"]
        self.assertRefused(d, "question")

    def test_a_company_without_its_name_is_refused(self):
        d = noda_data()
        d["tenant"]["company_name"] = ""
        self.assertRefused(d, "company_name")

    def test_asked_with_must_name_an_item(self):
        d = noda_data()
        d["intake_fields"][3]["asked_with"] = "shop"
        self.assertRefused(d, "asked_with")

    def test_a_follow_up_cannot_reuse_a_key(self):
        d = noda_data()
        d["follow_ups"][0]["key"] = "request"
        self.assertRefused(d, "follow_ups")

    def test_a_count_follow_up_needs_its_units(self):
        d = noda_data()
        d["follow_ups"][1]["count_units"] = []
        self.assertRefused(d, "count_units")

    def test_a_bad_name_or_a_missing_file_is_refused(self):
        with self.assertRaises(TenantError):
            tenants.path_for("../noda")
        with self.assertRaises(TenantError):
            tenants.load("no-such-company")


# Run in a separate process with AI_TENANT set: the settings are read once, when the modules are first imported.
PROBE = r"""
import json, sys
from prototype.admin import server as admin_server
from prototype.browser_lab import config, elevenagents, vendors
from prototype.browser_lab import server as lab_server
from prototype.concierge import prompts
from prototype.reception import faq, fake_agent, settings
from prototype.reception.service import ReceptionService, say_only_rules
from prototype.reception.store import Store

out = {"voice_rules": prompts.voice_rules("END", "SUMMARY"), "say_only": say_only_rules(prompts.voice_rules("E", "S")),
       "greeting": prompts.GREETING, "opening": prompts.OPENING_ANNOUNCEMENT, "goodbye": prompts.GOODBYE,
       "lab": config.LAB_INSTRUCTIONS, "persona": config.LAB_PERSONA, "persona_text": config.LAB_PERSONA_TEXT,
       "keywords": vendors.GROK_KEYTERMS + elevenagents.KEYWORDS, "agent": elevenagents.AGENT_NAME,
       "demo": admin_server.DEMO_SCRIPT, "fake_scripts": lab_server.FAKE_SCRIPTS, "faq_seeds": settings.FAQ_SEEDS,
       "fake_ask": fake_agent.ASK, "fake_order": list(fake_agent.ORDER),
       "fake_creds": vendors.mint({"adapter": "fake"}, {}, script=[])}
for name in ("TOOL_RULES", "DIRECT_TOOL_RULES", "SAY_ONLY_RULES", "SAY_ONLY_ASKING", "TEXT_AGENT_RULES",
             "DELEGATION_POLICY", "BACKEND_RULES"):
    out["faq." + name] = getattr(faq, name)
svc = ReceptionService(Store(":memory:"), env={})
cid = svc.start_call(at="2026-11-05T13:00", caller_id="092-000-0000")["id"]
out["instructions"] = svc.instructions(cid, preamble=config.LAB_RULES)
out["voice_backend"] = svc.voice_instructions(cid, config.LAB_PERSONA, backend=True)
out["voice_direct"] = svc.voice_instructions(cid, config.LAB_PERSONA, backend=False)
out["backend"] = svc.backend_instructions(cid)
out["text"] = svc.text_instructions(cid, config.LAB_PERSONA_TEXT)
r = svc.tool(cid, "save_field", {"field": "request", "value": "排水管の点検"})
out["after_request"] = r.get("say", "")
svc.ai_utterance(cid, out["after_request"])
svc.caller_utterance(cid, "12戸です。")
r = svc.tool(cid, "save_field", {"field": "request", "value": "排水管の点検、12戸"})
out["after_count"] = r.get("say", "")
sys.stdout.buffer.write(json.dumps(out, ensure_ascii=False, default=str).encode("utf-8"))
"""


class AnotherCompanyTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        env = {**os.environ, "AI_TENANT": str(FIXTURE), "PYTHONIOENCODING": "utf-8"}
        res = subprocess.run([sys.executable, "-c", PROBE], cwd=ROOT, env=env, capture_output=True, timeout=120)
        if res.returncode:
            raise AssertionError(res.stderr.decode("utf-8", "replace"))
        cls.out = json.loads(res.stdout.decode("utf-8"))

    def test_no_word_of_noda_reaches_the_ai(self):
        for key, value in self.out.items():
            text = json.dumps(value, ensure_ascii=False)
            self.assertIsNone(NODA_WORDS.search(text), f"{key}: {NODA_WORDS.search(text) and text[max(0, NODA_WORDS.search(text).start() - 40):][:100]}")

    def test_the_company_name_and_trade_are_used(self):
        for key in ("voice_backend", "voice_direct", "backend", "text", "instructions"):
            self.assertIn("みどり設備株式会社", self.out[key], key)
        self.assertIn("マンションの給排水設備の点検と修理", self.out["persona"])
        self.assertIn("みどり設備株式会社", self.out["goodbye"])
        self.assertIn("みどり設備株式会社", self.out["fake_creds"]["greeting"])   # the offline stand-in in the lab
        self.assertEqual(self.out["fake_creds"]["goodbye"], self.out["goodbye"])
        self.assertEqual(self.out["agent"], "みどり設備株式会社 受付AI（会話試験）")

    def test_the_company_order_and_questions_are_used(self):
        rules = self.out["voice_rules"]
        self.assertLess(rules.index("2. 建物名とお名前"), rules.index("3. 折り返しのお電話番号"))
        self.assertLess(rules.index("3. 折り返しのお電話番号"), rules.index("4. ご希望の日時"))
        self.assertIn("点検にお伺いするご希望の日時はございますか。", rules)
        self.assertEqual(self.out["fake_order"], ["request", "shop_name", "caller_name", "callback_number",
                                                  "preferred_datetime"])
        self.assertEqual(self.out["fake_ask"]["shop_name"], "恐れ入りますが、建物名をお伺いしてもよろしいでしょうか。")

    def test_the_company_follow_up_is_asked_and_its_answer_echoed(self):
        self.assertIn("何戸ございますか", self.out["after_request"])
        self.assertTrue(self.out["after_count"].startswith("12戸でございますね。"), self.out["after_count"])

    def test_the_company_faq_and_demo_lines_are_used(self):
        self.assertEqual([s[0] for s in self.out["faq_seeds"]], ["FAQ-01", "FAQ-02"])
        self.assertIn("みどりハイツ", self.out["demo"][0])
        self.assertIn("お見積もりは無料です。", self.out["lab"])


class NoCompanyWordsInCodeTest(unittest.TestCase):
    """The program's own strings carry no company words: they belong in the company settings (comments and
    docstrings may still tell the story of a decision)."""

    def test_no_company_word_in_a_string_of_the_program(self):
        found = []
        for p in sorted((ROOT / "prototype").rglob("*.py")):
            if "tests" in p.parts:
                continue
            tree = ast.parse(p.read_text(encoding="utf-8"))
            docstrings = {id(n.body[0].value) for n in ast.walk(tree)
                          if isinstance(n, (ast.Module, ast.FunctionDef, ast.ClassDef, ast.AsyncFunctionDef))
                          and n.body and isinstance(n.body[0], ast.Expr) and isinstance(n.body[0].value, ast.Constant)}
            for node in ast.walk(tree):
                if (isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docstrings
                        and re.search(r"野田|焼肉|ロースター|ほのか|上引き", node.value)):
                    found.append(f"{p.relative_to(ROOT)}:{node.lineno}")
        self.assertEqual(found, [])


if __name__ == "__main__":
    unittest.main()
