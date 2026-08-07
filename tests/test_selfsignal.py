"""Self-application tests: the analysts as gym artifacts, the tier-B lock and the
one-per-run cap on self-edits, and the guard that flags a self-edit for rollback.

Every ledger, run and score here is synthetic. The only real files touched are the
analyst instruction files themselves, and only ever read.
"""
import json, sys, pathlib, shutil, tempfile, unittest
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "scripts"))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "adapters" / "claude_code"))
import gym
import report
import schema
import selfsignal
import so_config
import synth
import templates

DATA = pathlib.Path("/fakehome/.claude")
EVOLVER_MD = str(selfsignal.PLUGIN_ROOT / "agents" / "evolver.md")
MINER_MD = str(selfsignal.PLUGIN_ROOT / "agents" / "transcript-miner.md")


def rec(analyst, title="a finding", category="bloat"):
    return {"title": title, "category": category, "analyst": analyst,
            "evidence_refs": ["usage:totals.sessions"], "impact": {"ordinal": "med"},
            "risk": "low", "metric": {"key": "correction_rate", "direction": "down"},
            "action": {"tier": "A", "type": "setting_change", "payload": {}}}


def self_edit_rec(path=EVOLVER_MD, tier="B", title="Tighten the anchor rule"):
    return {"title": title, "category": "skill-improve",
            "evidence_refs": ["artifact:analyst:evolver"], "impact": {"ordinal": "med"},
            "risk": "changes how the evolver reasons", "_analyst": "evolver",
            "metric": {"key": "correction_rate", "direction": "down", "scope": "global"},
            "action": {"harness": "claude-code", "tier": tier, "type": "file_ops",
                       "payload": {"path": path, "ops": [
                           {"op": "add", "anchor": "Rules:", "text": "- one more rule",
                            "motivated_by": ["artifact:analyst:evolver"]}]}}}


def write_run(state, run_id, findings=(), citation_detail=(), citation_checked=None,
              scores=None, verify_verdicts=()):
    """One completed run's artifacts, as the pipeline leaves them behind."""
    d = pathlib.Path(state) / "evidence" / run_id
    d.mkdir(parents=True, exist_ok=True)
    findings = list(findings)
    dropped = {"invalid": 0, "citations": len(citation_detail), "guard": 0,
               "suppressed": [], "citation_detail": list(citation_detail)}
    if citation_checked is not None:
        dropped["citation_checked"] = citation_checked
    (d / "findings.json").write_text(json.dumps({"findings": findings, "dropped": dropped}))
    if scores is not None:
        (d / "gym.json").write_text(json.dumps(scores))
    if verify_verdicts:
        (d / "verify.json").write_text(json.dumps(
            {"rows": [{"id": f"v{i}", "verdict": v} for i, v in enumerate(verify_verdicts)]}))
    return d


def write_pack(d, skills=("alpha",)):
    """The evidence files gym.accrue itself reads, alongside the run artifacts."""
    d = pathlib.Path(d)
    d.mkdir(parents=True, exist_ok=True)
    (d / "inventory.json").write_text(json.dumps(
        {"harness": "claude-code",
         "skills": [{"id": f"skill:{n}", "name": n, "source": "user",
                     "path": f"/synthetic/skills/{n}/SKILL.md"} for n in skills],
         "agents": [], "hooks": [], "guidance": []}))
    for name in ("sessions", "samples", "working"):
        key = "sessions" if name == "sessions" else "samples"
        (d / f"{name}.json").write_text(json.dumps({"harness": "claude-code", key: []}))
    return d


def write_ledger(state, entries):
    p = pathlib.Path(state) / "state"
    p.mkdir(parents=True, exist_ok=True)
    with open(p / "ledger.jsonl", "w") as f:
        for e in entries:
            f.write(json.dumps(e) + "\n")


class SelfCase(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        self.state = self.tmp / "state"
        self.cfg = so_config.load_config(self.state)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)


class TestArtifacts(SelfCase):
    def test_every_analyst_registers_as_an_artifact(self):
        ids = set(selfsignal.artifact_entries())
        self.assertEqual(ids, {"analyst:transcript-miner", "analyst:config-auditor",
                               "analyst:evolver", "analyst:sample-labeler"})
        for entry in selfsignal.artifact_entries().values():
            self.assertEqual(entry["kind"], "analyst")
            self.assertIsNone(entry["activation_key"])   # never fires in a user session
            self.assertTrue(entry["self_signal"])

    def test_only_the_analyst_files_are_analyst_paths(self):
        self.assertTrue(selfsignal.is_analyst_path(EVOLVER_MD))
        # a reference doc sitting in the same directory is not an analyst, and
        # neither is anything else in the plugin
        for other in ("taxonomy-v2-mast-mapping.md", "../scripts/gym.py", "../README.md"):
            self.assertFalse(selfsignal.is_analyst_path(
                str(selfsignal.PLUGIN_ROOT / "agents" / other)), other)
        self.assertFalse(selfsignal.is_analyst_path("/fakehome/.claude/agents/evolver.md"))
        self.assertFalse(selfsignal.is_analyst_path(""))
        self.assertFalse(selfsignal.is_analyst_path(None))

    def test_analyst_attribution_comes_from_the_stamp_not_a_guess(self):
        self.assertEqual(selfsignal.analyst_of(rec("miner")), "analyst:transcript-miner")
        # the citation-repair round is the same analyst
        self.assertEqual(selfsignal.artifact_for_stem("miner-retry"),
                         "analyst:transcript-miner")
        self.assertIsNone(selfsignal.analyst_of({"category": "bloat"}))
        self.assertIsNone(selfsignal.analyst_of(rec("curator")))   # a script, not an analyst


class TestSelfCorpus(SelfCase):
    def accrue(self, run_id="2026-06-02"):
        return gym.accrue(self.state, [write_pack(self.tmp / "pack")], run_id, self.cfg)

    def status(self):
        return {r["id"]: r for r in gym.artifact_status(self.state, self.cfg)}

    def test_status_reports_self_signal_case_counts(self):
        write_ledger(self.state, [
            {"id": "f1", "status": "rejected", "reason": "too blunt",
             "rec": rec("miner", "Add a rule")},
            {"id": "f2", "status": "verified", "rec": rec("miner", "Trim the memory note")},
            {"id": "f3", "status": "regressed", "rec": rec("auditor", "Disable a plugin")},
            {"id": "f4", "status": "proposed", "rec": rec("auditor", "Still open")},
        ])
        write_run(self.state, "2026-06-01",
                  findings=[dict(rec("evolver"), id="f5")],
                  citation_detail=[{"analyst": "evolver", "title": "Rewrite alpha",
                                    "failed_refs": ["sample:99"]}],
                  citation_checked=4,
                  scores={"f5": {"unscorable": False, "prevented": {"n": 0, "of": 3},
                                 "preserved": {"n": 3, "of": 3}}})
        summary = self.accrue()
        self.assertEqual(summary["added_self"], 5)

        rows = self.status()
        miner = rows["analyst:transcript-miner"]
        self.assertEqual(miner["kind"], "analyst")
        self.assertEqual((miner["self_failure_cases"], miner["self_working_cases"]), (1, 1))
        self.assertEqual((miner["failure_cases"], miner["working_cases"]), (1, 1))
        auditor = rows["analyst:config-auditor"]
        self.assertEqual((auditor["self_failure_cases"], auditor["self_working_cases"]), (1, 0))
        evolver = rows["analyst:evolver"]   # one citation drop + one poor gym score
        self.assertEqual((evolver["self_failure_cases"], evolver["self_working_cases"]), (2, 0))
        # a skill with no self-signal reports zeros, not blanks
        self.assertEqual(rows["skill:alpha"]["self_failure_cases"], 0)

    def test_a_real_rejection_becomes_a_failure_case(self):
        """End to end through apply.py: the rejection path has to keep the rec, or
        the analyst behind the finding is unattributable by the time it matters."""
        import apply
        write_ledger(self.state, [{"id": "f1", "status": "proposed",
                                   "rec": rec("evolver", "Rewrite alpha", "skill-improve"),
                                   "evidence_hash": "h"}])
        apply.cmd_reject("f1", "too broad", self.state)
        self.assertEqual(self.accrue()["added_self"], 1)
        row = self.status()["analyst:evolver"]
        self.assertEqual((row["self_failure_cases"], row["self_working_cases"]), (1, 0))

    def test_cases_do_not_double_count_across_runs(self):
        write_ledger(self.state, [{"id": "f1", "status": "rejected", "reason": "no",
                                   "rec": rec("miner")}])
        self.assertEqual(self.accrue("2026-06-02")["added_self"], 1)
        self.assertEqual(self.accrue("2026-06-03")["added_self"], 0)
        self.assertEqual(self.status()["analyst:transcript-miner"]["self_failure_cases"], 1)

    def test_analyst_is_scorable_on_self_signal_alone(self):
        write_ledger(self.state, [
            {"id": f"f{i}", "status": "rejected", "reason": "no",
             "rec": rec("miner", f"Finding {i}")} for i in range(3)]
            + [{"id": f"w{i}", "status": "verified", "rec": rec("miner", f"Good {i}")}
               for i in range(3)])
        self.accrue()
        row = self.status()["analyst:transcript-miner"]
        self.assertTrue(row["scorable"], row["reason"])
        # a hook still is not: it has neither activation nor a self-signal
        self.assertFalse(row["retired"])

    def test_a_poor_gym_score_is_a_failure_case_and_a_good_one_is_working(self):
        good = {"unscorable": False, "prevented": {"n": 2, "of": 4},
                "preserved": {"n": 5, "of": 5}}
        broke_working = {"unscorable": False, "prevented": {"n": 4, "of": 4},
                         "preserved": {"n": 4, "of": 5}}
        fixed_nothing = {"unscorable": False, "prevented": {"n": 0, "of": 4},
                         "preserved": {"n": 5, "of": 5}}
        self.assertEqual(selfsignal.score_side(good), "working")
        self.assertEqual(selfsignal.score_side(broke_working), "failure")
        self.assertEqual(selfsignal.score_side(fixed_nothing), "failure")
        self.assertIsNone(selfsignal.score_side({"unscorable": True}))

    def test_analyst_artifacts_never_retire_when_the_inventory_omits_them(self):
        for run in ("2026-06-01", "2026-06-02", "2026-06-03", "2026-06-04"):
            self.accrue(run)
        row = self.status()["analyst:evolver"]
        self.assertFalse(row["retired"])
        self.assertEqual(row["absent_runs"], 0)


class TestTierLockAndCap(unittest.TestCase):
    def test_a_self_edit_is_permanently_tier_b(self):
        self.assertEqual(schema.validate_rec(self_edit_rec(tier="B")), [])
        errs = schema.validate_rec(self_edit_rec(tier="A"))
        self.assertTrue(any("permanently tier B" in e for e in errs), errs)
        # the same shape on an ordinary artifact stays tier-A eligible
        ordinary = self_edit_rec(path="/fakehome/.claude/skills/x/SKILL.md", tier="A")
        self.assertEqual(schema.validate_rec(ordinary), [])

    def test_guard_admits_the_analyst_files_and_nothing_else_in_the_plugin(self):
        self.assertTrue(synth.guard(self_edit_rec(), DATA))
        for other in (str(selfsignal.PLUGIN_ROOT / "scripts" / "gym.py"),
                      str(selfsignal.PLUGIN_ROOT / "README.md"),
                      str(selfsignal.PLUGIN_ROOT / "agents" / "taxonomy-v2-mast-mapping.md")):
            self.assertFalse(synth.guard(self_edit_rec(path=other), DATA), other)

    def _ev(self, failure=4, working=1):
        return {"usage": {"totals": {"sessions": 10}}, "sessions": {"sessions": []},
                "activation": {"items": {}}, "samples": {"samples": []},
                "inventory": {"skills": [], "agents": [], "mcp_servers": [], "plugins": [],
                              "claude_md": []},
                "rules": {"rules": []},
                "artifacts": {"artifacts": [
                    {"id": "artifact:analyst:evolver", "kind": "analyst", "path": EVOLVER_MD,
                     "self_corpus": {"failure": failure, "working": working}},
                    {"id": "artifact:analyst:transcript-miner", "kind": "analyst",
                     "path": MINER_MD, "self_corpus": {"failure": 3, "working": 0}}]}}

    def test_one_self_edit_per_analyst_per_run(self):
        first = self_edit_rec(title="Tighten the anchor rule")
        second = self_edit_rec(title="Also reword the front section")
        second["action"]["payload"]["ops"][0]["anchor"] = "Operation semantics:"
        miner = self_edit_rec(path=MINER_MD, title="Tighten the miner")
        miner["evidence_refs"] = ["artifact:analyst:transcript-miner"]
        miner["action"]["payload"]["ops"][0]["motivated_by"] = \
            ["artifact:analyst:transcript-miner"]
        findings, dropped = synth.synthesize([first, second, miner], self._ev(), {}, DATA)
        titles = [f["title"] for f in findings]
        self.assertEqual(titles, ["Tighten the anchor rule", "Tighten the miner"])
        self.assertEqual(len(dropped["self_capped"]), 1)
        self.assertEqual(dropped["self_capped"][0]["analyst"], "analyst:evolver")

    def test_self_edit_carries_its_self_corpus_evidence(self):
        findings, _ = synth.synthesize([self_edit_rec()], self._ev(failure=6, working=2),
                                       {}, DATA)
        self.assertEqual(findings[0]["self_edit"],
                         {"analyst": "analyst:evolver", "failure_cases": 6,
                          "working_cases": 2})
        self.assertEqual(findings[0]["analyst"], "evolver")   # provenance for later runs

    def test_report_renders_the_self_corpus_and_the_tier_lock(self):
        findings, _ = synth.synthesize([self_edit_rec()], self._ev(failure=6, working=2),
                                       {}, DATA)
        md = report.render("r", findings, {"invalid": 0, "citations": 0, "guard": 0,
                                           "suppressed": []}, [], [],
                           {"totals": {"sessions": 10},
                            "parse": {"skipped_lines": 0, "redactions": 0}}, {})
        self.assertIn("self-application: bounded edit to the `analyst:evolver`", md)
        self.assertIn("6 recorded self-failure cases", md)
        self.assertIn("Permanently tier B", md)
        self.assertNotIn("/self-optimize apply", md)   # tier B: no one-click path

    def test_dashboard_card_shows_the_self_corpus(self):
        import dashboard
        findings, _ = synth.synthesize([self_edit_rec()], self._ev(failure=6, working=2),
                                       {}, DATA)
        html = dashboard._card_html(findings[0], None)
        self.assertIn("SELF-EDIT", html)
        self.assertIn("6 recorded self-failure cases", html)
        self.assertIn("card tier-b", html)

    def test_render_applies_bounded_ops_to_the_analyst_file(self):
        edits = templates.render(self_edit_rec()["action"], DATA)
        path, content = edits[0]
        self.assertEqual(str(path), EVOLVER_MD)
        self.assertIn("- one more rule", content)
        self.assertTrue(content.startswith("---"))        # frontmatter untouched
        self.assertEqual(pathlib.Path(EVOLVER_MD).read_text(), content.replace(
            "\n- one more rule", "", 1))                  # nothing else changed
        # and the write path still refuses anything else inside the plugin
        with self.assertRaises(ValueError):
            templates.render(self_edit_rec(
                path=str(selfsignal.PLUGIN_ROOT / "README.md"))["action"], DATA)


class TestRollbackGuard(SelfCase):
    def entries(self, applied_at="2026-06-01T12:00:00Z"):
        return {"s1": {"status": "applied", "applied_at": applied_at,
                       "rec": self_edit_rec()}}

    def test_a_self_edit_that_degrades_citations_is_flagged_for_rollback(self):
        write_run(self.state, "2026-06-01", findings=[{"id": "a"}] * 4,
                  citation_checked=4, verify_verdicts=["verified", "verified"])
        write_run(self.state, "2026-06-02", findings=[{"id": "a"}] * 2,
                  citation_detail=[{"analyst": "evolver", "title": "t",
                                    "failed_refs": ["sample:9"]}] * 2,
                  citation_checked=4, verify_verdicts=["verified", "verified"])
        rows = selfsignal.self_edit_rows(self.entries(), self.state, 0.10)
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["verdict"], "regressed")
        self.assertEqual(row["artifact"], "analyst:evolver")
        self.assertEqual((row["before_run"], row["after_run"]), ("2026-06-01", "2026-06-02"))
        self.assertEqual(row["rates"]["citation_rate"]["before"], 1.0)
        self.assertEqual(row["rates"]["citation_rate"]["after"], 0.5)
        self.assertIn("/self-optimize rollback s1", row["note"])

    def test_a_self_edit_that_degrades_verified_outcomes_is_flagged(self):
        write_run(self.state, "2026-06-01", findings=[{"id": "a"}] * 4, citation_checked=4,
                  verify_verdicts=["verified", "verified", "verified", "verified"])
        write_run(self.state, "2026-06-02", findings=[{"id": "a"}] * 4, citation_checked=4,
                  verify_verdicts=["verified", "regressed", "regressed", "regressed"])
        row = selfsignal.self_edit_rows(self.entries(), self.state, 0.10)[0]
        self.assertEqual(row["verdict"], "regressed")
        self.assertEqual(row["rates"]["verified_rate"]["after"], 0.25)
        self.assertEqual(row["rates"]["citation_rate"]["rel_change"], 0.0)

    def test_a_self_edit_that_holds_is_not_flagged(self):
        for run in ("2026-06-01", "2026-06-02"):
            write_run(self.state, run, findings=[{"id": "a"}] * 4, citation_checked=4,
                      verify_verdicts=["verified", "verified", "regressed"])
        row = selfsignal.self_edit_rows(self.entries(), self.state, 0.10)[0]
        self.assertEqual(row["verdict"], "held")

    def test_no_run_after_the_edit_yet_is_inconclusive(self):
        write_run(self.state, "2026-06-01", findings=[{"id": "a"}] * 4, citation_checked=4)
        row = selfsignal.self_edit_rows(self.entries(), self.state, 0.10)[0]
        self.assertEqual(row["verdict"], "inconclusive")
        self.assertIn("no completed run after this edit", row["note"])

    def test_ordinary_applied_changes_are_not_self_edits(self):
        write_run(self.state, "2026-06-01", findings=[{"id": "a"}], citation_checked=1)
        write_run(self.state, "2026-06-02", findings=[], citation_detail=[
            {"analyst": "miner", "title": "t", "failed_refs": ["sample:9"]}],
            citation_checked=1)
        entries = {"s1": {"status": "applied", "applied_at": "2026-06-01T12:00:00Z",
                          "rec": rec("miner")}}
        self.assertEqual(selfsignal.self_edit_rows(entries, self.state, 0.10), [])

    def test_report_renders_the_self_edit_guard_table(self):
        write_run(self.state, "2026-06-01", findings=[{"id": "a"}] * 4, citation_checked=4)
        write_run(self.state, "2026-06-02", findings=[{"id": "a"}] * 2, citation_checked=4,
                  citation_detail=[{"analyst": "evolver", "title": "t",
                                    "failed_refs": ["sample:9"]}] * 2)
        rows = selfsignal.self_edit_rows(self.entries(), self.state, 0.10)
        md = report.render("r", [], {"invalid": 0, "citations": 0, "guard": 0,
                                     "suppressed": []}, [], [],
                           {"totals": {"sessions": 10},
                            "parse": {"skipped_lines": 0, "redactions": 0}}, {},
                           self_edit_rows=rows)
        self.assertIn("Self-edits: pipeline health after the change", md)
        self.assertIn("**regressed**", md)
        self.assertIn("rollback: /self-optimize rollback s1", md)


class TestGatingTheEvolver(SelfCase):
    def test_an_analyst_is_offered_only_once_its_self_corpus_clears_the_floor(self):
        import inventory
        inv = {"skills": [], "agents": []}
        empty = inventory.build_artifacts(inv, None, state=self.state, cfg=self.cfg)
        self.assertEqual(empty["artifacts"], [])

        write_ledger(self.state, [
            {"id": f"f{i}", "status": "rejected", "reason": "no",
             "rec": rec("evolver", f"Finding {i}", "skill-improve")} for i in range(3)])
        gym.accrue(self.state, [write_pack(self.tmp / "pack")], "2026-06-02", self.cfg)
        art = inventory.build_artifacts(inv, None, state=self.state, cfg=self.cfg)
        ids = [a["id"] for a in art["artifacts"]]
        self.assertEqual(ids, ["artifact:analyst:evolver"])
        entry = art["artifacts"][0]
        self.assertEqual(entry["self_corpus"], {"failure": 3, "working": 0})
        self.assertIn("evolver", entry["body"])
        self.assertEqual(entry["path"], EVOLVER_MD)


if __name__ == "__main__":
    unittest.main()
