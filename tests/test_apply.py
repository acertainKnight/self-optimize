import contextlib, io, json, sys, pathlib, tempfile, unittest, unittest.mock
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "scripts"))
import apply as apply_mod
import ledger
import schema


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

    def test_diff_rec_applies_with_snapshot_and_rollback(self):
        agent_dir = self.data / "agents"
        agent_dir.mkdir()
        target = agent_dir / "helper.md"
        original = "---\nname: helper\nmodel: sonnet\n---\nold body\n"
        target.write_text(original)
        rec = setting_rec()
        rec["action"] = {"harness": "claude-code", "tier": "B", "type": "diff",
                         "payload": {"file": str(target),
                                     "diff": "@@ -5,1 +5,1 @@\n-old body\n+new body\n"}}
        ledger.append(self.lpath, {"id": "r7", "status": "proposed", "rec": rec,
                                   "evidence_hash": "e7"})
        apply_mod.cmd_apply(["r7"], self.state, self.data, self.ev)
        self.assertEqual(target.read_text(), "---\nname: helper\nmodel: sonnet\n---\nnew body\n")
        e = ledger.load(self.lpath)["r7"]
        self.assertEqual(e["status"], "applied")
        self.assertTrue(pathlib.Path(e["snapshot"]).exists())
        apply_mod.cmd_rollback("r7", self.state)
        self.assertEqual(target.read_text(), original)
        self.assertEqual(ledger.load(self.lpath)["r7"]["status"], "rolled_back")

    def test_diff_rec_non_string_diff_payload_fails_cleanly_not_crash(self):
        # a non-str "diff" field (e.g. a hand-typed amend action, or a
        # malformed analyst payload) would AttributeError inside
        # _apply_unified_diff's .split("\n") -- must mark apply_failed, not
        # crash the whole apply/decide batch.
        rec = setting_rec()
        rec["action"] = {"harness": "claude-code", "tier": "B", "type": "diff",
                         "payload": {"file": "/x/CLAUDE.md", "diff": None}}
        ledger.append(self.lpath, {"id": "r9", "status": "proposed", "rec": rec,
                                   "evidence_hash": "e9"})
        apply_mod.cmd_apply(["r9"], self.state, self.data, self.ev)   # must not raise
        e = ledger.load(self.lpath)["r9"]
        self.assertEqual(e["status"], "apply_failed")
        self.assertTrue(any("malformed payload" in x for x in e.get("errors", [])))

    def test_manual_rec_skipped_with_handoff_message(self):
        rec = setting_rec()
        rec["action"] = {"harness": "claude-code", "tier": "B", "type": "manual",
                         "payload": {"description": "install the plugin by hand"}}
        ledger.append(self.lpath, {"id": "r8", "status": "proposed", "rec": rec,
                                   "evidence_hash": "e8"})
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            apply_mod.cmd_apply(["r8"], self.state, self.data, self.ev)
        self.assertIn("manual action", buf.getvalue())
        self.assertEqual(ledger.load(self.lpath)["r8"]["status"], "proposed")   # untouched

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
        # only r1/r2/r3 belong to THIS run's findings; r4 is an unrelated open-backlog
        # entry that a stale/planted decisions file must NOT be able to reach
        (self.ev / "findings.json").write_text(json.dumps(
            {"findings": [{"id": "r1"}, {"id": "r2"}, {"id": "r3"}], "dropped": {}}))
        self.lpath = self.state / "state" / "ledger.jsonl"
        ledger.append(self.lpath, {"id": "r1", "status": "proposed", "rec": setting_rec(),
                                   "evidence_hash": "e1"})
        ledger.append(self.lpath, {"id": "r2", "status": "proposed", "rec": setting_rec(),
                                   "evidence_hash": "e2"})
        ledger.append(self.lpath, {"id": "r3", "status": "proposed", "rec": assist_rec(),
                                   "evidence_hash": "e3"})
        ledger.append(self.lpath, {"id": "r4", "status": "proposed", "rec": setting_rec(),
                                   "evidence_hash": "e4"})

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

    def test_id_not_in_run_findings_refused(self):
        # r4 is proposed tier-A in the ledger but not in this run's findings.json:
        # a decisions file must not be able to reach open-backlog ids
        dfile = self.downloads / "self-optimize-decisions-ev.json"
        dfile.write_text(json.dumps(self._decisions(apply=["r4"], reject=[], assist=[])))
        result = apply_mod.cmd_decide(str(dfile), self.state, self.data, self.ev)
        self.assertEqual(ledger.load(self.lpath)["r4"]["status"], "proposed")
        self.assertNotIn("r4", result.get("applied", []))

    def test_missing_findings_refuses_whole_decide(self):
        (self.ev / "findings.json").unlink()
        dfile = self.downloads / "self-optimize-decisions-ev.json"
        dfile.write_text(json.dumps(self._decisions()))
        result = apply_mod.cmd_decide(str(dfile), self.state, self.data, self.ev)
        self.assertEqual(result, {})
        self.assertEqual(ledger.load(self.lpath)["r1"]["status"], "proposed")

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

    def test_decide_persists_log_with_apply_and_reject(self):
        dfile = self.downloads / "self-optimize-decisions-ev.json"
        dfile.write_text(json.dumps(self._decisions()))
        result = apply_mod.cmd_decide(str(dfile), self.state, self.data, self.ev)
        decisions_dir = self.state / "state" / "decisions"
        files = list(decisions_dir.iterdir())
        self.assertEqual(len(files), 1)
        logged = json.loads(files[0].read_text())
        self.assertEqual(logged["apply"], ["r1"])
        self.assertEqual(logged["reject"], [{"id": "r2", "reason": "not now"}])
        self.assertEqual(logged["assist"], [{"id": "r3", "title": "Trim CLAUDE.md",
                                             "payload_type": "diff"}])
        self.assertEqual(logged["source_file"], str(dfile))
        self.assertEqual(logged["run_id"], "ev")
        self.assertTrue(logged["decided_at"].endswith("Z"))
        self.assertEqual(files[0].stat().st_mode & 0o777, 0o600)
        self.assertEqual(result["log"], str(files[0]))

    def test_decide_log_records_refused_out_of_run_id(self):
        # r4 is proposed tier-A in the ledger but not in this run's findings.json
        dfile = self.downloads / "self-optimize-decisions-ev.json"
        dfile.write_text(json.dumps(self._decisions(apply=["r4"], reject=[], assist=[])))
        apply_mod.cmd_decide(str(dfile), self.state, self.data, self.ev)
        decisions_dir = self.state / "state" / "decisions"
        files = list(decisions_dir.iterdir())
        self.assertEqual(len(files), 1)
        logged = json.loads(files[0].read_text())
        self.assertEqual(logged["refused"], ["r4"])
        self.assertEqual(logged["apply"], [])

    def test_amend_valid_action_rejects_original_and_applies_replacement(self):
        # r1 is a skillOverrides-name-only-shaped rec (actually "off" here, doesn't
        # matter); amend it into a plugin-disable action instead.
        dfile = self.downloads / "self-optimize-decisions-ev.json"
        amend_action = {"harness": "claude-code", "tier": "A", "type": "setting_change",
                         "payload": {"file": "settings.json",
                                     "key_path": ["enabledPlugins", "some-plugin"], "value": False}}
        dfile.write_text(json.dumps(self._decisions(
            apply=[], reject=[], assist=[],
            amend=[{"id": "r1", "reason": "prefer disabling the whole plugin",
                    "action": amend_action}])))
        result = apply_mod.cmd_decide(str(dfile), self.state, self.data, self.ev)
        led = ledger.load(self.lpath)
        self.assertEqual(led["r1"]["status"], "rejected")
        self.assertEqual(led["r1"]["reason"], "amended: prefer disabling the whole plugin")
        self.assertEqual(len(result["amended"]), 1)
        new_id = result["amended"][0]["new"]
        self.assertEqual(result["amended"][0]["orig"], "r1")
        self.assertEqual(result["amended"][0]["title"], "Amended: Disable dusty")
        self.assertEqual(led[new_id]["status"], "applied")
        self.assertEqual(led[new_id]["rec"]["action"], amend_action)
        settings = json.loads((self.data / "settings.json").read_text())
        self.assertEqual(settings["enabledPlugins"]["some-plugin"], False)
        self.assertEqual(result["amend_refused"], [])
        # decision log records the requested amend and its outcome
        logged = json.loads(list((self.state / "state" / "decisions").iterdir())[0].read_text())
        self.assertEqual(logged["amend"], [{"id": "r1", "reason": "prefer disabling the whole plugin",
                                            "action": amend_action}])
        self.assertEqual(logged["amended"], result["amended"])
        self.assertEqual(logged["amend_refused"], [])

    def test_amend_malicious_key_path_refused_leaves_original_untouched(self):
        # key_path root "hooks" is outside synth.guard's ALLOWED_SETTING_ROOTS
        dfile = self.downloads / "self-optimize-decisions-ev.json"
        malicious = {"harness": "claude-code", "tier": "A", "type": "setting_change",
                     "payload": {"file": "settings.json", "key_path": ["hooks", "x"], "value": "evil"}}
        dfile.write_text(json.dumps(self._decisions(
            apply=[], reject=[], assist=[],
            amend=[{"id": "r2", "reason": "try something else", "action": malicious}])))
        result = apply_mod.cmd_decide(str(dfile), self.state, self.data, self.ev)
        led = ledger.load(self.lpath)
        self.assertEqual(led["r2"]["status"], "proposed")   # untouched, not rejected
        self.assertEqual(result["amended"], [])
        self.assertEqual(len(result["amend_refused"]), 1)
        self.assertEqual(result["amend_refused"][0]["id"], "r2")
        settings = json.loads((self.data / "settings.json").read_text())
        self.assertNotIn("hooks", settings)

    def test_amend_malicious_file_create_outside_sanctioned_roots_refused(self):
        dfile = self.downloads / "self-optimize-decisions-ev.json"
        malicious = {"harness": "claude-code", "tier": "A", "type": "file_create",
                     "payload": {"path": "/tmp/self-optimize-test-evil.md", "content": "pwned"}}
        dfile.write_text(json.dumps(self._decisions(
            apply=[], reject=[], assist=[],
            amend=[{"id": "r2", "reason": "try something else", "action": malicious}])))
        result = apply_mod.cmd_decide(str(dfile), self.state, self.data, self.ev)
        self.assertEqual(ledger.load(self.lpath)["r2"]["status"], "proposed")
        self.assertEqual(result["amended"], [])
        self.assertEqual(len(result["amend_refused"]), 1)
        self.assertFalse(pathlib.Path("/tmp/self-optimize-test-evil.md").exists())

    def test_amend_id_not_in_run_findings_refused(self):
        # r4 is proposed tier-A in the ledger but not in this run's findings.json
        dfile = self.downloads / "self-optimize-decisions-ev.json"
        valid_action = {"harness": "claude-code", "tier": "A", "type": "setting_change",
                        "payload": {"file": "settings.json",
                                    "key_path": ["skillOverrides", "dusty"], "value": "name-only"}}
        dfile.write_text(json.dumps(self._decisions(
            apply=[], reject=[], assist=[],
            amend=[{"id": "r4", "reason": "not this run", "action": valid_action}])))
        result = apply_mod.cmd_decide(str(dfile), self.state, self.data, self.ev)
        self.assertEqual(ledger.load(self.lpath)["r4"]["status"], "proposed")
        self.assertIn("r4", result["refused"])
        self.assertEqual(result["amended"], [])
        self.assertEqual(result["amend_refused"], [])

    def test_duplicate_amend_ids_processed_once(self):
        dfile = self.downloads / "self-optimize-decisions-ev.json"
        action = {"harness": "claude-code", "tier": "A", "type": "setting_change",
                  "payload": {"file": "settings.json",
                              "key_path": ["skillOverrides", "dusty"], "value": "name-only"}}
        dfile.write_text(json.dumps(self._decisions(
            apply=[], reject=[], assist=[],
            amend=[{"id": "r1", "reason": "try name-only", "action": action},
                   {"id": "r1", "reason": "try name-only", "action": action}])))
        result = apply_mod.cmd_decide(str(dfile), self.state, self.data, self.ev)
        self.assertEqual(len(result["amended"]), 1)
        led = ledger.load(self.lpath)
        self.assertEqual(led["r1"]["status"], "rejected")

    def test_amend_tier_b_replacement_refused_not_silently_applied(self):
        # a tier-B action passes validate_rec + synth.guard (guard allows tier-B
        # through since it's ordinarily report-only) but amend always auto-applies,
        # so a tier-B "replacement" can never be honestly reported as amended
        dfile = self.downloads / "self-optimize-decisions-ev.json"
        tier_b_action = {"harness": "claude-code", "tier": "B", "type": "manual",
                         "payload": {"description": "do something by hand"}}
        dfile.write_text(json.dumps(self._decisions(
            apply=[], reject=[], assist=[],
            amend=[{"id": "r1", "reason": "try tier B", "action": tier_b_action}])))
        result = apply_mod.cmd_decide(str(dfile), self.state, self.data, self.ev)
        self.assertEqual(ledger.load(self.lpath)["r1"]["status"], "proposed")
        self.assertEqual(result["amended"], [])
        self.assertEqual(len(result["amend_refused"]), 1)

    def test_amend_replacement_apply_failure_leaves_original_untouched(self):
        # the replacement is a validly-guarded but unappliable action (target file
        # already exists) -> cmd_apply marks it apply_failed; the original must NOT
        # be rejected out from under a failed amend
        agent_dir = self.data / "agents"
        agent_dir.mkdir()
        existing = agent_dir / "already-here.md"
        existing.write_text("---\nname: already-here\n---\nold\n")
        dfile = self.downloads / "self-optimize-decisions-ev.json"
        doomed_action = {"harness": "claude-code", "tier": "A", "type": "file_create",
                         "payload": {"path": str(existing), "content": "---\nname: x\n---\nnew\n"}}
        dfile.write_text(json.dumps(self._decisions(
            apply=[], reject=[], assist=[],
            amend=[{"id": "r1", "reason": "try a new file", "action": doomed_action}])))
        result = apply_mod.cmd_decide(str(dfile), self.state, self.data, self.ev)
        self.assertEqual(ledger.load(self.lpath)["r1"]["status"], "proposed")
        self.assertEqual(result["amended"], [])
        self.assertEqual(len(result["amend_refused"]), 1)
        self.assertIn("failed to apply", result["amend_refused"][0]["reason"])
        self.assertEqual(existing.read_text(), "---\nname: already-here\n---\nold\n")

    def test_amend_missing_payload_key_does_not_crash_decide(self):
        # payload has no "value" for a setting_change; schema/guard only check
        # shape, not every payload key, so templates.render's p["value"] would
        # KeyError — decide must handle this cleanly, not crash mid-run
        dfile = self.downloads / "self-optimize-decisions-ev.json"
        broken_action = {"harness": "claude-code", "tier": "A", "type": "setting_change",
                         "payload": {"file": "settings.json", "key_path": ["skillOverrides", "dusty"]}}
        dfile.write_text(json.dumps(self._decisions(
            apply=[], reject=[], assist=[],
            amend=[{"id": "r1", "reason": "missing value", "action": broken_action}])))
        result = apply_mod.cmd_decide(str(dfile), self.state, self.data, self.ev)   # must not raise
        self.assertEqual(ledger.load(self.lpath)["r1"]["status"], "proposed")
        self.assertEqual(result["amended"], [])
        self.assertEqual(len(result["amend_refused"]), 1)
        self.assertIn("log", result)   # decision log still gets written

    def test_amend_malformed_entries_reported_not_dropped(self):
        # blank reason and non-dict action must surface in amend_refused, not vanish
        valid_action = {"harness": "claude-code", "tier": "A", "type": "setting_change",
                        "payload": {"file": "settings.json",
                                    "key_path": ["skillOverrides", "dusty"], "value": "off"}}
        dfile = self.downloads / "self-optimize-decisions-ev.json"
        dfile.write_text(json.dumps(self._decisions(
            apply=[], reject=[], assist=[],
            amend=[{"id": "r1", "reason": "   ", "action": valid_action},
                   {"id": "r2", "reason": "fine", "action": "not-a-dict"}])))
        result = apply_mod.cmd_decide(str(dfile), self.state, self.data, self.ev)
        self.assertEqual(ledger.load(self.lpath)["r1"]["status"], "proposed")
        self.assertEqual(ledger.load(self.lpath)["r2"]["status"], "proposed")
        self.assertEqual(result["amended"], [])
        refused_ids = {r["id"] for r in result["amend_refused"]}
        self.assertEqual(refused_ids, {"r1", "r2"})

    def test_amend_collision_with_live_proposed_finding_refused(self):
        # THE hijack: amend finding A into an action byte-identical to another live,
        # still-undecided finding B's payload. rec_id(replacement) == B's id, and B
        # is 'proposed'. A collision guard that treats 'proposed' as overwritable
        # would hijack B's ledger row to applied (with A's amended title) and
        # suppress B with no human decision. Must be refused: A stays proposed, B
        # stays proposed and untouched, nothing applied.
        recA = setting_rec()   # category bloat, action = skillOverrides dusty off
        id_a = schema.rec_id(recA)
        recB = setting_rec()
        recB["title"] = "Disable someplug"
        recB["action"] = {"harness": "claude-code", "tier": "A", "type": "setting_change",
                          "payload": {"file": "settings.json",
                                      "key_path": ["enabledPlugins", "someplug"], "value": False}}
        id_b = schema.rec_id(recB)
        ledger.append(self.lpath, {"id": id_a, "status": "proposed", "rec": recA,
                                   "evidence_hash": "ea"})
        ledger.append(self.lpath, {"id": id_b, "status": "proposed", "rec": recB,
                                   "evidence_hash": "eb"})
        findings = json.loads((self.ev / "findings.json").read_text())
        findings["findings"] += [{"id": id_a}, {"id": id_b}]
        (self.ev / "findings.json").write_text(json.dumps(findings))
        dfile = self.downloads / "self-optimize-decisions-ev.json"
        dfile.write_text(json.dumps(self._decisions(
            apply=[], reject=[], assist=[],
            amend=[{"id": id_a, "reason": "actually disable the plugin",
                    "action": recB["action"]}])))
        result = apply_mod.cmd_decide(str(dfile), self.state, self.data, self.ev)
        led = ledger.load(self.lpath)
        self.assertEqual(led[id_a]["status"], "proposed")       # A untouched
        self.assertEqual(led[id_b]["status"], "proposed")       # B NOT hijacked
        self.assertEqual(led[id_b]["rec"]["title"], "Disable someplug")   # B title intact
        self.assertEqual(result["amended"], [])
        self.assertEqual(len(result["amend_refused"]), 1)
        self.assertIn("collides", result["amend_refused"][0]["reason"])

    def test_amend_new_id_collision_with_existing_ledger_entry_refused(self):
        # rec_id hashes only category|type|payload — simulate an unrelated,
        # already-applied ledger entry that happens to hash to the same id this
        # amend's replacement would mint, and confirm decide refuses rather than
        # silently clobbering that unrelated entry's ledger history
        colliding_action = {"harness": "claude-code", "tier": "A", "type": "setting_change",
                            "payload": {"file": "settings.json",
                                        "key_path": ["enabledPlugins", "collide-plugin"], "value": False}}
        replacement_preview = dict(setting_rec())
        replacement_preview["action"] = colliding_action
        collide_id = schema.rec_id(replacement_preview)
        ledger.append(self.lpath, {"id": collide_id, "status": "applied",
                                   "rec": {"title": "unrelated prior change"},
                                   "snapshot": "/tmp/fake-snap-does-not-exist", "files": [],
                                   "evidence_hash": "prior"})
        dfile = self.downloads / "self-optimize-decisions-ev.json"
        dfile.write_text(json.dumps(self._decisions(
            apply=[], reject=[], assist=[],
            amend=[{"id": "r2", "reason": "disable collide-plugin instead",
                    "action": colliding_action}])))
        result = apply_mod.cmd_decide(str(dfile), self.state, self.data, self.ev)
        led = ledger.load(self.lpath)
        self.assertEqual(led["r2"]["status"], "proposed")
        self.assertEqual(result["amended"], [])
        self.assertEqual(len(result["amend_refused"]), 1)
        self.assertIn("collides", result["amend_refused"][0]["reason"])
        # the pre-existing colliding entry must be untouched
        self.assertEqual(led[collide_id]["status"], "applied")
        self.assertEqual(led[collide_id]["rec"]["title"], "unrelated prior change")

    def test_amend_non_dict_payload_refused_not_crash(self):
        # payload=[] is valid JSON, isinstance(action, dict) is True, but
        # synth.guard's p.get(...) calls would AttributeError on a list payload
        dfile = self.downloads / "self-optimize-decisions-ev.json"
        crashy_action = {"harness": "claude-code", "tier": "A", "type": "setting_change",
                         "payload": []}
        dfile.write_text(json.dumps(self._decisions(
            apply=[], reject=[], assist=[],
            amend=[{"id": "r1", "reason": "malformed on purpose", "action": crashy_action}])))
        result = apply_mod.cmd_decide(str(dfile), self.state, self.data, self.ev)   # must not raise
        self.assertEqual(ledger.load(self.lpath)["r1"]["status"], "proposed")
        self.assertEqual(result["amended"], [])
        self.assertEqual(len(result["amend_refused"]), 1)
        self.assertIn("log", result)

    def test_amend_unhashable_key_path_element_refused_not_crash(self):
        # a dict key_path element is unhashable -> dict.get(k) raises TypeError deep
        # inside templates.render if this ever reached cmd_apply; must be caught
        dfile = self.downloads / "self-optimize-decisions-ev.json"
        crashy_action = {"harness": "claude-code", "tier": "A", "type": "setting_change",
                         "payload": {"file": "settings.json",
                                     "key_path": ["skillOverrides", {"a": 1}], "value": "off"}}
        dfile.write_text(json.dumps(self._decisions(
            apply=[], reject=[], assist=[],
            amend=[{"id": "r1", "reason": "malformed key_path", "action": crashy_action}])))
        result = apply_mod.cmd_decide(str(dfile), self.state, self.data, self.ev)   # must not raise
        self.assertEqual(ledger.load(self.lpath)["r1"]["status"], "proposed")
        self.assertEqual(result["amended"], [])
        self.assertIn("log", result)

    def test_amend_identical_to_original_refused(self):
        # real ledger/synth ids ARE schema.rec_id(rec) — unlike this test file's
        # convenience literal ids ("r1" etc) — so a genuinely identical replacement
        # only collides with its own original id when the id is a real hash
        real_rec = setting_rec()
        real_id = schema.rec_id(real_rec)
        ledger.append(self.lpath, {"id": real_id, "status": "proposed", "rec": real_rec,
                                   "evidence_hash": "e-real"})
        findings = json.loads((self.ev / "findings.json").read_text())
        findings["findings"].append({"id": real_id})
        (self.ev / "findings.json").write_text(json.dumps(findings))
        dfile = self.downloads / "self-optimize-decisions-ev.json"
        dfile.write_text(json.dumps(self._decisions(
            apply=[], reject=[], assist=[],
            amend=[{"id": real_id, "reason": "no-op", "action": real_rec["action"]}])))
        result = apply_mod.cmd_decide(str(dfile), self.state, self.data, self.ev)
        self.assertEqual(ledger.load(self.lpath)[real_id]["status"], "proposed")
        self.assertEqual(result["amended"], [])
        self.assertEqual(len(result["amend_refused"]), 1)
        self.assertIn("identical", result["amend_refused"][0]["reason"])

    def test_amend_retry_after_failed_apply_succeeds(self):
        # first attempt: target already exists -> apply_failed, original untouched.
        # second attempt (same replacement, e.g. after the user removed the
        # conflict) must NOT be blocked by the leftover apply_failed ledger entry —
        # "fix and retry" must actually work
        agent_dir = self.data / "agents"
        agent_dir.mkdir()
        conflict = agent_dir / "conflict.md"
        conflict.write_text("---\nname: conflict\n---\nold\n")
        action = {"harness": "claude-code", "tier": "A", "type": "file_create",
                  "payload": {"path": str(conflict), "content": "---\nname: x\n---\nnew\n"}}
        dfile = self.downloads / "self-optimize-decisions-ev.json"
        dfile.write_text(json.dumps(self._decisions(
            apply=[], reject=[], assist=[],
            amend=[{"id": "r1", "reason": "first try", "action": action}])))
        first = apply_mod.cmd_decide(str(dfile), self.state, self.data, self.ev)
        self.assertEqual(len(first["amend_refused"]), 1)
        self.assertEqual(ledger.load(self.lpath)["r1"]["status"], "proposed")
        # fix the conflict, retry the exact same amend
        conflict.unlink()
        dfile2 = self.downloads / "self-optimize-decisions-ev2.json"
        dfile2.write_text(json.dumps(self._decisions(
            apply=[], reject=[], assist=[],
            amend=[{"id": "r1", "reason": "retry", "action": action}])))
        second = apply_mod.cmd_decide(str(dfile2), self.state, self.data, self.ev)
        self.assertEqual(len(second["amended"]), 1)
        self.assertEqual(ledger.load(self.lpath)["r1"]["status"], "rejected")
        self.assertTrue(conflict.exists())

    def test_amend_batch_collision_with_id_created_earlier_in_batch_refused(self):
        # per-iteration ledger reload must catch a collision against an id CREATED
        # earlier in the SAME batch: item 1 amends r1 into action Z (applied under a
        # fresh new_id N); item 2 amends r2 into the SAME action Z, which hashes to N
        # too. A stale pre-loop snapshot wouldn't see N and would clobber item 1's
        # just-applied row; the per-iteration reload sees N='applied' and refuses.
        action_z = {"harness": "claude-code", "tier": "A", "type": "setting_change",
                    "payload": {"file": "settings.json",
                                "key_path": ["enabledPlugins", "someplug"], "value": False}}
        expected_new = schema.rec_id({**setting_rec(), "action": action_z})
        dfile = self.downloads / "self-optimize-decisions-ev.json"
        dfile.write_text(json.dumps(self._decisions(
            apply=[], reject=[], assist=[],
            amend=[
                {"id": "r1", "reason": "disable someplug", "action": action_z},
                {"id": "r2", "reason": "same target", "action": action_z},
            ])))
        result = apply_mod.cmd_decide(str(dfile), self.state, self.data, self.ev)
        led = ledger.load(self.lpath)
        self.assertEqual(len(result["amended"]), 1)          # item 1 only
        self.assertEqual(result["amended"][0]["new"], expected_new)
        self.assertEqual(led["r1"]["status"], "rejected")
        self.assertEqual(led["r2"]["status"], "proposed")    # item 2 untouched
        self.assertEqual(len(result["amend_refused"]), 1)
        self.assertEqual(result["amend_refused"][0]["id"], "r2")
        self.assertIn("collides", result["amend_refused"][0]["reason"])
        # item 1's applied row must survive, not be clobbered by item 2
        self.assertEqual(led[expected_new]["status"], "applied")

    def test_amend_bearing_autopicked_file_proceeds(self):
        # amend rides the normal download→decide flow: an auto-picked file (no explicit
        # path) with amend entries proceeds like any other — decide is human-invoked and
        # the authored action is re-validated through guard/templates + snapshotted
        valid_action = {"harness": "claude-code", "tier": "A", "type": "setting_change",
                        "payload": {"file": "settings.json",
                                    "key_path": ["skillOverrides", "dusty"], "value": "name-only"}}
        dfile = self.downloads / "self-optimize-decisions-ev.json"
        dfile.write_text(json.dumps(self._decisions(
            apply=["r1"], reject=[], assist=[],
            amend=[{"id": "r2", "reason": "swap", "action": valid_action}])))
        with unittest.mock.patch.object(apply_mod, "_downloads_dir", return_value=self.downloads):
            result = apply_mod.cmd_decide(None, self.state, self.data, self.ev)   # auto-pick
        led = ledger.load(self.lpath)
        self.assertEqual(led["r1"]["status"], "applied")      # apply rode along fine
        self.assertEqual(led["r2"]["status"], "rejected")     # original amended-out
        self.assertEqual(len(result["amended"]), 1)           # replacement applied

    def test_amend_bearing_empty_string_path_proceeds(self):
        # "" is falsy → _find_decisions_file auto-picks; behaves identically to None
        valid_action = {"harness": "claude-code", "tier": "A", "type": "setting_change",
                        "payload": {"file": "settings.json",
                                    "key_path": ["skillOverrides", "dusty"], "value": "name-only"}}
        dfile = self.downloads / "self-optimize-decisions-ev.json"
        dfile.write_text(json.dumps(self._decisions(
            apply=[], reject=[], assist=[],
            amend=[{"id": "r2", "reason": "swap", "action": valid_action}])))
        with unittest.mock.patch.object(apply_mod, "_downloads_dir", return_value=self.downloads):
            result = apply_mod.cmd_decide("", self.state, self.data, self.ev)   # empty string
        self.assertEqual(len(result["amended"]), 1)
        self.assertEqual(ledger.load(self.lpath)["r2"]["status"], "rejected")

    def test_amend_bearing_explicit_path_proceeds(self):
        valid_action = {"harness": "claude-code", "tier": "A", "type": "setting_change",
                        "payload": {"file": "settings.json",
                                    "key_path": ["enabledPlugins", "someplug"], "value": False}}
        dfile = self.downloads / "self-optimize-decisions-ev.json"
        dfile.write_text(json.dumps(self._decisions(
            apply=[], reject=[], assist=[],
            amend=[{"id": "r1", "reason": "swap", "action": valid_action}])))
        result = apply_mod.cmd_decide(str(dfile), self.state, self.data, self.ev)   # explicit
        self.assertEqual(len(result["amended"]), 1)
        self.assertEqual(ledger.load(self.lpath)["r1"]["status"], "rejected")

    def test_autopick_apply_reject_only_still_works(self):
        # regression: an auto-picked file with NO amend proceeds as before
        dfile = self.downloads / "self-optimize-decisions-ev.json"
        dfile.write_text(json.dumps(self._decisions(apply=["r1"], reject=[], assist=[])))
        with unittest.mock.patch.object(apply_mod, "_downloads_dir", return_value=self.downloads):
            apply_mod.cmd_decide(None, self.state, self.data, self.ev)
        self.assertEqual(ledger.load(self.lpath)["r1"]["status"], "applied")

    def test_amend_refused_records_carry_attempted_action_for_replay(self):
        # forensic replay: a refused/malformed amend logs its attempted action
        malicious = {"harness": "claude-code", "tier": "A", "type": "setting_change",
                     "payload": {"file": "settings.json", "key_path": ["hooks", "x"], "value": "evil"}}
        dfile = self.downloads / "self-optimize-decisions-ev.json"
        dfile.write_text(json.dumps(self._decisions(
            apply=[], reject=[], assist=[],
            amend=[{"id": "r1", "reason": "guarded out", "action": malicious}])))
        result = apply_mod.cmd_decide(str(dfile), self.state, self.data, self.ev)
        self.assertEqual(result["amend_refused"][0]["action"], malicious)
        logged = json.loads(list((self.state / "state" / "decisions").iterdir())[0].read_text())
        self.assertEqual(logged["amend_refused"][0]["action"], malicious)

    def test_amend_retry_after_early_return_no_status_succeeds(self):
        # cmd_apply returns early (no terminal status written) when sessions.json is
        # missing, leaving the replacement dangling at 'proposed'. The collision
        # guard must still let the user "fix and retry" that exact action, so the
        # dangling remnant is demoted to apply_failed rather than left as a live
        # 'proposed' that would collision-refuse the retry forever.
        action = {"harness": "claude-code", "tier": "A", "type": "setting_change",
                  "payload": {"file": "settings.json",
                              "key_path": ["enabledPlugins", "someplug"], "value": False}}
        (self.ev / "sessions.json").unlink()   # force cmd_apply's early return
        dfile = self.downloads / "self-optimize-decisions-ev.json"
        dfile.write_text(json.dumps(self._decisions(
            apply=[], reject=[], assist=[],
            amend=[{"id": "r1", "reason": "first try", "action": action}])))
        first = apply_mod.cmd_decide(str(dfile), self.state, self.data, self.ev)
        self.assertEqual(len(first["amend_refused"]), 1)
        self.assertEqual(ledger.load(self.lpath)["r1"]["status"], "proposed")
        # restore sessions and retry the identical amend
        (self.ev / "sessions.json").write_text(json.dumps(
            {"sessions": [{"started_at": "2026-06-01", "corrections_count": 2,
                           "input_tokens": 1, "output_tokens": 1}]}))
        dfile2 = self.downloads / "self-optimize-decisions-ev2.json"
        dfile2.write_text(json.dumps(self._decisions(
            apply=[], reject=[], assist=[],
            amend=[{"id": "r1", "reason": "retry", "action": action}])))
        second = apply_mod.cmd_decide(str(dfile2), self.state, self.data, self.ev)
        self.assertEqual(len(second["amended"]), 1)
        self.assertEqual(ledger.load(self.lpath)["r1"]["status"], "rejected")

    def test_amend_refused_console_output_shown_when_all_entries_malformed(self):
        # when every amend entry is malformed, amend_items is empty but
        # amend_refused isn't — the console block must not be silently skipped
        dfile = self.downloads / "self-optimize-decisions-ev.json"
        dfile.write_text(json.dumps(self._decisions(
            apply=[], reject=[], assist=[],
            amend=[{"id": "r1", "reason": "   ", "action": {"tier": "A"}}])))
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            result = apply_mod.cmd_decide(str(dfile), self.state, self.data, self.ev)
        self.assertEqual(len(result["amend_refused"]), 1)
        self.assertIn("AMEND REFUSED", buf.getvalue())

    def test_amend_diff_replacement_now_allowed(self):
        # P5: applicability, not tier, gates amend replacements too -- a diff
        # replacement (tier B, but now applicable) must be allowed, not refused
        # for "not tier A" (that rationale no longer holds now that diff applies).
        agent_dir = self.data / "agents"
        agent_dir.mkdir()
        target = agent_dir / "helper.md"
        target.write_text("---\nname: helper\nmodel: sonnet\n---\nold body\n")
        diff_action = {"harness": "claude-code", "tier": "B", "type": "diff",
                       "payload": {"file": str(target),
                                   "diff": "@@ -5,1 +5,1 @@\n-old body\n+new body\n"}}
        dfile = self.downloads / "self-optimize-decisions-ev.json"
        dfile.write_text(json.dumps(self._decisions(
            apply=[], reject=[], assist=[],
            amend=[{"id": "r1", "reason": "prefer a targeted diff", "action": diff_action}])))
        result = apply_mod.cmd_decide(str(dfile), self.state, self.data, self.ev)
        self.assertEqual(len(result["amended"]), 1)
        self.assertEqual(result["amend_refused"], [])
        self.assertIn("new body", target.read_text())
        self.assertEqual(ledger.load(self.lpath)["r1"]["status"], "rejected")

    def test_backward_compat_no_amend_key(self):
        dfile = self.downloads / "self-optimize-decisions-ev.json"
        dfile.write_text(json.dumps(self._decisions()))   # no "amend" key at all
        result = apply_mod.cmd_decide(str(dfile), self.state, self.data, self.ev)
        led = ledger.load(self.lpath)
        self.assertEqual(led["r1"]["status"], "applied")
        self.assertEqual(led["r2"]["status"], "rejected")
        self.assertEqual(result["amended"], [])
        self.assertEqual(result["amend_refused"], [])


if __name__ == "__main__":
    unittest.main()


class TestDiffRenderOSError(unittest.TestCase):
    def test_directory_named_claude_md_fails_only_that_rec(self):
        tmp = tempfile.TemporaryDirectory()
        base = pathlib.Path(tmp.name)
        state, data, ev = base / "st", base / "cl", base / "ev"
        for d in (state, data, ev):
            d.mkdir()
        (data / "settings.json").write_text("{}")
        (ev / "sessions.json").write_text(json.dumps({"sessions": [
            {"started_at": "2026-06-01", "corrections_count": 1}]}))
        (data / "CLAUDE.md").mkdir()  # a DIRECTORY where a file is expected
        rec = setting_rec()
        rec["action"] = {"harness": "claude-code", "tier": "B", "type": "diff",
                         "payload": {"file": str(data / "CLAUDE.md"),
                                     "diff": "@@ -0,0 +1,1 @@\n+x\n"}}
        lp = state / "state" / "ledger.jsonl"
        ledger.append(lp, {"id": "d1", "status": "proposed", "rec": rec, "evidence_hash": "e"})
        apply_mod.cmd_apply(["d1"], state, data, ev)   # must not raise
        self.assertEqual(ledger.load(lp)["d1"]["status"], "apply_failed")
        tmp.cleanup()
