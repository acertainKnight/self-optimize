import json, sys, pathlib, tempfile, unittest
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "scripts"))
import apply as apply_mod
import ledger


def setting_rec():
    return {"title": "Disable dusty", "category": "bloat",
            "evidence_refs": ["inventory:skill:dusty"],
            "impact": {"ordinal": "med"}, "risk": "low",
            "metric": {"key": "correction_rate", "direction": "down", "scope": "global"},
            "action": {"harness": "claude-code", "tier": "A", "type": "setting_change",
                       "payload": {"file": "settings.json",
                                   "key_path": ["skillOverrides", "dusty"], "value": "off"}}}


class TestApply(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = pathlib.Path(self.tmp.name)
        self.state, self.data, self.ev = base / "state", base / "claude", base / "ev"
        for d in (self.state, self.data, self.ev):
            d.mkdir()
        (self.data / "settings.json").write_text(json.dumps({"model": "opusplan"}))
        (self.ev / "sessions.json").write_text(json.dumps(
            {"sessions": [{"started_at": "2026-06-01", "corrections_count": 2,
                           "input_tokens": 1, "output_tokens": 1}]}))
        self.lpath = self.state / "state" / "ledger.jsonl"
        ledger.append(self.lpath, {"id": "r1", "status": "proposed", "rec": setting_rec(),
                                   "evidence_hash": "e1"})

    def tearDown(self):
        self.tmp.cleanup()

    def test_apply_then_rollback_roundtrip(self):
        original = (self.data / "settings.json").read_text()
        apply_mod.cmd_apply(["r1"], self.state, self.data, self.ev)
        obj = json.loads((self.data / "settings.json").read_text())
        self.assertEqual(obj["skillOverrides"]["dusty"], "off")
        e = ledger.load(self.lpath)["r1"]
        self.assertEqual(e["status"], "applied")
        self.assertEqual(e["baseline"], {"value": 2.0, "n_sessions": 1})
        self.assertTrue(pathlib.Path(e["snapshot"]).exists())
        apply_mod.cmd_rollback("r1", self.state)
        self.assertEqual((self.data / "settings.json").read_text(), original)
        self.assertEqual(ledger.load(self.lpath)["r1"]["status"], "rolled_back")

    def test_smoke_failure_restores_and_marks(self):
        rec = setting_rec()
        rec["action"] = {"harness": "claude-code", "tier": "A", "type": "file_create",
                         "payload": {"path": str(self.data / "agents" / "bad.md"),
                                     "content": "---\nname: bad\nmodel: gpt-9\n---\n"}}
        ledger.append(self.lpath, {"id": "r2", "status": "proposed", "rec": rec,
                                   "evidence_hash": "e2"})
        apply_mod.cmd_apply(["r2"], self.state, self.data, self.ev)
        self.assertFalse((self.data / "agents" / "bad.md").exists())   # restored (deleted)
        self.assertEqual(ledger.load(self.lpath)["r2"]["status"], "apply_failed")

    def test_reject_records_reason(self):
        apply_mod.cmd_reject("r1", "keep it around", self.state)
        e = ledger.load(self.lpath)["r1"]
        self.assertEqual((e["status"], e["reason"]), ("rejected", "keep it around"))


if __name__ == "__main__":
    unittest.main()
