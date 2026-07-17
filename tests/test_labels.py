import json, pathlib, sys, tempfile, unittest
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "scripts"))
import labels, report


class TestLabels(unittest.TestCase):
    def test_validate_drops_bad_and_duplicate_labels(self):
        raw = [
            {"sample": 0, "category": "scope-creep"},
            {"sample": 1, "category": "verbosity"},
            {"sample": 1, "category": "style"},          # duplicate index
            {"sample": 9, "category": "style"},          # out of range
            {"sample": 2, "category": "made-up"},        # bad category
            "junk",
        ]
        counts, dropped = labels.validate_labels(raw, 3)
        self.assertEqual(counts, {"scope-creep": 1, "verbosity": 1})
        self.assertEqual(dropped, 4)

    def test_main_persists_counts_to_metrics(self):
        with tempfile.TemporaryDirectory() as d:
            base = pathlib.Path(d)
            ev, state = base / "ev", base / "st"
            ev.mkdir()
            (state / "state").mkdir(parents=True)
            (state / "state" / "metrics.jsonl").write_text(
                json.dumps({"run_id": "r1", "correction_rate": 0.1}) + "\n")
            (ev / "samples.json").write_text(json.dumps(
                {"samples": [{"user_text": "no"}, {"user_text": "stop"}]}))
            (ev / "labels.json").write_text(json.dumps(
                [{"sample": 0, "category": "scope-creep"},
                 {"sample": 1, "category": "scope-creep"}]))
            labels.main(["--evidence", str(ev), "--state", str(state)])
            row = json.loads((state / "state" / "metrics.jsonl").read_text().splitlines()[-1])
            self.assertEqual(row["corrections_by_category"], {"scope-creep": 2})
            self.assertEqual(json.loads((ev / "labels.json").read_text())["counts"],
                             {"scope-creep": 2})

    def test_report_renders_category_trend_with_prev(self):
        trend = [
            {"run_id": "a", "n_sessions": 5, "corrections_by_category": {"verbosity": 4}},
            {"run_id": "b", "n_sessions": 5, "corrections_by_category":
                {"verbosity": 1, "scope-creep": 2}},
        ]
        md = report.render("r", [], {"invalid": 0, "citations": 0, "guard": 0,
                                     "suppressed": []}, [], trend,
                           {"totals": {"sessions": 5},
                            "parse": {"skipped_lines": 0, "redactions": 0}}, {})
        self.assertIn("scope-creep 2 (prev 0)", md)
        self.assertIn("verbosity 1 (prev 4)", md)


if __name__ == "__main__":
    unittest.main()
