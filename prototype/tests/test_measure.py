import unittest
from array import array

from prototype.measure.audio import ulaw_decode_byte, ulaw_encode_sample
from prototype.measure.rate import compare_versions, measure_version
from prototype.measure.synthetic import RATE, build


def _segments(target_mora_s, target_pauses, post_mora_s=0.12):
    return [
        {"name": "pre", "morae": 16, "mora_s": 0.12, "gap_s": 0.01},
        {"name": "target", "morae": 18, "mora_s": target_mora_s, "gap_s": 0.01, "pauses": target_pauses},
        {"name": "post", "morae": 16, "mora_s": post_mora_s, "gap_s": 0.01},
    ]


class MuLawTest(unittest.TestCase):
    def test_round_trip_error_is_small(self):
        for s in (-32000, -1000, -10, 0, 10, 1000, 32000):
            back = ulaw_decode_byte(ulaw_encode_sample(s))
            self.assertLessEqual(abs(back - s), max(16, abs(s) * 0.07), s)


class RateTest(unittest.TestCase):
    def _measure(self, segs, seed=1):
        samples, bounds, truth = build(segs, seed=seed)
        return measure_version(samples, RATE, bounds), truth

    def test_articulation_rate_matches_truth(self):
        result, truth = self._measure(_segments(0.12, [(5, 0.2), (11, 0.2)]))
        for name, t in truth.items():
            m = result["segments"][name]
            rel = abs(m["articulation_rate"] - t["articulation_rate"]) / t["articulation_rate"]
            self.assertLess(rel, 0.05, (name, m, t))
            self.assertEqual(m["pause_count"], t["pause_count"], (name, m, t))

    def test_pause_only_slowdown_is_not_articulation_slowdown(self):
        n, _ = self._measure(_segments(0.12, [(5, 0.2), (11, 0.2)]))
        d, _ = self._measure(_segments(0.12, [(5, 0.6), (11, 0.6)]))
        cmp = compare_versions(n, d)
        self.assertFalse(cmp["articulation_slowed"])
        self.assertTrue(cmp["pause_only_slowdown"])

    def test_real_slowdown_and_return(self):
        n, _ = self._measure(_segments(0.12, [(5, 0.2), (11, 0.2)]))
        d, _ = self._measure(_segments(0.16, [(5, 0.3), (11, 0.3)]))
        cmp = compare_versions(n, d)
        self.assertTrue(cmp["articulation_slowed"], cmp)
        self.assertTrue(cmp["returned_to_normal"], cmp)

    def test_missing_return_is_detected(self):
        n, _ = self._measure(_segments(0.12, [(5, 0.2), (11, 0.2)]))
        d, _ = self._measure(_segments(0.16, [(5, 0.2), (11, 0.2)], post_mora_s=0.16))
        self.assertFalse(compare_versions(n, d)["returned_to_normal"])

    def test_rushing_after_the_slow_part_does_not_pass(self):
        n, _ = self._measure(_segments(0.12, [(5, 0.2), (11, 0.2)]))
        d, _ = self._measure(_segments(0.16, [(5, 0.2), (11, 0.2)], post_mora_s=0.095))
        cmp = compare_versions(n, d)
        self.assertGreater(cmp["return_ratio"], 1.10)
        self.assertFalse(cmp["returned_to_normal"])
        self.assertTrue(cmp["post_too_fast"])


if __name__ == "__main__":
    unittest.main()
