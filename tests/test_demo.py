"""Integration test over the synthetic demo corpus (demo/): the same fixtures
demo/run.sh scores end to end, exercised here as an automated regression check
that the planted flaw's fix scores above a deliberately degraded rewrite.
Every fixture this test reads is fabricated -- see demo/README.md."""
import pathlib
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "demo"))
import gym            # noqa: E402
import schema as so_schema  # noqa: E402
import run_demo        # noqa: E402


class DemoCorpusCase(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        self.state = self.tmp / "state"
        self.cfg = run_demo.judge_cfg(self.state)
        gym.accrue(self.state, [run_demo.EVIDENCE_DIR], "test-run", self.cfg)

    def test_demo_evidence_registers_the_skill_as_scorable(self):
        rows = gym.artifact_status(self.state, self.cfg)
        row = next(r for r in rows if r["id"] == run_demo.ARTIFACT_ID)
        self.assertTrue(row["scorable"], row["reason"])
        self.assertEqual(row["failure_cases"], 4)
        self.assertEqual(row["working_cases"], 4)

    def test_known_good_scores_strictly_above_known_bad(self):
        good = gym.score_artifact(self.state, run_demo.ARTIFACT_ID,
                                  run_demo.GOOD_CANDIDATE.read_text(), self.cfg)
        bad = gym.score_artifact(self.state, run_demo.ARTIFACT_ID,
                                 run_demo.BAD_CANDIDATE.read_text(), self.cfg)
        self.assertFalse(good["unscorable"], good["reason"])
        self.assertFalse(bad["unscorable"], bad["reason"])
        self.assertEqual(good["prevented"], {"n": 4, "of": 4})
        self.assertEqual(good["preserved"], {"n": 4, "of": 4})
        self.assertEqual(bad["prevented"], {"n": 0, "of": 4})
        self.assertEqual(bad["preserved"], {"n": 0, "of": 4})
        self.assertGreater(good["prevented"]["n"], bad["prevented"]["n"])
        self.assertGreater(good["preserved"]["n"], bad["preserved"]["n"])

    def test_bounded_edit_reproduces_the_known_good_candidate(self):
        fixed = so_schema.apply_ops(run_demo.SKILL_PATH.read_text(), run_demo.FIX_OPS)
        self.assertEqual(fixed, run_demo.GOOD_CANDIDATE.read_text())

    def test_gate_scores_the_fix_and_downgrades_nothing(self):
        findings_doc = run_demo.build_findings()
        findings_path = self.tmp / "findings.json"
        import json
        findings_path.write_text(json.dumps(findings_doc))
        summary = gym.gate(self.state, findings_path, self.cfg, run_id="test-run")
        self.assertEqual(summary["downgraded"], [])
        fix_score = summary["scores"]["demo-fix"]
        degrade_score = summary["scores"]["demo-degrade"]
        self.assertFalse(fix_score["unscorable"], fix_score["reason"])
        self.assertFalse(degrade_score["unscorable"], degrade_score["reason"])
        self.assertGreater(fix_score["prevented"]["n"], degrade_score["prevented"]["n"])


if __name__ == "__main__":
    unittest.main()
