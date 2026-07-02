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


if __name__ == "__main__":
    unittest.main()
