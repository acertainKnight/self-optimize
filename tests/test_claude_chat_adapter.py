import json, pathlib, sys, tempfile, unittest
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "adapters" / "claude_chat"))
import collect_chat

EXPORT = [
    {"uuid": "c1", "name": "chat one", "created_at": "2026-07-01T10:00:00Z",
     "updated_at": "2026-07-01T11:00:00Z",
     "chat_messages": [
         {"uuid": "m1", "sender": "human", "text": "summarize this doc",
          "created_at": "2026-07-01T10:00:00Z"},
         {"uuid": "m2", "sender": "assistant", "text": "Here is a long summary...",
          "created_at": "2026-07-01T10:01:00Z"},
         {"uuid": "m3", "sender": "human",
          "text": "no, that's wrong — the token is sk-ant-api03-" + "b" * 90,
          "created_at": "2026-07-01T10:02:00Z"},
     ]},
    {"uuid": "c2", "name": "quiet chat", "created_at": "2026-07-02T10:00:00Z",
     "updated_at": "2026-07-02T10:30:00Z",
     "chat_messages": [
         {"uuid": "m4", "sender": "human", "text": "hello",
          "created_at": "2026-07-02T10:00:00Z"},
         {"uuid": "m5", "sender": "assistant", "text": "hi",
          "created_at": "2026-07-02T10:00:30Z"},
     ]},
]


class TestClaudeChatAdapter(unittest.TestCase):
    def test_corrections_mined_and_scrubbed(self):
        sessions, samples = collect_chat.collect_conversations(EXPORT)
        self.assertEqual(len(sessions), 2)
        s1 = next(s for s in sessions if s["id"] == "c1")
        self.assertEqual(s1["corrections_count"], 1)
        self.assertEqual(s1["ended_on_correction"], 1)
        self.assertEqual(len(samples), 1)
        self.assertNotIn("sk-ant-api03", samples[0]["user_text"])
        self.assertIn("long summary", samples[0]["prior_assistant_text"])

    def test_since_filter_and_junk_tolerance(self):
        sessions, _ = collect_chat.collect_conversations(
            EXPORT + ["junk", {"no_messages": True}], since_iso="2026-07-02T00:00:00Z")
        self.assertEqual([s["id"] for s in sessions], ["c2"])   # undated junk filtered

    def test_main_writes_stamped_evidence(self):
        with tempfile.TemporaryDirectory() as d:
            exp = pathlib.Path(d) / "conversations.json"
            exp.write_text(json.dumps(EXPORT))
            ev = pathlib.Path(d) / "ev"
            collect_chat.main(["--export", str(exp), "--out", str(ev)])
            usage = json.loads((ev / "usage.json").read_text())
            self.assertEqual(usage["harness"], "claude-chat")
            self.assertEqual(usage["corrections"]["total"], 1)
            self.assertTrue(usage["parse"]["collector_limits"])
            self.assertEqual((ev / "samples.json").stat().st_mode & 0o777, 0o600)


if __name__ == "__main__":
    unittest.main()
