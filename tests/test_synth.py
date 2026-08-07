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

    def test_guard_file_replace_and_workflows_root(self):
        replace_ok = {"action": {"type": "file_replace", "tier": "A",
                                 "payload": {"path": "/fakehome/.claude/skills/x/SKILL.md", "content": "c"}}}
        workflow_ok = {"action": {"type": "file_create", "tier": "A",
                                  "payload": {"path": "/fakehome/.claude/workflows/w.md", "content": "c"}}}
        self.assertTrue(synth.guard(replace_ok, DATA))
        self.assertTrue(synth.guard(workflow_ok, DATA))

    def test_guard_extra_roots_accepted_and_traversal_rejected(self):
        memory_ok = {"action": {"type": "file_create", "tier": "A",
                                "payload": {"path": "/mem/notes/x.md", "content": "c"}}}
        memory_trav = {"action": {"type": "file_create", "tier": "A",
                                  "payload": {"path": "/mem/notes/../../etc/x.md", "content": "c"}}}
        self.assertFalse(synth.guard(memory_ok, DATA))                       # no extra_roots -> rejected
        self.assertTrue(synth.guard(memory_ok, DATA, extra_roots=["/mem/notes"]))
        self.assertFalse(synth.guard(memory_trav, DATA, extra_roots=["/mem/notes"]))

    def test_guard_file_replace_rejected_in_extra_roots(self):
        r = {"action": {"type": "file_replace", "tier": "A",
                        "payload": {"path": "/mem/notes/x.md", "content": "c"}}}
        self.assertFalse(synth.guard(r, DATA, extra_roots=["/mem/notes"]))
        r["action"]["type"] = "file_create"
        self.assertTrue(synth.guard(r, DATA, extra_roots=["/mem/notes"]))

    def test_synthesize_threads_extra_roots_to_guard(self):
        rec = bloat_rec()
        rec["category"] = "memory"
        rec["action"] = {"harness": "claude-code", "tier": "A", "type": "file_create",
                         "payload": {"path": "/mem/notes/new.md", "content": "c"}}
        findings, dropped = synth.synthesize([rec], EV, {}, DATA)
        self.assertEqual(findings, [])
        self.assertEqual(dropped["guard"], 1)
        findings, dropped = synth.synthesize([rec], EV, {}, DATA, extra_roots=["/mem/notes"])
        self.assertEqual(len(findings), 1)

    def test_guard_diff_bounded(self):
        ok_claude_md = {"action": {"type": "diff", "tier": "B",
                                   "payload": {"file": "/fakehome/.claude/CLAUDE.md",
                                               "diff": "-a\n+b"}}}
        ok_sanctioned = {"action": {"type": "diff", "tier": "B",
                                    "payload": {"file": "/fakehome/.claude/skills/x/SKILL.md",
                                                "diff": "-a\n+b"}}}
        bad = {"action": {"type": "diff", "tier": "B",
                          "payload": {"file": "/etc/passwd", "diff": "-a\n+b"}}}
        manual = {"action": {"type": "manual", "tier": "B",
                             "payload": {"description": "do this by hand"}}}
        self.assertTrue(synth.guard(ok_claude_md, DATA))
        self.assertTrue(synth.guard(ok_sanctioned, DATA))
        self.assertFalse(synth.guard(bad, DATA))
        self.assertTrue(synth.guard(manual, DATA))

    def test_guard_diff_extra_root_and_traversal(self):
        ok = {"action": {"type": "diff", "tier": "B",
                         "payload": {"file": "/mem/notes/x.md", "diff": "-a\n+b"}}}
        trav = {"action": {"type": "diff", "tier": "B",
                           "payload": {"file": "/mem/notes/../../etc/x.md", "diff": "-a\n+b"}}}
        self.assertFalse(synth.guard(ok, DATA))  # no extra_roots -> rejected
        self.assertTrue(synth.guard(ok, DATA, extra_roots=["/mem/notes"]))
        self.assertFalse(synth.guard(trav, DATA, extra_roots=["/mem/notes"]))

    def test_guard_diff_extra_root_existing_file_rejected_new_file_allowed(self):
        # memory is create-only (mirrors file_replace's rule): a diff against an
        # EXISTING extra-root note is a rewrite of existing memory — the exact
        # injection amplifier the create-only rule defends against. A diff
        # against a not-yet-existing extra-root path is fine (== file_create).
        import tempfile
        with tempfile.TemporaryDirectory() as mem:
            existing = pathlib.Path(mem) / "MEMORY.md"
            existing.write_text("---\nfoo\n---\nbody\n")
            rewrite = {"action": {"type": "diff", "tier": "B",
                                  "payload": {"file": str(existing), "diff": "-a\n+b"}}}
            create = {"action": {"type": "diff", "tier": "B",
                                 "payload": {"file": str(pathlib.Path(mem) / "new.md"),
                                             "diff": "-a\n+b"}}}
            self.assertFalse(synth.guard(rewrite, DATA, extra_roots=[mem]))
            self.assertTrue(synth.guard(create, DATA, extra_roots=[mem]))

    def test_guard_diff_extra_root_claude_md_existing_rejected(self):
        # naming an existing memory-root note "CLAUDE.md" must not bypass the
        # memory create-only rule: extra-root containment is checked BEFORE the
        # CLAUDE.md carve-out, not after.
        import tempfile
        with tempfile.TemporaryDirectory() as mem:
            existing = pathlib.Path(mem) / "CLAUDE.md"
            existing.write_text("body\n")
            rec = {"action": {"type": "diff", "tier": "B",
                              "payload": {"file": str(existing), "diff": "-a\n+b"}}}
            self.assertFalse(synth.guard(rec, DATA, extra_roots=[mem]))

    def test_guard_retire_confined_to_skills_and_agents(self):
        skill_ok = {"action": {"type": "retire", "tier": "B",
                               "payload": {"path": "/fakehome/.claude/skills/x/SKILL.md"}}}
        agent_ok = {"action": {"type": "retire", "tier": "B",
                               "payload": {"path": "/fakehome/.claude/agents/x.md"}}}
        bad = {"action": {"type": "retire", "tier": "B", "payload": {"path": "/etc/passwd"}}}
        workflow_bad = {"action": {"type": "retire", "tier": "B",
                                   "payload": {"path": "/fakehome/.claude/workflows/x.md"}}}
        self.assertTrue(synth.guard(skill_ok, DATA))
        self.assertTrue(synth.guard(agent_ok, DATA))
        self.assertFalse(synth.guard(bad, DATA))
        self.assertFalse(synth.guard(workflow_bad, DATA))

    def test_guard_frontmatter_edit_lifecycle_keys_allowed(self):
        for key in ("version", "superseded_by", "requires-tools"):
            rec = {"action": {"type": "frontmatter_edit", "tier": "A",
                              "payload": {"file": "/fakehome/.claude/skills/x/SKILL.md",
                                          "key": key, "value": "1"}}}
            self.assertTrue(synth.guard(rec, DATA), key)
        other = {"action": {"type": "frontmatter_edit", "tier": "A",
                            "payload": {"file": "/fakehome/.claude/skills/x/SKILL.md",
                                        "key": "tools", "value": "Bash"}}}
        self.assertFalse(synth.guard(other, DATA))

    def test_guard_diff_non_md_rejected(self):
        rec = {"action": {"type": "diff", "tier": "B",
                          "payload": {"file": "/fakehome/.claude/skills/x/helper.py",
                                      "diff": "-a\n+b"}}}
        self.assertFalse(synth.guard(rec, DATA))

    def test_synthesize_drops_out_of_bounds_diff(self):
        rec = bloat_rec()
        rec["action"] = {"harness": "claude-code", "tier": "B", "type": "diff",
                         "payload": {"file": "/etc/passwd", "diff": "-a\n+b"}}
        findings, dropped = synth.synthesize([rec], EV, {}, DATA)
        self.assertEqual(findings, [])
        self.assertEqual(dropped["guard"], 1)

    def test_check_citation_new_kinds(self):
        ev = dict(EV)
        ev["inventory"] = dict(EV["inventory"], available_plugins=[
            {"id": "availplugin:foo@mp", "name": "foo", "marketplace": "mp", "description": "d"}])
        ev["artifacts"] = {"artifacts": [{"id": "artifact:skill:tdd", "kind": "skill",
                                          "source": "user", "path": "/x/SKILL.md",
                                          "activation_count": 5, "body": "..."}]}
        ev["constraints"] = {"rejected": [{"title": "x", "reason": "y", "ts": "2026-06-01"}]}
        self.assertTrue(synth.check_citation("artifact:skill:tdd", ev))
        self.assertFalse(synth.check_citation("artifact:skill:ghost", ev))
        self.assertTrue(synth.check_citation("constraint:0", ev))
        self.assertFalse(synth.check_citation("constraint:5", ev))
        self.assertTrue(synth.check_citation("availplugin:foo@mp", ev))
        self.assertFalse(synth.check_citation("availplugin:bar@mp", ev))

    def test_check_citation_papercut(self):
        ev = dict(EV, papercuts={"lines": [{"id": "abc123", "date": "2026-08-01",
                                            "harness": "claude-code", "text": "t"}]})
        self.assertTrue(synth.check_citation("papercut:abc123", ev))
        self.assertFalse(synth.check_citation("papercut:ghost", ev))
        # no papercuts key at all (older evidence pack shape) -> resolves to false,
        # not a KeyError
        self.assertFalse(synth.check_citation("papercut:abc123", EV))

    def test_synthesize_accepts_papercut_citation(self):
        rec = bloat_rec()
        rec["evidence_refs"] = ["inventory:skill:dusty", "papercut:abc123"]
        ev = dict(EV, papercuts={"lines": [{"id": "abc123", "date": "2026-08-01",
                                            "harness": "claude-code", "text": "t"}]})
        findings, dropped = synth.synthesize([rec], ev, {}, DATA)
        self.assertEqual(len(findings), 1)
        self.assertEqual(dropped["citations"], 0)

    def test_check_citation_hooks_and_settings(self):
        ev = dict(EV)
        ev["inventory"] = dict(EV["inventory"],
                               hooks=[{"id": "hook:settings", "source": "user",
                                       "est_context_tokens": 10}],
                               settings={"model": "opusplan",
                                         "permissions_default_mode": "auto"})
        self.assertTrue(synth.check_citation("inventory:hook:settings", ev))
        self.assertFalse(synth.check_citation("inventory:hook:ghost", ev))
        self.assertTrue(synth.check_citation("inventory:settings.permissions_default_mode", ev))
        self.assertFalse(synth.check_citation("inventory:settings.nonexistent", ev))
        # composed sub-paths must still fail: unused items are cited by plain item id
        self.assertFalse(synth.check_citation("inventory:unused:skill:dusty", ev))

    def test_citation_drop_detail_names_analyst_and_refs(self):
        bad = bloat_rec()
        bad["evidence_refs"] = ["inventory:skill:dusty", "inventory:skill:ghost",
                                "rule:no-such-rule"]
        bad["_analyst"] = "auditor"
        findings, dropped = synth.synthesize([bad], EV, {}, DATA)
        self.assertEqual(findings, [])
        self.assertEqual(dropped["citations"], 1)
        detail = dropped["citation_detail"][0]
        self.assertEqual(detail["analyst"], "auditor")
        self.assertEqual(detail["title"], "Disable dusty")
        self.assertEqual(detail["failed_refs"], ["inventory:skill:ghost", "rule:no-such-rule"])

    def test_analyst_tag_does_not_change_rec_id(self):
        tagged = bloat_rec(); tagged["_analyst"] = "auditor"
        findings, _ = synth.synthesize([tagged], EV, {}, DATA)
        self.assertEqual(findings[0]["id"], schema.rec_id(bloat_rec()))

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

    def test_deep_localize_attaches_by_session_ref(self):
        rec = bloat_rec()
        rec["evidence_refs"] = ["inventory:skill:dusty", "session:s1"]
        localized = {"s1": {"bracket": [3, 4], "turns_total": 8, "calls": 3,
                            "rationale": "went sideways here"}}
        ev = dict(EV, localize=localized)
        findings, _ = synth.synthesize([rec], ev, {}, DATA)
        self.assertEqual(findings[0]["deep_localize"], [localized["s1"]])

    def test_deep_localize_attaches_by_sample_session_field(self):
        rec = bloat_rec()
        rec["evidence_refs"] = ["inventory:skill:dusty", "sample:0"]
        ev = dict(EV, samples={"samples": [{"user_text": "no, wrong", "session": "s9"}]},
                  localize={"s9": {"bracket": [0, 1], "turns_total": 4, "calls": 2,
                                   "rationale": "r"}})
        findings, _ = synth.synthesize([rec], ev, {}, DATA)
        self.assertEqual(findings[0]["deep_localize"][0]["bracket"], [0, 1])

    def test_deep_localize_absent_when_no_matching_session(self):
        rec = bloat_rec()
        ev = dict(EV, localize={"unrelated-session": {"bracket": [0, 1], "turns_total": 4,
                                                       "calls": 2, "rationale": "r"}})
        findings, _ = synth.synthesize([rec], ev, {}, DATA)
        self.assertNotIn("deep_localize", findings[0])

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


OPS_EV = dict(EV, artifacts={"artifacts": [{"id": "artifact:agent:helper"}]})


def ops_rec():
    return {"title": "Anchor the review rule", "category": "skill-improve",
            "evidence_refs": ["artifact:agent:helper"],
            "impact": {"ordinal": "med"}, "risk": "low",
            "metric": {"key": "correction_rate", "direction": "down", "scope": "global"},
            "action": {"harness": "claude-code", "tier": "A", "type": "file_ops",
                       "payload": {"path": "/fakehome/.claude/agents/helper.md",
                                   "ops": [{"op": "replace", "anchor": "- run the tests",
                                            "text": "- run the whole suite",
                                            "motivated_by": ["sample:0"]}]}}}


def rewrite_rec(marked=True):
    payload = {"path": "/fakehome/.claude/agents/helper.md", "content": "---\nname: h\n---\nnew\n"}
    if marked:
        payload["op"] = "rewrite"
    return {"title": "Restructure helper", "category": "skill-improve",
            "evidence_refs": ["artifact:agent:helper"],
            "impact": {"ordinal": "med"}, "risk": "structural — sections reordered",
            "metric": {"key": "correction_rate", "direction": "down", "scope": "global"},
            "action": {"harness": "claude-code", "tier": "B" if marked else "A",
                       "type": "file_replace", "payload": payload}}


class TestBoundedEditPayloads(unittest.TestCase):
    def test_file_ops_finding_survives_synthesis(self):
        findings, dropped = synth.synthesize([ops_rec()], OPS_EV, {}, DATA)
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0]["action"]["type"], "file_ops")
        self.assertEqual(dropped["invalid"], 0)

    def test_legacy_unmarked_rewrite_is_rejected(self):
        findings, dropped = synth.synthesize([rewrite_rec(marked=False)], OPS_EV, {}, DATA)
        self.assertEqual(findings, [])
        self.assertEqual(dropped["invalid"], 1)

    def test_declared_rewrite_is_kept_at_tier_b(self):
        findings, _ = synth.synthesize([rewrite_rec()], OPS_EV, {}, DATA)
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0]["action"]["tier"], "B")

    def test_per_op_provenance_is_machine_checked(self):
        r = ops_rec()
        r["action"]["payload"]["ops"][0]["motivated_by"] = ["sample:99"]
        findings, dropped = synth.synthesize([r], OPS_EV, {}, DATA)
        self.assertEqual(findings, [])
        self.assertEqual(dropped["citation_detail"][0]["failed_refs"], ["sample:99"])

    def test_guard_confines_file_ops_paths(self):
        ok = ops_rec()
        outside = ops_rec()
        outside["action"]["payload"]["path"] = "/etc/cron.d/x.md"
        not_md = ops_rec()
        not_md["action"]["payload"]["path"] = "/fakehome/.claude/agents/helper.txt"
        self.assertTrue(synth.guard(ok, DATA))
        self.assertFalse(synth.guard(outside, DATA))
        self.assertFalse(synth.guard(not_md, DATA))
        # memory notes stay create-only: no bounded edits of an existing note
        mem = ops_rec()
        mem["action"]["payload"]["path"] = "/fakehome/mem/note.md"
        self.assertFalse(synth.guard(mem, DATA, ["/fakehome/mem"]))


if __name__ == "__main__":
    unittest.main()
