import json, os, pathlib, sys, tempfile, unittest
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "scripts"))
import collect


def _line(**kw):
    return json.dumps(kw) + "\n"


def _assistant(uuid, blocks, ts="2026-06-20T10:00:00Z"):
    return _line(type="assistant", uuid=uuid, timestamp=ts,
                 message={"role": "assistant", "model": "m",
                          "usage": {"output_tokens": 1}, "content": blocks})


def _user_text(uuid, text, ts="2026-06-20T10:01:00Z", parent=None):
    return _line(type="user", uuid=uuid, timestamp=ts, parentUuid=parent,
                 message={"role": "user", "content": text})


def parse(content: str):
    with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False) as f:
        f.write(content)
        path = f.name
    counters = {"skipped_lines": 0}
    s = collect.parse_session(pathlib.Path(path), "p",
                              collect.DEFAULT_CORRECTION_RE, counters)
    os.unlink(path)
    return s


class TestFrictionSignals(unittest.TestCase):
    def test_revert_after_edit_counts(self):
        content = (
            _assistant("a1", [{"type": "tool_use", "id": "t1", "name": "Edit",
                               "input": {"file_path": "/x.py"}}])
            + _assistant("a2", [{"type": "tool_use", "id": "t2", "name": "Bash",
                                 "input": {"command": "git revert HEAD"}}])
        )
        self.assertEqual(parse(content)["revert_events"], 1)

    def test_revert_without_prior_edit_does_not_count(self):
        content = _assistant("a1", [{"type": "tool_use", "id": "t1", "name": "Bash",
                                     "input": {"command": "git revert HEAD"}}])
        self.assertEqual(parse(content)["revert_events"], 0)

    def test_reask_similar_user_messages(self):
        content = (
            _user_text("u1", "please refactor the parser module to use dataclasses")
            + _user_text("u2", "totally unrelated question about deployment")
            + _user_text("u3", "please refactor the parser module to use dataclasses now")
        )
        s = parse(content)
        self.assertEqual(s["reasks"], 1)

    def test_ended_on_correction_flag(self):
        content = (_user_text("u1", "build the thing")
                   + _user_text("u2", "no, that's wrong, stop"))
        self.assertEqual(parse(content)["ended_on_correction"], 1)
        content2 = (_user_text("u1", "no, that's wrong")
                    + _user_text("u2", "great, thanks, looks good to me"))
        self.assertEqual(parse(content2)["ended_on_correction"], 0)

    def test_stall_records_tool_and_scrubbed_detail(self):
        content = (
            _assistant("a1", [{"type": "tool_use", "id": "t9", "name": "Bash",
                               "input": {"command": "gh issue list --repo x/y"}}])
            + _line(type="user", uuid="u1", timestamp="2026-06-20T10:02:00Z",
                    toolUseResult="The user declined to run this command",
                    message={"role": "user", "content": [
                        {"type": "tool_result", "tool_use_id": "t9",
                         "content": "declined"}]})
        )
        s = parse(content)
        self.assertEqual(s["permission_stalls"], 1)
        self.assertEqual(s["_stall_details"][0]["tool"], "Bash")
        self.assertIn("gh issue list", s["_stall_details"][0]["detail"])

    def test_corpus_aggregates_stall_tools(self):
        with tempfile.TemporaryDirectory() as d:
            proj = pathlib.Path(d) / "projects" / "p1"
            proj.mkdir(parents=True)
            (proj / "s1.jsonl").write_text(
                _assistant("a1", [{"type": "tool_use", "id": "t9", "name": "Bash",
                                   "input": {"command": "gh pr view"}}])
                + _line(type="user", uuid="u1", timestamp="2026-06-20T10:02:00Z",
                        toolUseResult="permission to run was denied",
                        message={"role": "user", "content": [
                            {"type": "tool_result", "tool_use_id": "t9",
                             "content": "denied"}]}))
            pack = collect.collect_corpus(pathlib.Path(d), None, ["*"], [],
                                          collect.DEFAULT_CORRECTION_RE,
                                          {"excerpts": 5, "tokens_per_excerpt": 100,
                                           "total_tokens": 1000})
            waste = pack["usage"]["waste"]
            self.assertEqual(waste["top_stalled_tools"], [("Bash", 1)])
            self.assertEqual(waste["stall_examples"][0]["tool"], "Bash")
            self.assertIn("revert_events_total", waste)
            self.assertIn("reasks_total", waste)
            self.assertIn("ended_on_correction_total", waste)


if __name__ == "__main__":
    unittest.main()
