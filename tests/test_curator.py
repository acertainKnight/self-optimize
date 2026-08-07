import json, pathlib, sys, tempfile, unittest, unittest.mock
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "scripts"))
import curator
import gym
import schema
import so_config
import synth

REPORT_A = """---
name: report-writer
description: Writes a formatted report from data
---
# Report Writer

Gathers data points and writes them into a structured markdown report with a
summary section, a details table, and a closing recommendation.
"""

REPORT_B = """---
name: report-writer-v2
description: Writes a formatted report from data, v2
---
# Report Writer V2

Gathers data points and writes them into a structured markdown report with a
summary section, a details table, and a closing recommendation. Now with color.
"""

UNRELATED = """---
name: unrelated-skill
description: Fetches weather data and formats it for chat display
---
# Unrelated Skill

Does something entirely different: calls a weather API, parses the JSON
response, and prints a short forecast summary to the chat.
"""

COMPLETE = """---
name: complete-skill
description: Already has all lifecycle metadata
version: 2
superseded_by: none
requires-tools: git
---
# Complete Skill

Nothing to flag here.
"""


def write_skill(root: pathlib.Path, name: str, body: str) -> str:
    d = root / "skills" / name
    d.mkdir(parents=True, exist_ok=True)
    p = d / "SKILL.md"
    p.write_text(body)
    return str(p)


def inv_item(skill_id_name: str, path: str, description: str = "", source: str = "user") -> dict:
    return {"id": f"skill:{skill_id_name}", "name": skill_id_name, "source": source,
            "path": path, "description": description}


class TestStage1Duplicates(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp())

    def test_near_duplicate_pair_flagged_unrelated_skill_not(self):
        a = write_skill(self.tmp, "report-writer", REPORT_A)
        b = write_skill(self.tmp, "report-writer-v2", REPORT_B)
        c = write_skill(self.tmp, "unrelated-skill", UNRELATED)
        skills = curator.load_user_skills({"skills": [
            inv_item("report-writer", a), inv_item("report-writer-v2", b),
            inv_item("unrelated-skill", c)]})
        pairs = curator.find_duplicates(skills, floor=0.6)
        self.assertEqual(len(pairs), 1)
        names = {pairs[0]["a"]["name"], pairs[0]["b"]["name"]}
        self.assertEqual(names, {"report-writer", "report-writer-v2"})
        self.assertGreaterEqual(pairs[0]["body_ratio"], 0.6)

    def test_plugin_owned_skills_excluded(self):
        a = write_skill(self.tmp, "report-writer", REPORT_A)
        skills = curator.load_user_skills({"skills": [
            inv_item("report-writer", a, source="plugin:tk@mp")]})
        self.assertEqual(skills, [])

    def test_missing_file_on_disk_skipped_not_crash(self):
        skills = curator.load_user_skills({"skills": [
            inv_item("ghost", str(self.tmp / "skills" / "ghost" / "SKILL.md"))]})
        self.assertEqual(skills, [])


class TestStage1Retirement(unittest.TestCase):
    def test_never_fired_flagged(self):
        skills = [{"id": "skill:alpha", "name": "alpha"}, {"id": "skill:beta", "name": "beta"}]
        activation = {"skill:beta": {"count": 5, "last_used": "2026-08-01T00:00:00Z"}}
        never = curator.find_never_fired(skills, activation)
        self.assertEqual([s["id"] for s in never], ["skill:alpha"])

    def test_long_unfired_flagged_recent_not(self):
        skills = [{"id": "skill:old", "name": "old"}, {"id": "skill:fresh", "name": "fresh"}]
        now = curator.datetime.fromisoformat("2026-08-07T00:00:00+00:00")
        activation = {"skill:old": {"count": 3, "last_used": "2026-01-01T00:00:00Z"},
                      "skill:fresh": {"count": 3, "last_used": "2026-08-05T00:00:00Z"}}
        long_unfired = curator.find_long_unfired(skills, activation, days=60, now=now)
        self.assertEqual([s["id"] for s in long_unfired], ["skill:old"])
        self.assertGreaterEqual(long_unfired[0]["days_unfired"], 60)

    def test_never_fired_not_double_counted_as_long_unfired(self):
        skills = [{"id": "skill:alpha", "name": "alpha"}]
        long_unfired = curator.find_long_unfired(skills, {}, days=60)
        self.assertEqual(long_unfired, [])


class TestStage1Metadata(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp())

    def test_missing_all_three_keys_flagged(self):
        p = write_skill(self.tmp, "report-writer", REPORT_A)
        skills = curator.load_user_skills({"skills": [inv_item("report-writer", p)]})
        gaps = curator.find_metadata_gaps(skills)
        self.assertEqual({g["key"] for g in gaps}, {"version", "superseded_by", "requires-tools"})

    def test_complete_frontmatter_no_gaps(self):
        p = write_skill(self.tmp, "complete-skill", COMPLETE)
        skills = curator.load_user_skills({"skills": [inv_item("complete-skill", p)]})
        self.assertEqual(curator.find_metadata_gaps(skills), [])


class TestFindingShapes(unittest.TestCase):
    """Every finding curator emits must satisfy the same schema and guard the
    LLM analysts are held to -- this is what lets curator.json flow through
    synth.py unmodified."""
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        self.data_root = pathlib.Path("/fakehome/.claude")

    def test_metadata_finding_valid_and_guarded(self):
        skill = {"id": "skill:foo", "name": "foo", "path": "/fakehome/.claude/skills/foo/SKILL.md"}
        rec = curator.metadata_finding(skill, "version", "1")
        self.assertEqual(schema.validate_rec(rec), [])
        self.assertTrue(synth.guard(rec, self.data_root))

    def test_retire_finding_valid_and_guarded(self):
        skill = {"id": "skill:foo", "name": "foo", "path": "/fakehome/.claude/skills/foo/SKILL.md"}
        rec = curator.retire_finding(skill, "never-fired", ["inventory:skill:foo"])
        self.assertEqual(schema.validate_rec(rec), [])
        self.assertTrue(synth.guard(rec, self.data_root))
        self.assertTrue(schema.is_applicable(rec["action"]))

    def test_dedupe_manual_finding_valid_and_guarded(self):
        a = {"id": "skill:a", "name": "a", "path": "/fakehome/.claude/skills/a/SKILL.md"}
        b = {"id": "skill:b", "name": "b", "path": "/fakehome/.claude/skills/b/SKILL.md"}
        rec = curator.dedupe_manual_finding(a, b, {"body_ratio": 0.9})
        self.assertEqual(schema.validate_rec(rec), [])
        self.assertTrue(synth.guard(rec, self.data_root))
        self.assertFalse(schema.is_applicable(rec["action"]))   # manual: report-only

    def test_dedupe_diff_finding_valid_and_guarded(self):
        survivor = {"id": "skill:a", "name": "a", "path": "/fakehome/.claude/skills/a/SKILL.md",
                    "body": "---\nname: a\n---\nold\n"}
        retiree = {"id": "skill:b", "name": "b", "path": "/fakehome/.claude/skills/b/SKILL.md"}
        rec = curator.dedupe_diff_finding(survivor, retiree, "---\nname: a\n---\nnew\n")
        self.assertEqual(schema.validate_rec(rec), [])
        self.assertTrue(synth.guard(rec, self.data_root))
        self.assertTrue(schema.is_applicable(rec["action"]))


class TestStage2Merge(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        self.stub = self.tmp / "stub_merge.py"
        self.stub.write_text(
            "import sys\n"
            "prompt = sys.stdin.read()\n"
            "sys.stdout.write('---\\nname: report-writer\\n---\\nMERGED BODY\\n')\n")

    def _judge(self, script=None):
        if script is None:
            return {"command": [], "model": "", "timeout_s": 30}
        return {"command": [sys.executable, str(script)], "model": "m", "timeout_s": 30}

    def test_unconfigured_backend_skips_gracefully_zero_calls(self):
        a = {"id": "skill:a", "name": "a", "body": REPORT_A}
        b = {"id": "skill:b", "name": "b", "body": REPORT_B}
        with unittest.mock.patch.object(gym, "_invoke_judge",
                                        side_effect=AssertionError("must not be called")):
            result = curator.propose_merge(self._judge(), a, b)
        self.assertIsNone(result)

    def test_stub_backend_returns_merged_text(self):
        a = {"id": "skill:a", "name": "a", "body": REPORT_A}
        b = {"id": "skill:b", "name": "b", "body": REPORT_B}
        text = curator.propose_merge(self._judge(self.stub), a, b)
        self.assertIn("MERGED BODY", text)

    def test_broken_backend_returns_none_not_raise(self):
        broken = self.tmp / "broken.py"
        broken.write_text("import sys\nsys.stderr.write('boom')\nsys.exit(1)\n")
        a = {"id": "skill:a", "name": "a", "body": REPORT_A}
        b = {"id": "skill:b", "name": "b", "body": REPORT_B}
        result = curator.propose_merge(self._judge(broken), a, b)
        self.assertIsNone(result)

    def test_blank_output_treated_as_no_proposal(self):
        blank = self.tmp / "blank.py"
        blank.write_text("import sys\nsys.stdout.write('   \\n')\n")
        a = {"id": "skill:a", "name": "a", "body": REPORT_A}
        b = {"id": "skill:b", "name": "b", "body": REPORT_B}
        self.assertIsNone(curator.propose_merge(self._judge(blank), a, b))


class TestScanEndToEnd(unittest.TestCase):
    """Acceptance criterion: a fixture library plants a near-duplicate pair and
    a never-fired skill; the deterministic pass flags both with zero LLM calls."""
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        self.a = write_skill(self.tmp, "report-writer", REPORT_A)
        self.b = write_skill(self.tmp, "report-writer-v2", REPORT_B)
        self.never_fired = write_skill(self.tmp, "unrelated-skill", UNRELATED)
        self.inventory = {"skills": [
            inv_item("report-writer", self.a, description="Writes a formatted report from data"),
            inv_item("report-writer-v2", self.b, description="Writes a formatted report from data, v2"),
            inv_item("unrelated-skill", self.never_fired,
                    description="Fetches weather data and formats it for chat display"),
        ]}
        # report-writer fired, report-writer-v2 fired more (survives the pair),
        # unrelated-skill never fired at all
        self.activation = {"items": {
            "skill:report-writer": {"count": 2, "last_used": "2026-08-01T00:00:00Z"},
            "skill:report-writer-v2": {"count": 9, "last_used": "2026-08-06T00:00:00Z"},
        }}
        self.cfg = json.loads(json.dumps(so_config.DEFAULTS))

    def test_zero_llm_calls_flags_duplicate_and_never_fired(self):
        with unittest.mock.patch.object(gym, "_invoke_judge",
                                        side_effect=AssertionError("must not be called")):
            findings = curator.scan(self.inventory, self.activation, self.cfg)
        by_cat = {}
        for f in findings:
            by_cat.setdefault(f["category"], []).append(f)
        self.assertIn("skill-dedupe", by_cat)
        self.assertEqual(len(by_cat["skill-dedupe"]), 1)
        self.assertEqual(by_cat["skill-dedupe"][0]["action"]["type"], "manual")  # no backend
        retired_names = {f["title"] for f in by_cat["skill-retire"]}
        self.assertTrue(any("unrelated-skill" in t and "never-fired" in t for t in retired_names))
        # metadata gaps for all three skills (none carry lifecycle frontmatter)
        self.assertIn("skill-edit", by_cat)
        for rec in findings:
            self.assertEqual(schema.validate_rec(rec), [], rec)

    def test_configured_backend_promotes_pair_to_diff_plus_retire(self):
        stub = self.tmp / "stub_merge.py"
        stub.write_text(
            "import sys\n"
            "sys.stdout.write('---\\nname: report-writer-v2\\n---\\nMERGED\\n')\n")
        self.cfg["gym"]["judge"] = {"command": [sys.executable, str(stub)], "model": "m",
                                    "timeout_s": 30}
        findings = curator.scan(self.inventory, self.activation, self.cfg)
        dedupe = [f for f in findings if f["category"] == "skill-dedupe"]
        self.assertEqual(len(dedupe), 1)
        self.assertEqual(dedupe[0]["action"]["type"], "diff")
        # higher-activation skill (report-writer-v2, count 9) survives; the
        # lower one (report-writer, count 2) gets an extra retire finding
        retire_titles = [f["title"] for f in findings if f["category"] == "skill-retire"]
        self.assertTrue(any("report-writer" in t and "merged-duplicate" in t
                            for t in retire_titles if "report-writer-v2" not in t.split(":")[-1]))


class TestMainCli(unittest.TestCase):
    def test_writes_owner_only_json_and_empty_array_when_nothing_found(self):
        tmp = pathlib.Path(tempfile.mkdtemp())
        ev, state = tmp / "ev", tmp / "state"
        ev.mkdir()
        (ev / "inventory.json").write_text(json.dumps({"skills": []}))
        (ev / "activation.json").write_text(json.dumps({"items": {}}))
        out = tmp / "curator.json"
        curator.main(["--evidence", str(ev), "--state", str(state), "--out", str(out)])
        self.assertEqual(json.loads(out.read_text()), [])
        self.assertEqual(out.stat().st_mode & 0o777, 0o600)

    def test_missing_evidence_files_degrade_not_crash(self):
        tmp = pathlib.Path(tempfile.mkdtemp())
        ev, state = tmp / "ev", tmp / "state"
        ev.mkdir()
        out = tmp / "curator.json"
        curator.main(["--evidence", str(ev), "--state", str(state), "--out", str(out)])
        self.assertEqual(json.loads(out.read_text()), [])


if __name__ == "__main__":
    unittest.main()
