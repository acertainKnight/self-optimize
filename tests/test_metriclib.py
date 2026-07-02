import sys, pathlib, unittest
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "scripts"))
import metriclib

S = [
    {"started_at": "2026-06-01T00:00:00Z", "input_tokens": 100, "output_tokens": 100,
     "corrections_count": 2, "duplicate_reads": 1, "permission_stalls": 0,
     "models": {"claude-opus-4-8": {"input": 50, "output": 80}}},
    {"started_at": "2026-06-20T00:00:00Z", "input_tokens": 300, "output_tokens": 100,
     "corrections_count": 0, "duplicate_reads": 3, "permission_stalls": 2,
     "models": {"claude-sonnet-5": {"input": 100, "output": 90}}},
]
INV = {"base_context_est": 5000, "unused": ["skill:a", "agent:b"]}


class TestMetrics(unittest.TestCase):
    def test_session_means_and_after_filter(self):
        v, n = metriclib.compute_metric({"key": "tokens_per_session", "scope": "global"}, S, None)
        self.assertEqual((v, n), (300.0, 2))
        v, n = metriclib.compute_metric({"key": "correction_rate", "scope": "global"}, S, None,
                                        after_ts="2026-06-10T00:00:00Z")
        self.assertEqual((v, n), (0.0, 1))

    def test_model_scope_and_inventory_metrics(self):
        v, n = metriclib.compute_metric(
            {"key": "model_output_tokens", "scope": "model:claude-opus"}, S, None)
        self.assertEqual((v, n), (40.0, 2))  # 80 in one session, 0 in the other
        v, n = metriclib.compute_metric({"key": "unused_surface_count", "scope": "global"}, S, INV)
        self.assertEqual((v, n), (2, 1))
        v, n = metriclib.compute_metric({"key": "none"}, S, INV)
        self.assertEqual((v, n), (None, 0))


if __name__ == "__main__":
    unittest.main()
