import sys, pathlib, unittest
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "scripts"))
import collect

FIX = pathlib.Path(__file__).parent / "fixtures" / "basic_session.jsonl"


class TestParseSession(unittest.TestCase):
    def setUp(self):
        self.counters = {"skipped_lines": 0}
        self.s = collect.parse_session(FIX, "projA", collect.DEFAULT_CORRECTION_RE, self.counters)

    def test_robustness_and_identity(self):
        self.assertEqual(self.counters["skipped_lines"], 1)      # malformed line skipped, no crash
        self.assertEqual(self.s["id"], "basic_session")
        self.assertEqual(self.s["harness_version"], "2.1.177")
        self.assertEqual(self.s["cwd"], "/tmp/projA")
        self.assertEqual(self.s["turns"], 9)                      # user+assistant records only

    def test_token_accounting_per_model_and_sidechain(self):
        self.assertEqual(self.s["output_tokens"], 515)            # 50+60+400+5
        self.assertEqual(self.s["sidechain_output_tokens"], 400)
        self.assertEqual(self.s["models"]["claude-haiku-4-5"]["output"], 400)
        self.assertEqual(self.s["cache_read"], 1000)

    def test_corrections_exclude_sidechain_and_scrub_secrets(self):
        self.assertEqual(len(self.s["corrections"]), 1)           # u3 yes; u4 (sidechain) no
        c = self.s["corrections"][0]
        self.assertNotIn("SECRETSECRET", c["user_text"])
        self.assertIn("Reading the file first.", c["prior_assistant_text"])  # via parentUuid chain

    def test_waste_and_activation(self):
        self.assertEqual(self.s["duplicate_reads"], 1)            # parser.py read twice
        self.assertEqual(self.s["permission_stalls"], 1)          # u5
        self.assertEqual(self.s["activation"]["skill:tdd"], 2)    # tool_use + attributionSkill
        self.assertIn("mcp_tool:mcp__plugin_linear_linear__get_issue", self.s["activation"])

    def test_valid_json_non_object_lines_are_skipped(self):
        import os, tempfile
        with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False) as f:
            f.write('"just a string"\n42\n[1, 2]\n')
            f.write('{"type": "user", "uuid": "u1", "sessionId": "S", '
                    '"timestamp": "2026-06-20T10:00:00Z", '
                    '"message": {"role": "user", "content": "hello"}}\n')
            path = f.name
        counters = {"skipped_lines": 0}
        s = collect.parse_session(pathlib.Path(path), "p", collect.DEFAULT_CORRECTION_RE, counters)
        os.unlink(path)
        self.assertEqual(counters["skipped_lines"], 3)
        self.assertEqual(s["turns"], 1)


if __name__ == "__main__":
    unittest.main()
