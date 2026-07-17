import sys, pathlib, unittest
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "scripts"))
import verify

CFG = {"verify": {"min_sessions": 3, "min_rel_change": 0.10}}


def entry(baseline, direction="down"):
    return {"status": "applied", "applied_at": "2026-06-10T00:00:00+00:00",
            "baseline": {"value": baseline, "n_sessions": 5},
            "rec": {"title": "t", "metric": {"key": "correction_rate",
                                             "direction": direction, "scope": "global"}}}


def sessions(counts):
    return [{"started_at": f"2026-06-2{i}T00:00:00Z", "corrections_count": c}
            for i, c in enumerate(counts)]


class TestVerify(unittest.TestCase):
    def test_verified_regressed_inconclusive(self):
        rows = verify.verify_entries({"a": entry(2.0)}, sessions([0, 0, 0]), None, CFG)
        self.assertEqual(rows[0]["verdict"], "verified")       # 2.0 -> 0.0, direction down
        self.assertEqual(rows[0]["direction"], "down")         # report needs it for delta sign
        rows = verify.verify_entries({"a": entry(1.0)}, sessions([3, 3, 3]), None, CFG)
        self.assertEqual(rows[0]["verdict"], "regressed")      # 1.0 -> 3.0
        rows = verify.verify_entries({"a": entry(2.0)}, sessions([0]), None, CFG)
        self.assertEqual(rows[0]["verdict"], "inconclusive")   # n=1 < min_sessions
        rows = verify.verify_entries({"a": entry(2.0)}, sessions([2, 2, 2]), None,
                                     {"verify": {"min_sessions": 3, "min_rel_change": 0.10}})
        self.assertEqual(rows[0]["verdict"], "inconclusive")   # no movement beyond threshold

    def test_skips_non_applied_and_metric_none(self):
        e = entry(2.0); e["status"] = "rejected"
        self.assertEqual(verify.verify_entries({"a": e}, sessions([0, 0, 0]), None, CFG), [])
        e2 = entry(2.0); e2["rec"]["metric"] = {"key": "none"}
        self.assertEqual(verify.verify_entries({"a": e2}, sessions([0, 0, 0]), None, CFG), [])

    def test_snapshot_metric_verdict_comes_from_setting_still_in_effect(self):
        # the global number moving (up OR down) proves nothing about this rec —
        # any other config change moves it too. Verdict = is the setting still set.
        def snap_entry():
            e = entry(88911.0)
            e["rec"]["metric"] = {"key": "base_context_est", "direction": "down",
                                  "scope": "global"}
            e["rec"]["action"] = {"type": "setting_change", "payload": {
                "file": "settings.json",
                "key_path": ["skillOverrides", "d"], "value": "off"}}
            return e
        inventory = {"base_context_est": 150000, "unused": []}  # grew since apply

        rows = verify.verify_entries({"a": snap_entry()}, sessions([0]), inventory, CFG,
                                     settings={"skillOverrides": {"d": "off"}})
        self.assertEqual(rows[0]["verdict"], "verified")   # still in effect despite growth
        self.assertIsNone(rows[0]["rel_change"])           # snapshot delta is context only
        self.assertIsNone(rows[0]["p_value"])

        rows = verify.verify_entries({"a": snap_entry()}, sessions([0]), inventory, CFG,
                                     settings={"skillOverrides": {}})
        self.assertEqual(rows[0]["verdict"], "regressed")  # setting was reverted

        rows = verify.verify_entries({"a": snap_entry()}, sessions([0]), inventory, CFG,
                                     settings=None)
        self.assertEqual(rows[0]["verdict"], "inconclusive")   # cannot check

        no_action = entry(88911.0)
        no_action["rec"]["metric"] = {"key": "base_context_est", "direction": "down",
                                      "scope": "global"}
        rows = verify.verify_entries({"a": no_action}, sessions([0]), inventory, CFG,
                                     settings={"skillOverrides": {"d": "off"}})
        self.assertEqual(rows[0]["verdict"], "inconclusive")   # nothing checkable

    def test_snapshot_verdicts_rechecked_every_run(self):
        import json, tempfile
        from datetime import datetime, timedelta, timezone
        import apply as apply_mod
        import ledger
        base = pathlib.Path(tempfile.mkdtemp())
        state, data, ev = base / "state", base / "claude", base / "ev"
        for d in (state, data, ev):
            d.mkdir()
        (data / "settings.json").write_text(json.dumps({"skillOverrides": {}}))
        (ev / "sessions.json").write_text(json.dumps({"sessions": []}))
        (ev / "inventory.json").write_text(json.dumps({"base_context_est": 500, "unused": []}))
        rec = {"title": "t", "category": "bloat", "evidence_refs": ["x"],
               "impact": {"ordinal": "low"}, "risk": "low",
               "metric": {"key": "base_context_est", "direction": "down", "scope": "global"},
               "action": {"harness": "claude-code", "tier": "A", "type": "setting_change",
                          "payload": {"file": "settings.json",
                                      "key_path": ["skillOverrides", "d"], "value": "off"}}}
        lpath = state / "state" / "ledger.jsonl"
        ledger.append(lpath, {"id": "r1", "status": "proposed", "rec": rec, "evidence_hash": "e"})
        apply_mod.cmd_apply(["r1"], state, data, ev)
        args = ["--evidence", str(ev), "--state", str(state), "--data-root", str(data),
                "--out", str(ev / "verify.json")]
        verify.main(args)
        self.assertEqual(ledger.load(lpath)["r1"]["status"], "verified")
        n_lines = len(lpath.read_text().splitlines())
        verify.main(args)   # unchanged verdict: re-checked but no duplicate entry
        self.assertEqual(len(lpath.read_text().splitlines()), n_lines)
        self.assertEqual(ledger.load(lpath)["r1"]["status"], "verified")
        # hand-revert the setting: next run must flip verified -> regressed
        (data / "settings.json").write_text(json.dumps({"skillOverrides": {}}))
        verify.main(args)
        self.assertEqual(ledger.load(lpath)["r1"]["status"], "regressed")

    def test_missing_inventory_does_not_false_verify(self):
        # no evidence/inventory.json this run (inventory=None) must not read as
        # "unused surfaces dropped to zero" — that would falsely verify any
        # positive baseline
        e = entry(12.0)
        e["rec"]["metric"] = {"key": "unused_surface_count", "direction": "down", "scope": "global"}
        rows = verify.verify_entries({"a": e}, sessions([0]), None, CFG)
        self.assertEqual(rows[0]["verdict"], "inconclusive")

    def test_significant_distributions_verify(self):
        e = entry(10.0)
        e["baseline"]["samples"] = [10.0, 10.0, 10.0, 10.0]
        rows = verify.verify_entries({"a": e}, sessions([0, 0, 0, 0]), None,
                                     {"verify": {"min_sessions": 3, "min_rel_change": 0.10}})
        self.assertEqual(rows[0]["verdict"], "verified")
        self.assertEqual(rows[0]["p_value"], 0.0)

    def test_noisy_distributions_flip_verdict_to_inconclusive(self):
        legacy = entry(5.0)   # no "samples" key: legacy threshold-only path
        rows = verify.verify_entries({"a": legacy}, sessions([1, 5, 0, 6, 2, 4]), None,
                                     {"verify": {"min_sessions": 3, "min_rel_change": 0.10}})
        self.assertEqual(rows[0]["verdict"], "verified")   # legacy rule alone says verified
        self.assertIsNone(rows[0]["p_value"])
        noisy = entry(5.0)
        noisy["baseline"]["samples"] = [3.0, 7.0, 2.0, 8.0, 4.0, 6.0]   # high-variance baseline
        rows = verify.verify_entries({"a": noisy}, sessions([1, 5, 0, 6, 2, 4]), None,
                                     {"verify": {"min_sessions": 3, "min_rel_change": 0.10}})
        self.assertEqual(rows[0]["verdict"], "inconclusive")   # same movement, but p >= 0.05
        self.assertGreater(rows[0]["p_value"], 0.05)

    def test_legacy_baseline_without_samples_uses_threshold_only(self):
        rows = verify.verify_entries({"a": entry(2.0)}, sessions([0, 0, 0]), None, CFG)
        self.assertEqual(rows[0]["verdict"], "verified")
        self.assertIsNone(rows[0]["p_value"])

    def test_rollback_still_works_after_verdict(self):
        import json, tempfile
        from datetime import datetime, timedelta, timezone
        import apply as apply_mod
        import ledger
        base = pathlib.Path(tempfile.mkdtemp())
        state, data, ev = base / "state", base / "claude", base / "ev"
        for d in (state, data, ev):
            d.mkdir()
        (data / "settings.json").write_text(json.dumps({"model": "opusplan"}))
        (ev / "sessions.json").write_text(json.dumps({"sessions": [
            {"started_at": "2026-06-01T00:00:00Z", "corrections_count": 1}]}))
        rec = {"title": "t", "category": "bloat", "evidence_refs": ["x"],
               "impact": {"ordinal": "low"}, "risk": "low",
               "metric": {"key": "correction_rate", "direction": "down", "scope": "global"},
               "action": {"harness": "claude-code", "tier": "A", "type": "setting_change",
                          "payload": {"file": "settings.json",
                                      "key_path": ["skillOverrides", "d"], "value": "off"}}}
        lpath = state / "state" / "ledger.jsonl"
        ledger.append(lpath, {"id": "r1", "status": "proposed", "rec": rec, "evidence_hash": "e"})
        apply_mod.cmd_apply(["r1"], state, data, ev)
        now = datetime.now(timezone.utc)
        (ev / "sessions.json").write_text(json.dumps({"sessions": [
            {"started_at": (now + timedelta(days=i + 1)).isoformat(), "corrections_count": 5}
            for i in range(12)]}))
        verify.main(["--evidence", str(ev), "--state", str(state),
                     "--out", str(ev / "verify.json")])
        self.assertEqual(ledger.load(lpath)["r1"]["status"], "regressed")
        apply_mod.cmd_rollback("r1", state)   # the report's one-command rollback must work
        self.assertEqual(json.loads((data / "settings.json").read_text()), {"model": "opusplan"})

    def test_main_writes_verify_with_600_perms(self):
        import json, tempfile
        base = pathlib.Path(tempfile.mkdtemp())
        state, ev = base / "state", base / "ev"
        ev.mkdir()
        (ev / "sessions.json").write_text(json.dumps({"sessions": []}))
        out = ev / "verify.json"
        verify.main(["--evidence", str(ev), "--state", str(state), "--out", str(out)])
        mode = out.stat().st_mode & 0o777
        self.assertEqual(mode, 0o600)


if __name__ == "__main__":
    unittest.main()
