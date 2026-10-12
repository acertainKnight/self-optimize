import contextlib, io, json, os, pathlib, sys, tempfile, unittest, urllib.error
from unittest import mock
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "scripts"))
import cortex_writer
import schema

COST = {"kind": "rule",
        "statement": "When reporting costs, state the dollar figure; never describe a cost as small",
        "trigger": "reporting the cost of a run, a model call or a job"}


def rec(learned=COST, **extra):
    r = {"title": "State dollar figures for costs", "category": "claude-md",
         "evidence_refs": ["sample:0", "sample:3", "session:s1", "session:s2",
                           "usage:corrections_by_model.x"],
         "impact": {"ordinal": "high"}, "risk": "none",
         "metric": {"key": "correction_rate", "direction": "down", "scope": "global"},
         "action": {"harness": "claude-code", "tier": "B", "type": "manual",
                    "payload": {"description": "add the rule to CLAUDE.md"}}}
    if learned is not None:
        r["learned"] = learned
    r.update(extra)
    return r


def write_ledger(state, rows):
    p = pathlib.Path(state) / "state" / "ledger.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("".join(json.dumps(r) + "\n" for r in rows))


class Fake:
    def __init__(self, fail=None):
        self.calls, self.fail = [], fail or {}

    def __call__(self, url, token, payload):
        self.calls.append(payload)
        err = self.fail.get(payload["key"])
        if err:
            raise err
        return {"status": "created"}


class ValidateTest(unittest.TestCase):
    def test_shape(self):
        self.assertEqual(cortex_writer.validate(COST), [])
        self.assertTrue(cortex_writer.validate({**COST, "kind": "fact"}))
        self.assertTrue(cortex_writer.validate({**COST, "statement": ""}))
        self.assertTrue(cortex_writer.validate({**COST, "trigger": "a\nb"}))
        self.assertTrue(cortex_writer.validate({**COST, "statement": "x" * 301}))
        self.assertTrue(cortex_writer.validate("not an object"))

    def test_schema_checks_learned_and_keeps_the_id(self):
        self.assertEqual(schema.validate_rec(rec()), [])
        self.assertTrue(schema.validate_rec(rec({**COST, "kind": "fact"})))
        self.assertEqual(schema.rec_id(rec()), schema.rec_id(rec(learned=None)))


class FindingItemsTest(unittest.TestCase):
    def test_only_approved_findings_with_learned_are_sent(self):
        with tempfile.TemporaryDirectory() as st:
            write_ledger(st, [
                {"id": "a1", "status": "proposed", "rec": rec(), "ts": "2026-10-10T09:00:00"},
                {"id": "a1", "status": "applied", "rec": rec(), "ts": "2026-10-12T09:00:00"},
                {"id": "b2", "status": "proposed", "rec": rec()},
                {"id": "c3", "status": "applied", "rec": rec(learned=None)},
                {"id": "d4", "status": "adopted", "rec": rec({**COST, "kind": "warning"})},
                {"id": "e5", "status": "applied", "rec": rec()},
                {"id": "e5", "status": "rolled_back"},
            ])
            items = {i["key"]: i for i in cortex_writer.finding_items(pathlib.Path(st))}
        self.assertEqual(sorted(items), ["self-optimize:a1", "self-optimize:d4",
                                         "self-optimize:e5"])
        a1 = items["self-optimize:a1"]
        self.assertEqual(a1["statement"], COST["statement"])
        self.assertEqual(a1["created_by"], "self-optimize")
        self.assertFalse(a1["archived"])
        self.assertEqual(a1["evidence_count"], 2)
        self.assertEqual(a1["provenance"], [
            {"type": "finding", "ref": "self-optimize:a1", "date": "2026-10-10"},
            {"type": "session", "ref": "session:s1"},
            {"type": "session", "ref": "session:s2"}])
        self.assertEqual(items["self-optimize:d4"]["kind"], "warning")
        self.assertTrue(items["self-optimize:e5"]["archived"])

    def test_no_transcript_text_in_any_payload(self):
        r = rec()
        r["action"]["payload"]["description"] = "user said: SECRET-EXCERPT"
        with tempfile.TemporaryDirectory() as st:
            write_ledger(st, [{"id": "a1", "status": "applied", "rec": r}])
            [item] = cortex_writer.finding_items(pathlib.Path(st))
        self.assertNotIn("SECRET-EXCERPT", json.dumps(item))
        self.assertNotIn("sample:", json.dumps(item))


class SkillItemsTest(unittest.TestCase):
    def test_model_invocable_skills_with_a_description(self):
        inv = {"skills": [
            {"name": "grill-me", "path": "/h/.claude/skills/grill-me/SKILL.md",
             "description": "Stress-test a plan\n before it is built",
             "disable_model_invocation": "false", "override": None},
            {"name": "runner", "path": "/p/SKILL.md", "description": "Run the loop",
             "disable_model_invocation": "true"},
            {"name": "off", "path": "/o/SKILL.md", "description": "x", "override": "off"},
            {"name": "bare", "path": "/b/SKILL.md", "description": ""},
        ]}
        with tempfile.TemporaryDirectory() as ev:
            (pathlib.Path(ev) / "inventory.json").write_text(json.dumps(inv))
            items = cortex_writer.skill_items(pathlib.Path(ev))
        self.assertEqual(items, [{
            "kind": "skill_pointer", "skill_name": "grill-me",
            "skill_path": "/h/.claude/skills/grill-me/SKILL.md",
            "trigger": "Stress-test a plan before it is built",
            "key": "self-optimize:skill:grill-me", "created_by": "self-optimize"}])


class SyncTest(unittest.TestCase):
    def items(self):
        return [{"kind": "rule", "key": "self-optimize:a1", **COST},
                {"kind": "skill_pointer", "key": "self-optimize:skill:grill-me",
                 "skill_name": "grill-me", "skill_path": "/s/SKILL.md", "trigger": "t"}]

    def test_sends_once_then_skips_unchanged_and_archives_vanished_skills(self):
        with tempfile.TemporaryDirectory() as st:
            post = Fake()
            c = cortex_writer.sync(self.items(), pathlib.Path(st), "http://b", "tok", post)
            self.assertEqual((c["sent"], c["unchanged"], c["failed"]), (2, 0, 0))
            c = cortex_writer.sync(self.items(), pathlib.Path(st), "http://b", "tok", post)
            self.assertEqual((c["sent"], c["unchanged"]), (0, 2))
            c = cortex_writer.sync(self.items()[:1], pathlib.Path(st), "http://b", "tok", post)
            self.assertEqual(c["sent"], 1)
            self.assertEqual(post.calls[-1], {"kind": "skill_pointer",
                                              "key": "self-optimize:skill:grill-me",
                                              "created_by": "self-optimize",
                                              "archived": True})
            c = cortex_writer.sync(self.items()[:1], pathlib.Path(st), "http://b", "tok", post)
            self.assertEqual((c["sent"], c["unchanged"]), (0, 2))
            mode = (pathlib.Path(st) / "state" / "cortex_sent.json").stat().st_mode
            self.assertEqual(mode & 0o777, 0o600)

    def test_failures_are_counted_and_retried_next_run(self):
        with tempfile.TemporaryDirectory() as st:
            boom = urllib.error.URLError("unreachable")
            post = Fake(fail={"self-optimize:a1": boom})
            c = cortex_writer.sync(self.items(), pathlib.Path(st), "http://b", "tok", post)
            self.assertEqual((c["sent"], c["failed"]), (1, 1))
            self.assertIn("unreachable", c["errors"][0])
            c = cortex_writer.sync(self.items(), pathlib.Path(st), "http://b", "tok", Fake())
            self.assertEqual((c["sent"], c["unchanged"]), (1, 1))

    def test_archiving_an_item_the_brain_never_held_is_not_a_failure(self):
        archived = {"kind": "rule", "key": "self-optimize:z9", "archived": True, **COST}
        missing = urllib.error.HTTPError("u", 404, "not found", {}, None)
        with tempfile.TemporaryDirectory() as st:
            c = cortex_writer.sync([archived], pathlib.Path(st), "http://b", "tok",
                                   Fake(fail={"self-optimize:z9": missing}))
        self.assertEqual((c["sent"], c["unchanged"], c["failed"]), (0, 1, 0))


class MainTest(unittest.TestCase):
    def test_off_unless_both_variables_are_set(self):
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.dict(os.environ, {"CORTEX_BRAIN_URL": "http://b"}, clear=False), \
                mock.patch.object(cortex_writer, "_post") as post:
            os.environ.pop("CORTEX_API_TOKEN", None)
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(cortex_writer.main(["--evidence", d, "--state", d]), 0)
        self.assertIn("cortex: off", out.getvalue())
        post.assert_not_called()

    def test_posts_learned_items_with_the_bearer_token(self):
        with tempfile.TemporaryDirectory() as d:
            write_ledger(d, [{"id": "a1", "status": "applied", "rec": rec()}])
            env = {"CORTEX_BRAIN_URL": "http://b/", "CORTEX_API_TOKEN": "tok"}
            seen = []

            def fake_urlopen(req, timeout):
                seen.append((req.full_url, req.get_header("Authorization"),
                             json.loads(req.data)))
                return io.BytesIO(b'{"status": "created"}')

            with mock.patch.dict(os.environ, env), \
                    mock.patch.object(cortex_writer.urllib.request, "urlopen", fake_urlopen), \
                    contextlib.redirect_stdout(io.StringIO()) as out:
                cortex_writer.main(["--evidence", d, "--state", d])
        self.assertIn("cortex: sent=1", out.getvalue())
        url, auth, body = seen[0]
        self.assertEqual((url, auth), ("http://b/v1/memory/learned", "Bearer tok"))
        self.assertEqual(body["key"], "self-optimize:a1")


if __name__ == "__main__":
    unittest.main()
