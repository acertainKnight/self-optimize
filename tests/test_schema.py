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

    def test_action_types_and_categories_extended(self):
        self.assertIn("file_replace", schema.ACTION_TYPES_A)
        self.assertEqual(len(schema.CATEGORIES), 14)
        for cat in ("skill-improve", "new-agent", "new-workflow", "new-plugin", "memory"):
            self.assertIn(cat, schema.CATEGORIES)
        r = good_rec()
        r["action"] = {"harness": "claude-code", "tier": "A", "type": "file_replace",
                       "payload": {"path": "/x/agents/y.md", "content": "z"}}
        self.assertEqual(schema.validate_rec(r), [])
        r["action"]["tier"] = "B"
        self.assertTrue(any("tier" in e for e in schema.validate_rec(r)))

    def test_derive_extra_roots(self):
        self.assertEqual(schema.derive_extra_roots({"settings": {"autoMemoryDirectory": "~/mem"}}),
                         [str(pathlib.Path("~/mem").expanduser())])
        self.assertIsNone(schema.derive_extra_roots({"settings": {"autoMemoryDirectory": "relative/mem"}}))
        self.assertIsNone(schema.derive_extra_roots({"settings": {"autoMemoryDirectory": 42}}))
        self.assertIsNone(schema.derive_extra_roots({"settings": {"autoMemoryDirectory": "  "}}))
        self.assertIsNone(schema.derive_extra_roots(None))
        self.assertIsNone(schema.derive_extra_roots({}))
        self.assertIsNone(schema.derive_extra_roots({"settings": "corrupt"}))

    def test_derive_extra_roots_rejects_overlap_with_data_root(self):
        data_root = "/fakehome/.claude"
        for amd in ("/fakehome/.claude/skills", "/fakehome/.claude/skills/notes"):
            self.assertIsNone(schema.derive_extra_roots(
                {"settings": {"autoMemoryDirectory": amd}}, data_root))
        self.assertEqual(schema.derive_extra_roots(
            {"settings": {"autoMemoryDirectory": "/fakehome/.claude/memory"}}, data_root),
            ["/fakehome/.claude/memory"])


def ops_rec(ops=None, tier="A"):
    return {
        "title": "Anchor the test rule in the review skill",
        "category": "skill-improve",
        "evidence_refs": ["artifact:skill:review"],
        "impact": {"ordinal": "med"},
        "risk": "low — one bullet changes",
        "metric": {"key": "correction_rate", "direction": "down", "scope": "global"},
        "action": {"harness": "claude-code", "tier": tier, "type": "file_ops",
                   "payload": {"path": "/fakehome/.claude/skills/review/SKILL.md",
                               "ops": ops if ops is not None else [
                                   {"op": "replace", "anchor": "- run the tests",
                                    "text": "- run `python3 -m unittest` before committing",
                                    "motivated_by": ["sample:0"]}]}},
    }


class TestOpSchema(unittest.TestCase):
    def test_valid_file_ops_rec_passes_at_either_tier(self):
        self.assertEqual(schema.validate_rec(ops_rec()), [])
        self.assertEqual(schema.validate_rec(ops_rec(tier="B")), [])
        r = ops_rec(); r["action"]["tier"] = "C"
        self.assertTrue(any("tier must be A or B" in e for e in schema.validate_rec(r)))

    def test_malformed_ops_are_rejected(self):
        cases = {
            "op must be": [{"op": "rewrite", "anchor": "- x", "text": "y",
                            "motivated_by": ["sample:0"]}],
            "anchor must be a non-empty": [{"op": "delete", "anchor": "  ",
                                            "motivated_by": ["sample:0"]}],
            "anchor must be a single line": [{"op": "delete", "anchor": "- x\n- y",
                                              "motivated_by": ["sample:0"]}],
            "delete carries no text": [{"op": "delete", "anchor": "- x", "text": "y",
                                        "motivated_by": ["sample:0"]}],
            "needs non-empty text": [{"op": "add", "anchor": "- x", "text": "",
                                      "motivated_by": ["sample:0"]}],
            "motivated_by must be": [{"op": "add", "anchor": "- x", "text": "y"}],
        }
        for want, ops in cases.items():
            with self.subTest(want=want):
                errs = schema.validate_rec(ops_rec(ops))
                self.assertTrue(any(want in e for e in errs), errs)
        self.assertTrue(any("non-empty ops list" in e for e in schema.validate_rec(ops_rec([]))))

    def test_rewrite_payload_is_pinned_to_tier_b(self):
        r = ops_rec()
        r["action"]["type"] = "file_replace"
        r["action"]["payload"] = {"op": "rewrite", "path": "/fakehome/.claude/skills/r/SKILL.md",
                                  "content": "---\nname: r\n---\nbody\n"}
        self.assertTrue(any("tier must be B" in e for e in schema.validate_rec(r)))
        r["action"]["tier"] = "B"
        self.assertEqual(schema.validate_rec(r), [])
        self.assertTrue(schema.is_rewrite(r["action"]))
        self.assertFalse(schema.is_rewrite(ops_rec()["action"]))


class TestApplyOps(unittest.TestCase):
    BODY = "---\nname: review\n---\n# review\n- run the tests\n- ship it\n"

    def test_replace_add_and_delete(self):
        out = schema.apply_ops(self.BODY, [
            {"op": "replace", "anchor": "- run the tests", "text": "- run the full suite"},
            {"op": "add", "anchor": "- run the full suite", "text": "- read the diff\n- then ship"},
            {"op": "delete", "anchor": "- ship it"},
        ])
        self.assertEqual(out, "---\nname: review\n---\n# review\n- run the full suite\n"
                              "- read the diff\n- then ship\n")

    def test_untouched_bytes_survive_verbatim(self):
        self.assertEqual(schema.apply_ops(self.BODY, [{"op": "delete", "anchor": "- ship it"}]),
                         "---\nname: review\n---\n# review\n- run the tests\n")
        self.assertEqual(schema.apply_ops(self.BODY, [
            {"op": "add", "anchor": "- ship it", "text": "- and tell someone"}]),
            self.BODY + "- and tell someone\n")

    def test_ambiguous_anchor_refuses(self):
        with self.assertRaises(ValueError) as cm:
            schema.apply_ops("- x\n- dup\n- dup\n", [{"op": "delete", "anchor": "- dup"}])
        self.assertIn("ambiguous anchor", str(cm.exception))

    def test_missing_anchor_refuses(self):
        with self.assertRaises(ValueError) as cm:
            schema.apply_ops(self.BODY, [{"op": "replace", "anchor": "- run the tests ",
                                          "text": "x"}])
        self.assertIn("anchor not found", str(cm.exception))

    def test_reapplying_the_same_ops_refuses(self):
        for ops in ([{"op": "replace", "anchor": "- run the tests", "text": "- run all tests"}],
                    [{"op": "delete", "anchor": "- ship it"}],
                    [{"op": "add", "anchor": "- ship it", "text": "- and tell someone"}]):
            with self.subTest(op=ops[0]["op"]):
                once = schema.apply_ops(self.BODY, ops)
                with self.assertRaises(ValueError) as cm:
                    schema.apply_ops(once, ops)
                self.assertIn("already applied", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
