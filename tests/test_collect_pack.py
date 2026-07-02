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

    def test_scale_caps_to_budget_no_cap_when_absent_or_generous(self):
        self.assertEqual(collect.scale_caps_to_budget(dict(CAPS), 0), CAPS)
        self.assertEqual(collect.scale_caps_to_budget(dict(CAPS), None), CAPS)
        self.assertEqual(collect.scale_caps_to_budget(dict(CAPS), 1_000_000)["total_tokens"],
                         CAPS["total_tokens"])

    def test_scale_caps_to_budget_refuses_below_floor(self):
        with self.assertRaises(SystemExit) as cm:
            collect.scale_caps_to_budget(dict(CAPS), 9000)
        self.assertEqual(cm.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
