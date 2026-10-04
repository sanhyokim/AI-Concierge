import unittest

from prototype.measure.rate import FRAME_MS, measure_segment
from prototype.realvoice.compare import isolated_short_runs, label_offset, sweep_tool_edges


def _mask(spec):
    """spec: [(voiced: bool, ms), ...] -> frame mask."""
    out = []
    for voiced, ms in spec:
        out += [voiced] * (ms // FRAME_MS)
    return out


# phrase A ends with a fragmented tail (20 ms on, 10 ms off, 10 ms on), pause 300 ms, phrase B, pause, phrase C
SPEC = [(False, 100), (True, 600), (False, 10), (True, 20), (False, 10), (True, 10), (False, 300),
        (True, 500), (False, 250), (True, 400), (False, 100)]
A_END = (100 + 600 + 10 + 20 + 10 + 10) / 1000
B_START = A_END + 0.3


class EdgeRuleTest(unittest.TestCase):
    def test_fragmented_tail_within_30ms_is_dropped_as_one_group(self):
        mask = _mask(SPEC)
        details = {}
        measure_segment(mask, "x", A_END - 0.03, 2.5, 1, details=details)
        self.assertEqual(len(details["removed"]), 1)
        self.assertAlmostEqual(details["span"][0], B_START)

    def test_tail_longer_than_30ms_is_kept(self):
        details = {}
        measure_segment(_mask(SPEC), "x", A_END - 0.04, 2.5, 1, details=details)
        self.assertEqual(details["removed"], [])
        self.assertLess(details["span"][0], A_END)

    def test_segment_starting_inside_a_phrase_loses_nothing(self):
        details = {}
        measure_segment(_mask(SPEC), "x", B_START + 0.05, 2.5, 1, details=details)
        self.assertEqual(details["removed"], [])

    def test_short_isolated_group_is_reported(self):
        mask = _mask([(False, 300), (True, 25), (False, 300), (True, 300), (False, 300)])
        runs = isolated_short_runs(mask, 30, 6)
        self.assertEqual(len(runs), 1)

    def test_tool_edge_sweep_on_synthetic_mask(self):
        mask = _mask(SPEC)
        details = {}
        measure_segment(mask, "x", 0.0, len(mask) * FRAME_MS / 1000, 1, details=details)
        stats = sweep_tool_edges(mask, [], details["pauses"], details["span"])
        for d in ("10", "20", "30"):
            self.assertEqual(stats["spill"][d]["kept"], 0, d)
        for d in ("40", "50"):
            self.assertEqual(stats["spill"][d]["removed"], 0, d)
        self.assertEqual(stats["inside_removed"], [])


class LabelOffsetTest(unittest.TestCase):
    def test_position_dependent_offset_is_detected(self):
        # labels read late by 40 ms per second of window position
        pairs = []
        for k in range(8):
            t0, t1 = 0.2 + k * 0.29, 0.4 + k * 0.29
            pairs.append({"ref": (t0, t1), "start_err_ms": round(-40 * (t0 % 2.5)),
                          "end_err_ms": round(-40 * (t1 % 2.5))})
        out = label_offset(pairs)
        self.assertAlmostEqual(out["slope_ms_per_s_of_window_position"], -40, delta=1)
        self.assertLess(out["corr"], -0.95)


if __name__ == "__main__":
    unittest.main()
