"""Gym corpus tests. Every pack here is synthetic — the real corpus holds real
transcript excerpts and never enters this repo."""
import json, sys, pathlib, shutil, tempfile, unittest
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "scripts"))
import gym
import so_config


def write_pack(ev: pathlib.Path, skills=("alpha",), agents=(), sessions=(), samples=(),
               working=(), harness="claude-code", guidance=(), hooks=()):
    ev.mkdir(parents=True, exist_ok=True)
    inv = {"schema_version": "1", "harness": harness,
           "skills": [{"id": f"skill:{n}", "name": n, "source": "user",
                       "path": f"/synthetic/skills/{n}/SKILL.md"} for n in skills],
           "agents": [{"id": f"agent:{n}", "name": n, "source": "user",
                       "path": f"/synthetic/agents/{n}.md"} for n in agents],
           "hooks": [{"id": h, "source": "user", "est_context_tokens": 10} for h in hooks],
           "guidance": [{"id": f"guidance:{p}", "path": p, "kind": "memory",
                         "bytes": 10, "est_tokens": 2, "mtime": "", "body": "b"}
                        for p in guidance]}
    (ev / "inventory.json").write_text(json.dumps(inv))
    (ev / "sessions.json").write_text(json.dumps(
        {"schema_version": "1", "harness": harness, "sessions": list(sessions)}))
    (ev / "samples.json").write_text(json.dumps(
        {"schema_version": "1", "harness": harness, "samples": list(samples)}))
    (ev / "working.json").write_text(json.dumps(
        {"schema_version": "1", "harness": harness, "samples": list(working)}))
    return ev


def session(sid, artifacts, corrections=0, project="p"):
    return {"id": sid, "project": project, "corrections_count": corrections,
            "activation": {a: 1 for a in artifacts}}


def correction(sid, i, project="p"):
    return {"session": sid, "project": project, "ts": f"2026-06-{i:02d}T10:00:00Z",
            "kind": "correction", "pattern": "no",
            "user_text": f"no, not like that ({i})", "prior_assistant_text": f"did it wrong {i}"}


def working_row(sid, i, project="p"):
    return {"session": sid, "project": project, "ts": f"2026-06-{i:02d}T11:00:00Z",
            "kind": "working", "user_text": f"please do the thing ({i})",
            "assistant_text": f"done, here it is {i}"}


class GymCase(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        self.state = self.tmp / "state"
        self.cfg = so_config.load_config(self.state)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def status_by_id(self):
        return {r["id"]: r for r in gym.artifact_status(self.state, self.cfg)}


class TestRegistry(GymCase):
    def test_registry_is_derived_from_inventory_each_run(self):
        ev = write_pack(self.tmp / "ev1", skills=("alpha",), agents=("helper",),
                        hooks=("hook:settings",), guidance=("/synthetic/CLAUDE.md",))
        gym.accrue(self.state, [ev], "2026-06-01", self.cfg)
        rows = self.status_by_id()
        self.assertEqual(set(rows), {"skill:alpha", "agent:helper", "hook:settings",
                                     "guidance:/synthetic/CLAUDE.md"})
        self.assertEqual(rows["skill:alpha"]["failure_cases"], 0)
        self.assertFalse(rows["skill:alpha"]["scorable"])

        # a new skill on disk registers on the next run with an empty corpus
        ev2 = write_pack(self.tmp / "ev2", skills=("alpha", "beta"), agents=("helper",))
        summary = gym.accrue(self.state, [ev2], "2026-06-02", self.cfg)
        self.assertIn("skill:beta", summary["new"])
        self.assertEqual(self.status_by_id()["skill:beta"]["working_cases"], 0)

    def test_absent_artifact_retires_after_the_configured_window(self):
        ev = write_pack(self.tmp / "ev1", skills=("alpha", "doomed"))
        gym.accrue(self.state, [ev], "2026-06-01", self.cfg)
        gone = write_pack(self.tmp / "ev2", skills=("alpha",))
        for i, run in enumerate(("2026-06-02", "2026-06-03"), start=1):
            gym.accrue(self.state, [gone], run, self.cfg)
            row = self.status_by_id()["skill:doomed"]
            self.assertFalse(row["retired"], f"retired too early after {i} absences")
        gym.accrue(self.state, [gone], "2026-06-04", self.cfg)
        row = self.status_by_id()["skill:doomed"]
        self.assertTrue(row["retired"])
        self.assertFalse(row["scorable"])
        self.assertIn("retired", row["reason"])

    def test_absence_counts_once_per_run_id(self):
        gym.accrue(self.state, [write_pack(self.tmp / "ev1", skills=("alpha", "doomed"))],
                   "2026-06-01", self.cfg)
        gone = write_pack(self.tmp / "ev2", skills=("alpha",))
        for _ in range(5):
            gym.accrue(self.state, [gone], "2026-06-02", self.cfg)
        self.assertEqual(self.status_by_id()["skill:doomed"]["absent_runs"], 1)

    def test_retired_artifact_returns_with_its_corpus(self):
        ev = write_pack(self.tmp / "ev1", skills=("alpha",),
                        sessions=[session("s1", ["skill:alpha"], corrections=1)],
                        samples=[correction("s1", 1)])
        gym.accrue(self.state, [ev], "2026-06-01", self.cfg)
        gone = write_pack(self.tmp / "ev2", skills=("other",))
        for run in ("2026-06-02", "2026-06-03", "2026-06-04"):
            gym.accrue(self.state, [gone], run, self.cfg)
        self.assertTrue(self.status_by_id()["skill:alpha"]["retired"])
        gym.accrue(self.state, [write_pack(self.tmp / "ev3", skills=("alpha",))],
                   "2026-06-05", self.cfg)
        row = self.status_by_id()["skill:alpha"]
        self.assertFalse(row["retired"])
        self.assertEqual(row["failure_cases"], 1)

    def test_empty_inventory_never_retires_the_registry(self):
        gym.accrue(self.state, [write_pack(self.tmp / "ev1", skills=("alpha",))],
                   "2026-06-01", self.cfg)
        blank = self.tmp / "blank"
        blank.mkdir()
        for run in ("2026-06-02", "2026-06-03", "2026-06-04", "2026-06-05"):
            gym.accrue(self.state, [blank], run, self.cfg)
        self.assertFalse(self.status_by_id()["skill:alpha"]["retired"])


class TestAccrual(GymCase):
    def test_cases_attach_to_the_artifacts_active_in_that_session(self):
        ev = write_pack(
            self.tmp / "ev", skills=("alpha", "beta"),
            sessions=[session("s1", ["skill:alpha"], corrections=1),
                      session("s2", ["skill:beta"], corrections=0)],
            samples=[correction("s1", 1)], working=[working_row("s2", 2)])
        gym.accrue(self.state, [ev], "2026-06-01", self.cfg)
        rows = self.status_by_id()
        self.assertEqual((rows["skill:alpha"]["failure_cases"], rows["skill:alpha"]["working_cases"]),
                         (1, 0))
        self.assertEqual((rows["skill:beta"]["failure_cases"], rows["skill:beta"]["working_cases"]),
                         (0, 1))

    def test_working_excerpts_from_corrected_sessions_are_refused(self):
        ev = write_pack(self.tmp / "ev", skills=("alpha",),
                        sessions=[session("s1", ["skill:alpha"], corrections=2)],
                        working=[working_row("s1", 1)])
        gym.accrue(self.state, [ev], "2026-06-01", self.cfg)
        self.assertEqual(self.status_by_id()["skill:alpha"]["working_cases"], 0)

    def test_overlapping_windows_do_not_duplicate_cases(self):
        ev = write_pack(self.tmp / "ev", skills=("alpha",),
                        sessions=[session("s1", ["skill:alpha"], corrections=1)],
                        samples=[correction("s1", 1)])
        gym.accrue(self.state, [ev], "2026-06-01", self.cfg)
        summary = gym.accrue(self.state, [ev], "2026-06-02", self.cfg)
        self.assertEqual(summary["added_failure"], 0)
        self.assertEqual(self.status_by_id()["skill:alpha"]["failure_cases"], 1)

    def test_cases_carry_harness_session_and_timestamp_and_are_redacted(self):
        smp = correction("s1", 1)
        smp["user_text"] = "no, the key sk-ant-api03-FAKEFAKEFAKEFAKEFAKE must not be stored"
        ev = write_pack(self.tmp / "ev", skills=("alpha",), harness="codex",
                        sessions=[session("s1", ["skill:alpha"], corrections=1)], samples=[smp])
        gym.accrue(self.state, [ev], "2026-06-01", self.cfg)
        case = gym.load_corpus(gym.gym_dir(self.state), "skill:alpha")["failure"][0]
        self.assertEqual(case["harness"], "codex")
        self.assertEqual(case["session"], "s1")
        self.assertEqual(case["ts"], "2026-06-01T10:00:00Z")
        self.assertIn("[REDACTED:", case["user_text"])
        self.assertNotIn("sk-ant-api03-FAKEFAKEFAKEFAKEFAKE", case["user_text"])

    def test_multiple_harness_packs_merge_into_one_corpus(self):
        cc = write_pack(self.tmp / "cc", skills=("alpha",),
                        sessions=[session("s1", ["skill:alpha"], corrections=1)],
                        samples=[correction("s1", 1)])
        cx = write_pack(self.tmp / "cx", skills=("alpha",), harness="codex",
                        sessions=[session("s9", ["skill:alpha"], corrections=1)],
                        samples=[correction("s9", 2)])
        gym.accrue(self.state, [cc, cx], "2026-06-01", self.cfg)
        cases = gym.load_corpus(gym.gym_dir(self.state), "skill:alpha")["failure"]
        self.assertEqual({c["harness"] for c in cases}, {"claude-code", "codex"})

    def test_sessions_without_an_activation_map_attach_nothing(self):
        ev = write_pack(self.tmp / "ev", skills=("alpha",),
                        sessions=[{"id": "s1", "project": "p", "corrections_count": 1}],
                        samples=[correction("s1", 1)])
        gym.accrue(self.state, [ev], "2026-06-01", self.cfg)
        self.assertEqual(self.status_by_id()["skill:alpha"]["failure_cases"], 0)


class TestRetentionAndFloor(GymCase):
    def _pack_with(self, n_failure, n_working, skill="alpha"):
        sessions = [session("f", [f"skill:{skill}"], corrections=n_failure),
                    session("w", [f"skill:{skill}"], corrections=0)]
        return write_pack(self.tmp / f"ev-{n_failure}-{n_working}", skills=(skill,),
                          sessions=sessions,
                          samples=[correction("f", i) for i in range(1, n_failure + 1)],
                          working=[working_row("w", i) for i in range(1, n_working + 1)])

    def test_cases_age_out_fifo_past_the_cap(self):
        cfg = json.loads(json.dumps(self.cfg))
        cfg["gym"]["max_cases_per_side"] = 3
        gym.accrue(self.state, [self._pack_with(6, 0)], "2026-06-01", cfg)
        cases = gym.load_corpus(gym.gym_dir(self.state), "skill:alpha")["failure"]
        self.assertEqual(len(cases), 3)
        self.assertEqual([c["ts"] for c in cases],
                         ["2026-06-04T10:00:00Z", "2026-06-05T10:00:00Z", "2026-06-06T10:00:00Z"])

    def test_below_floor_is_unscorable_never_a_score(self):
        gym.accrue(self.state, [self._pack_with(3, 2)], "2026-06-01", self.cfg)
        row = self.status_by_id()["skill:alpha"]
        self.assertFalse(row["scorable"])
        self.assertIn("working 2/3", row["reason"])

    def test_floor_met_on_both_sides_is_scorable(self):
        gym.accrue(self.state, [self._pack_with(3, 3)], "2026-06-01", self.cfg)
        row = self.status_by_id()["skill:alpha"]
        self.assertTrue(row["scorable"], row["reason"])

    def test_kinds_without_an_activation_signal_are_unscorable(self):
        gym.accrue(self.state, [write_pack(self.tmp / "ev", skills=(), hooks=("hook:settings",))],
                   "2026-06-01", self.cfg)
        row = self.status_by_id()["hook:settings"]
        self.assertFalse(row["scorable"])
        self.assertIn("activation", row["reason"])


class TestCliAndPermissions(GymCase):
    def test_corpus_files_are_owner_only(self):
        ev = write_pack(self.tmp / "ev", skills=("alpha",),
                        sessions=[session("s1", ["skill:alpha"], corrections=1)],
                        samples=[correction("s1", 1)])
        gym.accrue(self.state, [ev], "2026-06-01", self.cfg)
        gdir = gym.gym_dir(self.state)
        self.assertEqual((gdir / "registry.json").stat().st_mode & 0o777, 0o600)
        for f in (gdir / "corpus").glob("*.json"):
            self.assertEqual(f.stat().st_mode & 0o777, 0o600)
        self.assertEqual(gdir.stat().st_mode & 0o777, 0o700)

    def test_update_then_status_cli(self):
        ev = write_pack(self.tmp / "ev", skills=("alpha",), agents=("helper",),
                        sessions=[session("s1", ["skill:alpha"], corrections=1)],
                        samples=[correction("s1", 1)])
        with self.assertRaises(SystemExit) as cm:
            gym.main(["update", "--evidence", str(ev), "--state", str(self.state),
                      "--run-id", "2026-06-01"])
        self.assertEqual(cm.exception.code, 0)
        import contextlib, io
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), self.assertRaises(SystemExit):
            gym.main(["status", "--state", str(self.state), "--json"])
        rows = json.loads(buf.getvalue())["artifacts"]
        self.assertEqual({r["id"] for r in rows}, {"skill:alpha", "agent:helper"})


STUB_JUDGE = '''\
"""Stub judge backend: reads a prompt on stdin, writes a JSON verdict on stdout.
Verdict rule — does the candidate artifact text still mention the keyword the case
turns on? That makes a deliberately degraded candidate score worse, deterministically.
"""
import json, sys

prompt = sys.stdin.read()
side = [l for l in prompt.splitlines() if l.startswith("CASE SIDE: ")][0].split(": ")[1]
head, _, rest = prompt.partition("--- CANDIDATE ARTIFACT TEXT ---")
candidate, _, _ = rest.partition("--- END CANDIDATE ARTIFACT TEXT ---")
good = "KEYWORD" in candidate
key = "prevented" if side == "failure" else "preserved"
sys.stdout.write(json.dumps({key: good}))
'''

CHATTY_JUDGE = '''\
import json, sys
prompt = sys.stdin.read()
side = [l for l in prompt.splitlines() if l.startswith("CASE SIDE: ")][0].split(": ")[1]
key = "prevented" if side == "failure" else "preserved"
sys.stdout.write("Sure! Here is my verdict:\\n" + json.dumps({key: True}) + "\\nHope that helps.")
'''

BROKEN_JUDGE = 'import sys\nsys.stderr.write("model unavailable\\n")\nsys.exit(1)\n'


class TestScoring(GymCase):
    def setUp(self):
        super().setUp()
        self.stub = self.tmp / "stub_judge.py"
        self.stub.write_text(STUB_JUDGE)
        self.good = self.tmp / "good.md"
        self.good.write_text("# artifact\nAlways run the KEYWORD check before finishing.\n")
        self.degraded = self.tmp / "degraded.md"
        self.degraded.write_text("# artifact\nDo whatever seems reasonable.\n")

    def stock_corpus(self, n=4, skill="alpha"):
        ev = write_pack(
            self.tmp / "ev", skills=(skill,),
            sessions=[session("f", [f"skill:{skill}"], corrections=n),
                      session("w", [f"skill:{skill}"], corrections=0)],
            samples=[correction("f", i) for i in range(1, n + 1)],
            working=[working_row("w", i) for i in range(1, n + 1)])
        gym.accrue(self.state, [ev], "2026-06-01", self.cfg)

    def with_judge(self, script: pathlib.Path, model="stub-model"):
        cfg = json.loads(json.dumps(self.cfg))
        cfg["gym"]["judge"] = {"command": [sys.executable, str(script), "--model", "{model}"],
                               "model": model, "timeout_s": 30}
        return cfg

    def test_degraded_candidate_preserves_visibly_less(self):
        self.stock_corpus()
        cfg = self.with_judge(self.stub)
        cur = gym.score_artifact(self.state, "skill:alpha", self.good.read_text(), cfg)
        bad = gym.score_artifact(self.state, "skill:alpha", self.degraded.read_text(), cfg)
        self.assertFalse(cur["unscorable"], cur["reason"])
        self.assertEqual(cur["preserved"], {"n": 4, "of": 4})
        self.assertEqual(bad["preserved"], {"n": 0, "of": 4})
        self.assertLess(bad["preserved"]["n"], cur["preserved"]["n"])
        self.assertEqual(cur["prevented"], {"n": 4, "of": 4})
        self.assertEqual(bad["prevented"], {"n": 0, "of": 4})

    def test_backend_swap_is_config_only(self):
        self.stock_corpus()
        other = self.tmp / "other_backend.py"
        other.write_text(CHATTY_JUDGE)   # different CLI, different output style
        first = gym.score_artifact(self.state, "skill:alpha", self.degraded.read_text(),
                                   self.with_judge(self.stub))
        second = gym.score_artifact(self.state, "skill:alpha", self.degraded.read_text(),
                                    self.with_judge(other, model="other-model"))
        self.assertEqual(first["preserved"], {"n": 0, "of": 4})
        self.assertEqual(second["preserved"], {"n": 4, "of": 4})   # same code, new backend
        self.assertEqual(second["errors"], 0)

    def test_missing_judge_config_is_a_refusal_not_a_fallback(self):
        self.stock_corpus()
        with self.assertRaises(gym.JudgeNotConfigured):
            gym.score_artifact(self.state, "skill:alpha", self.good.read_text(), self.cfg)

    def test_cli_refusal_prints_instructions_and_exits_2(self):
        self.stock_corpus()
        import contextlib, io
        err, out = io.StringIO(), io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(out), \
                self.assertRaises(SystemExit) as cm:
            gym.main(["score", "--artifact", "skill:alpha", "--candidate", str(self.good),
                      "--state", str(self.state)])
        self.assertEqual(cm.exception.code, 2)
        self.assertIn("gym.judge.command", err.getvalue())
        self.assertEqual(out.getvalue(), "")

    def test_below_floor_short_circuits_without_calling_the_judge(self):
        self.stock_corpus(n=2)
        missing = self.tmp / "no_such_backend.py"   # any judge call would blow up
        cfg = self.with_judge(missing)
        result = gym.score_artifact(self.state, "skill:alpha", self.good.read_text(), cfg)
        self.assertTrue(result["unscorable"])
        self.assertIn("below case floor", result["reason"])
        self.assertEqual(result["per_case"], [])
        self.assertEqual(result["tokens_est"], 0)

    def test_retired_and_unknown_artifacts_are_unscorable(self):
        self.stock_corpus()
        cfg = self.with_judge(self.stub)
        unknown = gym.score_artifact(self.state, "skill:ghost", self.good.read_text(), cfg)
        self.assertTrue(unknown["unscorable"])
        self.assertIn("not in the gym registry", unknown["reason"])
        gone = write_pack(self.tmp / "ev-gone", skills=("other",))
        for run in ("2026-06-02", "2026-06-03", "2026-06-04"):
            gym.accrue(self.state, [gone], run, self.cfg)
        retired = gym.score_artifact(self.state, "skill:alpha", self.good.read_text(), cfg)
        self.assertTrue(retired["unscorable"])
        self.assertIn("retired", retired["reason"])

    def test_judge_errors_do_not_become_scores(self):
        self.stock_corpus()
        broken = self.tmp / "broken.py"
        broken.write_text(BROKEN_JUDGE)
        result = gym.score_artifact(self.state, "skill:alpha", self.good.read_text(),
                                    self.with_judge(broken))
        self.assertTrue(result["unscorable"])
        self.assertEqual(result["errors"], 8)
        self.assertEqual(result["prevented"], {"n": 0, "of": 0})
        self.assertIn("too few usable verdicts", result["reason"])

    def test_budget_skips_cases_instead_of_scoring_on_a_slice(self):
        self.stock_corpus()
        result = gym.score_artifact(self.state, "skill:alpha", self.good.read_text(),
                                    self.with_judge(self.stub), budget=200)
        self.assertGreater(result["skipped_budget"], 0)
        self.assertTrue(result["unscorable"])
        self.assertIn("budget", result["reason"])

    def test_prompts_are_deterministic_and_carry_no_timestamps(self):
        self.stock_corpus()
        corpus = gym.load_corpus(gym.gym_dir(self.state), "skill:alpha")
        case = corpus["failure"][0]
        a = gym.build_prompt("skill:alpha", "text", case, "failure")
        b = gym.build_prompt("skill:alpha", "text", case, "failure")
        self.assertEqual(a, b)
        self.assertNotIn(case["ts"], a)
        self.assertIn("CASE SIDE: failure", a)
        self.assertIn('{"prevented": true}', a)

    def test_score_writes_owner_only_json_for_the_report(self):
        self.stock_corpus()
        cfgfile = self.state / "config.json"
        cfg = json.loads(cfgfile.read_text())
        cfg["gym"] = {"judge": {"command": [sys.executable, str(self.stub)],
                                "model": "stub-model", "timeout_s": 30}}
        cfgfile.write_text(json.dumps(cfg))
        out = self.tmp / "score.json"
        import contextlib, io
        with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(SystemExit) as cm:
            gym.main(["score", "--artifact", "skill:alpha", "--candidate", str(self.good),
                      "--state", str(self.state), "--out", str(out)])
        self.assertEqual(cm.exception.code, 0)
        self.assertEqual(out.stat().st_mode & 0o777, 0o600)
        written = json.loads(out.read_text())
        self.assertFalse(written["unscorable"])
        self.assertEqual(written["candidate"], str(self.good))


class TestGate(GymCase):
    """The gate is what stands between an evolver proposal and a one-click apply:
    score the candidate the user would actually get, and downgrade anything it could
    not score to tier B."""

    ARTIFACT = "# alpha\n- always run the KEYWORD check\n- then ship\n"

    def setUp(self):
        super().setUp()
        self.stub = self.tmp / "stub_judge.py"
        self.stub.write_text(STUB_JUDGE)
        self.artifact = self.tmp / "SKILL.md"
        self.artifact.write_text(self.ARTIFACT)
        ev = write_pack(
            self.tmp / "ev", skills=("alpha",),
            sessions=[session("f", ["skill:alpha"], corrections=4),
                      session("w", ["skill:alpha"], corrections=0)],
            samples=[correction("f", i) for i in range(1, 5)],
            working=[working_row("w", i) for i in range(1, 5)])
        gym.accrue(self.state, [ev], "2026-06-01", self.cfg)
        self.findings = self.tmp / "findings.json"

    def with_judge(self):
        cfg = json.loads(json.dumps(self.cfg))
        cfg["gym"]["judge"] = {"command": [sys.executable, str(self.stub)],
                               "model": "stub-model", "timeout_s": 30}
        return cfg

    def ops_finding(self, ops, fid="f1"):
        return {"id": fid, "title": "Sharpen alpha", "category": "skill-improve",
                "evidence_refs": ["artifact:skill:alpha", "sample:0"],
                "impact": {"ordinal": "med"}, "risk": "low",
                "metric": {"key": "correction_rate", "direction": "down", "scope": "global"},
                "action": {"harness": "claude-code", "tier": "A", "type": "file_ops",
                           "payload": {"path": str(self.artifact), "ops": ops}}}

    def write_findings(self, findings):
        self.findings.write_text(json.dumps({"findings": findings, "dropped": {}}))

    def run_gate(self, cfg=None):
        return gym.gate(self.state, self.findings, cfg or self.with_judge(), run_id="r1")

    def reloaded(self):
        return json.loads(self.findings.read_text())["findings"]

    def test_scores_the_artifact_as_the_ops_would_leave_it(self):
        keep = self.ops_finding([{"op": "add", "anchor": "- then ship",
                                  "text": "- read the diff first",
                                  "motivated_by": ["sample:0"]}])
        self.write_findings([keep])
        summary = self.run_gate()
        score = summary["scores"]["f1"]
        self.assertFalse(score["unscorable"])
        self.assertEqual(score["prevented"], {"n": 4, "of": 4})
        self.assertEqual(score["preserved"], {"n": 4, "of": 4})
        self.assertEqual(summary["downgraded"], [])
        self.assertEqual(self.reloaded()[0]["action"]["tier"], "A")
        # the judged text is the current artifact with the op applied, not the payload
        self.assertEqual(gym.candidate_text(keep)[0],
                         "# alpha\n- always run the KEYWORD check\n- then ship\n"
                         "- read the diff first\n")

    def test_ops_that_no_longer_anchor_are_unscorable_and_tier_b(self):
        self.write_findings([self.ops_finding([{"op": "delete", "anchor": "- gone from the file",
                                                "motivated_by": ["sample:0"]}])])
        summary = self.run_gate()
        self.assertIn("anchor not found", summary["scores"]["f1"]["reason"])
        self.assertEqual(summary["downgraded"], ["f1"])
        self.assertEqual(self.reloaded()[0]["action"]["tier"], "B")

    def test_unregistered_artifact_is_unscorable_and_tier_b(self):
        unknown = self.ops_finding([{"op": "delete", "anchor": "- then ship",
                                     "motivated_by": ["sample:0"]}])
        unknown["evidence_refs"] = ["artifact:skill:ghost"]   # not in the registry
        self.write_findings([unknown])
        summary = self.run_gate()
        self.assertIn("no registered gym artifact", summary["scores"]["f1"]["reason"])
        self.assertEqual(self.reloaded()[0]["action"]["tier"], "B")

    def test_no_judge_configured_leaves_everything_at_tier_b(self):
        self.write_findings([self.ops_finding([{"op": "delete", "anchor": "- then ship",
                                                "motivated_by": ["sample:0"]}])])
        summary = self.run_gate(cfg=self.cfg)   # no gym.judge.command
        self.assertTrue(summary["judge_missing"])
        self.assertIn("gym.judge.command", summary["scores"]["f1"]["reason"])
        self.assertEqual(self.reloaded()[0]["action"]["tier"], "B")

    def test_symlinked_artifact_diff_is_not_treated_as_a_candidate(self):
        diff = self.ops_finding([], fid="d1")
        diff["action"] = {"harness": "claude-code", "tier": "B", "type": "diff",
                          "payload": {"file": str(self.artifact), "diff": "@@ -1 +1 @@\n-a\n+b\n"}}
        self.write_findings([diff])
        summary = self.run_gate()
        self.assertEqual(summary["scores"], {})
        self.assertEqual(self.reloaded()[0]["action"]["tier"], "B")

    def test_non_evolver_findings_are_left_alone(self):
        other = {"id": "b1", "title": "Disable dusty", "category": "bloat",
                 "evidence_refs": ["inventory:skill:dusty"], "impact": {"ordinal": "med"},
                 "risk": "low", "metric": {"key": "base_context_est", "direction": "down"},
                 "action": {"harness": "claude-code", "tier": "A", "type": "setting_change",
                            "payload": {"key_path": ["skillOverrides", "dusty"], "value": "off"}}}
        self.write_findings([other])
        summary = self.run_gate()
        self.assertEqual(summary["scores"], {})
        self.assertEqual(self.reloaded()[0]["action"]["tier"], "A")

    def test_cli_writes_scores_at_mode_600(self):
        cfgfile = self.state / "config.json"
        cfg = json.loads(cfgfile.read_text())
        cfg["gym"]["judge"] = {"command": [sys.executable, str(self.stub)],
                               "model": "stub-model", "timeout_s": 30}
        cfgfile.write_text(json.dumps(cfg))
        self.write_findings([self.ops_finding([{"op": "delete", "anchor": "- then ship",
                                                "motivated_by": ["sample:0"]}])])
        out = self.tmp / "gym.json"
        import contextlib, io
        with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(SystemExit) as cm:
            gym.main(["gate", "--findings", str(self.findings), "--state", str(self.state),
                      "--out", str(out)])
        self.assertEqual(cm.exception.code, 0)
        self.assertEqual(out.stat().st_mode & 0o777, 0o600)
        self.assertIn("f1", json.loads(out.read_text()))


class TestVerdictParsing(unittest.TestCase):
    def test_bare_json(self):
        self.assertTrue(gym.parse_verdict('{"prevented": true}', "failure"))
        self.assertFalse(gym.parse_verdict('{"preserved": false}', "working"))

    def test_json_wrapped_in_chatter(self):
        self.assertTrue(gym.parse_verdict('Verdict:\n{"preserved": true}\ndone', "working"))

    def test_missing_or_non_boolean_verdict_raises(self):
        for bad in ("", "no idea", '{"prevented": "yes"}', '{"preserved": true}'):
            with self.assertRaises(gym.JudgeError):
                gym.parse_verdict(bad, "failure")


if __name__ == "__main__":
    unittest.main()
