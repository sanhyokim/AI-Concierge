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

    def test_plan2_is_measured_from_todays_mobile_forwarding(self):
        live = next(c for c in cc.candidates() if c["id"] == "gpt-live-1")
        low = cm.SCENARIOS["少"]
        per_call = cc.plan2_ai_call(live)
        self.assertNotIn("F", per_call.terms)                  # no new NTT leg: the existing forward is re-pointed
        self.assertEqual(per_call.terms[cc.DELTA_F], 1)
        self.assertAlmostEqual(per_call.usd, cc.common_ai_call().usd + cc.ai_part(live).usd)
        existing = cc.existing_monthly(low)
        self.assertEqual(existing.terms, {"F_mob": low["ai"] + low["human"], "B_fwd": 1})
        self.assertEqual((existing.usd, existing.jpy), (0.0, 0.0))   # unknown contract prices stay symbols
        p2, _ = cc.plan2_monthly(live, low)
        p1, _ = cc.monthly_total(live, low)
        self.assertLess(p2.jpy_total(), p1.jpy_total())         # no extra for human-answered calls
        for sym in ("B_num", "U_ntt", "F"):
            self.assertNotIn(sym, p2.terms)
        number = cm.PRICES["twilio_number_050"]["value"]
        _, _, line = cc.line_cost(low["ai"], low["recipients"])
        self.assertAlmostEqual(p2.jpy_total(), per_call.scale(low["ai"]).jpy_total()
                               + cm.Money(usd=number).jpy_total() + line.jpy)
        msgs, lp, money = cc.line_cost(cm.SCENARIOS["中"]["ai"], 1)
        self.assertEqual((round(msgs), money.jpy), (198, 0))    # one recipient stays in the free 200 messages
        text = cc.render_frame()
        for want in ("回線案2（検討中）", "採用は未確定", "今すでに払っている費用", "ΔF", "8-4"):
            self.assertIn(want, text)
        docs = pathlib.Path(__file__).resolve().parents[2] / "docs"
        doc = (docs / "candidate-comparison-v1.md").read_text(encoding="utf-8")
        self.assertEqual(doc, text)                              # the document is generated from this code
        lst = (docs / "candidates-v1.md").read_text(encoding="utf-8")
        self.assertEqual(lst, cc.render_list())

    def test_browser_budget_is_per_session_without_caps(self):
        from prototype.browser_lab import config as lc
        est = mb.browser_stage_estimate()
        for r in est["rows"]:
            # the table's maximum is what the lab reserves before each session
            self.assertAlmostEqual(r["max_usd"], round(lc.session_reserve_usd(lc.CANDIDATES[r["id"]], {}), 3))
            self.assertGreater(r["max_usd"], r["expected_usd"])
            self.assertAlmostEqual(r["examples"][10]["max_usd"], round(10 * lc.session_reserve_usd(
                lc.CANDIDATES[r["id"]], {}), 2))
        limits = {r["id"]: r["sessions_limit"] for r in est["rows"]}
        self.assertEqual(limits, {"gpt-live-1": None, "gemini-3.8-live": None, "gpt-realtime-2.1": 0,
                                  "elevenagents": 0, "cartesia-agents": 0})   # user decision 2026-10-08
        self.assertEqual({v: c for v, c in est["ledger_caps_usd"].items() if c != 0},
                         {"openai_lab": None, "google_lab": None})
        live = next(r for r in est["rows"] if r["id"] == "gpt-live-1")
        self.assertGreater(live["backend_strict_usd"], live["backend_typical_usd"])   # two assumptions, both shown
        self.assertEqual(est["max_session_min"], 5.0)

    def test_budget_keeps_payments_estimates_unconfirmed_and_limits_apart(self):
        md = mb.browser_budget_markdown()
        for head in ("① 支払い額", "② 既知の費目の試算", "③ 未確認の費用", "④ アプリ内の制限", "固定費・利用枠"):
            self.assertIn(head, md)
        self.assertNotIn("残高が上限", md)                      # a prepaid balance is not a spending cap
        self.assertIn("費用の上限ではありません", md)
        self.assertIn("約10分", md)
        self.assertIn("L_live", md)
        self.assertIn("上限なし（利用者の決定、2026-10-08）", md)
        self.assertIn("本番の通話を何分で止めるかは未決定", md)
        self.assertIn("消費税10%", md)
        docs = pathlib.Path(__file__).resolve().parents[2] / "docs"
        doc = (docs / "measurement-plan-v2.md").read_text(encoding="utf-8")
        self.assertIn(md, doc)   # the plan shows exactly what the code computes
        self.assertNotIn("残高が上限", doc)


if __name__ == "__main__":
    unittest.main()
