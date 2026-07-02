import json, sys, pathlib, tempfile, unittest
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "scripts"))
import dashboard

FINDINGS = [
    {"id": "aaa111", "title": "Disable dusty", "category": "bloat",
     "impact": {"ordinal": "med"}, "risk": "low", "delta_tokens": 9000,
     "evidence_refs": ["inventory:skill:dusty"],
     "metric": {"key": "base_context_est", "direction": "down", "scope": "global"},
     "action": {"tier": "A", "type": "setting_change", "payload": {"file": "settings.json"}}},
    {"id": "bbb222", "title": "Trim CLAUDE.md", "category": "claude-md",
     "impact": {"ordinal": "high"}, "risk": "med", "delta_tokens": None,
     "evidence_refs": ["usage:totals.sessions"],
     "metric": {"key": "none"},
     "action": {"tier": "B", "type": "diff", "payload": {"file": "/x", "diff": "- old\n+ new"}}},
]
DROPPED = {"invalid": 1, "citations": 2, "guard": 0, "suppressed": []}
VERIFY = [{"id": "ccc333", "title": "Old change", "metric": "correction_rate",
           "baseline": 2.0, "value": 1.0, "n": 12, "verdict": "verified", "rel_change": -0.5}]
USAGE = {"totals": {"sessions": 100}, "base_context_est": 42651}
CUMULATIVE = [("correction_rate", [4.0, 2])]


class TestRenderDashboard(unittest.TestCase):
    def test_ids_present(self):
        html = dashboard.render_dashboard("r1", FINDINGS, DROPPED, VERIFY, CUMULATIVE, USAGE, {})
        self.assertIn("aaa111", html)
        self.assertIn("bbb222", html)

    def test_json_island_has_no_raw_lt(self):
        html = dashboard.render_dashboard("r1", FINDINGS, DROPPED, VERIFY, CUMULATIVE, USAGE, {})
        island = html.split('<script type="application/json" id="data">', 1)[1].split("</script>", 1)[0]
        self.assertNotIn("<", island)

    def test_xss_title_escaped_only(self):
        f = dict(FINDINGS[0])
        f["title"] = "<img src=x onerror=alert(1)>"
        html = dashboard.render_dashboard("r1", [f], DROPPED, [], [], USAGE, {})
        self.assertIn("&lt;img", html)
        self.assertNotIn("<img src=x", html)

    def test_tier_a_card_has_toggle_tier_b_has_checkbox(self):
        html = dashboard.render_dashboard("r1", FINDINGS, DROPPED, [], [], USAGE, {})
        self.assertIn('name="choice-aaa111"', html)
        self.assertIn('id="assist-bbb222"', html)

    def test_ledger_applied_rec_renders_badge_no_toggle(self):
        ledger_entries = {"aaa111": {"status": "applied"}}
        html = dashboard.render_dashboard("r1", FINDINGS, DROPPED, [], [], USAGE, ledger_entries)
        self.assertIn('badge good">applied</span>', html)
        self.assertNotIn('name="choice-aaa111"', html)

    def test_decisions_filename_string_present(self):
        html = dashboard.render_dashboard("r1", FINDINGS, DROPPED, [], [], USAGE, {})
        self.assertIn("self-optimize-decisions-", html)

    def test_no_network_or_fetch_strings(self):
        html = dashboard.render_dashboard("r1", FINDINGS, DROPPED, VERIFY, CUMULATIVE, USAGE, {})
        for bad in ("http://", "https://", "fetch("):
            self.assertNotIn(bad, html)

    def test_outcomes_table_rendered_when_verify_rows_present(self):
        html = dashboard.render_dashboard("r1", [], DROPPED, VERIFY, [], USAGE, {})
        self.assertIn("Applied changes: outcomes", html)
        self.assertIn("ccc333", html)


class TestDashboardMain(unittest.TestCase):
    def test_main_writes_run_and_latest_600(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        base = pathlib.Path(tmp.name)
        state, ev = base / "state", base / "ev"
        state.mkdir(); ev.mkdir()
        (ev / "findings.json").write_text(json.dumps({"findings": FINDINGS, "dropped": DROPPED}))
        (ev / "usage.json").write_text(json.dumps(USAGE))
        import so_config
        so_config.load_config(state)
        dashboard.main(["--evidence", str(ev), "--state", str(state), "--run-id", "r1"])
        rdir = state / "reports"
        run_html = rdir / "r1.html"
        latest = rdir / "latest.html"
        self.assertTrue(run_html.exists())
        self.assertTrue(latest.exists())
        self.assertEqual(oct(run_html.stat().st_mode)[-3:], "600")
        self.assertEqual(oct(latest.stat().st_mode)[-3:], "600")
        self.assertIn("aaa111", run_html.read_text())


if __name__ == "__main__":
    unittest.main()
