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


if __name__ == "__main__":
    unittest.main()
