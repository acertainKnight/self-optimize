import json, pathlib, sqlite3, sys, tempfile, unittest
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "adapters" / "opencode"))
import collect_opencode

CONFIG_JSONC = """
{
  // inline comment before a real key
  "$schema": "https://opencode.ai/config.json",
  "model": "openrouter/some-model", /* block comment */
  "plugin": ["some-plugin@latest"],
  "mcp": {
    "snowflake": {"type": "local", "command": ["bash", "start.sh"], "enabled": true},
    "slack": {"type": "remote", "url": "https://mcp.example.com/mcp", "enabled": true},
  },
  "skills": {"paths": ["__SKILLS_ROOT__"]},
}
"""


def make_session_db(root: pathlib.Path, sessions=(), messages=(), parts=()):
    root.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(root / "opencode.db")
    conn.execute("CREATE TABLE session (id TEXT, directory TEXT, version TEXT, model TEXT,"
                 " time_created INTEGER, time_updated INTEGER, tokens_input INTEGER,"
                 " tokens_output INTEGER, tokens_cache_read INTEGER, tokens_cache_write INTEGER)")
    conn.execute("CREATE TABLE message (id TEXT, session_id TEXT, time_created INTEGER, data TEXT)")
    conn.execute("CREATE TABLE part (id TEXT, message_id TEXT, session_id TEXT, data TEXT)")
    conn.executemany("INSERT INTO session VALUES (?,?,?,?,?,?,?,?,?,?)", sessions)
    conn.executemany("INSERT INTO message VALUES (?,?,?,?)", messages)
    conn.executemany("INSERT INTO part VALUES (?,?,?,?)", parts)
    conn.commit()
    conn.close()


def make_config_root(root: pathlib.Path, skills_root: pathlib.Path):
    root.mkdir(parents=True, exist_ok=True)
    (root / "opencode.jsonc").write_text(CONFIG_JSONC.replace("__SKILLS_ROOT__", str(skills_root)))
    (root / "AGENTS.md").write_text("# agents\nbe good\n")
    (root / "agent").mkdir()
    (root / "agent" / "reviewer.md").write_text("---\nname: reviewer\n---\nbody")
    (root / "command").mkdir()
    (root / "command" / "handoff.md").write_text("write a handoff")
    sk = skills_root / "my-skill"
    sk.mkdir(parents=True)
    (sk / "SKILL.md").write_text("---\nname: my-skill\n---\nbody")
    # credential material that must never be opened by this collector
    (root / "auth.json").write_text('{"secret_do_not_read": "topsecret123"}')
    (root / "mcp-auth.json").write_text('{"token_do_not_read": "alsosecret456"}')


MODEL_A = json.dumps({"id": "x-ai/grok-4.5", "providerID": "openrouter", "variant": "default"})

SESS1 = ("ses_1", "/proj/a", "1.17.9", MODEL_A, 1_752_700_000_000, 1_752_700_600_000,
         100, 200, 0, 0)
SESS2 = ("ses_2", "/proj/b", "1.17.9", MODEL_A, 1_752_800_000_000, 1_752_800_300_000,
         50, 80, 0, 0)


def user_msg(mid, sid, ts):
    return (mid, sid, ts, json.dumps({"role": "user", "time": {"created": ts}}))


def asst_msg(mid, sid, ts):
    return (mid, sid, ts, json.dumps({"role": "assistant", "parentID": "x",
                                      "modelID": "grok-4.5", "time": {"created": ts}}))


def text_part(pid, mid, sid, text):
    return (pid, mid, sid, json.dumps({"type": "text", "text": text}))


class TestOpencodeAdapter(unittest.TestCase):
    def test_sessions_from_session_table(self):
        with tempfile.TemporaryDirectory() as d:
            home = pathlib.Path(d) / "opencode"
            make_session_db(home, [SESS1, SESS2])
            sessions, samples = collect_opencode.collect_sessions(home)
            self.assertEqual(len(sessions), 2)
            self.assertEqual(samples, [])
            s = sessions[0]
            self.assertEqual(s["id"], "ses_1")
            self.assertEqual(s["project"], "/proj/a")
            self.assertEqual(s["input_tokens"], 100)
            self.assertEqual(s["output_tokens"], 200)
            self.assertEqual(s["models"], {"x-ai/grok-4.5": {"input": 100, "output": 200}})
            self.assertTrue(s["started_at"].startswith("20"))

    def test_missing_or_empty_db_degrades_to_zero_sessions(self):
        with tempfile.TemporaryDirectory() as d:
            home = pathlib.Path(d) / "opencode"
            make_session_db(home)   # empty session table
            self.assertEqual(collect_opencode.collect_sessions(home), ([], []))
            self.assertEqual(collect_opencode.collect_sessions(pathlib.Path(d) / "nope"), ([], []))

    def test_session_without_messages(self):
        with tempfile.TemporaryDirectory() as d:
            home = pathlib.Path(d) / "opencode"
            make_session_db(home, [SESS1])   # no message rows for ses_1
            sessions, samples = collect_opencode.collect_sessions(home)
            self.assertEqual(len(sessions), 1)
            self.assertEqual(sessions[0]["turns"], 0)
            self.assertEqual(sessions[0]["corrections_count"], 0)
            self.assertEqual(samples, [])

    def test_correction_extraction(self):
        with tempfile.TemporaryDirectory() as d:
            home = pathlib.Path(d) / "opencode"
            messages = [
                user_msg("m1", "ses_1", 1_752_700_010_000),
                asst_msg("m2", "ses_1", 1_752_700_020_000),
                user_msg("m3", "ses_1", 1_752_700_030_000),
            ]
            parts = [
                text_part("p1", "m1", "ses_1", "add a helper function"),
                text_part("p2", "m2", "ses_1", "done, added the helper"),
                text_part("p3", "m3", "ses_1", "no, that's wrong — use the other file instead"),
            ]
            make_session_db(home, [SESS1], messages, parts)
            sessions, samples = collect_opencode.collect_sessions(home)
            s = sessions[0]
            self.assertEqual(s["corrections_count"], 1)
            self.assertEqual(s["ended_on_correction"], 1)
            self.assertEqual(len(samples), 1)
            self.assertEqual(samples[0]["session"], "ses_1")
            self.assertIn("wrong", samples[0]["user_text"])
            self.assertIn("added the helper", samples[0]["prior_assistant_text"])

    def test_since_filtering(self):
        with tempfile.TemporaryDirectory() as d:
            home = pathlib.Path(d) / "opencode"
            make_session_db(home, [SESS1, SESS2])
            sessions, _ = collect_opencode.collect_sessions(home, since_iso="2025-07-17T00:00:00Z")
            self.assertEqual([s["id"] for s in sessions], ["ses_2"])

    def test_inventory_reads_config_surface(self):
        with tempfile.TemporaryDirectory() as d:
            root = pathlib.Path(d) / "config"
            skills_root = pathlib.Path(d) / "skills-root"
            make_config_root(root, skills_root)
            inv = collect_opencode.build_inventory(root)
            self.assertEqual({m["id"] for m in inv["mcp_servers"]}, {"mcp:snowflake", "mcp:slack"})
            self.assertEqual(inv["skills"][0]["id"], "skill:my-skill")
            self.assertEqual(inv["agents"][0]["id"], "agent:reviewer")
            self.assertEqual(inv["commands"][0]["id"], "command:handoff")
            self.assertEqual(inv["settings"]["model"], "openrouter/some-model")
            self.assertTrue(inv["base_context_est"] > 0)

    def test_no_credential_files_opened(self):
        opened = []
        real_read_text = pathlib.Path.read_text

        def spy_read_text(self, *a, **k):
            opened.append(self.name)
            return real_read_text(self, *a, **k)

        with tempfile.TemporaryDirectory() as d:
            home = pathlib.Path(d) / "opencode"
            root = pathlib.Path(d) / "config"
            skills_root = pathlib.Path(d) / "skills-root"
            ev = pathlib.Path(d) / "ev"
            make_session_db(home, [SESS1])
            make_config_root(root, skills_root)
            pathlib.Path.read_text = spy_read_text
            try:
                collect_opencode.main(["--opencode-home", str(home), "--opencode-config", str(root),
                                       "--out", str(ev)])
            finally:
                pathlib.Path.read_text = real_read_text
            self.assertNotIn("auth.json", opened)
            self.assertNotIn("mcp-auth.json", opened)
            for f in ev.glob("*.json"):
                blob = f.read_text()
                self.assertNotIn("topsecret123", blob)
                self.assertNotIn("alsosecret456", blob)

    def test_main_exits_nonzero_when_db_missing(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(SystemExit) as cm:
                collect_opencode.main(["--opencode-home", str(pathlib.Path(d) / "nope"),
                                       "--out", str(pathlib.Path(d) / "ev")])
            self.assertNotEqual(cm.exception.code, 0)

    def test_main_writes_stamped_600_files(self):
        with tempfile.TemporaryDirectory() as d:
            home = pathlib.Path(d) / "opencode"
            root = pathlib.Path(d) / "config"
            skills_root = pathlib.Path(d) / "skills-root"
            ev = pathlib.Path(d) / "ev"
            make_session_db(home, [SESS1, SESS2])
            make_config_root(root, skills_root)
            collect_opencode.main(["--opencode-home", str(home), "--opencode-config", str(root),
                                   "--out", str(ev)])
            for name in ("sessions", "usage", "inventory", "samples"):
                f = ev / f"{name}.json"
                data = json.loads(f.read_text())
                self.assertEqual(data["harness"], "opencode")
                self.assertEqual(f.stat().st_mode & 0o777, 0o600)


if __name__ == "__main__":
    unittest.main()
