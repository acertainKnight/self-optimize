import sys, pathlib, unittest
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "scripts"))
import report

FINDINGS = [
    {"id": "aaa111", "title": "Disable dusty", "category": "bloat",
     "impact": {"ordinal": "med"}, "risk": "low", "delta_tokens": 9000,
     "evidence_refs": ["inventory:skill:dusty"],
     "metric": {"key": "base_context_est", "direction": "down", "scope": "global"},
     "action": {"tier": "A", "type": "setting_change", "payload": {}}},
    {"id": "bbb222", "title": "Trim CLAUDE.md", "category": "claude-md",
     "impact": {"ordinal": "high"}, "risk": "med", "delta_tokens": None,
     "evidence_refs": ["usage:totals.sessions"], "prior_rejection": "too soon",
     "metric": {"key": "none"},
     "action": {"tier": "B", "type": "diff", "payload": {"file": "/x", "diff": "- old\n+ new"}}},
]
DROPPED = {"invalid": 1, "citations": 2, "guard": 0, "suppressed": []}
VERIFY = [{"id": "ccc333", "title": "Old change", "metric": "correction_rate",
           "baseline": 2.0, "value": 1.0, "n": 12, "verdict": "verified", "rel_change": -0.5}]
TREND = [{"run_id": "2026-06-24", "n_sessions": 50, "tokens_per_session": 1000.0,
          "correction_rate": 1.5, "duplicate_read_rate": 0.5,
          "base_context_est": 40000, "unused_surface_count": 12}]
USAGE = {"totals": {"sessions": 100}, "parse": {"skipped_lines": 3, "redactions": 5}}


class TestReport(unittest.TestCase):
    def test_render_sections_and_order(self):
        md = report.render("2026-07-01", FINDINGS, DROPPED, VERIFY, TREND, USAGE,
                            {"analyst_tokens": {"miner": 8000, "auditor": 4345}})
        self.assertLess(md.index("Applied changes: outcomes"), md.index("## Findings"))
        self.assertIn("**verified**", md)
        self.assertIn("~9,000 tok/window", md)
        self.assertIn("/self-optimize apply aaa111", md)      # tier A gets an apply hint
        self.assertIn("- old", md)                            # tier B diff rendered inline
        self.assertIn("previously rejected: too soon", md)
        self.assertIn("failed citations: 2", md)
        self.assertIn("| 2026-06-24 | 50 |", md)
        self.assertIn("analyst tokens: miner=8,000, auditor=4,345 (total 12,345)", md)

    def test_pipe_in_title_does_not_break_table(self):
        f = dict(FINDINGS[0])
        f["title"] = "before | after"
        md = report.render("r", [f], DROPPED, [], [], USAGE, {})
        row = next(l for l in md.splitlines() if "before" in l and l.startswith("| 1 |"))
        self.assertIn("before \\| after", row)
        self.assertEqual(row.count(" | "), 5)  # six columns stay intact

    def test_verify_none_values_do_not_crash(self):
        rows = [{"id": "x", "title": "t", "metric": "correction_rate", "baseline": None,
                 "value": None, "n": 2, "verdict": "inconclusive", "rel_change": None}]
        md = report.render("r", [], DROPPED, rows, [], USAGE, {})
        self.assertIn("inconclusive", md)

    def test_regressed_row_prints_rollback_command(self):
        rows = [{"id": "ccc333", "title": "Old change", "metric": "correction_rate",
                 "baseline": 2.0, "value": 3.0, "n": 12, "verdict": "regressed", "rel_change": 0.5}]
        md = report.render("r", [], DROPPED, rows, [], USAGE, {})
        self.assertIn("· rollback: /self-optimize rollback ccc333", md)


if __name__ == "__main__":
    unittest.main()
