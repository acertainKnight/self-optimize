import io, json, pathlib, subprocess, sys, tempfile, unittest
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "hooks"))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "scripts"))
import capture_trigger as ct


def _assistant(uuid, blocks, side=False):
    d = {"type": "assistant", "uuid": uuid, "timestamp": "2026-06-20T10:00:00Z",
         "message": {"role": "assistant", "model": "m", "usage": {"output_tokens": 1},
                     "content": blocks}}
    if side:
        d["isSidechain"] = True
    return d


def _tool_use(i, name="Read"):
    return {"type": "tool_use", "id": f"t{i}", "name": name, "input": {}}


def _user_text(uuid, text):
    return {"type": "user", "uuid": uuid, "timestamp": "2026-06-20T10:00:00Z",
            "message": {"role": "user", "content": text}}


def _tool_result(uuid, result):
    return {"type": "user", "uuid": uuid, "timestamp": "2026-06-20T10:00:00Z",
            "toolUseResult": result,
            "message": {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "t1", "content": "x"}]}}


class TestDetectTriggers(unittest.TestCase):
    def test_correction_detected_with_keyword_hint(self):
        records = [_user_text("u1", "no, that's wrong, use the lexer")]
        triggers = ct.detect_triggers(records)
        self.assertEqual(len(triggers), 1)
        self.assertEqual(triggers[0][0], "correction")
        self.assertTrue(triggers[0][1])  # a short keyword hint, not the sentence

    def test_no_correction_on_plain_message(self):
        records = [_user_text("u1", "please add tests for this")]
        self.assertEqual(ct.detect_triggers(records), [])

    def test_task_completed_at_five_tool_calls(self):
        records = [_assistant("a1", [_tool_use(i) for i in range(5)])]
        triggers = ct.detect_triggers(records)
        self.assertIn(("task_completed", "5_tool_calls"), triggers)

    def test_no_task_completed_below_threshold(self):
        records = [_assistant("a1", [_tool_use(i) for i in range(4)])]
        self.assertNotIn("task_completed", [t for t, _ in ct.detect_triggers(records)])

    def test_dead_end_then_working_needs_recovery(self):
        records = [
            _assistant("a1", [_tool_use(1, "Bash")]),
            _tool_result("u1", {"error": "command failed"}),
            _assistant("a2", [_tool_use(2, "Bash")]),  # retried after the failure
        ]
        triggers = ct.detect_triggers(records)
        self.assertIn(("dead_end_then_working", None), triggers)

    def test_error_without_recovery_does_not_trigger(self):
        records = [
            _assistant("a1", [_tool_use(1, "Bash")]),
            _tool_result("u1", {"error": "command failed"}),
            # turn ends here — abandoned, not recovered
        ]
        self.assertNotIn("dead_end_then_working",
                         [t for t, _ in ct.detect_triggers(records)])

    def test_permission_decline_is_not_a_tool_error(self):
        records = [
            _assistant("a1", [_tool_use(1, "Bash")]),
            _tool_result("u1", "The user doesn't want to proceed with this tool use."),
            _assistant("a2", [_tool_use(2, "Bash")]),
        ]
        self.assertNotIn("dead_end_then_working",
                         [t for t, _ in ct.detect_triggers(records)])

    def test_sidechain_records_excluded(self):
        records = [_assistant("a1", [_tool_use(i) for i in range(5)], side=True)]
        self.assertEqual(ct.detect_triggers(records), [])


class TestMainIntegration(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.data_root = pathlib.Path(self.tmp) / "data"
        self.data_root.mkdir()
        self.state = self.data_root / "self-optimize"
        self.orig_env = dict(__import__("os").environ)
        __import__("os").environ["CLAUDE_CONFIG_DIR"] = str(self.data_root)
        self.transcript = pathlib.Path(self.tmp) / "session.jsonl"

    def tearDown(self):
        import os
        os.environ.clear()
        os.environ.update(self.orig_env)
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run_main(self, payload):
        stdin = io.StringIO(json.dumps(payload))
        real_stdin = sys.stdin
        sys.stdin = stdin
        try:
            ct.main()
        finally:
            sys.stdin = real_stdin

    def test_correction_turn_appends_one_queue_line(self):
        self.transcript.write_text(
            json.dumps(_user_text("u1", "no, that's wrong")) + "\n")
        self._run_main({"session_id": "S1", "transcript_path": str(self.transcript)})
        qpath = self.state / "capture-queue.jsonl"
        self.assertTrue(qpath.exists())
        lines = qpath.read_text().splitlines()
        self.assertEqual(len(lines), 1)
        entry = json.loads(lines[0])
        self.assertEqual(entry["session_id"], "S1")
        self.assertEqual(entry["trigger"], "correction")
        self.assertEqual(entry["harness"], "claude-code")
        self.assertNotIn("no, that's wrong", json.dumps(entry))  # no transcript content

    def test_incremental_offset_does_not_reprocess_same_bytes(self):
        self.transcript.write_text(
            json.dumps(_user_text("u1", "no, that's wrong")) + "\n")
        self._run_main({"session_id": "S2", "transcript_path": str(self.transcript)})
        # second call with no new bytes appended must not duplicate the entry
        self._run_main({"session_id": "S2", "transcript_path": str(self.transcript)})
        qpath = self.state / "capture-queue.jsonl"
        self.assertEqual(len(qpath.read_text().splitlines()), 1)

    def test_session_cap_stops_all_further_writes(self):
        cursor_path = self.state / "state" / "capture-cursor" / "S3.json"
        cursor_path.parent.mkdir(parents=True)
        cursor_path.write_text(json.dumps({"offset": 0, "emitted": ct.MAX_PER_SESSION}))
        self.transcript.write_text(
            json.dumps(_user_text("u1", "no, that's wrong")) + "\n")
        self._run_main({"session_id": "S3", "transcript_path": str(self.transcript)})
        qpath = self.state / "capture-queue.jsonl"
        self.assertFalse(qpath.exists())  # capped: never even opened the queue

    def test_missing_transcript_is_a_silent_noop(self):
        self._run_main({"session_id": "S4", "transcript_path": str(self.transcript)})  # never created
        self.assertFalse((self.state / "capture-queue.jsonl").exists())

class TestScriptNeverAffectsTheSession(unittest.TestCase):
    """Runs the real script as Claude Code would (subprocess, JSON on stdin) —
    exit 0 no matter what it receives is the actual acceptance bar, not just
    that the Python function doesn't raise."""
    SCRIPT = pathlib.Path(__file__).resolve().parents[1] / "hooks" / "capture_trigger.py"

    def test_malformed_stdin_exits_zero(self):
        result = subprocess.run([sys.executable, str(self.SCRIPT)], input="not json",
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0)

    def test_empty_stdin_exits_zero(self):
        result = subprocess.run([sys.executable, str(self.SCRIPT)], input="",
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0)


if __name__ == "__main__":
    unittest.main()
