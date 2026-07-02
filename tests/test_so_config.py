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


if __name__ == "__main__":
    unittest.main()
