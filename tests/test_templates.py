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

    def test_apply_unified_diff_additive_new_file(self):
        result = templates._apply_unified_diff("", "@@ -0,0 +1,2 @@\n+line1\n+line2\n")
        self.assertEqual(result, "line1\nline2\n")

    def test_apply_unified_diff_multi_hunk(self):
        original = "line1\nline2\nline3\nline4\nline5\n"
        diff = ("@@ -2,1 +2,1 @@\n-line2\n+line2mod\n"
                "@@ -4,1 +4,1 @@\n-line4\n+line4mod\n")
        result = templates._apply_unified_diff(original, diff)
        self.assertEqual(result, "line1\nline2mod\nline3\nline4mod\nline5\n")

    def test_apply_unified_diff_zero_count_insert_after_last_line(self):
        # a pure-insertion hunk ("-N,0") means "insert after old line N", not
        # "insert before old line N" -- verified against GNU patch's actual
        # behavior for this hunk shape (an off-by-one here would silently
        # misplace every append-only diff, e.g. adding a new CLAUDE.md section).
        original = "line1\nline2\nline3\nline4\nline5\n"
        result = templates._apply_unified_diff(original, "@@ -5,0 +6,1 @@\n+NEWLINE\n")
        self.assertEqual(result, "line1\nline2\nline3\nline4\nline5\nNEWLINE\n")

    def test_apply_unified_diff_zero_count_insert_mid_file(self):
        original = "line1\nline2\nline3\nline4\nline5\n"
        result = templates._apply_unified_diff(original, "@@ -2,0 +3,1 @@\n+INSERTED\n")
        self.assertEqual(result, "line1\nline2\nINSERTED\nline3\nline4\nline5\n")

    def test_apply_unified_diff_context_mismatch_raises(self):
        with self.assertRaises(ValueError):
            templates._apply_unified_diff("line1\nline2\n", "@@ -1,1 +1,1 @@\n-WRONG\n+x\n")

    def test_diff_render_to_real_claude_md(self):
        import os as _os
        claude_md = self.root / "CLAUDE.md"
        claude_md.write_text("# Notes\nold line\n")
        edits = templates.render({"type": "diff",
                                  "payload": {"file": str(claude_md),
                                              "diff": "@@ -2,1 +2,1 @@\n-old line\n+new line\n"}},
                                 self.root)
        self.assertEqual(str(edits[0][0]), _os.path.realpath(str(claude_md)))
        self.assertEqual(edits[0][1], "# Notes\nnew line\n")

    def test_diff_render_through_claude_md_symlink_writes_real_file(self):
        real = self.root / "real-claude"
        real.mkdir()
        real_md = real / "CLAUDE.md"
        real_md.write_text("# Notes\nold line\n")
        link = self.root / "CLAUDE.md"
        link.symlink_to(real_md)
        edits = templates.render({"type": "diff",
                                  "payload": {"file": str(link),
                                              "diff": "@@ -2,1 +2,1 @@\n-old line\n+new line\n"}},
                                 self.root)
        import os as _os
        self.assertEqual(str(edits[0][0]), _os.path.realpath(str(link)))
        self.assertEqual(edits[0][1], "# Notes\nnew line\n")

    def test_diff_render_claude_md_symlink_to_other_file_rejected(self):
        other = self.root / "not-claude.md"
        other.write_text("something\n")
        link = self.root / "CLAUDE.md"
        link.symlink_to(other)
        with self.assertRaises(ValueError):
            templates.render({"type": "diff",
                              "payload": {"file": str(link),
                                          "diff": "@@ -1,1 +1,1 @@\n-something\n+else\n"}},
                             self.root)

    def test_diff_render_to_sanctioned_skill_file(self):
        (self.root / "skills").mkdir()
        skill = self.root / "skills" / "SKILL.md"
        skill.write_text("---\nname: s\n---\nold\n")
        edits = templates.render({"type": "diff",
                                  "payload": {"file": str(skill),
                                              "diff": "@@ -4,1 +4,1 @@\n-old\n+new\n"}},
                                 self.root)
        self.assertEqual(edits[0][1], "---\nname: s\n---\nnew\n")

    def test_diff_render_arbitrary_path_rejected(self):
        with self.assertRaises(ValueError):
            templates.render({"type": "diff",
                              "payload": {"file": "/tmp/x/notes.txt",
                                          "diff": "@@ -1,1 +1,1 @@\n-a\n+b\n"}},
                             self.root)
        with self.assertRaises(ValueError):
            templates.render({"type": "diff",
                              "payload": {"file": "~/.zshrc",
                                          "diff": "@@ -1,1 +1,1 @@\n-a\n+b\n"}},
                             self.root)

    def test_diff_render_context_mismatch_raises_valueerror(self):
        (self.root / "skills").mkdir()
        skill = self.root / "skills" / "SKILL.md"
        skill.write_text("---\nname: s\n---\nold\n")
        with self.assertRaises(ValueError):
            templates.render({"type": "diff",
                              "payload": {"file": str(skill),
                                          "diff": "@@ -4,1 +4,1 @@\n-WRONG\n+new\n"}},
                             self.root)

    def test_diff_render_extra_root_existing_file_rejected(self):
        with tempfile.TemporaryDirectory() as mem:
            existing = pathlib.Path(mem) / "MEMORY.md"
            existing.write_text("old\n")
            with self.assertRaises(ValueError):   # memory create-only: no rewriting existing notes
                templates.render({"type": "diff",
                                  "payload": {"file": str(existing),
                                              "diff": "@@ -1,1 +1,1 @@\n-old\n+new\n"}},
                                 self.root, extra_roots=[mem])

    def test_diff_render_extra_root_new_file_allowed(self):
        with tempfile.TemporaryDirectory() as mem:
            new_path = pathlib.Path(mem) / "new-note.md"
            edits = templates.render({"type": "diff",
                                      "payload": {"file": str(new_path),
                                                  "diff": "@@ -0,0 +1,1 @@\n+hello\n"}},
                                     self.root, extra_roots=[mem])
            self.assertEqual(edits[0][1], "hello\n")

    def test_diff_render_extra_root_claude_md_existing_rejected(self):
        # naming an existing memory-root note "CLAUDE.md" must not bypass the
        # memory create-only rule: extra-root containment is checked BEFORE the
        # CLAUDE.md carve-out, not after.
        with tempfile.TemporaryDirectory() as mem:
            existing = pathlib.Path(mem) / "CLAUDE.md"
            existing.write_text("old\n")
            with self.assertRaises(ValueError):
                templates.render({"type": "diff",
                                  "payload": {"file": str(existing),
                                              "diff": "@@ -1,1 +1,1 @@\n-old\n+new\n"}},
                                 self.root, extra_roots=[mem])

    def test_apply_unified_diff_malformed_hunk_header_raises_not_silently_skipped(self):
        # a hunk header missing its trailing space fails the strict regex; it
        # must not be silently skipped (dropping it and its now-orphaned body
        # lines) while an earlier hunk still applies -- that would silently
        # write a partially-patched file with no error.
        original = "line1\nline2\nline3\nline4\nline5\n"
        diff = "@@ -2,1 +2,1 @@\n-line2\n+line2mod\n@@ -4,1 +4,1@@\n-line4\n+line4mod\n"
        with self.assertRaises(ValueError):
            templates._apply_unified_diff(original, diff)

    def test_apply_unified_diff_tolerates_file_header_preamble(self):
        original = "line1\nline2\n"
        diff = "--- a/file\n+++ b/file\n@@ -2,1 +2,1 @@\n-line2\n+line2mod\n"
        result = templates._apply_unified_diff(original, diff)
        self.assertEqual(result, "line1\nline2mod\n")

    def test_smoke_check_claude_md_no_frontmatter_required(self):
        claude_md = self.root / "CLAUDE.md"
        claude_md.write_text("# just prose, no frontmatter\n")
        errs = templates.smoke_check([claude_md], self.root)
        self.assertEqual(errs, [])

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
