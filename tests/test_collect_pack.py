import json, shutil, sys, pathlib, tempfile, unittest
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "scripts"))
import collect

FIX = pathlib.Path(__file__).parent / "fixtures" / "basic_session.jsonl"
CAPS = {"excerpts": 40, "tokens_per_excerpt": 1500, "total_tokens": 60000}


class TestPack(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        root = pathlib.Path(self.tmp) / "data"
        (root / "projects" / "-tmp-projA").mkdir(parents=True)
        (root / "projects" / "-tmp-projB").mkdir(parents=True)
        shutil.copy(FIX, root / "projects" / "-tmp-projA" / "s1.jsonl")
        shutil.copy(FIX, root / "projects" / "-tmp-projB" / "s2.jsonl")
        self.root = root

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def test_pack_aggregates_and_filters(self):
        pack = collect.collect_corpus(self.root, None, ["*"], [], collect.DEFAULT_CORRECTION_RE, CAPS)
        self.assertEqual(pack["usage"]["totals"]["sessions"], 2)
        self.assertEqual(pack["usage"]["corrections"]["total"], 2)
        self.assertEqual(pack["activation"]["items"]["skill:tdd"]["count"], 4)
        self.assertEqual(len(pack["samples"]), 2)
        self.assertEqual(pack["usage"]["waste"]["top_duplicate_read_paths"][0],
                         ("/tmp/projA/parser.py", 4))
        self.assertNotIn("_dup_read_paths", pack["sessions"][0])
        # exclude glob drops projB
        pack2 = collect.collect_corpus(self.root, None, ["*"], ["*projB*"], collect.DEFAULT_CORRECTION_RE, CAPS)
        self.assertEqual(pack2["usage"]["totals"]["sessions"], 1)
        # since filter after fixture date drops everything
        pack3 = collect.collect_corpus(self.root, "2027-01-01T00:00:00Z", ["*"], [], collect.DEFAULT_CORRECTION_RE, CAPS)
        self.assertEqual(pack3["usage"]["totals"]["sessions"], 0)

    def test_corrections_by_model_and_per_model_sessions(self):
        pack = collect.collect_corpus(self.root, None, ["*"], [], collect.DEFAULT_CORRECTION_RE, CAPS)
        self.assertEqual(pack["usage"]["corrections_by_model"], {"claude-fable-5": 2})
        self.assertEqual(pack["usage"]["per_model"]["claude-fable-5"]["sessions"], 2)
        self.assertEqual(pack["usage"]["per_model"]["claude-haiku-4-5"]["sessions"], 2)
        self.assertEqual(pack["usage"]["waste"]["main_model_heavy_sessions"], 0)

    def test_main_model_heavy_sessions_detected(self):
        root = pathlib.Path(self.tmp) / "heavy"
        (root / "projects" / "-heavy").mkdir(parents=True)
        lines = [
            json.dumps({"type": "user", "uuid": "u1", "timestamp": "2026-06-20T10:00:00Z",
                        "message": {"role": "user", "content": "please add a feature"}}),
            json.dumps({"type": "assistant", "uuid": "a1", "parentUuid": "u1",
                        "timestamp": "2026-06-20T10:00:05Z",
                        "message": {"role": "assistant", "model": "claude-opus-4-7",
                                    "usage": {"input_tokens": 10, "output_tokens": 60000},
                                    "content": [{"type": "text", "text": "Reading file."}] +
                                              [{"type": "tool_use", "id": f"t{i}", "name": "Read",
                                                "input": {"file_path": "same.py"}} for i in range(6)]}}),
            json.dumps({"type": "user", "uuid": "u2", "parentUuid": "a1",
                        "timestamp": "2026-06-20T10:00:10Z",
                        "message": {"role": "user", "content": "no, wrong approach"}}),
        ]
        (root / "projects" / "-heavy" / "s.jsonl").write_text("\n".join(lines) + "\n")
        pack = collect.collect_corpus(root, None, ["*"], [], collect.DEFAULT_CORRECTION_RE, CAPS)
        self.assertEqual(pack["usage"]["waste"]["main_model_heavy_sessions"], 1)
        self.assertEqual(pack["usage"]["corrections_by_model"], {"claude-opus-4-7": 1})
        self.assertEqual(pack["usage"]["per_model"]["claude-opus-4-7"]["sessions"], 1)

    def test_same_filename_across_projects_counts_are_isolated(self):
        root = pathlib.Path(self.tmp) / "data2"
        (root / "projects" / "-a").mkdir(parents=True)
        (root / "projects" / "-b").mkdir(parents=True)
        shutil.copy(FIX, root / "projects" / "-a" / "same.jsonl")
        shutil.copy(FIX, root / "projects" / "-b" / "same.jsonl")
        pack = collect.collect_corpus(root, None, ["*"], [], collect.DEFAULT_CORRECTION_RE, CAPS)
        self.assertEqual([s["corrections_count"] for s in pack["sessions"]], [1, 1])

    def test_write_pack_and_metrics(self):
        out = pathlib.Path(self.tmp) / "ev"
        state = pathlib.Path(self.tmp) / "state"
        pack = collect.collect_corpus(self.root, None, ["*"], [], collect.DEFAULT_CORRECTION_RE, CAPS)
        collect.write_pack(out, pack)
        u = json.loads((out / "usage.json").read_text())
        self.assertEqual(u["schema_version"], "1")
        self.assertEqual(u["harness"], "claude-code")
        sess = json.loads((out / "sessions.json").read_text())["sessions"][0]
        self.assertNotIn("corrections", sess)              # excerpts live only in samples.json
        self.assertEqual(sess["corrections_count"], 1)
        collect.append_metrics(state, collect.metrics_row("2026-07-01", pack))
        row = json.loads((state / "state" / "metrics.jsonl").read_text().splitlines()[-1])
        self.assertEqual(row["n_sessions"], 2)
        self.assertAlmostEqual(row["correction_rate"], 1.0)

    def test_write_pack_includes_constraints_when_provided(self):
        out = pathlib.Path(self.tmp) / "ev2"
        pack = collect.collect_corpus(self.root, None, ["*"], [], collect.DEFAULT_CORRECTION_RE, CAPS)
        constraints = {"schema_version": "1", "harness": "claude-code",
                       "rejected": [{"title": "t", "reason": "r", "ts": "2026-06-01T00:00:00Z"}]}
        collect.write_pack(out, pack, constraints)
        c = json.loads((out / "constraints.json").read_text())
        self.assertEqual(c["rejected"][0]["title"], "t")
        mode = (out / "constraints.json").stat().st_mode & 0o777
        self.assertEqual(mode, 0o600)

    def test_constraints_pack_reads_last_20_rejected_with_titles(self):
        lpath = pathlib.Path(self.tmp) / "state" / "state" / "ledger.jsonl"
        lpath.parent.mkdir(parents=True)
        lines = []
        for i in range(25):
            rid = f"r{i}"
            lines.append(json.dumps({"id": rid, "status": "proposed",
                                     "rec": {"title": f"idea {i}"}, "ts": f"2026-06-{i + 1:02d}T00:00:00Z"}))
            lines.append(json.dumps({"id": rid, "status": "rejected", "reason": f"no {i}",
                                     "ts": f"2026-06-{i + 1:02d}T00:05:00Z"}))
        lpath.write_text("\n".join(lines) + "\n")
        c = collect.constraints_pack(lpath)
        self.assertEqual(c["schema_version"], "1")
        self.assertEqual(len(c["rejected"]), 20)
        self.assertEqual(c["rejected"][0]["title"], "idea 5")    # oldest of the most recent 20
        self.assertEqual(c["rejected"][-1], {"title": "idea 24", "reason": "no 24",
                                             "ts": "2026-06-25T00:05:00Z"})

    def test_constraints_pack_missing_ledger_is_empty(self):
        c = collect.constraints_pack(pathlib.Path(self.tmp) / "nope" / "ledger.jsonl")
        self.assertEqual(c["rejected"], [])

    def test_constraints_pack_scrubs_secrets(self):
        lpath = pathlib.Path(self.tmp) / "state2" / "ledger.jsonl"
        lpath.parent.mkdir(parents=True)
        lpath.write_text(json.dumps({"id": "r1", "status": "rejected",
                                     "reason": "keep sk-ant-api03-FAKESECRETFAKESECRET out"}) + "\n")
        reason = collect.constraints_pack(lpath)["rejected"][0]["reason"]
        self.assertIn("[REDACTED:", reason)
        self.assertNotIn("sk-ant-api03-FAKESECRETFAKESECRET", reason)

    def test_scale_caps_to_budget_shrinks_proportionally(self):
        out = collect.scale_caps_to_budget(dict(CAPS), 10000)
        self.assertEqual(out["total_tokens"], 2000)
        self.assertEqual(out["excerpts"], 1)
        # tokens_per_excerpt shrinks by the same factor, so one excerpt can
        # never blow the scaled total and silently yield zero samples
        self.assertEqual(out["tokens_per_excerpt"], 50)
        self.assertLessEqual(out["tokens_per_excerpt"], out["total_tokens"])

    def test_scaled_caps_still_yield_a_sample(self):
        caps = collect.scale_caps_to_budget(dict(CAPS), 10000)
        pack = collect.collect_corpus(self.root, None, ["*"], [], collect.DEFAULT_CORRECTION_RE, caps)
        self.assertGreaterEqual(len(pack["samples"]), 1)

    def test_scale_caps_to_budget_no_cap_when_absent_or_generous(self):
        self.assertEqual(collect.scale_caps_to_budget(dict(CAPS), 0), CAPS)
        self.assertEqual(collect.scale_caps_to_budget(dict(CAPS), None), CAPS)
        self.assertEqual(collect.scale_caps_to_budget(dict(CAPS), 1_000_000)["total_tokens"],
                         CAPS["total_tokens"])

    def test_scale_caps_to_budget_refuses_below_floor(self):
        with self.assertRaises(SystemExit) as cm:
            collect.scale_caps_to_budget(dict(CAPS), 9000)
        self.assertEqual(cm.exception.code, 2)

    def test_sessions_carry_their_own_activation_for_the_gym(self):
        pack = collect.collect_corpus(self.root, None, ["*"], [], collect.DEFAULT_CORRECTION_RE, CAPS)
        self.assertEqual(pack["sessions"][0]["activation"]["skill:tdd"], 2)
        # and it survives into the written pack
        out = pathlib.Path(self.tmp) / "ev3"
        collect.write_pack(out, pack)
        written = json.loads((out / "sessions.json").read_text())["sessions"][0]
        self.assertIn("skill:tdd", written["activation"])

    def test_working_excerpts_only_from_sessions_with_no_correction(self):
        root = pathlib.Path(self.tmp) / "clean"
        (root / "projects" / "-clean").mkdir(parents=True)
        lines = [
            json.dumps({"type": "user", "uuid": "u1", "timestamp": "2026-06-21T10:00:00Z",
                        "cwd": "/tmp/clean",
                        "message": {"role": "user", "content": "write the changelog entry"}}),
            json.dumps({"type": "assistant", "uuid": "a1", "parentUuid": "u1",
                        "timestamp": "2026-06-21T10:00:05Z", "attributionSkill": "tdd",
                        "message": {"role": "assistant", "model": "m",
                                    "usage": {"input_tokens": 5, "output_tokens": 5},
                                    "content": [{"type": "text",
                                                 "text": "Added it under Unreleased."}]}}),
            json.dumps({"type": "user", "uuid": "u2", "parentUuid": "a1",
                        "timestamp": "2026-06-21T10:01:00Z",
                        "message": {"role": "user", "content": "thanks, ship it"}}),
        ]
        (root / "projects" / "-clean" / "s.jsonl").write_text("\n".join(lines) + "\n")
        pack = collect.collect_corpus(root, None, ["*"], [], collect.DEFAULT_CORRECTION_RE, CAPS)
        self.assertEqual(len(pack["working"]), 1)
        w = pack["working"][0]
        self.assertEqual(w["kind"], "working")
        self.assertEqual(w["user_text"], "write the changelog entry")
        self.assertEqual(w["assistant_text"], "Added it under Unreleased.")
        self.assertNotIn("_working", pack["sessions"][0])
        # the corrected fixture sessions contribute nothing to the working side
        corrected = collect.collect_corpus(self.root, None, ["*"], [],
                                           collect.DEFAULT_CORRECTION_RE, CAPS)
        self.assertEqual(corrected["working"], [])
        out = pathlib.Path(self.tmp) / "ev4"
        collect.write_pack(out, pack)
        doc = json.loads((out / "working.json").read_text())
        self.assertEqual(doc["schema_version"], "1")
        self.assertEqual(len(doc["samples"]), 1)
        self.assertEqual((out / "working.json").stat().st_mode & 0o777, 0o600)

    def test_working_excerpts_are_redacted(self):
        root = pathlib.Path(self.tmp) / "secret"
        (root / "projects" / "-secret").mkdir(parents=True)
        lines = [
            json.dumps({"type": "user", "uuid": "u1", "timestamp": "2026-06-21T10:00:00Z",
                        "message": {"role": "user",
                                    "content": "use sk-ant-api03-FAKEFAKEFAKEFAKEFAKE for the call"}}),
            json.dumps({"type": "assistant", "uuid": "a1", "parentUuid": "u1",
                        "timestamp": "2026-06-21T10:00:05Z",
                        "message": {"role": "assistant", "model": "m",
                                    "usage": {"input_tokens": 1, "output_tokens": 1},
                                    "content": [{"type": "text", "text": "Done."}]}}),
        ]
        (root / "projects" / "-secret" / "s.jsonl").write_text("\n".join(lines) + "\n")
        pack = collect.collect_corpus(root, None, ["*"], [], collect.DEFAULT_CORRECTION_RE, CAPS)
        self.assertIn("[REDACTED:", pack["working"][0]["user_text"])
        self.assertNotIn("sk-ant-api03-FAKEFAKEFAKEFAKEFAKE", pack["working"][0]["user_text"])

    def test_metrics_row_honest_additions(self):
        pack = collect.collect_corpus(self.root, None, ["*"], [], collect.DEFAULT_CORRECTION_RE, CAPS)
        row = collect.metrics_row("r", pack)
        self.assertEqual(row["zero_correction_session_rate"], 0.0)   # both sessions had 1 correction
        # fixture spans 10:00:00Z-10:02:10Z (130s = 2.1667min); brief's test text said 1.1,
        # which doesn't match this fixture's actual timestamp range — corrected here
        self.assertAlmostEqual(row["mean_session_minutes"], 13 / 6, places=3)
        self.assertGreater(row["turns_per_session"], 0)


if __name__ == "__main__":
    unittest.main()
