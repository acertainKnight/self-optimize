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
     "action": {"tier": "B", "type": "manual", "payload": {"description": "trim CLAUDE.md by hand"}}},
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

    def test_applicable_card_has_toggle_manual_card_has_checkbox(self):
        # applicability (incl. diff), not literal tier, decides the control: the
        # tier-A setting_change gets the Apply/Reject/Amend/Skip toggle, the tier-B
        # manual card gets the "select for assisted work" checkbox.
        html = dashboard.render_dashboard("r1", FINDINGS, DROPPED, [], [], USAGE, {})
        self.assertIn('name="choice-aaa111"', html)
        self.assertIn('id="assist-bbb222"', html)

    def test_diff_card_gets_apply_toggle_not_assist_checkbox(self):
        diff_rec = {"id": "ddd999", "title": "Trim CLAUDE.md diff", "category": "claude-md",
                    "impact": {"ordinal": "high"}, "risk": "med", "delta_tokens": None,
                    "evidence_refs": ["usage:totals.sessions"],
                    "metric": {"key": "none"},
                    "action": {"tier": "B", "type": "diff",
                              "payload": {"file": "/x/CLAUDE.md", "diff": "-old\n+new"}}}
        html = dashboard.render_dashboard("r1", [diff_rec], DROPPED, [], [], USAGE, {})
        self.assertIn('name="choice-ddd999"', html)
        self.assertNotIn('id="assist-ddd999"', html)

    def test_data_island_and_js_route_diff_to_apply_manual_to_assist(self):
        diff_rec = {"id": "diff1", "title": "d", "category": "claude-md",
                    "impact": {"ordinal": "med"}, "risk": "low", "delta_tokens": None,
                    "evidence_refs": ["usage:totals.sessions"], "metric": {"key": "none"},
                    "action": {"tier": "B", "type": "diff",
                              "payload": {"file": "/x", "diff": "-a\n+b"}}}
        manual_rec = {"id": "manual1", "title": "m", "category": "hooks",
                     "impact": {"ordinal": "med"}, "risk": "low", "delta_tokens": None,
                     "evidence_refs": ["usage:totals.sessions"], "metric": {"key": "none"},
                     "action": {"tier": "B", "type": "manual", "payload": {"description": "do it"}}}
        html = dashboard.render_dashboard("r1", [diff_rec, manual_rec], DROPPED, [], [], USAGE, {})
        island = json.loads(
            html.split('<script type="application/json" id="data">', 1)[1].split("</script>", 1)[0])
        by_id = {f["id"]: f for f in island["findings"]}
        self.assertTrue(by_id["diff1"]["applicable"])
        self.assertFalse(by_id["manual1"]["applicable"])
        self.assertIn('name="choice-diff1"', html)
        self.assertIn('id="assist-manual1"', html)
        # JS partitions by applicability, not literal tier, for apply/reject/amend vs assist
        self.assertIn("applicableIds", html)
        self.assertIn("manualIds", html)
        self.assertIn("f.applicable && f.interactive", html)

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

    def test_copy_command_sanitizes_shell_risky_reason(self):
        # copy-to-shell path must strip backticks/$/quotes/backslash from the reason
        html = dashboard.render_dashboard("r1", FINDINGS, DROPPED, [], [], USAGE, {})
        self.assertIn(r"replace(/[`$" + '"' + r"\\]/g, ' ')", html)
        self.assertIn("sanitized for shell safety", html)

    def test_outcomes_table_rendered_when_verify_rows_present(self):
        html = dashboard.render_dashboard("r1", [], DROPPED, VERIFY, [], USAGE, {})
        self.assertIn("Applied changes: outcomes", html)
        self.assertIn("ccc333", html)

    def test_plugin_disable_rec_produces_name_only_skill_alt_in_island(self):
        rec = {"id": "ddd444", "title": "Disable bloat plugin", "category": "bloat",
               "impact": {"ordinal": "med"}, "risk": "low", "delta_tokens": 500,
               "evidence_refs": ["inventory:plugin:bloatplug", "inventory:skill:dusty"],
               "metric": {"key": "base_context_est", "direction": "down", "scope": "global"},
               "action": {"tier": "A", "type": "setting_change",
                         "payload": {"file": "settings.json",
                                     "key_path": ["enabledPlugins", "bloatplug"], "value": False}}}
        html = dashboard.render_dashboard("r1", [rec], DROPPED, [], [], USAGE, {})
        island = json.loads(
            html.split('<script type="application/json" id="data">', 1)[1].split("</script>", 1)[0])
        finding = next(f for f in island["findings"] if f["id"] == "ddd444")
        labels = [a["label"] for a in finding["alts"]]
        self.assertIn("name-only skill dusty", labels)
        self.assertIn("off skill dusty", labels)
        name_only = next(a for a in finding["alts"] if a["label"] == "name-only skill dusty")
        self.assertEqual(name_only["action"]["payload"]["key_path"], ["skillOverrides", "dusty"])
        self.assertEqual(name_only["action"]["payload"]["value"], "name-only")

    def test_amend_control_and_custom_json_path_in_js(self):
        html = dashboard.render_dashboard("r1", FINDINGS, DROPPED, [], [], USAGE, {})
        self.assertIn('value="amend"', html)
        self.assertIn('id="amend-select-aaa111"', html)
        self.assertIn('id="amend-custom-aaa111"', html)
        self.assertIn("JSON.parse(customVal)", html)   # try/catch custom-action path
        self.assertIn("amend has no CLI flag", html)   # copy-command never encodes amend

    def test_custom_amend_json_must_be_plain_object(self):
        # a parseable-but-non-object custom amend JSON (number/string/array/null)
        # must not be treated as a valid amend action client-side
        html = dashboard.render_dashboard("r1", FINDINGS, DROPPED, [], [], USAGE, {})
        self.assertIn("Array.isArray(parsed)", html)

    def test_alts_never_carry_analyst_free_text(self):
        rec = {"id": "eee555", "title": "SECRET-FREE-TEXT-MARKER", "category": "bloat",
               "impact": {"ordinal": "med"}, "risk": "SECRET-FREE-TEXT-MARKER",
               "delta_tokens": 500,
               "evidence_refs": ["inventory:plugin:bloatplug", "inventory:skill:dusty"],
               "metric": {"key": "base_context_est", "direction": "down", "scope": "global"},
               "action": {"tier": "A", "type": "setting_change",
                         "payload": {"file": "settings.json",
                                     "key_path": ["enabledPlugins", "bloatplug"], "value": False}}}
        alts = dashboard._alts_for(rec)
        self.assertNotIn("SECRET-FREE-TEXT-MARKER", json.dumps(alts))
        self.assertTrue(alts)   # sanity: alts were actually produced


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
