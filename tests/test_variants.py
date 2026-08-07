"""Variant-archive tests. Every artifact body, case and score here is synthetic —
the real archive holds a user's own artifact text and never enters this repo."""
import contextlib, io, json, sys, pathlib, shutil, tempfile, unittest
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "scripts"))
import apply as apply_mod
import dashboard
import gym
import ledger
import so_config
import variants

ARTIFACT = "---\nname: alpha\n---\n# alpha\n- always run the KEYWORD check\n- then ship\n"

STUB_JUDGE = '''\
"""Stub judge: yes when the candidate still mentions the keyword the case turns on."""
import json, sys

prompt = sys.stdin.read()
side = [l for l in prompt.splitlines() if l.startswith("CASE SIDE: ")][0].split(": ")[1]
_, _, rest = prompt.partition("--- CANDIDATE ARTIFACT TEXT ---")
candidate, _, _ = rest.partition("--- END CANDIDATE ARTIFACT TEXT ---")
key = "prevented" if side == "failure" else "preserved"
sys.stdout.write(json.dumps({key: "KEYWORD" in candidate}))
'''


def write_pack(ev, skills=("alpha",), sessions=(), samples=(), working=()):
    ev.mkdir(parents=True, exist_ok=True)
    (ev / "inventory.json").write_text(json.dumps(
        {"schema_version": "1", "harness": "claude-code",
         "skills": [{"id": f"skill:{n}", "name": n, "source": "user",
                     "path": f"/synthetic/skills/{n}/SKILL.md"} for n in skills]}))
    (ev / "sessions.json").write_text(json.dumps({"sessions": list(sessions)}))
    (ev / "samples.json").write_text(json.dumps({"samples": list(samples)}))
    (ev / "working.json").write_text(json.dumps({"samples": list(working)}))
    return ev


def session(sid, corrections=0):
    return {"id": sid, "project": "p", "corrections_count": corrections,
            "started_at": "2026-06-01", "input_tokens": 1, "output_tokens": 1,
            "activation": {"skill:alpha": 1}}


def correction(sid, i):
    return {"session": sid, "project": "p", "ts": f"2026-06-{i:02d}T10:00:00Z",
            "kind": "correction", "pattern": "no", "user_text": f"no, not like that ({i})",
            "prior_assistant_text": f"did it wrong {i}"}


def working_row(sid, i):
    return {"session": sid, "project": "p", "ts": f"2026-06-{i:02d}T11:00:00Z",
            "kind": "working", "user_text": f"please do the thing ({i})",
            "assistant_text": f"done, here it is {i}"}


def scores(prevented, preserved, of=4, unscorable=False, reason=""):
    return {"prevented": {"n": prevented, "of": of}, "preserved": {"n": preserved, "of": of},
            "unscorable": unscorable, "reason": reason}


class ArchiveCase(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        self.state = self.tmp / "state"
        self.data = self.tmp / "claude"
        self.cfg = so_config.load_config(self.state)
        self.artifact = self.data / "skills" / "alpha" / "SKILL.md"
        self.artifact.parent.mkdir(parents=True)
        self.artifact.write_text(ARTIFACT)
        self.stub = self.tmp / "stub_judge.py"
        self.stub.write_text(STUB_JUDGE)
        self.ev = write_pack(
            self.tmp / "ev",
            sessions=[session("f", corrections=4), session("w", corrections=0)],
            samples=[correction("f", i) for i in range(1, 5)],
            working=[working_row("w", i) for i in range(1, 5)])
        gym.accrue(self.state, [self.ev], "2026-06-01", self.cfg)
        self.findings = self.tmp / "findings.json"

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def with_judge(self):
        cfg = json.loads(json.dumps(self.cfg))
        cfg["gym"]["judge"] = {"command": [sys.executable, str(self.stub)],
                               "model": "stub-model", "timeout_s": 30}
        return cfg

    def finding(self, ops, fid="f1", title="Sharpen alpha", **payload):
        return {"id": fid, "title": title, "category": "skill-improve",
                "evidence_refs": ["artifact:skill:alpha", "sample:0"],
                "impact": {"ordinal": "med"}, "risk": "low", "evidence_hash": "e1",
                "metric": {"key": "correction_rate", "direction": "down", "scope": "global"},
                "action": {"harness": "claude-code", "tier": "A", "type": "file_ops",
                           "payload": {"path": str(self.artifact), "ops": ops, **payload}}}

    def run_gate(self, findings, run_id="2026-06-01", cfg=None):
        self.findings.write_text(json.dumps({"findings": findings, "dropped": {}}))
        return gym.gate(self.state, self.findings, cfg or self.with_judge(), run_id=run_id)

    def archived(self):
        return variants.load(self.state, "skill:alpha")


class TestLineage(ArchiveCase):
    def test_two_runs_over_one_artifact_build_a_two_node_lineage(self):
        """Run one proposes an edit; it is applied; run two edits the result. The
        archive holds both versions, the second pointing at the first, with the scores
        and the exact edits that produced each still readable."""
        first = self.finding([{"op": "add", "anchor": "- then ship",
                               "text": "- read the diff first",
                               "motivated_by": ["sample:0"]}], fid="f1")
        self.run_gate([first])
        v1 = self.archived()[0]
        self.assertIsNone(v1["parent"])
        self.assertEqual(v1["scores"]["prevented"], {"n": 4, "of": 4})
        self.assertEqual(v1["scores"]["preserved"], {"n": 4, "of": 4})
        self.assertFalse(v1["scores"]["unscorable"])
        self.assertEqual(v1["ops"][0]["text"], "- read the diff first")
        self.assertIn("- read the diff first", v1["text"])
        self.assertEqual(v1["provenance"]["title"], "Sharpen alpha")

        self.artifact.write_text(v1["text"])          # the human applied it
        second = self.finding([{"op": "add", "anchor": "- read the diff first",
                                "text": "- KEYWORD twice", "motivated_by": ["sample:0"]}],
                              fid="f2", title="Sharpen alpha again")
        self.run_gate([second], run_id="2026-06-02")
        records = self.archived()
        self.assertEqual(len(records), 2)
        v2 = records[1]
        self.assertEqual(v2["parent"], v1["id"])      # lineage edge, derived from content
        self.assertEqual(v2["run_id"], "2026-06-02")
        self.assertNotEqual(v1["text"], v2["text"])   # both bodies recoverable, so is the diff
        self.assertEqual(v2["ops"][0]["anchor"], "- read the diff first")
        self.assertEqual(variants.live_id(records), v1["id"])

    def test_unapplied_candidates_stay_siblings_off_the_live_version(self):
        self.run_gate([self.finding([{"op": "add", "anchor": "- then ship", "text": "- one",
                                      "motivated_by": ["sample:0"]}], fid="f1")])
        self.run_gate([self.finding([{"op": "add", "anchor": "- then ship", "text": "- two",
                                      "motivated_by": ["sample:0"]}], fid="f2")],
                      run_id="2026-06-02")
        records = self.archived()
        self.assertEqual(len(records), 2)
        self.assertEqual([v["parent"] for v in records], [None, None])
        self.assertIsNone(variants.live_id(records))  # the file is still the base text

    def test_re_proposing_the_same_candidate_does_not_grow_the_lineage(self):
        f = self.finding([{"op": "add", "anchor": "- then ship", "text": "- one",
                           "motivated_by": ["sample:0"]}])
        self.run_gate([f])
        self.run_gate([json.loads(json.dumps(f))], run_id="2026-06-02")
        self.assertEqual(len(self.archived()), 1)

    def test_archive_files_are_owner_only(self):
        self.run_gate([self.finding([{"op": "add", "anchor": "- then ship", "text": "- one",
                                      "motivated_by": ["sample:0"]}])])
        d = variants.artifact_dir(self.state, "skill:alpha")
        self.assertEqual(d.stat().st_mode & 0o777, 0o700)
        self.assertEqual(variants.archive_root(self.state).stat().st_mode & 0o777, 0o700)
        for f in d.glob("*.json"):
            self.assertEqual(f.stat().st_mode & 0o777, 0o600)


class TestBranching(ArchiveCase):
    """A proposal may branch from any archived variant, including one nobody applied.
    Its edits are written against that variant's text, so the gate flattens them onto
    the parent's own chain before anything scores, renders or applies them."""

    def seed_parent(self):
        self.run_gate([self.finding([{"op": "add", "anchor": "- then ship",
                                      "text": "- read the diff first",
                                      "motivated_by": ["sample:0"]}], fid="f1")])
        return self.archived()[0]

    def branch(self, parent_id, fid="f2"):
        return self.finding([{"op": "add", "anchor": "- read the diff first",
                              "text": "- and run the KEYWORD check twice",
                              "motivated_by": ["sample:0"]}],
                            fid=fid, title="Build on the archived variant",
                            parent_variant=parent_id,
                            expected_improvement="keeps the prevention, restores preservation")

    def test_branch_ops_are_flattened_onto_the_parent_chain(self):
        v1 = self.seed_parent()
        summary = self.run_gate([self.branch(v1["id"])], run_id="2026-06-02")
        self.assertEqual(summary["composed"], ["f2"])
        payload = json.loads(self.findings.read_text())["findings"][0]["action"]["payload"]
        self.assertEqual([op["text"] for op in payload["ops"]],
                         ["- read the diff first", "- and run the KEYWORD check twice"])
        self.assertTrue(payload["ops_composed"])
        v2 = [v for v in self.archived() if v["id"] != v1["id"]][0]
        self.assertEqual(v2["parent"], v1["id"])
        self.assertEqual(len(v2["ops"]), 1)           # the delta stays the delta
        self.assertEqual(len(v2["chain_ops"]), 2)     # the flattened set is what applies
        self.assertEqual(v2["provenance"]["expected_improvement"],
                         "keeps the prevention, restores preservation")
        self.assertFalse(v2["scores"]["unscorable"])

    def test_flattening_is_idempotent_across_repeated_gates(self):
        v1 = self.seed_parent()
        self.run_gate([self.branch(v1["id"])], run_id="2026-06-02")
        first = json.loads(self.findings.read_text())["findings"]
        second_summary = gym.gate(self.state, self.findings, self.with_judge(),
                                  run_id="2026-06-03")
        self.assertEqual(second_summary["composed"], [])
        self.assertEqual(json.loads(self.findings.read_text())["findings"], first)

    def test_missing_parent_is_unscorable_and_drops_to_tier_b(self):
        self.seed_parent()
        summary = self.run_gate([self.branch("deadbeefdead")], run_id="2026-06-02")
        self.assertIn("not in the archive", summary["scores"]["f2"]["reason"])
        self.assertEqual(summary["downgraded"], ["f2"])
        self.assertEqual(json.loads(self.findings.read_text())["findings"][0]["action"]["tier"],
                         "B")

    def test_branching_off_a_whole_body_rewrite_is_refused(self):
        rewrite = variants.record(self.state, "skill:alpha", "# rewritten\n",
                                  scores(4, 4), run_id="2026-06-01",
                                  path=str(self.artifact))
        summary = self.run_gate([self.branch(rewrite["id"])], run_id="2026-06-02")
        self.assertIn("no edit chain", summary["scores"]["f2"]["reason"])
        self.assertEqual(summary["downgraded"], ["f2"])

    def test_branch_round_trips_through_decide_and_apply(self):
        v1 = self.seed_parent()
        branch = self.branch(v1["id"])
        lpath = self.state / "state" / "ledger.jsonl"
        ledger.append(lpath, {"id": "f2", "status": "proposed", "rec": branch,
                              "evidence_hash": "e1"})
        self.run_gate([branch], run_id="2026-06-02")

        decide_ev = self.tmp / "ev"
        shutil.copyfile(self.findings, decide_ev / "findings.json")
        dfile = self.tmp / "self-optimize-decisions-ev.json"
        dfile.write_text(json.dumps({"run_id": "ev", "apply": ["f2"], "reject": [],
                                     "assist": [], "amend": []}))
        with contextlib.redirect_stdout(io.StringIO()):
            result = apply_mod.cmd_decide(str(dfile), self.state, self.data, decide_ev)
        self.assertEqual(result["applied"], ["f2"])
        self.assertEqual(ledger.load(lpath)["f2"]["status"], "applied")
        self.assertEqual(self.artifact.read_text(),
                         ARTIFACT + "- read the diff first\n- and run the KEYWORD check twice\n")
        # and the applied text is now the live node of the lineage it branched from
        records = self.archived()
        self.assertEqual(variants.live_id(records),
                         next(v["id"] for v in records if v["parent"] == v1["id"]))


class TestRetention(unittest.TestCase):
    """Dominated first: a variant another variant beats on BOTH sides has nothing left
    to offer a front, so it goes before anything that still states a trade-off."""

    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        self.state = self.tmp / "state"

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def add(self, name, score, text=None, path=None):
        return variants.record(self.state, "skill:alpha", text or f"body {name}", score,
                               ops=[{"op": "add", "anchor": "x", "text": name}],
                               run_id=name, finding_id=name, path=path, cap=0)

    def test_dominated_variants_are_evicted_before_anything_else(self):
        best = self.add("best", scores(4, 4))
        trade = self.add("trade", scores(4, 1))       # best on prevented, worst on preserved
        weak = self.add("weak", scores(1, 1))         # beaten by best on both sides
        newest = self.add("newest", scores(2, 4))     # ties best on preserved, so it stays
        records = variants.load(self.state, "skill:alpha")
        self.assertEqual(variants.dominated_ids(records), [weak["id"]])
        kept, evicted = variants.evict(records, 3, live=None)
        self.assertEqual([v["id"] for v in evicted], [weak["id"]])
        self.assertEqual({v["id"] for v in kept},
                         {best["id"], trade["id"], newest["id"]})

    def test_ties_are_not_domination(self):
        a = self.add("a", scores(4, 2))
        b = self.add("b", scores(4, 1))              # ties on prevented, worse on preserved
        records = variants.load(self.state, "skill:alpha")
        self.assertEqual(variants.dominated_ids(records), [])
        self.assertFalse(variants.dominates(a, b))

    def test_unscorable_variants_neither_dominate_nor_are_dominated(self):
        self.add("good", scores(4, 4))
        self.add("blind", scores(0, 0, unscorable=True, reason="below case floor"))
        records = variants.load(self.state, "skill:alpha")
        self.assertEqual(variants.dominated_ids(records), [])

    def test_cap_falls_back_to_oldest_first_once_nothing_is_dominated(self):
        for i in range(4):
            self.add(f"v{i}", scores(i, 3 - i))       # a pure trade-off ladder
        records = variants.load(self.state, "skill:alpha")
        self.assertEqual(variants.dominated_ids(records), [])
        kept, evicted = variants.evict(records, 2, live=None)
        self.assertEqual([v["run_id"] for v in evicted], ["v0", "v1"])
        self.assertEqual([v["run_id"] for v in kept], ["v2", "v3"])

    def test_the_live_variant_and_the_newest_survive_the_cap(self):
        live_file = self.tmp / "alpha.md"
        live_file.write_text("body live")
        oldest = self.add("live", scores(0, 0), text="body live", path=live_file)
        self.add("mid", scores(3, 3), path=live_file)
        newest = self.add("new", scores(1, 1), path=live_file)
        records = variants.load(self.state, "skill:alpha")
        self.assertEqual(variants.live_id(records), oldest["id"])
        kept, _ = variants.evict(records, 2, live=variants.live_id(records))
        self.assertEqual({v["id"] for v in kept}, {oldest["id"], newest["id"]})

    def test_record_enforces_the_cap_as_it_writes(self):
        self.add("keep", scores(4, 4))
        self.add("weak", scores(1, 1))
        variants.record(self.state, "skill:alpha", "body newest", scores(2, 2),
                        run_id="newest", cap=2)
        self.assertEqual({v["run_id"] for v in variants.load(self.state, "skill:alpha")},
                         {"keep", "newest"})


class TestViews(ArchiveCase):
    def seed(self):
        self.run_gate([self.finding([{"op": "add", "anchor": "- then ship",
                                      "text": "- read the diff first",
                                      "motivated_by": ["sample:0"]}])])
        return self.archived()[0]

    def test_cli_prints_the_lineage_with_scores(self):
        v1 = self.seed()
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), self.assertRaises(SystemExit) as cm:
            gym.main(["archive", "skill:alpha", "--state", str(self.state)])
        self.assertEqual(cm.exception.code, 0)
        out = buf.getvalue()
        self.assertIn(v1["id"], out)
        self.assertIn("prevented 4/4", out)
        self.assertIn("preserved 4/4", out)
        self.assertIn("- read the diff first", out)

    def test_cli_json_view_carries_the_whole_record(self):
        v1 = self.seed()
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), self.assertRaises(SystemExit):
            gym.main(["archive", "skill:alpha", "--state", str(self.state), "--json"])
        doc = json.loads(buf.getvalue())
        self.assertEqual(doc["artifact"], "skill:alpha")
        self.assertEqual(doc["variants"][0]["id"], v1["id"])

    def test_cli_on_an_artifact_with_no_variants_says_so(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), self.assertRaises(SystemExit):
            gym.main(["archive", "skill:ghost", "--state", str(self.state)])
        self.assertIn("no archived variants", buf.getvalue())

    def test_dashboard_renders_the_lineage_per_artifact(self):
        v1 = self.seed()
        html = dashboard.render_dashboard("r1", [], {}, [], [], {"totals": {"sessions": 1}}, {},
                                          archive=variants.load_all(self.state))
        self.assertIn("Variant archive", html)
        self.assertIn("skill:alpha", html)
        self.assertIn(v1["id"], html)
        self.assertIn("<td>4/4</td>", html)

    def test_dashboard_omits_the_section_with_an_empty_archive(self):
        html = dashboard.render_dashboard("r1", [], {}, [], [], {"totals": {"sessions": 1}}, {})
        self.assertNotIn("Variant archive", html)


if __name__ == "__main__":
    unittest.main()
