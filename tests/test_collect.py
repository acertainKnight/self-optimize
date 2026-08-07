import json, sys, pathlib, unittest
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

    def test_correction_records_prior_assistant_model(self):
        self.assertEqual(self.s["corrections"][0]["model"], "claude-fable-5")

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

    def _session_lines(self, assistant_text, user_text):
        return [
            json.dumps({"type": "user", "uuid": "u1", "timestamp": "2026-06-20T10:00:00Z",
                        "message": {"role": "user",
                                    "content": "the login button is broken, please fix it"}}),
            json.dumps({"type": "assistant", "uuid": "a1", "parentUuid": "u1",
                        "timestamp": "2026-06-20T10:00:05Z",
                        "message": {"role": "assistant", "model": "m",
                                    "usage": {"input_tokens": 5, "output_tokens": 5},
                                    "content": [{"type": "text", "text": assistant_text}]}}),
            json.dumps({"type": "user", "uuid": "u2", "parentUuid": "a1",
                        "timestamp": "2026-06-20T10:00:10Z",
                        "message": {"role": "user", "content": user_text}}),
        ]

    def _parse(self, assistant_text, user_text):
        import os, tempfile
        with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False) as f:
            f.write("\n".join(self._session_lines(assistant_text, user_text)) + "\n")
            path = f.name
        counters = {"skipped_lines": 0}
        s = collect.parse_session(pathlib.Path(path), "p", collect.DEFAULT_CORRECTION_RE, counters)
        os.unlink(path)
        return s

    def test_claim_then_redo_pair_flagged_deterministically(self):
        s = self._parse("Fixed! The tests should pass now.",
                        "still failing, same error as before")
        self.assertEqual(s["silent_failure_candidates"], 1)
        self.assertEqual(len(s["corrections"]), 1)
        c = s["corrections"][0]
        self.assertTrue(c["silent_failure_candidate"])
        self.assertEqual(c["user_text"], "still failing, same error as before")
        self.assertIn("Fixed!", c["prior_assistant_text"])
        # deterministic: same input, same flag, every time
        s2 = self._parse("Fixed! The tests should pass now.",
                         "still failing, same error as before")
        self.assertEqual(s2["silent_failure_candidates"], 1)

    def test_benign_thanks_after_completion_claim_does_not_flag(self):
        s = self._parse("Fixed! The tests should pass now.", "thanks, that's perfect")
        self.assertEqual(s["silent_failure_candidates"], 0)
        self.assertEqual(len(s["corrections"]), 0)

    def test_new_topic_after_completion_claim_does_not_flag(self):
        s = self._parse("Fixed! The tests should pass now.",
                        "can you also add a README section for this?")
        self.assertEqual(s["silent_failure_candidates"], 0)
        self.assertEqual(len(s["corrections"]), 0)

    def test_redo_language_without_a_prior_completion_claim_does_not_flag(self):
        # the redo wording alone isn't signal — it needs the prior assistant claim too
        s = self._parse("Reading the file first.", "still failing, same error as before")
        self.assertEqual(s["silent_failure_candidates"], 0)

    def test_namespaced_skill_and_agent_activations_normalize_to_bare(self):
        import os, tempfile
        line = json.dumps({
            "type": "assistant", "uuid": "a1", "timestamp": "2026-06-20T10:00:00Z",
            "attributionSkill": "remember:remember",
            "message": {"role": "assistant", "model": "m", "usage": {"output_tokens": 1},
                        "content": [
                            {"type": "tool_use", "id": "t1", "name": "Skill",
                             "input": {"skill": "superpowers:writing-plans"}},
                            {"type": "tool_use", "id": "t2", "name": "Agent",
                             "input": {"subagent_type": "pr-review-toolkit:code-reviewer"}}]}})
        with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False) as f:
            f.write(line + "\n")
            path = f.name
        counters = {"skipped_lines": 0}
        s = collect.parse_session(pathlib.Path(path), "p", collect.DEFAULT_CORRECTION_RE, counters)
        os.unlink(path)
        self.assertEqual(s["activation"]["skill:writing-plans"], 1)
        self.assertEqual(s["activation"]["skill:remember"], 1)
        self.assertEqual(s["activation"]["agent:code-reviewer"], 1)


if __name__ == "__main__":
    unittest.main()
