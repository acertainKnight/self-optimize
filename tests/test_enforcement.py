import contextlib, io, json, sys, pathlib, tempfile, unittest
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "scripts"))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "hooks"))
import apply as apply_mod
import enforce as enforce_hook
import enforcement
import ledger
import report
import schema
import synth
import verify

DATA = pathlib.Path("/fakehome/.claude")

# synthetic pack: the same correction turns up in two different sessions
EV = {
    "usage": {"totals": {"sessions": 40}, "waste": {"duplicate_reads_total": 3}},
    "sessions": {"sessions": [{"id": "s1"}, {"id": "s2"}]},
    "activation": {"items": {}},
    "samples": {"samples": [
        {"session": "s1", "user_text": "no, do not use the recursive delete flag"},
        {"session": "s2", "user_text": "again — never pass the recursive delete flag"},
    ]},
    "inventory": {"skills": [], "agents": [], "mcp_servers": [], "plugins": [],
                  "claude_md": []},
    "rules": {"rules": []},
}


def enf_rec():
    return {"title": "Stop passing the recursive delete flag", "category": "hooks",
            "evidence_refs": ["sample:0", "sample:1", "session:s1", "session:s2"],
            "impact": {"ordinal": "high"}, "risk": "blocks a command shape outright",
            "metric": {"key": "correction_rate", "direction": "down", "scope": "global"},
            "action": {"harness": "claude-code", "tier": "B", "type": "manual",
                       "payload": {"description": "add the rendered block to settings.json"}},
            "enforcement": {"kind": "hook", "rule": "forbid_bash_substring",
                            "params": {"value": "rm -rf"},
                            "prediction": {"category": "spec-violation",
                                           "direction": "down"}}}


class TestProposal(unittest.TestCase):
    def test_repeated_correction_pack_yields_proposal_with_prediction_and_citations(self):
        findings, dropped = synth.synthesize([enf_rec()], EV, {}, DATA)
        self.assertEqual(len(findings), 1)
        f = findings[0]
        self.assertEqual(f["enforcement"]["prediction"],
                         {"category": "spec-violation", "direction": "down"})
        self.assertEqual(f["evidence_refs"],
                         ["sample:0", "sample:1", "session:s1", "session:s2"])
        self.assertEqual(f["action"]["tier"], "B")

    def test_tier_a_enforcement_is_invalid(self):
        for mutate in (lambda r: r["action"].__setitem__("tier", "A"),
                       lambda r: r["action"].update({"tier": "A", "type": "file_create",
                                                     "payload": {"path": "/x.md",
                                                                 "content": "c"}})):
            rec = enf_rec()
            mutate(rec)
            errs = schema.validate_rec(rec)
            self.assertTrue(any("permanently tier B" in e for e in errs), errs)
            findings, dropped = synth.synthesize([rec], EV, {}, DATA)
            self.assertEqual((findings, dropped["invalid"]), ([], 1))

    def test_bad_payloads_are_dropped(self):
        bad_rule = enf_rec(); bad_rule["enforcement"]["rule"] = "run_anything"
        bad_param = enf_rec(); bad_param["enforcement"]["params"] = {"value": "a\nb"}
        extra_param = enf_rec(); extra_param["enforcement"]["params"]["shell"] = "sh -c x"
        bad_cat = enf_rec(); bad_cat["enforcement"]["prediction"]["category"] = "vibes"
        bad_dir = enf_rec(); bad_dir["enforcement"]["prediction"]["direction"] = "up"
        for rec in (bad_rule, bad_param, extra_param, bad_cat, bad_dir):
            self.assertTrue(schema.validate_rec(rec))
            self.assertEqual(synth.synthesize([rec], EV, {}, DATA)[0], [])

    def test_one_session_is_not_durable(self):
        rec = enf_rec()
        rec["evidence_refs"] = ["sample:0", "session:s1"]   # one session, cited twice
        self.assertEqual(schema.validate_rec(rec), [])      # shape is fine
        findings, dropped = synth.synthesize([rec], EV, {}, DATA)
        self.assertEqual((findings, dropped["invalid"]), ([], 1))

    def test_id_includes_the_check_and_leaves_legacy_ids_alone(self):
        a, b = enf_rec(), enf_rec()
        b["enforcement"]["params"] = {"value": "git push --force"}
        self.assertNotEqual(schema.rec_id(a), schema.rec_id(b))
        plain = enf_rec(); plain.pop("enforcement")
        legacy = schema.rec_id({"category": plain["category"],
                                "action": plain["action"]})
        self.assertEqual(schema.rec_id(plain), legacy)


class TestRender(unittest.TestCase):
    def test_command_is_template_rendered_and_quoted(self):
        r = enforcement.render(enf_rec()["enforcement"], checker="/plug/hooks/enforce.py")
        self.assertEqual(r["event"], "PreToolUse")
        self.assertEqual(r["matcher"], "Bash")
        self.assertEqual(r["command"],
                         "python3 /plug/hooks/enforce.py --rule forbid_bash_substring "
                         "--arg 'value=rm -rf'")
        hook = r["settings_block"]["hooks"]["PreToolUse"][0]["hooks"][0]
        self.assertEqual(hook["command"], r["command"])

    def test_shell_metacharacters_in_a_param_stay_one_argv_word(self):
        enf = enf_rec()["enforcement"]
        enf["params"] = {"value": "x'; rm -rf ~; echo '"}
        cmd = enforcement.render(enf, checker="/plug/hooks/enforce.py")["command"]
        import shlex
        self.assertEqual(shlex.split(cmd)[-1], "value=x'; rm -rf ~; echo '")

    def test_check_kind_renders_a_bare_command(self):
        enf = enf_rec()["enforcement"]
        enf["kind"] = "check"
        self.assertNotIn("settings_block", enforcement.render(enf))

    def test_report_renders_the_block_and_the_adopt_hint(self):
        rec = enf_rec(); rec["id"] = "abc123"
        md = report.render("2026-08-07", [rec], {"invalid": 0, "citations": 0, "guard": 0,
                                                 "suppressed": []}, [], [],
                           {"totals": {"sessions": 5}, "parse": {"skipped_lines": 0}}, {})
        self.assertIn("permanently tier B", md)
        self.assertIn("/self-optimize adopt abc123", md)
        self.assertIn("spec-violation", md)
        self.assertIn("PreToolUse", md)


class TestHookChecks(unittest.TestCase):
    def test_rule_table_and_checker_agree(self):
        self.assertEqual(set(enforcement.RULES), set(enforce_hook.CHECKS))

    def test_forbid_bash_substring(self):
        p = {"value": "rm -rf"}
        self.assertTrue(enforce_hook.check("forbid_bash_substring", p, "Bash",
                                           {"command": "rm -rf build"}))
        self.assertIsNone(enforce_hook.check("forbid_bash_substring", p, "Bash",
                                             {"command": "rm build"}))
        self.assertIsNone(enforce_hook.check("forbid_bash_substring", p, "Read",
                                             {"file_path": "rm -rf"}))

    def test_require_bash_flag_looks_at_each_segment(self):
        p = {"program": "pytest", "flag": "-q"}
        self.assertIsNone(enforce_hook.check("require_bash_flag", p, "Bash",
                                             {"command": "pytest -q tests"}))
        self.assertTrue(enforce_hook.check("require_bash_flag", p, "Bash",
                                           {"command": "cd x && pytest tests"}))
        self.assertIsNone(enforce_hook.check("require_bash_flag", p, "Bash",
                                             {"command": "echo pytest tests"}))

    def test_forbid_write_path_boundary(self):
        p = {"prefix": "/srv/data"}
        self.assertTrue(enforce_hook.check("forbid_write_path", p, "Write",
                                           {"file_path": "/srv/data/x/y.txt"}))
        self.assertIsNone(enforce_hook.check("forbid_write_path", p, "Write",
                                             {"file_path": "/srv/database/y.txt"}))
        self.assertIsNone(enforce_hook.check("forbid_write_path", p, "Bash",
                                             {"command": "touch /srv/data/x"}))

    def test_main_exit_codes(self):
        def run(argv, payload):
            stdin, sys.stdin = sys.stdin, io.StringIO(json.dumps(payload))
            err = io.StringIO()
            try:
                with contextlib.redirect_stderr(err):
                    return enforce_hook.main(argv)
            finally:
                sys.stdin = stdin
        block = ["--rule", "forbid_bash_substring", "--arg", "value=rm -rf"]
        self.assertEqual(run(block, {"tool_name": "Bash", "tool_input": {"command": "rm -rf /"}}), 2)
        self.assertEqual(run(block, {"tool_name": "Bash", "tool_input": {"command": "ls"}}), 0)
        self.assertEqual(run(["--rule", "nope"], {}), 1)
        # a missing parameter is a misconfigured check, never a blocked session
        self.assertEqual(run(["--rule", "forbid_bash_substring"],
                             {"tool_name": "Bash", "tool_input": {"command": "ls"}}), 1)


class TestAdoptAndVerify(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = pathlib.Path(self.tmp.name)
        self.state, self.data, self.ev = base / "state", base / "claude", base / "ev"
        for d in (self.state, self.data, self.ev):
            d.mkdir()
        (self.ev / "sessions.json").write_text(json.dumps(
            {"sessions": [{"started_at": "2026-06-01", "corrections_count": 2,
                           "input_tokens": 1, "output_tokens": 1}]}))
        (self.ev / "labels.json").write_text(json.dumps(
            {"counts": {"spec-violation": 8, "other": 12}, "dropped": 0}))
        self.lpath = self.state / "state" / "ledger.jsonl"
        self.rec = enf_rec()
        self.rec["id"], self.rec["evidence_hash"] = "enf1", "e1"
        ledger.append(self.lpath, {"id": "enf1", "status": "proposed", "rec": self.rec,
                                   "evidence_hash": "e1"})

    def tearDown(self):
        self.tmp.cleanup()

    def _metrics(self, rows):
        (self.state / "state").mkdir(parents=True, exist_ok=True)
        (self.state / "state" / "metrics.jsonl").write_text(
            "".join(json.dumps(r) + "\n" for r in rows))

    def _adopt(self):
        with contextlib.redirect_stdout(io.StringIO()) as out:
            apply_mod.cmd_adopt("enf1", self.state, self.ev)
        return out.getvalue()

    def test_apply_refuses_enforcement_even_when_the_action_is_tier_a(self):
        rec = enf_rec()
        rec["action"] = {"harness": "claude-code", "tier": "A", "type": "file_create",
                         "payload": {"path": str(self.data / "skills" / "x" / "SKILL.md"),
                                     "content": "---\nname: x\n---\n"}}
        ledger.append(self.lpath, {"id": "enf2", "status": "proposed", "rec": rec,
                                   "evidence_hash": "e2"})
        with contextlib.redirect_stdout(io.StringIO()) as out:
            apply_mod.cmd_apply(["enf2"], self.state, self.data, self.ev)
        self.assertIn("never auto-applied", out.getvalue())
        self.assertFalse((self.data / "skills").exists())
        self.assertEqual(ledger.load(self.lpath)["enf2"]["status"], "proposed")

    def test_adopt_records_the_baseline_and_suppresses_reproposal(self):
        self._adopt()
        e = ledger.load(self.lpath)["enf1"]
        self.assertEqual(e["status"], "adopted")
        self.assertEqual(e["enforcement_baseline"]["counts"]["spec-violation"], 8)
        self.assertEqual(e["enforcement_baseline"]["run_id"], "ev")
        self.assertIn("already adopted", ledger.suppress_reason(self.rec,
                                                                ledger.load(self.lpath)))
        with contextlib.redirect_stdout(io.StringIO()) as out:
            apply_mod.cmd_adopt("enf1", self.state, self.ev)
        self.assertIn("not adoptable", out.getvalue())

    def test_predicted_vs_observed(self):
        cfg = {"verify": {"min_sessions": 3, "min_rel_change": 0.10}}
        self._adopt()
        entries = ledger.load(self.lpath)

        # only the adoption run is labeled: nothing to measure against yet
        self._metrics([{"run_id": "ev", "corrections_by_category": {"spec-violation": 8,
                                                                    "other": 12}}])
        rows = verify.verify_enforcement(entries, enforcement.read_metrics_rows(self.state), cfg)
        self.assertEqual(rows[0]["verdict"], "inconclusive")
        self.assertIn("no labeled correction run since adoption", rows[0]["note"])

        # a later run: the predicted category's share of corrections halves
        after = [{"run_id": "ev", "corrections_by_category": {"spec-violation": 8, "other": 12}},
                 {"run_id": "later", "corrections_by_category": {"spec-violation": 2,
                                                                 "other": 18}}]
        self._metrics(after)
        rows = verify.verify_enforcement(entries, enforcement.read_metrics_rows(self.state), cfg)
        self.assertEqual(rows[0]["verdict"], "verified")
        self.assertEqual(rows[0]["observed_run"], "later")
        self.assertAlmostEqual(rows[0]["rel_change"], (0.1 - 0.4) / 0.4)

        # and the same measurement can contradict the prediction
        after[1]["corrections_by_category"] = {"spec-violation": 16, "other": 4}
        self._metrics(after)
        rows = verify.verify_enforcement(entries, enforcement.read_metrics_rows(self.state), cfg)
        self.assertEqual(rows[0]["verdict"], "regressed")

        # thin evidence never produces a verdict
        after[1]["corrections_by_category"] = {"spec-violation": 0, "other": 2}
        self._metrics(after)
        rows = verify.verify_enforcement(entries, enforcement.read_metrics_rows(self.state), cfg)
        self.assertEqual(rows[0]["verdict"], "inconclusive")

    def test_verify_cli_writes_the_enforcement_rows(self):
        self._adopt()
        self._metrics([{"run_id": "ev", "corrections_by_category": {"spec-violation": 8,
                                                                    "other": 12}},
                       {"run_id": "later", "corrections_by_category": {"spec-violation": 1,
                                                                       "other": 19}}])
        out = self.ev / "verify.json"
        with contextlib.redirect_stdout(io.StringIO()) as printed:
            verify.main(["--evidence", str(self.ev), "--state", str(self.state),
                         "--data-root", str(self.data), "--out", str(out)])
        data = json.loads(out.read_text())
        self.assertEqual(data["enforcement"][0]["verdict"], "verified")
        self.assertIn("enf1=verified", printed.getvalue())
        md = report.render("later", [], {"invalid": 0, "citations": 0, "guard": 0,
                                         "suppressed": []}, data["rows"], [],
                           {"totals": {"sessions": 5}, "parse": {"skipped_lines": 0}}, {},
                           enforcement_rows=data["enforcement"])
        self.assertIn("Enforcement proposals: predicted vs observed", md)
        self.assertIn("**verified**", md)

    def test_rejected_enforcement_enters_constraints(self):
        import collect
        apply_mod.cmd_reject("enf1", "too blunt a check", self.state)
        pack = collect.constraints_pack(self.lpath)
        self.assertEqual(pack["rejected"][-1]["reason"], "too blunt a check")
        self.assertEqual(pack["rejected"][-1]["title"], self.rec["title"])
        # and it stays suppressed until the evidence behind it changes
        self.assertIn("rejected", ledger.suppress_reason(self.rec, ledger.load(self.lpath)))


if __name__ == "__main__":
    unittest.main()
