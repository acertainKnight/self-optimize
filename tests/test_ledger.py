import sys, pathlib, tempfile, unittest
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "scripts"))
import ledger


class TestLedger(unittest.TestCase):
    def test_load_last_wins_and_append(self):
        with tempfile.TemporaryDirectory() as d:
            p = pathlib.Path(d) / "ledger.jsonl"
            ledger.append(p, {"id": "abc", "status": "proposed", "evidence_hash": "e1"})
            ledger.append(p, {"id": "abc", "status": "rejected", "reason": "keep opus",
                              "evidence_hash": "e1"})
            entries = ledger.load(p)
            self.assertEqual(entries["abc"]["status"], "rejected")
            self.assertTrue(entries["abc"]["ts"])

    def test_load_skips_idless_and_non_dict_lines(self):
        with tempfile.TemporaryDirectory() as d:
            p = pathlib.Path(d) / "l.jsonl"
            p.write_text('42\n{"no_id": true}\n{"id": "a", "status": "proposed"}\n')
            self.assertEqual(list(ledger.load(p)), ["a"])

    def test_suppression_rules(self):
        entries = {
            "ap1": {"status": "applied", "evidence_hash": "x"},
            "rj1": {"status": "rejected", "reason": "keep opus", "evidence_hash": "same"},
        }
        self.assertEqual(ledger.suppress_reason({"id": "new1", "evidence_hash": "y"}, entries), None)
        self.assertEqual(ledger.suppress_reason({"id": "ap1", "evidence_hash": "x"}, entries),
                         "already applied")
        self.assertEqual(ledger.suppress_reason({"id": "rj1", "evidence_hash": "same"}, entries),
                         "rejected: keep opus")
        # rejected but evidence materially changed -> resurfaces
        self.assertIsNone(ledger.suppress_reason({"id": "rj1", "evidence_hash": "CHANGED"}, entries))


if __name__ == "__main__":
    unittest.main()
