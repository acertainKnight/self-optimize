import json, os, sys, pathlib, tempfile, unittest
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "scripts"))
import so_config


class TestConfig(unittest.TestCase):
    def test_creates_defaults_then_merges_overrides(self):
        with tempfile.TemporaryDirectory() as d:
            state = pathlib.Path(d) / "so"
            cfg = so_config.load_config(state)
            self.assertEqual(cfg["since_days"], 30)
            self.assertTrue((state / "config.json").exists())
            # user edits one key; reload merges over defaults
            on_disk = json.loads((state / "config.json").read_text())
            on_disk["since_days"] = 7
            (state / "config.json").write_text(json.dumps(on_disk))
            cfg2 = so_config.load_config(state)
            self.assertEqual(cfg2["since_days"], 7)
            self.assertEqual(cfg2["verify"]["min_sessions"], 10)

    def test_config_dir_and_resolve_honor_instance(self):
        old = os.environ.get("CLAUDE_CONFIG_DIR")
        os.environ["CLAUDE_CONFIG_DIR"] = "/tmp/claude-x"
        try:
            self.assertEqual(so_config.config_dir(), pathlib.Path("/tmp/claude-x"))
            d, st = so_config.resolve(None, None)
            self.assertEqual((d, st), (pathlib.Path("/tmp/claude-x"),
                                       pathlib.Path("/tmp/claude-x") / "self-optimize"))
            d2, st2 = so_config.resolve("/data", None)
            self.assertEqual((d2, st2), (pathlib.Path("/data"),
                                         pathlib.Path("/data") / "self-optimize"))
        finally:
            if old is None:
                os.environ.pop("CLAUDE_CONFIG_DIR", None)
            else:
                os.environ["CLAUDE_CONFIG_DIR"] = old

    def test_harness_roots_default_expand_override_and_disable(self):
        roots = so_config.harness_roots(so_config.DEFAULTS)
        self.assertEqual(sorted(roots), ["codex", "opencode"])
        self.assertEqual(roots["codex"]["home"], pathlib.Path.home() / ".codex")
        self.assertEqual(roots["opencode"]["config"], pathlib.Path.home() / ".config" / "opencode")
        # a partial override keeps the other default keys, and omitting "enabled"
        # does not disable the harness (config.json merges one level deep)
        cfg = {"harnesses": {"codex": {"home": "/opt/codex"},
                             "opencode": {"enabled": False}}}
        roots = so_config.harness_roots(cfg)
        self.assertEqual(sorted(roots), ["codex"])
        self.assertEqual(roots["codex"]["home"], pathlib.Path("/opt/codex"))

    def test_harness_defaults_reach_config_json(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = so_config.load_config(pathlib.Path(d) / "so")
            self.assertTrue(cfg["harnesses"]["codex"]["enabled"])
            self.assertEqual(cfg["harnesses"]["opencode"]["home"], "~/.local/share/opencode")

    def test_project_weights_dead_key_deleted(self):
        self.assertNotIn("project_weights", so_config.DEFAULTS)
        with tempfile.TemporaryDirectory() as d:
            cfg = so_config.load_config(pathlib.Path(d) / "so")
            self.assertNotIn("project_weights", cfg)


if __name__ == "__main__":
    unittest.main()
