import json, sys, pathlib, unittest
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
USAGE_MODELS = {"totals": {"sessions": 100}, "parse": {"skipped_lines": 0, "redactions": 0},
                 "per_model": {"claude-opus-4-8": {"input": 500, "output": 2000, "sessions": 10},
                               "claude-sonnet-5": {"input": 300, "output": 900, "sessions": 20}},
                 "corrections_by_model": {"claude-opus-4-8": 3, "claude-sonnet-5": 1}}


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

    def test_shadow_and_roi_rendered(self):
        md = report.render("r", FINDINGS, DROPPED, [], [], USAGE, {},
                           shadow={"aaa111": {"prevented": 3, "total": 4}},
                           roi={"saved": 9000, "spent": 12000})
        self.assertIn("prevented 3/4", md)
        self.assertIn("est. 9,000 tok/window saved", md)
        self.assertIn("not yet paying for itself", md)
        md2 = report.render("r", FINDINGS, DROPPED, [], [], USAGE, {},
                            roi={"saved": 50000, "spent": 12000})
        self.assertNotIn("not yet paying", md2)

    def test_gym_scores_render_both_sides_on_the_finding(self):
        md = report.render("r", FINDINGS, DROPPED, [], [], USAGE, {},
                           gym={"aaa111": {"prevented": {"n": 4, "of": 6},
                                           "preserved": {"n": 5, "of": 5},
                                           "unscorable": False},
                                "bbb222": {"unscorable": True,
                                           "reason": "below case floor: working 1/3"}})
        self.assertIn("prevented 4/6 failure cases and preserved 5/5 working cases", md)
        self.assertIn("not an auto-gate", md)
        self.assertIn("unscorable — below case floor: working 1/3", md)

    def test_deep_localize_renders_as_advisory_line_on_the_finding(self):
        localized = dict(FINDINGS[0], deep_localize=[{"bracket": [3, 4], "turns_total": 8,
                                                       "calls": 3, "rationale": "went sideways"}])
        md = report.render("r", [localized, FINDINGS[1]], DROPPED, [], [], USAGE, {})
        self.assertIn("deep localize: session went off track around turn 4-5 of 8", md)
        self.assertIn("went sideways", md)

    def test_gym_json_is_optional_and_survives_corruption(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            base = pathlib.Path(d)
            state, ev = base / "st", base / "ev"
            ev.mkdir()
            (ev / "findings.json").write_text(json.dumps({"findings": FINDINGS, "dropped": DROPPED}))
            (ev / "usage.json").write_text(json.dumps(USAGE))
            (ev / "gym.json").write_text("{not json")
            report.main(["--evidence", str(ev), "--state", str(state), "--run-id", "r1"])
            md = (state / "reports" / "r1.md").read_text()
            self.assertIn("## Findings", md)
            self.assertNotIn("gym score", md)

    def test_verify_note_rendered_in_outcomes_table(self):
        rows = [dict(VERIFY[0], note="applied setting still in effect")]
        md = report.render("2026-07-01", [], DROPPED, rows, [], USAGE, {})
        self.assertIn("| note |", md)
        self.assertIn("applied setting still in effect", md)

    def test_analyst_tokens_fallback_file(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            base = pathlib.Path(d)
            state, ev = base / "st", base / "ev"
            ev.mkdir()
            (ev / "findings.json").write_text(json.dumps({"findings": [], "dropped": DROPPED}))
            (ev / "usage.json").write_text(json.dumps(USAGE))
            (ev / "analyst_tokens.json").write_text(json.dumps({"miner": 111, "auditor": 222}))
            import so_config
            report.main(["--evidence", str(ev), "--state", str(state), "--run-id", "r1"])
            cfg = so_config.load_config(state)
            md = (pathlib.Path(cfg["report_dir"]) / "r1.md").read_text()
            self.assertIn("miner=111, auditor=222 (total 333)", md)

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

    def test_model_performance_table(self):
        md = report.render("r", [], DROPPED, [], [], USAGE_MODELS, {})
        self.assertIn("## Model performance", md)
        self.assertIn("| claude-opus-4-8 | 10 | 2,000 | 3 |", md)
        self.assertIn("| claude-sonnet-5 | 20 | 900 | 1 |", md)

    def test_no_model_table_when_absent(self):
        md = report.render("r", [], DROPPED, [], [], USAGE, {})
        self.assertNotIn("## Model performance", md)

    def test_cumulative_verified_improvement_rendered(self):
        cum = [("correction_rate", [4.0, 2]), ("tokens_per_session", [120.0, 1])]
        md = report.render("r", [], DROPPED, [], TREND, USAGE, {}, cumulative=cum)
        self.assertIn("Cumulative verified improvement", md)
        self.assertIn("correction_rate: 4.00 (2 verified changes)", md)
        self.assertIn("tokens_per_session: 120.00 (1 verified change)", md)

    def test_no_cumulative_section_when_empty(self):
        md = report.render("r", [], DROPPED, [], [], USAGE, {})
        self.assertNotIn("Cumulative verified improvement", md)

    def test_cumulative_savings_sums_verified_entries_only(self):
        entries = {
            "a": {"status": "verified", "rec": {"metric": {"key": "correction_rate"}},
                  "baseline": {"value": 5.0}, "measured": {"value": 1.0}},
            "b": {"status": "regressed", "rec": {"metric": {"key": "correction_rate"}},
                  "baseline": {"value": 5.0}, "measured": {"value": 9.0}},
            "c": {"status": "verified", "rec": {"metric": {"key": "correction_rate"}},
                  "baseline": {"value": 3.0}, "measured": {"value": 2.0}},
        }
        out = dict(report._cumulative_savings(entries))
        self.assertEqual(out["correction_rate"], [5.0, 2])   # (5-1)+(3-2)=5.0, count=2

    def test_direction_up_deltas_positive(self):
        # up-is-better verified metric: improvement must render positive, not sign-flipped
        entries = {"a": {"status": "verified",
                         "rec": {"metric": {"key": "sessions_ok", "direction": "up"}},
                         "baseline": {"value": 1.0}, "measured": {"value": 3.0}}}
        self.assertEqual(dict(report._cumulative_savings(entries))["sessions_ok"], [2.0, 1])
        rows = [{"id": "a", "metric": "sessions_ok", "direction": "up",
                 "baseline": 1.0, "value": 3.0, "verdict": "verified"}]
        self.assertEqual(report._verified_deltas(rows), {"sessions_ok": 2.0})

    def test_verified_deltas_this_run_only(self):
        rows = [
            {"id": "a", "metric": "correction_rate", "baseline": 5.0, "value": 1.0,
             "verdict": "verified"},
            {"id": "b", "metric": "correction_rate", "baseline": 3.0, "value": 2.0,
             "verdict": "verified"},
            {"id": "c", "metric": "tokens_per_session", "baseline": 1000.0, "value": 400.0,
             "verdict": "regressed"},
        ]
        self.assertEqual(report._verified_deltas(rows), {"correction_rate": 5.0})

    def test_verified_deltas_written_to_runs_jsonl(self):
        import tempfile
        import so_config
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        base = pathlib.Path(tmp.name)
        state, ev = base / "state", base / "ev"
        state.mkdir(); ev.mkdir()
        (ev / "findings.json").write_text(json.dumps({"findings": [], "dropped": DROPPED}))
        (ev / "usage.json").write_text(json.dumps(USAGE))
        (ev / "verify.json").write_text(json.dumps({"rows": [
            {"id": "x", "title": "t", "metric": "correction_rate", "baseline": 5.0,
             "value": 1.0, "n": 12, "verdict": "verified", "rel_change": -0.8, "p_value": 0.01}]}))
        so_config.load_config(state)
        report.main(["--evidence", str(ev), "--state", str(state), "--run-id", "r1"])
        lines = (state / "state" / "runs.jsonl").read_text().splitlines()
        rows = [json.loads(x) for x in lines]
        self.assertEqual(rows[-1]["verified_deltas"], {"correction_rate": 4.0})


if __name__ == "__main__":
    unittest.main()
