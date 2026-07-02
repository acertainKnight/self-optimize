import sys, pathlib, unittest
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "scripts"))
import schema, synth

DATA = pathlib.Path("/fakehome/.claude")

EV = {
    "usage": {"totals": {"sessions": 100}, "waste": {"duplicate_reads_total": 7}},
    "sessions": {"sessions": [{"id": "s1"}]},
    "activation": {"items": {"skill:used": {"count": 5}}},
    "samples": {"samples": [{"user_text": "no, wrong"}]},
    "inventory": {"skills": [{"id": "skill:dusty", "name": "dusty", "source": "plugin:tk@mp",
                              "path": "/x/SKILL.md", "est_context_tokens": 90}],
                  "agents": [], "mcp_servers": [], "plugins": [], "claude_md": []},
    "rules": {"rules": [{"id": "skill-overrides"}]},
}


def bloat_rec():
    return {"title": "Disable dusty", "category": "bloat",
            "evidence_refs": ["inventory:skill:dusty", "rule:skill-overrides"],
            "impact": {"ordinal": "med"}, "risk": "low",
            "metric": {"key": "base_context_est", "direction": "down", "scope": "global"},
            "action": {"harness": "claude-code", "tier": "A", "type": "setting_change",
                       "payload": {"file": "settings.json",
                                   "key_path": ["skillOverrides", "dusty"], "value": "off"}}}


class TestSynth(unittest.TestCase):
    def test_pipeline_drops_and_ranks(self):
        bad_cite = bloat_rec(); bad_cite["evidence_refs"] = ["inventory:skill:ghost"]
        evil = bloat_rec(); evil["action"]["payload"]["key_path"] = ["hooks", "PostToolUse"]
        dup = bloat_rec()
        findings, dropped = synth.synthesize([bloat_rec(), bad_cite, evil, dup], EV, {}, DATA)
        self.assertEqual(len(findings), 1)
        self.assertEqual(dropped["citations"], 1)
        self.assertEqual(dropped["guard"], 1)
        self.assertEqual(findings[0]["delta_tokens"], 9000)   # 90 tok * 100 sessions

    def test_ledger_suppression_and_resurface(self):
        r = bloat_rec()
        rid = schema.rec_id(r)
        led = {rid: {"status": "rejected", "reason": "keep it",
                     "evidence_hash": schema.evidence_hash(r)}}
        findings, dropped = synth.synthesize([bloat_rec()], EV, led, DATA)
        self.assertEqual(findings, [])
        self.assertEqual(dropped["suppressed"][0]["reason"], "rejected: keep it")
        # evidence changed -> resurfaces, carrying the prior rejection
        led[rid]["evidence_hash"] = "OLDHASH"
        findings, _ = synth.synthesize([bloat_rec()], EV, led, DATA)
        self.assertEqual(findings[0]["prior_rejection"], "keep it")

    def test_guard_paths(self):
        ok = {"action": {"type": "file_create", "tier": "A",
                         "payload": {"path": "/fakehome/.claude/agents/x.md", "content": "c"}}}
        bad = {"action": {"type": "file_create", "tier": "A",
                          "payload": {"path": "/etc/cron.d/x.md", "content": "c"}}}
        self.assertTrue(synth.guard(ok, DATA))
        self.assertFalse(synth.guard(bad, DATA))

    def test_guard_rejects_sibling_and_traversal(self):
        sib = {"action": {"type": "file_create", "tier": "A",
                          "payload": {"path": "/fakehome/.claude/skills-evil/x.md",
                                      "content": "c"}}}
        trav = {"action": {"type": "file_create", "tier": "A",
                           "payload": {"path": "/fakehome/.claude/skills/../../../etc/x.md",
                                       "content": "c"}}}
        self.assertFalse(synth.guard(sib, DATA))
        self.assertFalse(synth.guard(trav, DATA))

    def test_fenced_output_parses(self):
        import json, tempfile
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            f.write("```json\n" + json.dumps([bloat_rec()]) + "\n```")
        self.assertEqual(len(synth.load_analyst_output(f.name)), 1)

    def test_adversarial_junk_degrades_not_crashes(self):
        import tempfile
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            f.write('```json[{"title": "x"}]```')  # single-line fence, no newline
        self.assertEqual(synth.load_analyst_output(f.name), [])
        self.assertFalse(synth.check_citation(None, EV))
        self.assertFalse(synth.check_citation(42, EV))

    def test_main_writes_findings_with_600_perms(self):
        import json, tempfile
        base = pathlib.Path(tempfile.mkdtemp())
        ev, state = base / "ev", base / "state"
        ev.mkdir()
        (ev / "usage.json").write_text(json.dumps({"totals": {"sessions": 0}}))
        (ev / "sessions.json").write_text(json.dumps({"sessions": []}))
        (ev / "activation.json").write_text(json.dumps({"items": {}}))
        (ev / "samples.json").write_text(json.dumps({"samples": []}))
        rules = base / "rules.json"
        rules.write_text(json.dumps({"rules": []}))
        analyst = base / "miner.json"
        analyst.write_text("[]")
        out = base / "findings.json"
        synth.main(["--evidence", str(ev), "--data-root", str(base / "claude"),
                    "--state", str(state), "--rules", str(rules),
                    "--analyst", str(analyst), "--out", str(out)])
        mode = out.stat().st_mode & 0o777
        self.assertEqual(mode, 0o600)


if __name__ == "__main__":
    unittest.main()
