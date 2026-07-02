import json, sys, pathlib, tempfile, unittest
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "scripts"))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "adapters" / "claude_code"))
import templates


class TestTemplates(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)
        (self.root / "settings.json").write_text(json.dumps({"model": "opusplan",
                                                             "enabledPlugins": {"a@b": True}}))
        (self.root / "agents").mkdir()
        (self.root / "agents" / "helper.md").write_text("---\nname: helper\nmodel: opus\n---\nbody\n")

    def tearDown(self):
        self.tmp.cleanup()

    def test_setting_change_preserves_other_keys(self):
        edits = templates.render({"type": "setting_change",
                                  "payload": {"key_path": ["skillOverrides", "dusty"], "value": "off"}},
                                 self.root)
        path, content = edits[0]
        obj = json.loads(content)
        self.assertEqual(obj["skillOverrides"]["dusty"], "off")
        self.assertEqual(obj["model"], "opusplan")
        self.assertEqual(obj["enabledPlugins"]["a@b"], True)

    def test_frontmatter_edit_and_file_create(self):
        agent = self.root / "agents" / "helper.md"
        edits = templates.render({"type": "frontmatter_edit",
                                  "payload": {"file": str(agent), "key": "model", "value": "haiku"}},
                                 self.root)
        self.assertIn("model: haiku", edits[0][1])
        self.assertIn("body", edits[0][1])
        edits = templates.render({"type": "file_create",
                                  "payload": {"path": str(self.root / "agents" / "shadow.md"),
                                              "content": "---\nname: shadow\nmodel: sonnet\n---\nx\n"}},
                                 self.root)
        self.assertEqual(edits[0][0].name, "shadow.md")

    def test_file_replace_overwrites_existing_and_requires_prior_file(self):
        agent = self.root / "agents" / "helper.md"
        edits = templates.render({"type": "file_replace",
                                  "payload": {"path": str(agent),
                                              "content": "---\nname: helper\nmodel: sonnet\n---\nnew body\n"}},
                                 self.root)
        self.assertEqual(edits[0][1], "---\nname: helper\nmodel: sonnet\n---\nnew body\n")
        with self.assertRaises(ValueError):   # target must already exist
            templates.render({"type": "file_replace",
                              "payload": {"path": str(self.root / "agents" / "ghost.md"),
                                          "content": "c"}}, self.root)

    def test_file_create_and_replace_into_workflows_root(self):
        (self.root / "workflows").mkdir()
        wf = self.root / "workflows" / "existing.md"
        wf.write_text("---\nname: existing\n---\nold\n")
        edits = templates.render({"type": "file_create",
                                  "payload": {"path": str(self.root / "workflows" / "new.md"),
                                              "content": "---\nname: new\n---\nbody\n"}}, self.root)
        self.assertEqual(edits[0][0].name, "new.md")
        edits = templates.render({"type": "file_replace",
                                  "payload": {"path": str(wf), "content": "---\nname: existing\n---\nnew\n"}},
                                 self.root)
        self.assertIn("new", edits[0][1])

    def test_extra_roots_file_create_ok_file_replace_rejected(self):
        with tempfile.TemporaryDirectory() as mem:
            existing = pathlib.Path(mem) / "old.md"
            existing.write_text("---\nname: old\n---\nbody\n")
            edits = templates.render({"type": "file_create",
                                      "payload": {"path": str(pathlib.Path(mem) / "new.md"),
                                                  "content": "---\nname: new\n---\nbody\n"}},
                                     self.root, extra_roots=[mem])
            self.assertEqual(edits[0][0].name, "new.md")
            with self.assertRaises(ValueError):   # memory is create-only: never rewrite existing notes
                templates.render({"type": "file_replace",
                                  "payload": {"path": str(existing), "content": "c"}},
                                 self.root, extra_roots=[mem])

    def test_file_replace_symlinked_target_rejected(self):
        real = self.root / "real.md"
        real.write_text("---\nname: real\n---\nbody\n")
        link = self.root / "agents" / "link.md"
        link.symlink_to(real)
        with self.assertRaises(ValueError):
            templates.render({"type": "file_replace",
                              "payload": {"path": str(link), "content": "c"}}, self.root)

    def test_file_create_under_symlinked_dir_rejected(self):
        (self.root / "skills").mkdir()
        outside = self.root / "outside"
        outside.mkdir()
        (self.root / "skills" / "sub").symlink_to(outside)
        with self.assertRaises(ValueError):
            templates.render({"type": "file_create",
                              "payload": {"path": str(self.root / "skills" / "sub" / "x.md"),
                                          "content": "c"}}, self.root)

    def test_extra_roots_traversal_rejected(self):
        with tempfile.TemporaryDirectory() as mem:
            with self.assertRaises(ValueError):
                templates.render({"type": "file_create",
                                  "payload": {"path": str(pathlib.Path(mem) / ".." / "evil.md"),
                                              "content": "c"}}, self.root, extra_roots=[mem])
            with self.assertRaises(ValueError):   # not in extra_roots at all when omitted
                templates.render({"type": "file_create",
                                  "payload": {"path": str(pathlib.Path(mem) / "x.md"), "content": "c"}},
                                 self.root)

    def test_policy_violations_raise(self):
        with self.assertRaises(ValueError):   # forbidden settings root
            templates.render({"type": "setting_change",
                              "payload": {"key_path": ["hooks", "PostToolUse"], "value": "x"}}, self.root)
        with self.assertRaises(ValueError):   # path outside skills/agents
            templates.render({"type": "file_create",
                              "payload": {"path": "/etc/x.md", "content": "c"}}, self.root)
        with self.assertRaises(ValueError):   # .. traversal escaping the sanctioned dirs
            templates.render({"type": "file_create",
                              "payload": {"path": str(self.root / "agents" / ".." / ".." / "evil.md"),
                                          "content": "c"}}, self.root)
        with self.assertRaises(ValueError):   # sibling-dir prefix bypass
            templates.render({"type": "file_create",
                              "payload": {"path": str(self.root) + "/agents-evil/x.md",
                                          "content": "c"}}, self.root)

    def test_render_raises_valueerror_not_typeerror_on_scalar_path(self):
        with self.assertRaises(ValueError):
            templates.render({"type": "setting_change",
                              "payload": {"key_path": ["model", "x"], "value": "y"}}, self.root)

    def test_smoke_check_reports_unreadable_md_instead_of_raising(self):
        errs = templates.smoke_check([self.root / "agents" / "ghost.md"], self.root)
        self.assertEqual(len(errs), 1)
        self.assertIn("unreadable", errs[0])

    def test_frontmatter_value_newline_injection_rejected(self):
        with self.assertRaises(ValueError):
            templates.render({"type": "frontmatter_edit",
                              "payload": {"file": str(self.root / "agents" / "helper.md"),
                                          "key": "description",
                                          "value": "x\ntools: Bash, Write"}}, self.root)

    def test_frontmatter_key_newline_injection_rejected(self):
        with self.assertRaises(ValueError):
            templates.render({"type": "frontmatter_edit",
                              "payload": {"file": str(self.root / "agents" / "helper.md"),
                                          "key": "description\ntools",
                                          "value": "Bash, Write"}}, self.root)

    def test_frontmatter_edit_missing_file_raises_valueerror(self):
        with self.assertRaises(ValueError):
            templates.render({"type": "frontmatter_edit",
                              "payload": {"file": str(self.root / "agents" / "ghost.md"),
                                          "key": "model", "value": "haiku"}}, self.root)

    def test_smoke_check_catches_breakage(self):
        bad = self.root / "agents" / "bad.md"
        bad.write_text("---\nname: bad\nmodel: gpt-9\n---\n")
        errs = templates.smoke_check([bad, self.root / "settings.json"], self.root)
        self.assertEqual(len(errs), 1)
        self.assertIn("gpt-9", errs[0])
        (self.root / "settings.json").write_text("{broken")
        errs = templates.smoke_check([self.root / "settings.json"], self.root)
        self.assertIn("invalid JSON", errs[0])


if __name__ == "__main__":
    unittest.main()
