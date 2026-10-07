import pathlib
import unittest

from prototype.cost import candidate_costs as cc
from prototype.cost import cost_model as cm
from prototype.cost import measurement_budget as mb


class CandidateFrameTest(unittest.TestCase):
    def test_every_candidate_has_sources_and_unconfirmed_points(self):
        for c in cc.candidates():
            self.assertTrue(c["sources"], c["id"])
            self.assertTrue(c["confirmed"], c["id"])
            self.assertIn("unconfirmed", c)

    def test_same_premise_common_part_and_symbols(self):
        common = cc.common_ai_call()
        billed = cm.twilio_min(cm.DUR_MIN)
        expected = (0.0100 + 0.0044 + 0.0025) * billed + cm.summary("anthropic_haiku45").usd
        self.assertAlmostEqual(common.usd, expected)
        self.assertEqual(common.terms, {"F": 1})
        live = next(c for c in cc.candidates() if c["id"] == "gpt-live-1")
        self.assertAlmostEqual(cc.ai_part(live).usd, 0.15)
        self.assertIn("L_live", cc.ai_part(live).terms)
        eleven = next(c for c in cc.candidates() if c["id"] == "elevenagents")
        self.assertNotIn("P_11", cc.ai_part(eleven).terms)       # a monthly plan, not per call
        total, _ = cc.monthly_total(eleven, cm.SCENARIOS["少"])
        self.assertEqual(total.terms["P_11"], 1)
        rt = next(c for c in cc.candidates() if c["id"] == "gpt-realtime-2.1")
        self.assertGreater(cc.ai_part(rt, cache=False).usd, cc.ai_part(rt).usd)

    def test_rendered_frame_states_rule_and_unmeasured_quality(self):
        text = cc.render_frame()
        self.assertIn("コストと性能のバランス", text)
        self.assertIn("未実測", text)
        self.assertIn("受付1件あたりの費用", text)
        self.assertNotIn("品質 → 費用 → 運用負担", text)
        self.assertIn("AIの部分を含まない", text)               # Qwen: AI part unknown
        lst = cc.render_list()
        self.assertIn("Hume EVI", lst)
        self.assertIn("接続の調査を優先", lst)

    def test_browser_budget_caps_cover_the_maximum_per_vendor(self):
        est = mb.browser_stage_estimate()
        cumulative = {}
        for stage in ("connection", "detailed"):
            st = est["stages"][stage]
            per_vendor = {}
            for r in st["rows"]:
                per_vendor[r["ledger_vendor"]] = per_vendor.get(r["ledger_vendor"], 0) + r["max_usd"]
                cumulative[r["ledger_vendor"]] = cumulative.get(r["ledger_vendor"], 0) + r["max_usd"]
            caps = st["runtime_caps_usd"]
            for vendor, mx in (per_vendor if stage == "connection" else cumulative).items():
                self.assertGreaterEqual(caps[vendor] + 1e-9, mx, (stage, vendor))
            self.assertGreater(st["max_usd"], st["expected_usd"])
        conn = {r["id"]: r["sessions"] for r in est["stages"]["connection"]["rows"]}
        self.assertTrue(all(1 <= n <= 2 for n in conn.values()))           # 1-2 sessions per candidate first
        self.assertEqual(est["runtime_caps_total_usd"], 29.0)

    def test_budget_tables_cover_the_six_items(self):
        md = mb.browser_budget_markdown()
        for head in ("接続の予備試験", "詳しい会話比較", "固定費・利用枠", "別料金", "総額", "未確定項目"):
            self.assertIn(head, md)
        self.assertIn("二重に数えない", md)
        doc = (pathlib.Path(__file__).resolve().parents[2] / "docs" / "measurement-plan-v2.md").read_text()
        self.assertIn(md, doc)   # the plan shows exactly what the code computes
        self.assertIn("消費税10%", md)


if __name__ == "__main__":
    unittest.main()
