import json, pathlib, sys, tempfile, unittest
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "scripts"))
import labels, report


class TestLabels(unittest.TestCase):
    def test_categories_are_mast_v2(self):
        # 14 adapted MAST failure modes + the "other" catch-all.
        self.assertEqual(len(labels.CATEGORIES), 15)
        self.assertIn("other", labels.CATEGORIES)

    def test_validate_drops_bad_and_duplicate_labels(self):
        raw = [
            {"sample": 0, "category": "spec-violation"},
            {"sample": 1, "category": "no-stop-condition"},
            {"sample": 1, "category": "role-violation"},  # duplicate index
            {"sample": 9, "category": "role-violation"},  # out of range
            {"sample": 2, "category": "made-up"},         # bad category
            "junk",
        ]
        counts, dropped = labels.validate_labels(raw, 3)
        self.assertEqual(counts, {"spec-violation": 1, "no-stop-condition": 1})
        self.assertEqual(dropped, 4)

    def test_validate_rejects_v1_labels(self):
        # Old-taxonomy strings are free text under v2 and must be rejected,
        # not silently accepted or auto-migrated at label time.
        raw = [{"sample": 0, "category": "scope-creep"},   # still valid, kept in v2
               {"sample": 1, "category": "verbosity"},     # v1-only, now invalid
               {"sample": 2, "category": "wrong-target"}]  # v1-only, now invalid
        counts, dropped = labels.validate_labels(raw, 3)
        self.assertEqual(counts, {"scope-creep": 1})
        self.assertEqual(dropped, 2)

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
                [{"sample": 0, "category": "spec-violation"},
                 {"sample": 1, "category": "spec-violation"}]))
            labels.main(["--evidence", str(ev), "--state", str(state)])
            row = json.loads((state / "state" / "metrics.jsonl").read_text().splitlines()[-1])
            self.assertEqual(row["corrections_by_category"], {"spec-violation": 2})
            self.assertEqual(json.loads((ev / "labels.json").read_text())["counts"],
                             {"spec-violation": 2})

    def test_migrate_categories_maps_v1_to_v2_and_sums_collisions(self):
        # wrong-assumption and premature-action both collapse onto
        # no-clarification; scope-creep and other pass through unchanged.
        counts = {"style": 3, "wrong-assumption": 1, "premature-action": 2,
                  "scope-creep": 4, "other": 1}
        self.assertEqual(labels.migrate_categories(counts),
                         {"role-violation": 3, "no-clarification": 3,
                          "scope-creep": 4, "other": 1})

    def test_migrate_categories_is_noop_on_v2_rows(self):
        counts = {"spec-violation": 2, "no-verification": 1}
        self.assertEqual(labels.migrate_categories(counts), counts)

    def test_report_renders_category_trend_with_prev(self):
        trend = [
            {"run_id": "a", "n_sessions": 5, "corrections_by_category": {"no-stop-condition": 4}},
            {"run_id": "b", "n_sessions": 5, "corrections_by_category":
                {"no-stop-condition": 1, "scope-creep": 2}},
        ]
        md = report.render("r", [], {"invalid": 0, "citations": 0, "guard": 0,
                                     "suppressed": []}, [], trend,
                           {"totals": {"sessions": 5},
                            "parse": {"skipped_lines": 0, "redactions": 0}}, {})
        self.assertIn("scope-creep 2 (prev 0)", md)
        self.assertIn("no-stop-condition 1 (prev 4)", md)

    def test_report_renders_continuity_across_taxonomy_migration(self):
        # Fixture: a pre-migration run recorded under the v1 vocabulary, and a
        # post-migration run recorded under v2. The trend must attribute the
        # v1 row's counts to their v2 names, not read as a reset to zero.
        trend = [
            {"run_id": "v1-run", "n_sessions": 5,
             "corrections_by_category": {"style": 3, "scope-creep": 2}},
            {"run_id": "v2-run", "n_sessions": 5,
             "corrections_by_category": {"role-violation": 1, "scope-creep": 4}},
        ]
        md = report.render("r", [], {"invalid": 0, "citations": 0, "guard": 0,
                                     "suppressed": []}, [], trend,
                           {"totals": {"sessions": 5},
                            "parse": {"skipped_lines": 0, "redactions": 0}}, {})
        self.assertIn("role-violation 1 (prev 3)", md)
        self.assertIn("scope-creep 4 (prev 2)", md)


if __name__ == "__main__":
    unittest.main()
