"""Bisection localization tests. The fixture transcript is synthetic, built here —
no real content, usernames, or paths."""
import json, math, pathlib, sys, tempfile, unittest
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "scripts"))
import gym
import localize
import so_config


def _line(**kw):
    return json.dumps(kw) + "\n"


def _user(uuid, text, ts="2026-06-20T10:00:00Z"):
    return _line(type="user", uuid=uuid, timestamp=ts, message={"role": "user", "content": text})


def _assistant(uuid, text, ts="2026-06-20T10:00:01Z"):
    return _line(type="assistant", uuid=uuid, timestamp=ts,
                 message={"role": "assistant", "content": [{"type": "text", "text": text}]})


# 8 assistant turns: 1-4 on track, 5-8 planted derail (marked DERAILED). The true
# derail turn is index 4 (0-based) / turn 5 (1-based).
def build_fixture_transcript() -> str:
    out = []
    for i in range(1, 5):
        out.append(_user(f"u{i}", f"please do step {i} of the task"))
        out.append(_assistant(f"a{i}", f"did step {i} correctly"))
    for i in range(5, 9):
        out.append(_user(f"u{i}", f"please do step {i} of the task"))
        out.append(_assistant(f"a{i}", f"DERAILED: went off in the wrong direction at step {i}"))
    return "".join(out)


# Scripted stub judge: reads the turn under review out of the prompt and reports
# on_track False iff it contains the planted DERAILED marker. Deterministic, no
# network, no vendor.
STUB_BISECT_JUDGE = '''\
import json, sys
prompt = sys.stdin.read()
_, _, rest = prompt.partition("--- TURN UNDER REVIEW ---")
block, _, _ = rest.partition("--- END TURN UNDER REVIEW ---")
on_track = "DERAILED" not in block
sys.stdout.write(json.dumps({"on_track": on_track, "rationale": "checked for derail marker"}))
'''

def session_row(sid, project="p", **friction):
    row = {"id": sid, "project": project}
    row.update(friction)
    return row


class TestFrictionRanking(unittest.TestCase):
    def test_only_sessions_above_threshold_and_capped_at_top_n(self):
        sessions = [
            session_row("quiet", corrections_count=0),
            session_row("mild", corrections_count=1),
            session_row("loud", corrections_count=3, permission_stalls=2),
            session_row("loudest", corrections_count=5, reasks=1),
        ]
        top = localize.top_friction_sessions(sessions, top_n=2)
        self.assertEqual([s["id"] for s in top], ["loudest", "loud"])

    def test_zero_friction_everywhere_yields_nothing(self):
        sessions = [session_row("a"), session_row("b", duplicate_reads=99)]
        self.assertEqual(localize.top_friction_sessions(sessions, top_n=5), [])


class TestLoadTurns(unittest.TestCase):
    def test_parses_assistant_turns_in_order(self):
        with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False) as f:
            f.write(build_fixture_transcript())
            path = pathlib.Path(f.name)
        turns = localize.load_turns(path)
        self.assertEqual(len(turns), 8)
        self.assertNotIn("DERAILED", turns[3]["assistant"])
        self.assertIn("DERAILED", turns[4]["assistant"])


class TestBisection(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        self.stub = self.tmp / "stub_judge.py"
        self.stub.write_text(STUB_BISECT_JUDGE)
        self.judge = {"command": [sys.executable, str(self.stub)], "timeout_s": 30}
        with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False) as f:
            f.write(build_fixture_transcript())
            self.transcript = pathlib.Path(f.name)

    def test_brackets_planted_derail_within_tolerance_and_call_cap(self):
        turns = localize.load_turns(self.transcript)
        row, spent = localize.bisect_session("s1", turns, self.judge)
        self.assertIsNotNone(row)
        self.assertLessEqual(abs(row["bracket"][0] - 4), 2)
        self.assertLessEqual(abs(row["bracket"][1] - 4), 2)
        self.assertEqual(row["calls"], math.ceil(math.log2(8)))
        self.assertIn("derail marker", row["rationale"])
        self.assertGreater(spent, 0)

    def test_single_turn_session_is_not_bisected(self):
        row, spent = localize.bisect_session("s1", [{"user": "x", "assistant": "y"}], self.judge)
        self.assertIsNone(row)
        self.assertEqual(spent, 0)

    def test_budget_exhaustion_skips_before_any_call(self):
        turns = localize.load_turns(self.transcript)
        row, spent = localize.bisect_session("s1", turns, self.judge, budget=1)
        self.assertIsNone(row)


class TestRunPipeline(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        self.data_root = self.tmp / "claude"
        self.state = self.data_root / "self-optimize"
        self.cfg = so_config.load_config(self.state)
        self.ev = self.tmp / "ev"
        self.ev.mkdir()
        proj = self.data_root / "projects" / "p"
        proj.mkdir(parents=True)
        (proj / "s1.jsonl").write_text(build_fixture_transcript())
        self.stub = self.tmp / "stub_judge.py"
        self.stub.write_text(STUB_BISECT_JUDGE)

    def write_sessions(self, rows):
        (self.ev / "sessions.json").write_text(json.dumps({"sessions": rows}))

    def test_disabled_by_default_makes_zero_calls_without_reading_sessions(self):
        # no sessions.json on disk at all -- if this were read, it'd raise
        result = localize.run(self.ev, self.data_root, self.cfg)
        self.assertEqual(result, {})

    def test_enabled_but_no_friction_makes_zero_judge_calls(self):
        self.write_sessions([session_row("s1", corrections_count=0)])
        cfg = json.loads(json.dumps(self.cfg))
        cfg["deep_localize"] = {"enabled": True, "top_n": 3}
        cfg["gym"]["judge"] = {"command": [sys.executable, str(self.tmp / "no_such_file.py")]}
        result = localize.run(self.ev, self.data_root, cfg)
        self.assertEqual(result, {})

    def test_enabled_with_friction_brackets_the_session(self):
        self.write_sessions([session_row("s1", corrections_count=4)])
        cfg = json.loads(json.dumps(self.cfg))
        cfg["deep_localize"] = {"enabled": True, "top_n": 3}
        cfg["gym"]["judge"] = {"command": [sys.executable, str(self.stub)], "timeout_s": 30}
        result = localize.run(self.ev, self.data_root, cfg)
        self.assertIn("s1", result)
        self.assertLessEqual(abs(result["s1"]["bracket"][0] - 4), 2)
        self.assertEqual(result["s1"]["friction_score"], 4)

    def test_friction_but_no_judge_configured_raises(self):
        self.write_sessions([session_row("s1", corrections_count=4)])
        cfg = json.loads(json.dumps(self.cfg))
        cfg["deep_localize"] = {"enabled": True, "top_n": 3}
        with self.assertRaises(gym.JudgeNotConfigured):
            localize.run(self.ev, self.data_root, cfg)

    def test_cli_exits_2_without_judge_configured(self):
        import contextlib, io
        self.write_sessions([session_row("s1", corrections_count=4)])
        cfgfile = self.state / "config.json"
        on_disk = json.loads(cfgfile.read_text())
        on_disk["deep_localize"] = {"enabled": True, "top_n": 3}
        cfgfile.write_text(json.dumps(on_disk))
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code = localize.main(["--evidence", str(self.ev), "--data-root", str(self.data_root),
                                  "--state", str(self.state), "--out", str(self.tmp / "loc.json")])
        self.assertEqual(code, 2)
        self.assertIn("gym.judge.command", err.getvalue())


if __name__ == "__main__":
    unittest.main()
