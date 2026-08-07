import json, pathlib, sys, tempfile, unittest
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "scripts"))
import collect

CAPS = {"excerpts": 40, "tokens_per_excerpt": 1500, "total_tokens": 60000}


def _line(**kw):
    return json.dumps(kw) + "\n"


def _assistant(uuid, ts, blocks):
    return _line(type="assistant", uuid=uuid, timestamp=ts,
                 message={"role": "assistant", "model": "m",
                          "usage": {"output_tokens": 1}, "content": blocks})


def _user_text(uuid, ts, text, parent=None):
    return _line(type="user", uuid=uuid, timestamp=ts, parentUuid=parent,
                 message={"role": "user", "content": text})


class TestReadCaptureQueue(unittest.TestCase):
    def test_missing_file_returns_empty(self):
        entries, malformed = collect.read_capture_queue(pathlib.Path("/nope/capture-queue.jsonl"))
        self.assertEqual(entries, [])
        self.assertEqual(malformed, 0)

    def test_skips_malformed_with_count(self):
        with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False) as f:
            f.write("not json at all {\n")                                # bad JSON
            f.write(json.dumps([1, 2, 3]) + "\n")                          # valid JSON, not an object
            f.write(json.dumps({"trigger": "correction"}) + "\n")          # object, no session_id
            f.write(json.dumps({"ts": "x", "harness": "claude-code",
                                "session_id": "S1", "trigger": "correction"}) + "\n")  # good
            f.write("\n")                                                  # blank line, ignored
            path = pathlib.Path(f.name)
        entries, malformed = collect.read_capture_queue(path)
        path.unlink()
        self.assertEqual(malformed, 3)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["session_id"], "S1")

    def test_flagged_session_ids_excludes_consumed(self):
        entries = [{"session_id": "A"}, {"session_id": "B", "consumed": True},
                  {"session_id": "A"}]  # dup id collapses via set
        self.assertEqual(collect.flagged_session_ids(entries), {"A"})


class TestMarkQueueConsumed(unittest.TestCase):
    def test_marks_matching_leaves_others_and_malformed_untouched(self):
        with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False) as f:
            f.write(json.dumps({"ts": "1", "harness": "claude-code",
                                "session_id": "A", "trigger": "correction"}) + "\n")
            f.write(json.dumps({"ts": "2", "harness": "claude-code",
                                "session_id": "B", "trigger": "task_completed"}) + "\n")
            f.write("this is not json\n")
            path = pathlib.Path(f.name)
        before_lines = path.read_text().splitlines()
        collect.mark_queue_consumed(path, {"A"})
        after_lines = path.read_text().splitlines()
        path.unlink()
        self.assertEqual(len(after_lines), len(before_lines))   # nothing deleted
        a, b, garbage = (json.loads(after_lines[0]), json.loads(after_lines[1]), after_lines[2])
        self.assertTrue(a["consumed"])
        self.assertNotIn("consumed", b)
        self.assertEqual(garbage, "this is not json")           # malformed line survives verbatim

    def test_noop_on_missing_file_or_no_ids(self):
        missing = pathlib.Path("/nope/capture-queue.jsonl")
        collect.mark_queue_consumed(missing, {"A"})  # must not raise
        self.assertFalse(missing.exists())
        with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False) as f:
            f.write(json.dumps({"session_id": "A"}) + "\n")
            path = pathlib.Path(f.name)
        original = path.read_text()
        collect.mark_queue_consumed(path, set())
        self.assertEqual(path.read_text(), original)
        path.unlink()


class TestCapSamplesPriority(unittest.TestCase):
    def test_flagged_session_sampled_ahead_of_more_recent_unflagged(self):
        sessions = [{"project": "p"}]
        samples = [
            {"session": "s-old", "project": "p", "ts": "2026-06-01T00:00:00Z",
             "user_text": "a", "prior_assistant_text": ""},
            {"session": "s-new", "project": "p", "ts": "2026-06-05T00:00:00Z",
             "user_text": "b", "prior_assistant_text": ""},
        ]
        caps = {"excerpts": 1, "tokens_per_excerpt": 100, "total_tokens": 1000}
        baseline = collect._cap_samples(samples, sessions, caps)
        self.assertEqual(baseline[0]["session"], "s-new")       # recency wins with no flags
        flagged = collect._cap_samples(samples, sessions, caps, flagged={"s-old"})
        self.assertEqual(flagged[0]["session"], "s-old")        # flag overrides recency

    def test_no_flags_matches_pre_capture_queue_behavior(self):
        sessions = [{"project": "p"}]
        samples = [
            {"session": "s1", "project": "p", "ts": "2026-06-01T00:00:00Z",
             "user_text": "a", "prior_assistant_text": ""},
            {"session": "s2", "project": "p", "ts": "2026-06-05T00:00:00Z",
             "user_text": "b", "prior_assistant_text": ""},
        ]
        out_default = collect._cap_samples(samples, sessions, CAPS)
        out_empty_flag = collect._cap_samples(samples, sessions, CAPS, flagged=frozenset())
        self.assertEqual(out_default, out_empty_flag)


class TestCollectCorpusEndToEnd(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _make_session(self, name, correction_ts):
        root = pathlib.Path(self.tmp) / "data"
        proj = root / "projects" / "-proj"
        proj.mkdir(parents=True, exist_ok=True)
        content = (
            _user_text("u1", "2026-06-01T09:00:00Z", "please refactor the parser")
            + _assistant("a1", "2026-06-01T09:00:05Z",
                         [{"type": "text", "text": "on it"}])
            + _user_text("u2", correction_ts, "no, that's wrong, do the lexer instead", parent="a1")
        )
        (proj / f"{name}.jsonl").write_text(content)
        return name

    def test_queue_steers_which_session_gets_sampled(self):
        older = self._make_session("older", "2026-06-01T09:01:00Z")
        newer = self._make_session("newer", "2026-06-01T09:05:00Z")
        caps = {"excerpts": 1, "tokens_per_excerpt": 100, "total_tokens": 1000}
        root = pathlib.Path(self.tmp) / "data"

        # unflagged baseline: the more recent correction (newer) wins the single slot
        baseline = collect.collect_corpus(root, None, ["*"], [], collect.DEFAULT_CORRECTION_RE, caps)
        self.assertEqual(len(baseline["samples"]), 1)
        self.assertEqual(baseline["samples"][0]["session"], newer)

        # a synthetic capture-queue flagging the OLDER session must steer sampling to it
        state = pathlib.Path(self.tmp) / "state"
        state.mkdir()
        qpath = state / "capture-queue.jsonl"
        qpath.write_text(json.dumps({"ts": "2026-06-01T09:01:30Z", "harness": "claude-code",
                                     "session_id": older, "trigger": "correction",
                                     "artifact_hint": "no"}) + "\n")
        entries, malformed = collect.read_capture_queue(qpath)
        self.assertEqual(malformed, 0)
        flagged = collect.flagged_session_ids(entries)
        self.assertEqual(flagged, {older})
        steered = collect.collect_corpus(root, None, ["*"], [], collect.DEFAULT_CORRECTION_RE,
                                         caps, flagged)
        self.assertEqual(len(steered["samples"]), 1)
        self.assertEqual(steered["samples"][0]["session"], older)

        # after collect consumes the queue, the entry is marked, not removed
        collect.mark_queue_consumed(qpath, flagged)
        remaining = qpath.read_text().splitlines()
        self.assertEqual(len(remaining), 1)
        self.assertTrue(json.loads(remaining[0])["consumed"])


if __name__ == "__main__":
    unittest.main()
