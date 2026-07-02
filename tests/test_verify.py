import sys, pathlib, unittest
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "scripts"))
import verify

CFG = {"verify": {"min_sessions": 3, "min_rel_change": 0.10}}


def entry(baseline, direction="down"):
    return {"status": "applied", "applied_at": "2026-06-10T00:00:00+00:00",
            "baseline": {"value": baseline, "n_sessions": 5},
            "rec": {"title": "t", "metric": {"key": "correction_rate",
                                             "direction": direction, "scope": "global"}}}


def sessions(counts):
    return [{"started_at": f"2026-06-2{i}T00:00:00Z", "corrections_count": c}
            for i, c in enumerate(counts)]


class TestVerify(unittest.TestCase):
    def test_verified_regressed_inconclusive(self):
        rows = verify.verify_entries({"a": entry(2.0)}, sessions([0, 0, 0]), None, CFG)
        self.assertEqual(rows[0]["verdict"], "verified")       # 2.0 -> 0.0, direction down
        rows = verify.verify_entries({"a": entry(1.0)}, sessions([3, 3, 3]), None, CFG)
        self.assertEqual(rows[0]["verdict"], "regressed")      # 1.0 -> 3.0
        rows = verify.verify_entries({"a": entry(2.0)}, sessions([0]), None, CFG)
        self.assertEqual(rows[0]["verdict"], "inconclusive")   # n=1 < min_sessions
        rows = verify.verify_entries({"a": entry(2.0)}, sessions([2, 2, 2]), None,
                                     {"verify": {"min_sessions": 3, "min_rel_change": 0.10}})
        self.assertEqual(rows[0]["verdict"], "inconclusive")   # no movement beyond threshold

    def test_skips_non_applied_and_metric_none(self):
        e = entry(2.0); e["status"] = "rejected"
        self.assertEqual(verify.verify_entries({"a": e}, sessions([0, 0, 0]), None, CFG), [])
        e2 = entry(2.0); e2["rec"]["metric"] = {"key": "none"}
        self.assertEqual(verify.verify_entries({"a": e2}, sessions([0, 0, 0]), None, CFG), [])


if __name__ == "__main__":
    unittest.main()
