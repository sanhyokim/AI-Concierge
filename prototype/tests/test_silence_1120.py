"""2026-11-20 (user): 「客が一定時間しゃべらなかったら後追いでAIがしゃべったほうがいいかも」.

The company's words for a silent caller come from the company settings and go to the page with each session;
the page speaks them, once each, when the caller says nothing for 8 s after a question (browser_lab_smoke.cjs)."""
import pathlib
import tempfile
import unittest

from prototype.browser_lab.server import Lab
from prototype.tenants import current, from_dict
from prototype.tests.test_manners import FORBIDDEN


class SilencePromptTest(unittest.TestCase):
    def test_noda_has_two_follow_ups(self):
        prompts = current().silence_prompts
        self.assertEqual(len(prompts), 2)
        self.assertTrue(prompts[0].startswith("お電話が遠い"))
        for p in prompts:
            self.assertTrue(p.endswith("か。"), p)   # a question: the caller is asked to answer
            for w in FORBIDDEN:
                self.assertNotIn(w, p)

    def test_the_words_go_to_the_page_with_the_session(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = pathlib.Path(tmp)
            lab = Lab(env={}, ledger_path=d / "ledger.jsonl", results_dir=d / "results", agent_state=d / "agents.json")
            s = lab.start("fake")
        self.assertEqual(len(s["silence_prompts"]), 2)
        self.assertIn("お聞こえでしょうか", s["silence_prompts"][0])
        self.assertNotIn("恐れ入", s["silence_prompts"][1])   # kana for the voice (おそれいります)

    def test_no_words_means_no_follow_up(self):
        data = current().raw
        tenant = dict(data["tenant"])
        tenant.pop("silence_prompts", None)
        self.assertEqual(from_dict({**data, "tenant": tenant}).silence_prompts, ())


if __name__ == "__main__":
    unittest.main()
