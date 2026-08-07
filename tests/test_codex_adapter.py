import json, pathlib, sqlite3, sys, tempfile, unittest
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "adapters" / "codex"))
import collect_codex

CONFIG_TOML = """
forced_login_method = "chatgpt"

[model_providers.litellm]
name = "router"
base_url = "http://localhost:4000/v1"
wire_api = "responses"

[profiles.litellm]
model_provider = "litellm"
model = "chatgpt/gpt-5.3-codex"

[mcp_servers.snowflake]
command = "bash"
args = ["start.sh"]

[mcp_servers.slack]
url = "https://mcp.slack.com/mcp"
auth = "oauth"
"""


def make_codex_home(root: pathlib.Path, threads=()):
    root.mkdir(parents=True)
    (root / "config.toml").write_text(CONFIG_TOML)
    (root / "AGENTS.md").write_text("# agents\nbe good\n")
    (root / "prompts").mkdir()
    (root / "prompts" / "handoff.md").write_text("write a handoff")
    sk = root / "skills" / "my-skill"
    sk.mkdir(parents=True)
    (sk / "SKILL.md").write_text("---\nname: my-skill\n---\nbody")
    conn = sqlite3.connect(root / "state_5.sqlite")
    conn.execute("CREATE TABLE threads (id TEXT, rollout_path TEXT, cwd TEXT,"
                 " model TEXT, model_provider TEXT, tokens_used INTEGER,"
                 " created_at_ms INTEGER, updated_at_ms INTEGER)")
    conn.executemany("INSERT INTO threads VALUES (?,?,?,?,?,?,?,?)", threads)
    conn.commit()
    conn.close()


THREADS = [
    ("t1", "/r/t1.jsonl", "/proj/a", "gpt-5.3-codex", "litellm", 1200,
     1752700000000, 1752700600000),
    ("t2", "/r/t2.jsonl", "/proj/b", "gpt-5.3-codex", "litellm", 300,
     1752800000000, 1752800300000),
]


def _rec(ts, type_, payload):
    return json.dumps({"timestamp": ts, "type": type_, "payload": payload})


def rollout_lines():
    """Synthetic rollout JSONL covering: session_meta, turn_context, a user turn,
    a tool call (function_call), an assistant turn, a token_count event, a
    correction (user turn matching the correction regex), a retry cluster (three
    identical tool calls), a git-revert tool call, an error event_msg, a
    malformed (non-JSON) line, and an unrecognized top-level record type."""
    lines = [
        _rec("2026-08-01T10:00:00Z", "session_meta",
             {"cli_version": "0.144.2", "cwd": "/proj/a", "session_id": "s1"}),
        _rec("2026-08-01T10:00:01Z", "turn_context",
             {"turn_id": "turn-1", "model": "gpt-5.3-codex", "cwd": "/proj/a"}),
        _rec("2026-08-01T10:00:02Z", "response_item",
             {"type": "message", "role": "user",
              "content": [{"type": "input_text",
                          "text": "please add retry logic to the fetch call"}]}),
        _rec("2026-08-01T10:00:03Z", "response_item",
             {"type": "function_call", "name": "shell", "arguments": "git status"}),
        _rec("2026-08-01T10:00:04Z", "response_item",
             {"type": "message", "role": "assistant",
              "content": [{"type": "output_text",
                          "text": "Added a simple retry loop around the fetch call."}]}),
        _rec("2026-08-01T10:00:05Z", "event_msg",
             {"type": "token_count",
              "info": {"last_token_usage": {"input_tokens": 500, "output_tokens": 120}}}),
        _rec("2026-08-01T10:00:06Z", "response_item",
             {"type": "message", "role": "user",
              "content": [{"type": "input_text",
                          "text": "no, that's wrong - it needs exponential backoff"}]}),
        _rec("2026-08-01T10:00:07Z", "response_item",
             {"type": "function_call", "name": "shell", "arguments": "git status"}),
        _rec("2026-08-01T10:00:08Z", "response_item",
             {"type": "function_call", "name": "shell", "arguments": "git status"}),
        _rec("2026-08-01T10:00:09Z", "response_item",
             {"type": "function_call", "name": "shell",
              "arguments": "git reset --hard HEAD~1"}),
        _rec("2026-08-01T10:00:10Z", "event_msg",
             {"type": "error", "message": "shell command exited 1"}),
        "not json {{{",
        _rec("2026-08-01T10:00:11Z", "some_future_record_type", {}),
    ]
    return "\n".join(lines) + "\n"


class TestCodexAdapter(unittest.TestCase):
    def test_sessions_from_threads_table(self):
        with tempfile.TemporaryDirectory() as d:
            home = pathlib.Path(d) / "codex"
            make_codex_home(home, THREADS)
            sessions = collect_codex.collect_sessions(home)
            self.assertEqual(len(sessions), 2)
            s = sessions[0]
            self.assertEqual(s["id"], "t1")
            self.assertEqual(s["project"], "/proj/a")
            self.assertEqual(s["output_tokens"], 1200)
            self.assertTrue(s["started_at"].startswith("2025") or
                            s["started_at"].startswith("2026"))

    def test_missing_or_empty_db_degrades_to_zero_sessions(self):
        with tempfile.TemporaryDirectory() as d:
            home = pathlib.Path(d) / "codex"
            make_codex_home(home)   # empty threads table
            self.assertEqual(collect_codex.collect_sessions(home), [])
            self.assertEqual(collect_codex.collect_sessions(pathlib.Path(d) / "nope"), [])

    def test_highest_numbered_state_db_wins(self):
        with tempfile.TemporaryDirectory() as d:
            home = pathlib.Path(d) / "codex"
            make_codex_home(home, THREADS)   # state_5 with rows
            conn = sqlite3.connect(home / "state_12.sqlite")
            conn.execute("CREATE TABLE threads (id TEXT, rollout_path TEXT, cwd TEXT,"
                         " model TEXT, model_provider TEXT, tokens_used INTEGER,"
                         " created_at_ms INTEGER, updated_at_ms INTEGER)")
            conn.commit()
            conn.close()
            self.assertEqual(collect_codex.collect_sessions(home), [])   # 12 > 5, empty

    def test_inventory_and_usage(self):
        with tempfile.TemporaryDirectory() as d:
            home = pathlib.Path(d) / "codex"
            make_codex_home(home, THREADS)
            inv = collect_codex.build_inventory(home)
            self.assertEqual({m["id"] for m in inv["mcp_servers"]},
                             {"mcp:snowflake", "mcp:slack"})
            self.assertEqual(inv["skills"][0]["id"], "skill:my-skill")
            self.assertEqual(inv["prompts"][0]["id"], "prompt:handoff")
            self.assertEqual(inv["settings"]["profiles"]["litellm"]["model"],
                             "chatgpt/gpt-5.3-codex")
            usage = collect_codex.build_usage(collect_codex.collect_sessions(home), None)
            self.assertEqual(usage["totals"]["sessions"], 2)
            self.assertEqual(usage["totals"]["output_tokens"], 1500)
            self.assertTrue(usage["parse"]["collector_limits"])

    def test_main_writes_stamped_600_files(self):
        with tempfile.TemporaryDirectory() as d:
            home = pathlib.Path(d) / "codex"
            ev = pathlib.Path(d) / "ev"
            make_codex_home(home, THREADS)
            collect_codex.main(["--codex-home", str(home), "--out", str(ev)])
            for name in ("sessions", "usage", "inventory", "samples"):
                f = ev / f"{name}.json"
                data = json.loads(f.read_text())
                self.assertEqual(data["harness"], "codex")
                self.assertEqual(f.stat().st_mode & 0o777, 0o600)


class TestParseRollout(unittest.TestCase):
    def _write(self, d, name="rollout.jsonl"):
        p = pathlib.Path(d) / name
        p.write_text(rollout_lines())
        return p

    def test_user_assistant_tool_and_correction_extraction(self):
        with tempfile.TemporaryDirectory() as d:
            parsed = collect_codex.parse_rollout(self._write(d))
            self.assertEqual(parsed["turns"], 3)   # 2 user + 1 assistant message
            self.assertEqual(parsed["cli_version"], "0.144.2")
            self.assertEqual(parsed["input_tokens"], 500)
            self.assertEqual(parsed["output_tokens"], 120)
            self.assertEqual(parsed["models"], {"gpt-5.3-codex": {"input": 500, "output": 120}})
            self.assertEqual(parsed["corrections_count"], 1)
            self.assertEqual(len(parsed["samples"]), 1)
            smp = parsed["samples"][0]
            self.assertIn("exponential backoff", smp["user_text"])
            self.assertIn("retry loop", smp["prior_assistant_text"])
            self.assertEqual(parsed["ended_on_correction"], 1)

    def test_retry_cluster_and_revert_detection(self):
        with tempfile.TemporaryDirectory() as d:
            parsed = collect_codex.parse_rollout(self._write(d))
            # "git status" called 3 times (c=3 > 2) -> repeated_calls = 3-2 = 1
            self.assertEqual(parsed["repeated_calls"], 1)
            self.assertEqual(parsed["revert_events"], 1)   # git reset --hard

    def test_malformed_and_unknown_lines_counted_not_fatal(self):
        with tempfile.TemporaryDirectory() as d:
            parsed = collect_codex.parse_rollout(self._write(d))
            # 1 non-JSON line + 1 unrecognized top-level record type
            self.assertEqual(parsed["skipped_lines"], 2)

    def test_missing_rollout_file_degrades_quietly(self):
        parsed = collect_codex.parse_rollout(pathlib.Path("/nope/does-not-exist.jsonl"))
        self.assertEqual(parsed["turns"], 0)
        self.assertEqual(parsed["samples"], [])
        self.assertEqual(parsed["skipped_lines"], 0)

    def test_error_event_does_not_crash_or_count_as_unknown(self):
        with tempfile.TemporaryDirectory() as d:
            parsed = collect_codex.parse_rollout(self._write(d))
            # the error event_msg record is a known top-level type (event_msg), so it
            # must not be counted among skipped_lines even though it isn't mined further
            self.assertEqual(parsed["skipped_lines"], 2)


class TestRolloutWiredIntoSessions(unittest.TestCase):
    def test_real_rollout_file_populates_session_and_samples(self):
        with tempfile.TemporaryDirectory() as d:
            home = pathlib.Path(d) / "codex"
            rollout_dir = pathlib.Path(d) / "rollouts"
            rollout_dir.mkdir()
            rollout_path = rollout_dir / "rollout-t1.jsonl"
            rollout_path.write_text(rollout_lines())
            threads = [("t1", str(rollout_path), "/proj/a", "gpt-5.3-codex", "litellm", 1200,
                       1752700000000, 1752700600000)]
            make_codex_home(home, threads)
            sessions = collect_codex.collect_sessions(home)
            self.assertEqual(len(sessions), 1)
            s = sessions[0]
            self.assertEqual(s["turns"], 3)
            self.assertEqual(s["input_tokens"], 500)
            self.assertEqual(s["output_tokens"], 120)
            self.assertEqual(s["corrections_count"], 1)
            self.assertEqual(s["repeated_calls"], 1)
            self.assertEqual(s["revert_events"], 1)
            self.assertEqual(s["harness_version"], "0.144.2")
            self.assertEqual(len(s["_samples"]), 1)

            usage = collect_codex.build_usage(sessions, None)
            self.assertEqual(usage["totals"]["input_tokens"], 500)
            self.assertEqual(usage["totals"]["output_tokens"], 120)
            self.assertEqual(usage["waste"]["repeated_calls_total"], 1)
            self.assertEqual(usage["waste"]["revert_events_total"], 1)
            self.assertEqual(usage["corrections"]["total"], 1)
            self.assertEqual(usage["parse"]["skipped_lines"], 2)

    def test_fallback_scan_finds_rollout_when_sqlite_path_is_stale(self):
        with tempfile.TemporaryDirectory() as d:
            home = pathlib.Path(d) / "codex"
            # sqlite points at a path that no longer exists...
            threads = [("019fake0-thread-id", "/gone/missing.jsonl", "/proj/a",
                       "gpt-5.3-codex", "litellm", 1200, 1752700000000, 1752700600000)]
            make_codex_home(home, threads)
            # ...but a rollout file carrying the thread id exists under sessions/
            sess_dir = home / "sessions" / "2026" / "08" / "01"
            sess_dir.mkdir(parents=True)
            (sess_dir / "rollout-2026-08-01T10-00-00-019fake0-thread-id.jsonl").write_text(
                rollout_lines())
            sessions = collect_codex.collect_sessions(home)
            self.assertEqual(sessions[0]["turns"], 3)
            self.assertEqual(sessions[0]["corrections_count"], 1)

    def test_main_writes_non_empty_samples_and_token_split(self):
        with tempfile.TemporaryDirectory() as d:
            home = pathlib.Path(d) / "codex"
            ev = pathlib.Path(d) / "ev"
            rollout_dir = pathlib.Path(d) / "rollouts"
            rollout_dir.mkdir()
            rollout_path = rollout_dir / "rollout-t1.jsonl"
            rollout_path.write_text(rollout_lines())
            threads = [("t1", str(rollout_path), "/proj/a", "gpt-5.3-codex", "litellm", 1200,
                       1752700000000, 1752700600000)]
            make_codex_home(home, threads)
            collect_codex.main(["--codex-home", str(home), "--out", str(ev)])
            samples = json.loads((ev / "samples.json").read_text())["samples"]
            self.assertEqual(len(samples), 1)
            self.assertEqual(samples[0]["session"], "t1")
            self.assertEqual(samples[0]["project"], "/proj/a")
            usage = json.loads((ev / "usage.json").read_text())
            self.assertEqual(usage["totals"]["input_tokens"], 500)
            self.assertEqual(usage["totals"]["output_tokens"], 120)
            sessions = json.loads((ev / "sessions.json").read_text())["sessions"]
            self.assertNotIn("_samples", sessions[0])
            self.assertNotIn("_skipped_lines", sessions[0])


if __name__ == "__main__":
    unittest.main()
