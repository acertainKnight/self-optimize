import pathlib, sys, tempfile, unittest
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "scripts"))
import papercuts


class TestReadPapercuts(unittest.TestCase):
    def test_missing_file_is_silently_empty(self):
        self.assertEqual(papercuts.read_papercuts("/does/not/exist/papercuts.md"), [])

    def test_parses_lines_and_skips_malformed(self):
        with tempfile.TemporaryDirectory() as d:
            p = pathlib.Path(d) / "papercuts.md"
            p.write_text(
                "# Papercuts\n"
                "\n"
                "- 2026-08-01 claude-code the linter flags valid syntax, ignore and continue\n"
                "- 2026-08-02 codex retried a flaky network call once, then it worked\n"
                "not a papercut line at all\n"
                "- missing the date field entirely\n"
            )
            lines = papercuts.read_papercuts(p)
            self.assertEqual(len(lines), 2)
            self.assertEqual(lines[0]["date"], "2026-08-01")
            self.assertEqual(lines[0]["harness"], "claude-code")
            self.assertIn("linter flags valid syntax", lines[0]["text"])
            self.assertEqual(lines[1]["harness"], "codex")

    def test_ids_are_stable_and_distinct(self):
        with tempfile.TemporaryDirectory() as d:
            p = pathlib.Path(d) / "papercuts.md"
            p.write_text(
                "- 2026-08-01 claude-code first line\n"
                "- 2026-08-02 claude-code second line\n"
            )
            a = papercuts.read_papercuts(p)
            b = papercuts.read_papercuts(p)
            self.assertEqual(a, b)  # same file, same ids, every time
            self.assertNotEqual(a[0]["id"], a[1]["id"])

    def test_lines_after_archive_heading_are_not_live(self):
        with tempfile.TemporaryDirectory() as d:
            p = pathlib.Path(d) / "papercuts.md"
            p.write_text(
                "- 2026-08-05 claude-code still live\n"
                "\n"
                "## Archive\n"
                "\n"
                "- 2026-07-01 claude-code already archived\n"
            )
            lines = papercuts.read_papercuts(p)
            self.assertEqual(len(lines), 1)
            self.assertIn("still live", lines[0]["text"])

    def test_redacts_secret_looking_text(self):
        with tempfile.TemporaryDirectory() as d:
            p = pathlib.Path(d) / "papercuts.md"
            p.write_text("- 2026-08-01 claude-code leaked sk-ant-abcdefgh12345678\n")
            lines = papercuts.read_papercuts(p)
            self.assertNotIn("sk-ant-abcdefgh12345678", lines[0]["text"])
            self.assertIn("REDACTED", lines[0]["text"])


class TestArchiveLines(unittest.TestCase):
    def _write(self, d, text):
        p = pathlib.Path(d) / "papercuts.md"
        p.write_text(text)
        return p

    def test_missing_file_or_empty_ids_is_a_noop(self):
        with tempfile.TemporaryDirectory() as d:
            p = pathlib.Path(d) / "gone.md"
            self.assertEqual(papercuts.archive_lines(p, {"abc"}), 0)
            p2 = self._write(d, "- 2026-08-01 claude-code x\n")
            self.assertEqual(papercuts.archive_lines(p2, set()), 0)

    def test_archives_only_cited_lines_leaves_others_untouched(self):
        with tempfile.TemporaryDirectory() as d:
            p = self._write(d,
                "- 2026-08-01 claude-code fix this one\n"
                "- 2026-08-02 codex leave this one alone\n")
            lines = papercuts.read_papercuts(p)
            target = next(l for l in lines if "fix this one" in l["text"])
            n = papercuts.archive_lines(p, {target["id"]})
            self.assertEqual(n, 1)
            text = p.read_text()
            self.assertIn("## Archive", text)
            self.assertIn("2026-08-01 claude-code fix this one", text)
            self.assertIn("2026-08-02 codex leave this one alone", text)
            # the archived line now sits below the heading, the untouched one above it
            heading_idx = text.index("## Archive")
            self.assertLess(text.index("leave this one alone"), heading_idx)
            self.assertGreater(text.index("fix this one"), heading_idx)
            # re-reading live papercuts now only shows the untouched line
            remaining = papercuts.read_papercuts(p)
            self.assertEqual(len(remaining), 1)
            self.assertIn("leave this one alone", remaining[0]["text"])

    def test_appends_after_existing_archive_content(self):
        with tempfile.TemporaryDirectory() as d:
            p = self._write(d,
                "- 2026-08-03 claude-code newly fixed\n"
                "\n## Archive\n\n"
                "- 2026-07-01 claude-code already archived earlier\n")
            lines = papercuts.read_papercuts(p)
            n = papercuts.archive_lines(p, {lines[0]["id"]})
            self.assertEqual(n, 1)
            text = p.read_text()
            self.assertIn("already archived earlier", text)
            self.assertIn("newly fixed", text)
            # the prior archive entry is untouched and still precedes the new one
            self.assertLess(text.index("already archived earlier"), text.index("newly fixed"))

    def test_idempotent_second_call_moves_nothing(self):
        with tempfile.TemporaryDirectory() as d:
            p = self._write(d, "- 2026-08-01 claude-code fix this one\n")
            target_id = papercuts.read_papercuts(p)[0]["id"]
            self.assertEqual(papercuts.archive_lines(p, {target_id}), 1)
            self.assertEqual(papercuts.archive_lines(p, {target_id}), 0)

    def test_unmatched_id_moves_nothing(self):
        with tempfile.TemporaryDirectory() as d:
            p = self._write(d, "- 2026-08-01 claude-code fix this one\n")
            self.assertEqual(papercuts.archive_lines(p, {"nosuchid"}), 0)
            self.assertNotIn("## Archive", p.read_text())


if __name__ == "__main__":
    unittest.main()
