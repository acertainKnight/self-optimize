import sys, pathlib, unittest
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "scripts"))
import schema


def good_rec():
    return {
        "title": "Disable never-used skill foo",
        "category": "bloat",
        "evidence_refs": ["activation:skill:foo", "inventory:skill:foo"],
        "impact": {"ordinal": "med"},
        "risk": "low — user-invocable path preserved via name-only",
        "metric": {"key": "base_context_est", "direction": "down", "scope": "global"},
        "action": {"harness": "claude-code", "tier": "A", "type": "setting_change",
                   "payload": {"file": "settings.json",
                               "key_path": ["skillOverrides", "foo"], "value": "off"}},
    }


class TestSchema(unittest.TestCase):
    def test_valid_rec_passes(self):
        self.assertEqual(schema.validate_rec(good_rec()), [])

    def test_missing_field_and_bad_values_flagged(self):
        r = good_rec(); del r["metric"]
        self.assertIn("missing metric", schema.validate_rec(r))
        r2 = good_rec(); r2["action"]["type"] = "shell_exec"
        self.assertTrue(any("action type" in e for e in schema.validate_rec(r2)))
        r3 = good_rec(); r3["action"]["tier"] = "B"  # setting_change must be tier A
        self.assertTrue(any("tier" in e for e in schema.validate_rec(r3)))

    def test_non_dict_action_or_metric_returns_errors_not_crash(self):
        r = good_rec(); r["action"] = "oops"
        self.assertIn("action must be an object", schema.validate_rec(r))
        r2 = good_rec(); r2["metric"] = None
        self.assertIn("metric must be an object", schema.validate_rec(r2))

    def test_rec_id_stable_and_payload_sensitive(self):
        a, b = good_rec(), good_rec()
        self.assertEqual(schema.rec_id(a), schema.rec_id(b))
        b["action"]["payload"]["value"] = "name-only"
        self.assertNotEqual(schema.rec_id(a), schema.rec_id(b))

    def test_non_dict_impact_and_bad_ordinal_return_errors(self):
        r = good_rec(); r["impact"] = "high"
        self.assertIn("impact must be an object", schema.validate_rec(r))
        r2 = good_rec(); r2["impact"] = {"ordinal": "huge"}
        self.assertTrue(any("ordinal" in e for e in schema.validate_rec(r2)))

    def test_frontmatter(self):
        text = "---\nname: foo\nmodel: haiku\n---\n# body\n"
        fm = schema.parse_frontmatter(text)
        self.assertEqual(fm, {"name": "foo", "model": "haiku"})
        self.assertEqual(schema.parse_frontmatter("no frontmatter"), {})


if __name__ == "__main__":
    unittest.main()
