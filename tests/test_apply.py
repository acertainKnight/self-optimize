import json, sys, pathlib, tempfile, unittest, unittest.mock
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "scripts"))
import apply as apply_mod
import ledger


def setting_rec():
    return {"title": "Disable dusty", "category": "bloat",
            "evidence_refs": ["inventory:skill:dusty"],
            "impact": {"ordinal": "med"}, "risk": "low",
            "metric": {"key": "correction_rate", "direction": "down", "scope": "global"},
            "action": {"harness": "claude-code", "tier": "A", "type": "setting_change",
                       "payload": {"file": "settings.json",
                                   "key_path": ["skillOverrides", "dusty"], "value": "off"}}}


class TestApply(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = pathlib.Path(self.tmp.name)
        self.state, self.data, self.ev = base / "state", base / "claude", base / "ev"
        for d in (self.state, self.data, self.ev):
            d.mkdir()
        (self.data / "settings.json").write_text(json.dumps({"model": "opusplan"}))
        (self.ev / "sessions.json").write_text(json.dumps(
            {"sessions": [{"started_at": "2026-06-01", "corrections_count": 2,
                           "input_tokens": 1, "output_tokens": 1}]}))
        self.lpath = self.state / "state" / "ledger.jsonl"
        ledger.append(self.lpath, {"id": "r1", "status": "proposed", "rec": setting_rec(),
                                   "evidence_hash": "e1"})

    def tearDown(self):
        self.tmp.cleanup()

    def test_apply_then_rollback_roundtrip(self):
        original = (self.data / "settings.json").read_text()
        apply_mod.cmd_apply(["r1"], self.state, self.data, self.ev)
        obj = json.loads((self.data / "settings.json").read_text())
        self.assertEqual(obj["skillOverrides"]["dusty"], "off")
        e = ledger.load(self.lpath)["r1"]
        self.assertEqual(e["status"], "applied")
        self.assertEqual(e["baseline"], {"value": 2.0, "n_sessions": 1, "samples": [2.0]})
        self.assertTrue(pathlib.Path(e["snapshot"]).exists())
        self.assertTrue(ledger.load(self.lpath)["r1"]["applied_at"].endswith("Z"))
        apply_mod.cmd_rollback("r1", self.state)
        self.assertEqual((self.data / "settings.json").read_text(), original)
        self.assertEqual(ledger.load(self.lpath)["r1"]["status"], "rolled_back")

    def test_smoke_failure_restores_and_marks(self):
        rec = setting_rec()
        rec["action"] = {"harness": "claude-code", "tier": "A", "type": "file_create",
                         "payload": {"path": str(self.data / "agents" / "bad.md"),
                                     "content": "---\nname: bad\nmodel: gpt-9\n---\n"}}
        ledger.append(self.lpath, {"id": "r2", "status": "proposed", "rec": rec,
                                   "evidence_hash": "e2"})
        apply_mod.cmd_apply(["r2"], self.state, self.data, self.ev)
        self.assertFalse((self.data / "agents" / "bad.md").exists())   # restored (deleted)
        self.assertEqual(ledger.load(self.lpath)["r2"]["status"], "apply_failed")

    def test_reject_records_reason(self):
        apply_mod.cmd_reject("r1", "keep it around", self.state)
        e = ledger.load(self.lpath)["r1"]
        self.assertEqual((e["status"], e["reason"]), ("rejected", "keep it around"))

    def test_duplicate_ids_apply_once(self):
        apply_mod.cmd_apply(["r1", "r1"], self.state, self.data, self.ev)
        snaps = list((self.state / "state" / "snapshots").rglob("r1"))
        self.assertEqual(len(snaps), 1)
        self.assertEqual(ledger.load(self.lpath)["r1"]["status"], "applied")

    def test_missing_sessions_evidence_fails_cleanly(self):
        import tempfile
        empty = pathlib.Path(tempfile.mkdtemp())
        apply_mod.cmd_apply(["r1"], self.state, self.data, empty)   # must not raise
        self.assertEqual(ledger.load(self.lpath)["r1"]["status"], "proposed")

    def test_write_failure_restores_and_records(self):
        from unittest import mock
        original = (self.data / "settings.json").read_text()
        with mock.patch.object(pathlib.Path, "write_text", side_effect=OSError("disk full")):
            apply_mod.cmd_apply(["r1"], self.state, self.data, self.ev)
        self.assertEqual((self.data / "settings.json").read_text(), original)
        e = ledger.load(self.lpath)["r1"]
        self.assertEqual(e["status"], "apply_failed")
        self.assertTrue(any("io:" in x for x in e.get("errors", [])))

    def test_baseline_samples_capped_at_fifty(self):
        many = {"sessions": [{"started_at": f"2026-05-{(i % 28) + 1:02d}", "corrections_count": i,
                              "input_tokens": 1, "output_tokens": 1} for i in range(60)]}
        (self.ev / "sessions.json").write_text(json.dumps(many))
        apply_mod.cmd_apply(["r1"], self.state, self.data, self.ev)
        e = ledger.load(self.lpath)["r1"]
        self.assertEqual(len(e["baseline"]["samples"]), 50)
        self.assertEqual(e["baseline"]["samples"], [float(x) for x in range(10, 60)])

    def test_file_replace_apply_and_rollback_roundtrip(self):
        agent_dir = self.data / "agents"
        agent_dir.mkdir()
        target = agent_dir / "helper.md"
        target.write_text("---\nname: helper\nmodel: sonnet\n---\nold body\n")
        rec = setting_rec()
        rec["action"] = {"harness": "claude-code", "tier": "A", "type": "file_replace",
                         "payload": {"path": str(target),
                                     "content": "---\nname: helper\nmodel: sonnet\n---\nnew body\n"}}
        ledger.append(self.lpath, {"id": "r3", "status": "proposed", "rec": rec,
                                   "evidence_hash": "e3"})
        apply_mod.cmd_apply(["r3"], self.state, self.data, self.ev)
        self.assertIn("new body", target.read_text())
        self.assertEqual(ledger.load(self.lpath)["r3"]["status"], "applied")
        apply_mod.cmd_rollback("r3", self.state)
        self.assertIn("old body", target.read_text())
        self.assertEqual(ledger.load(self.lpath)["r3"]["status"], "rolled_back")

    def test_file_replace_must_exist_fails_apply(self):
        rec = setting_rec()
        rec["action"] = {"harness": "claude-code", "tier": "A", "type": "file_replace",
                         "payload": {"path": str(self.data / "agents" / "ghost.md"), "content": "c"}}
        ledger.append(self.lpath, {"id": "r4", "status": "proposed", "rec": rec,
                                   "evidence_hash": "e4"})
        apply_mod.cmd_apply(["r4"], self.state, self.data, self.ev)
        self.assertEqual(ledger.load(self.lpath)["r4"]["status"], "apply_failed")

    def test_file_replace_traversal_reject_fails_apply(self):
        rec = setting_rec()
        rec["action"] = {"harness": "claude-code", "tier": "A", "type": "file_replace",
                         "payload": {"path": "/etc/passwd", "content": "c"}}
        ledger.append(self.lpath, {"id": "r5", "status": "proposed", "rec": rec,
                                   "evidence_hash": "e5"})
        apply_mod.cmd_apply(["r5"], self.state, self.data, self.ev)
        self.assertEqual(ledger.load(self.lpath)["r5"]["status"], "apply_failed")

    def test_memory_root_file_create_via_inventory_extra_roots(self):
        mem = self.data.parent / "mem"
        mem.mkdir()
        (self.ev / "inventory.json").write_text(json.dumps(
            {"settings": {"autoMemoryDirectory": str(mem)}}))
        rec = setting_rec()
        rec["action"] = {"harness": "claude-code", "tier": "A", "type": "file_create",
                         "payload": {"path": str(mem / "new-note.md"),
                                     "content": "---\nname: new-note\n---\nbody\n"}}
        ledger.append(self.lpath, {"id": "r6", "status": "proposed", "rec": rec,
                                   "evidence_hash": "e6"})
        apply_mod.cmd_apply(["r6"], self.state, self.data, self.ev)
        self.assertTrue((mem / "new-note.md").exists())
        self.assertEqual(ledger.load(self.lpath)["r6"]["status"], "applied")


def assist_rec():
    return {"title": "Trim CLAUDE.md", "category": "claude-md",
            "evidence_refs": ["usage:totals.sessions"],
            "impact": {"ordinal": "high"}, "risk": "med",
            "metric": {"key": "none"},
            "action": {"harness": "claude-code", "tier": "B", "type": "diff",
                       "payload": {"file": "/x", "diff": "- old\n+ new"}}}


class TestDecide(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = pathlib.Path(self.tmp.name)
        self.state, self.data, self.ev = base / "state", base / "claude", base / "ev"
        self.downloads = base / "downloads"
        for d in (self.state, self.data, self.ev, self.downloads):
            d.mkdir()
        (self.data / "settings.json").write_text(json.dumps({"model": "opusplan"}))
        (self.ev / "sessions.json").write_text(json.dumps(
            {"sessions": [{"started_at": "2026-06-01", "corrections_count": 2,
                           "input_tokens": 1, "output_tokens": 1}]}))
        self.lpath = self.state / "state" / "ledger.jsonl"
        ledger.append(self.lpath, {"id": "r1", "status": "proposed", "rec": setting_rec(),
                                   "evidence_hash": "e1"})
        ledger.append(self.lpath, {"id": "r2", "status": "proposed", "rec": setting_rec(),
                                   "evidence_hash": "e2"})
        ledger.append(self.lpath, {"id": "r3", "status": "proposed", "rec": assist_rec(),
                                   "evidence_hash": "e3"})

    def tearDown(self):
        self.tmp.cleanup()

    def _decisions(self, **overrides):
        # run_id must match the evidence dir's basename or decide refuses the file
        payload = {"run_id": "ev", "apply": ["r1"],
                   "reject": [{"id": "r2", "reason": "not now"}], "assist": ["r3"]}
        payload.update(overrides)
        return payload

    def test_decide_applies_rejects_and_reports_assist(self):
        dfile = self.downloads / "self-optimize-decisions-ev.json"
        # "ghost" never applies (unknown id) — must not be reported as applied
        dfile.write_text(json.dumps(self._decisions(apply=["r1", "ghost"])))
        result = apply_mod.cmd_decide(str(dfile), self.state, self.data, self.ev)
        led = ledger.load(self.lpath)
        self.assertEqual(led["r1"]["status"], "applied")
        self.assertEqual(led["r2"]["status"], "rejected")
        self.assertEqual(led["r2"]["reason"], "not now")
        self.assertEqual(result["applied"], ["r1"])
        self.assertEqual(result["assist"], [{"id": "r3", "title": "Trim CLAUDE.md",
                                             "payload_type": "diff"}])
        # tier-B payload never executed: no file at the diff's target path
        self.assertFalse(pathlib.Path("/x").exists())

    def test_run_id_mismatch_refuses_file(self):
        dfile = self.downloads / "self-optimize-decisions-other.json"
        dfile.write_text(json.dumps(self._decisions(run_id="other-run")))
        result = apply_mod.cmd_decide(str(dfile), self.state, self.data, self.ev)
        self.assertEqual(result, {})
        self.assertEqual(ledger.load(self.lpath)["r1"]["status"], "proposed")

    def test_missing_file_prints_clean_message_and_returns_empty(self):
        missing = self.downloads / "nope.json"
        result = apply_mod.cmd_decide(str(missing), self.state, self.data, self.ev)
        self.assertEqual(result, {})
        self.assertEqual(ledger.load(self.lpath)["r1"]["status"], "proposed")

    def test_newest_in_downloads_selected_when_no_path_given(self):
        older = self.downloads / "self-optimize-decisions-old.json"
        newer = self.downloads / "self-optimize-decisions-new.json"
        older.write_text(json.dumps(self._decisions(apply=["r2"], reject=[], assist=[])))
        import os
        older_time = older.stat().st_mtime
        newer.write_text(json.dumps(self._decisions(apply=["r1"], reject=[], assist=[])))
        os.utime(newer, (older_time + 10, older_time + 10))  # deterministic mtime ordering
        with unittest.mock.patch.object(apply_mod, "_downloads_dir", return_value=self.downloads):
            apply_mod.cmd_decide(None, self.state, self.data, self.ev)
        # the newer file (apply r1) should have been used, not the older (apply r2)
        led = ledger.load(self.lpath)
        self.assertEqual(led["r1"]["status"], "applied")
        self.assertEqual(led["r2"]["status"], "proposed")

    def test_no_downloads_match_prints_clean_message(self):
        with unittest.mock.patch.object(apply_mod, "_downloads_dir", return_value=self.downloads):
            result = apply_mod.cmd_decide(None, self.state, self.data, self.ev)
        self.assertEqual(result, {})


if __name__ == "__main__":
    unittest.main()
