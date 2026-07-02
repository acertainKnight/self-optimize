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
        # exclude glob drops projB
        pack2 = collect.collect_corpus(self.root, None, ["*"], ["*projB*"], collect.DEFAULT_CORRECTION_RE, CAPS)
        self.assertEqual(pack2["usage"]["totals"]["sessions"], 1)
        # since filter after fixture date drops everything
        pack3 = collect.collect_corpus(self.root, "2027-01-01T00:00:00Z", ["*"], [], collect.DEFAULT_CORRECTION_RE, CAPS)
        self.assertEqual(pack3["usage"]["totals"]["sessions"], 0)

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


if __name__ == "__main__":
    unittest.main()
