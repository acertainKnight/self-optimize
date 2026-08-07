import json, shutil, sys, pathlib, tempfile, unittest
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "scripts"))
import collect
import so_config

FIX = pathlib.Path(__file__).parent / "fixtures" / "basic_session.jsonl"
CAPS = {"excerpts": 40, "tokens_per_excerpt": 1500, "total_tokens": 60000}
SINCE = "2026-01-01T00:00:00Z"   # fixed window: the default is now-30d, which moves

# A stand-in adapter: same CLI contract as the real ones (--out/--since), writes
# whatever pack the test hands it, or exits 1 on demand. Keeps these tests off the
# machine's actual Codex/opencode installs.
FAKE_ADAPTER = '''import argparse, json, pathlib, sys
ap = argparse.ArgumentParser()
ap.add_argument("--out", required=True)
ap.add_argument("--since", default=None)
ap.add_argument("--pack", default=None)
ap.add_argument("--fail", action="store_true")
a = ap.parse_args()
if a.fail:
    sys.stderr.write("boom: adapter exploded\\n")
    raise SystemExit(1)
out = pathlib.Path(a.out)
out.mkdir(parents=True, exist_ok=True)
for fname, doc in json.loads(pathlib.Path(a.pack).read_text()).items():
    (out / fname).write_text(json.dumps(doc))
'''


def fake_pack(n, project="proj", day="01", limits=("estimates only",)):
    sessions = [{"id": f"s{i}", "project": project, "cwd": None, "harness_version": None,
                 "started_at": f"2026-06-{day}T00:00:00Z", "ended_at": f"2026-06-{day}T00:10:00Z",
                 "turns": 2, "input_tokens": 1, "output_tokens": 2, "cache_read": 0,
                 "cache_write": 0, "sidechain_output_tokens": 0, "models": {},
                 "corrections_count": 1, "duplicate_reads": 0, "repeated_calls": 0,
                 "permission_stalls": 0, "revert_events": 0, "reasks": 0,
                 "ended_on_correction": 0, "redactions": 0} for i in range(n)]
    samples = [{"session": f"s{i}", "project": project, "ts": f"2026-06-{day}T00:05:00Z",
                "kind": "correction", "pattern": "no", "user_text": f"no not like that {i}",
                "prior_assistant_text": "did a thing"} for i in range(n)]
    usage = {"window": {"since": None, "until": f"2026-06-{day}T00:10:00Z"},
             "totals": {"sessions": n, "turns": 2 * n, "input_tokens": n,
                        "output_tokens": 2 * n, "cache_read": 0, "cache_write": 0},
             "per_project": {project: {"sessions": n, "output_tokens": 2 * n}},
             "per_model": {}, "corrections_by_model": {},
             "waste": {"duplicate_reads_total": 0, "repeated_calls_total": 0,
                       "permission_stalls_total": 0, "main_model_heavy_sessions": 0,
                       "revert_events_total": 0, "reasks_total": 0,
                       "ended_on_correction_total": 0, "top_duplicate_read_paths": [],
                       "top_stalled_tools": [], "stall_examples": []},
             "corrections": {"total": n, "rate_per_session": 1.0},
             "parse": {"skipped_lines": 0, "files": n, "redactions": 0,
                       "collector_limits": list(limits)}}
    return {"sessions": sessions, "samples": samples, "working": [],
            "activation": {"items": {}}, "usage": usage}


def pack_docs(pack):
    return {"sessions.json": {"sessions": pack["sessions"]},
            "samples.json": {"samples": pack["samples"]},
            "usage.json": pack["usage"]}


class TestMerge(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def test_single_harness_pack_is_unchanged_apart_from_the_harness_field(self):
        pack = fake_pack(3)
        merged = collect.merge_packs([("claude-code", pack)], CAPS)
        self.assertEqual([dict(s, harness=None) for s in merged["sessions"]],
                         [dict(s, harness=None) for s in pack["sessions"]])
        self.assertEqual({s["harness"] for s in merged["sessions"]}, {"claude-code"})
        self.assertEqual(len(merged["samples"]), 3)
        usage = dict(merged["usage"])
        per_harness = usage.pop("per_harness")
        expected = json.loads(json.dumps(pack["usage"]))
        expected["parse"]["collector_limits"] = ["claude-code: estimates only"]
        self.assertEqual(usage, expected)
        self.assertEqual(per_harness, {"claude-code": {"status": "ok", "sessions": 3,
                                                       "corrections": 3, "samples": 3}})

    def test_merge_is_deterministic_and_order_independent(self):
        a, b = fake_pack(3, "pa", "01"), fake_pack(2, "pb", "02")
        one = collect.merge_packs([("claude-code", a), ("codex", b)], CAPS)
        two = collect.merge_packs([("codex", b), ("claude-code", a)], CAPS)
        self.assertEqual(json.dumps(one, indent=1), json.dumps(two, indent=1))
        # sorted harness order, and every row says where it came from
        self.assertEqual([s["harness"] for s in one["sessions"]],
                         ["claude-code"] * 3 + ["codex"] * 2)
        self.assertEqual({s["harness"] for s in one["samples"]}, {"claude-code", "codex"})
        self.assertEqual(one["usage"]["totals"]["sessions"], 5)
        self.assertEqual(one["usage"]["corrections"]["total"], 5)
        self.assertEqual(one["usage"]["per_project"], {"pa": {"sessions": 3, "output_tokens": 6},
                                                       "pb": {"sessions": 2, "output_tokens": 4}})
        self.assertEqual(one["usage"]["parse"]["collector_limits"],
                         ["claude-code: estimates only", "codex: estimates only"])

    def test_per_harness_counts_land_in_usage(self):
        merged = collect.merge_packs([("claude-code", fake_pack(3, "pa")),
                                      ("codex", fake_pack(2, "pb"))], CAPS,
                                     failures={"opencode": "boom"})
        self.assertEqual(merged["usage"]["per_harness"], {
            "claude-code": {"status": "ok", "sessions": 3, "corrections": 3, "samples": 3},
            "codex": {"status": "ok", "sessions": 2, "corrections": 2, "samples": 2},
            "opencode": {"status": "failed", "sessions": 0, "corrections": 0,
                         "samples": 0, "error": "boom"}})

    def test_proportional_sampling_leaves_the_quiet_harness_a_share(self):
        # same project name and the chatty harness's excerpts are strictly newer, so
        # project-proportional capping alone would hand it all 10 slots
        chatty = fake_pack(20, "proj", "02")
        quiet = fake_pack(2, "proj", "01")
        caps = {"excerpts": 10, "tokens_per_excerpt": 1500, "total_tokens": 60000}
        merged = collect.merge_packs([("claude-code", chatty), ("codex", quiet)], caps)
        counts = {"claude-code": 0, "codex": 0}
        for s in merged["samples"]:
            counts[s["harness"]] += 1
        self.assertEqual(sum(counts.values()), 10)
        self.assertEqual(counts, {"claude-code": 9, "codex": 1})

    def test_harness_budgets_sum_to_total_and_floor_at_one(self):
        self.assertEqual(collect._harness_budgets(40, {"a": 7}), {"a": 40})
        split = collect._harness_budgets(10, {"a": 100, "b": 1})
        self.assertEqual(sum(split.values()), 10)
        self.assertEqual(split["b"], 1)
        even = collect._harness_budgets(10, {"a": 5, "b": 5})
        self.assertEqual(even, {"a": 5, "b": 5})
        # more harnesses than slots: everyone still gets one, nobody gets zero
        tight = collect._harness_budgets(2, {"a": 9, "b": 1, "c": 1})
        self.assertEqual(set(tight.values()), {1})
        # a zero cap means zero samples, not one per harness
        self.assertEqual(collect._harness_budgets(0, {"a": 3, "b": 1}), {"a": 0, "b": 0})

    def test_read_harness_pack_degrades_on_missing_and_broken_files(self):
        d = pathlib.Path(self.tmp) / "hp"
        d.mkdir()
        empty = collect.read_harness_pack(d)
        self.assertEqual(empty["sessions"], [])
        self.assertEqual(empty["usage"], {})
        (d / "sessions.json").write_text("{ not json")
        (d / "samples.json").write_text(json.dumps({"samples": ["nope", {"ok": 1}]}))
        pack = collect.read_harness_pack(d)
        self.assertEqual(pack["sessions"], [])
        self.assertEqual(pack["samples"], [{"ok": 1}])

    def test_harness_exit_code_only_fires_when_everything_failed(self):
        self.assertEqual(collect.harness_exit_code({}), 0)
        self.assertEqual(collect.harness_exit_code({"a": {"status": "ok"},
                                                    "b": {"status": "failed"}}), 0)
        self.assertEqual(collect.harness_exit_code({"a": {"status": "failed"},
                                                    "b": {"status": "failed"}}), 1)


class TestDiscovery(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def test_discovery_probes_roots_and_honors_config(self):
        home = self.tmp / "codex-home"
        home.mkdir()
        cfg = {"harnesses": {"codex": {"home": str(home)},
                             "opencode": {"home": str(self.tmp / "nope")}}}
        specs = collect.discover_harnesses(cfg)
        self.assertEqual([s["name"] for s in specs], ["codex"])
        self.assertIn("--codex-home", specs[0]["argv"])
        self.assertIn(str(home), specs[0]["argv"])
        self.assertTrue(specs[0]["argv"][1].endswith("adapters/codex/collect_codex.py"))
        # explicitly disabled -> not probed at all
        cfg["harnesses"]["codex"]["enabled"] = False
        self.assertEqual(collect.discover_harnesses(cfg), [])

    def test_run_adapter_reports_failure_instead_of_raising(self):
        script = self.tmp / "fake.py"
        script.write_text(FAKE_ADAPTER)
        spec = {"name": "codex", "argv": [sys.executable, str(script), "--fail"]}
        r = collect.run_adapter(spec, self.tmp / "harness")
        self.assertFalse(r["ok"])
        self.assertIn("adapter exploded", r["error"])
        missing = collect.run_adapter({"name": "codex", "argv": [str(self.tmp / "gone")]},
                                      self.tmp / "harness")
        self.assertFalse(missing["ok"])


class TestCollectMain(unittest.TestCase):
    """End-to-end: one collect.py invocation, several harnesses, one merged pack."""

    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        self.root = self.tmp / "data"
        (self.root / "projects" / "-tmp-projA").mkdir(parents=True)
        shutil.copy(FIX, self.root / "projects" / "-tmp-projA" / "s1.jsonl")
        self.state = self.tmp / "state"
        self.script = self.tmp / "fake.py"
        self.script.write_text(FAKE_ADAPTER)
        self._real_discover = collect.discover_harnesses

    def tearDown(self):
        collect.discover_harnesses = self._real_discover
        shutil.rmtree(self.tmp)

    def _adapter(self, name, n, fail=False, day="01"):
        argv = [sys.executable, str(self.script)]
        if fail:
            return {"name": name, "argv": argv + ["--fail"]}
        pf = self.tmp / f"{name}-pack.json"
        pf.write_text(json.dumps(pack_docs(fake_pack(n, f"{name}-proj", day))))
        return {"name": name, "argv": argv + ["--pack", str(pf)]}

    def _run(self, specs, out):
        collect.discover_harnesses = lambda cfg, plugin_root=None: specs
        collect.main(["--data-root", str(self.root), "--state", str(self.state),
                      "--out", str(out), "--run-id", "r1", "--since", SINCE])
        return {f.name: json.loads(f.read_text()) for f in sorted(out.glob("*.json"))}

    def test_single_harness_fallback_is_the_pre_merge_pack(self):
        out = self.tmp / "ev-solo"
        files = self._run([], out)
        expected = collect.collect_corpus(self.root, SINCE, ["*"], [],
                                          collect.DEFAULT_CORRECTION_RE,
                                          so_config.DEFAULTS["sample_caps"])
        self.assertEqual([dict(s, harness=None) for s in files["sessions.json"]["sessions"]],
                         [dict(s, harness=None) for s in expected["sessions"]])
        self.assertEqual(files["usage.json"]["totals"], expected["usage"]["totals"])
        self.assertEqual(files["usage.json"]["waste"]["top_duplicate_read_paths"],
                         [list(kv) for kv in expected["usage"]["waste"]["top_duplicate_read_paths"]])
        self.assertEqual(files["usage.json"]["per_harness"],
                         {"claude-code": {"status": "ok", "sessions": 1,
                                          "corrections": 1, "samples": 1}})
        self.assertFalse((out / "harness").exists())

    def test_merged_run_across_three_harnesses(self):
        out = self.tmp / "ev-multi"
        files = self._run([self._adapter("codex", 4), self._adapter("opencode", 2)], out)
        self.assertEqual(files["usage.json"]["totals"]["sessions"], 7)
        self.assertEqual({s["harness"] for s in files["sessions.json"]["sessions"]},
                         {"claude-code", "codex", "opencode"})
        self.assertEqual({s["harness"] for s in files["samples.json"]["samples"]},
                         {"claude-code", "codex", "opencode"})
        self.assertEqual(files["usage.json"]["per_harness"]["codex"]["sessions"], 4)
        self.assertEqual(files["usage.json"]["per_harness"]["opencode"]["sessions"], 2)
        # each adapter's own output survives under EV/harness/<name>/
        self.assertTrue((out / "harness" / "codex" / "sessions.json").exists())
        # every working row is stamped too, and the analysts' files still parse
        for row in files["working.json"]["samples"]:
            self.assertIn("harness", row)

    def test_merged_run_is_idempotent(self):
        a = self._run([self._adapter("codex", 4)], self.tmp / "ev-a")
        b = self._run([self._adapter("codex", 4)], self.tmp / "ev-b")
        self.assertEqual(json.dumps(a, indent=1), json.dumps(b, indent=1))

    def test_one_adapter_failing_does_not_take_the_run_down(self):
        out = self.tmp / "ev-partial"
        files = self._run([self._adapter("codex", 4), self._adapter("opencode", 0, fail=True)],
                          out)
        per = files["usage.json"]["per_harness"]
        self.assertEqual(per["codex"]["status"], "ok")
        self.assertEqual(per["opencode"]["status"], "failed")
        self.assertIn("adapter exploded", per["opencode"]["error"])
        self.assertEqual(per["claude-code"]["status"], "ok")
        self.assertEqual(files["usage.json"]["totals"]["sessions"], 5)

    def test_papercuts_are_read_once_at_the_merged_level(self):
        pc = self.tmp / "papercuts.md"
        pc.write_text("- 2026-08-01 claude-code seeded friction line\n")
        so_config.load_config(self.state)  # creates config.json with defaults
        cfg_path = self.state / "config.json"
        cfg = json.loads(cfg_path.read_text())
        cfg["papercuts_path"] = str(pc)
        cfg_path.write_text(json.dumps(cfg))
        out = self.tmp / "ev-papercuts"
        files = self._run([], out)
        self.assertEqual(len(files["papercuts.json"]["lines"]), 1)
        self.assertIn("seeded friction line", files["papercuts.json"]["lines"][0]["text"])

    def test_absent_papercuts_file_is_a_silent_noop(self):
        # point at a definitely-nonexistent path within tmp, not the default
        # $HOME/papercuts.md — this must not depend on the test runner's actual home
        so_config.load_config(self.state)
        cfg_path = self.state / "config.json"
        cfg = json.loads(cfg_path.read_text())
        cfg["papercuts_path"] = str(self.tmp / "nope" / "papercuts.md")
        cfg_path.write_text(json.dumps(cfg))
        out = self.tmp / "ev-no-papercuts"
        files = self._run([], out)
        self.assertEqual(files["papercuts.json"]["lines"], [])

    def test_every_harness_failing_exits_non_zero(self):
        def boom(*args, **kwargs):
            raise RuntimeError("corpus walk died")
        real = collect.collect_corpus
        collect.collect_corpus = boom
        try:
            with self.assertRaises(SystemExit) as cm:
                self._run([self._adapter("codex", 0, fail=True)], self.tmp / "ev-dead")
        finally:
            collect.collect_corpus = real
        self.assertEqual(cm.exception.code, 1)
        usage = json.loads((self.tmp / "ev-dead" / "usage.json").read_text())
        self.assertEqual(usage["per_harness"]["claude-code"]["status"], "failed")
        self.assertIn("corpus walk died", usage["per_harness"]["claude-code"]["error"])


if __name__ == "__main__":
    unittest.main()
