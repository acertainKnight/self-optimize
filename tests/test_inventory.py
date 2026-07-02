import json, sys, pathlib, tempfile, unittest
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "scripts"))
import inventory

SKILL_MD = "---\nname: used-skill\ndescription: a used skill\n---\nbody\n"
SKILL2_MD = "---\nname: dusty-skill\ndescription: never invoked\ndisable-model-invocation: true\n---\nbody\n"
AGENT_MD = "---\nname: helper\nmodel: opus\ndescription: does things\n---\nprompt\n"


def make_fake_claude(root: pathlib.Path):
    # settings + installed plugin with one skill, one agent, and hooks (settings + plugin)
    (root).mkdir(parents=True)
    (root / "settings.json").write_text(json.dumps({
        "model": "opusplan", "outputStyle": "explanatory",
        "enabledPlugins": {"toolkit@mp": True},
        "skillOverrides": {"dusty-skill": "off"},
        "env": {"SECRET_TOKEN": "must-never-appear"},
        "permissions": {"defaultMode": "acceptEdits"},
        "hooks": {"PreToolUse": [{"matcher": "Bash",
                                  "hooks": [{"type": "command", "command": "echo hi"}]}]}}))
    plug = root / "plugins" / "cache" / "mp" / "toolkit" / "1.0.0"
    (plug / "skills" / "used-skill").mkdir(parents=True)
    (plug / "skills" / "used-skill" / "SKILL.md").write_text(SKILL_MD)
    (plug / "skills" / "dusty-skill").mkdir(parents=True)
    (plug / "skills" / "dusty-skill" / "SKILL.md").write_text(SKILL2_MD)
    (plug / "agents").mkdir(parents=True)
    (plug / "agents" / "helper.md").write_text(AGENT_MD)
    (plug / ".claude-plugin").mkdir(parents=True)
    (plug / ".claude-plugin" / "plugin.json").write_text(json.dumps({"hooks": "hooks/hooks.json"}))
    (plug / "hooks").mkdir(parents=True)
    (plug / "hooks" / "hooks.json").write_text(json.dumps(
        {"PostToolUse": [{"matcher": "Write", "hooks": [{"type": "command", "command": "lint"}]}]}))
    (root / "plugins").mkdir(exist_ok=True)
    (root / "plugins" / "installed_plugins.json").write_text(json.dumps({
        "version": 2, "plugins": {"toolkit@mp": [{"installPath": str(plug), "version": "1.0.0"}]}}))
    (root / "skills" / "my-skill").mkdir(parents=True)
    (root / "skills" / "my-skill" / "SKILL.md").write_text(SKILL_MD.replace("used-skill", "my-skill"))
    (root / "CLAUDE.md").write_text("# global memory\n" * 20)
    # user MCP config, located inside the config dir (CLAUDE_CONFIG_DIR layout)
    (root / ".claude.json").write_text(json.dumps({"mcpServers": {"snowflake": {"url": "x"}}}))


class TestInventory(unittest.TestCase):
    def test_surface_scan_join_and_secret_safety(self):
        with tempfile.TemporaryDirectory() as d:
            root = pathlib.Path(d) / "claude"
            ev = pathlib.Path(d) / "ev"
            ev.mkdir()
            make_fake_claude(root)
            (ev / "activation.json").write_text(json.dumps(
                {"schema_version": "1", "harness": "claude-code",
                 "items": {"skill:used-skill": {"count": 5, "last_used": "2026-06-30", "projects": ["p"]}}}))
            inv = inventory.build_inventory(root, ev)
            ids = {s["id"] for s in inv["skills"]}
            self.assertIn("skill:used-skill", ids)
            self.assertIn("skill:dusty-skill", ids)
            self.assertIn("skill:my-skill", ids)
            self.assertIn("agent:helper", {a["id"] for a in inv["agents"]})
            self.assertIn("mcp:snowflake", {m["id"] for m in inv["mcp_servers"]})
            self.assertIn("skill:dusty-skill", inv["unused"])       # zero activations
            self.assertNotIn("skill:used-skill", inv["unused"])
            self.assertGreater(inv["base_context_est"], 0)
            dusty = next(s for s in inv["skills"] if s["id"] == "skill:dusty-skill")
            self.assertEqual(dusty["override"], "off")
            self.assertEqual(dusty["disable_model_invocation"], "true")
            self.assertNotIn("must-never-appear", json.dumps(inv))  # env allowlist holds
            self.assertTrue(inv["claude_md"][0]["id"].startswith("claude_md:"))

    def test_user_skill_shadows_plugin_skill_and_empty_installpath_skipped(self):
        with tempfile.TemporaryDirectory() as d:
            root = pathlib.Path(d) / "claude"
            ev = pathlib.Path(d) / "ev"
            ev.mkdir()
            make_fake_claude(root)
            (root / "skills" / "used-skill").mkdir(parents=True)
            (root / "skills" / "used-skill" / "SKILL.md").write_text(SKILL_MD)
            ip = json.loads((root / "plugins" / "installed_plugins.json").read_text())
            ip["plugins"]["ghost@mp"] = [{}]
            (root / "plugins" / "installed_plugins.json").write_text(json.dumps(ip))
            s = json.loads((root / "settings.json").read_text())
            s["enabledPlugins"]["ghost@mp"] = True
            (root / "settings.json").write_text(json.dumps(s))
            inv = inventory.build_inventory(root, ev)
            used = [x for x in inv["skills"] if x["id"] == "skill:used-skill"]
            self.assertEqual(len(used), 1)
            self.assertEqual(used[0]["source"], "user")

    def test_malformed_settings_degrades_gracefully(self):
        with tempfile.TemporaryDirectory() as d:
            root = pathlib.Path(d) / "claude"
            root.mkdir(parents=True)
            (root / "settings.json").write_text("{broken")
            inv = inventory.build_inventory(root, None)
            self.assertEqual(inv["plugins"], [])

    def test_hooks_inventory_from_settings_and_plugin_manifest(self):
        with tempfile.TemporaryDirectory() as d:
            root = pathlib.Path(d) / "claude"
            ev = pathlib.Path(d) / "ev"
            ev.mkdir()
            make_fake_claude(root)
            inv = inventory.build_inventory(root, ev)
            ids = {h["id"] for h in inv["hooks"]}
            self.assertIn("hook:settings", ids)
            self.assertIn("hook:plugin:toolkit@mp", ids)
            settings_hook = next(h for h in inv["hooks"] if h["id"] == "hook:settings")
            self.assertGreater(settings_hook["est_context_tokens"], 0)

    def test_main_writes_inventory_with_600_perms(self):
        with tempfile.TemporaryDirectory() as d:
            root = pathlib.Path(d) / "claude"
            ev = pathlib.Path(d) / "ev"
            ev.mkdir()
            make_fake_claude(root)
            inventory.main(["--data-root", str(root), "--state", str(pathlib.Path(d) / "st"),
                            "--out", str(ev)])
            mode = (ev / "inventory.json").stat().st_mode & 0o777
            self.assertEqual(mode, 0o600)


if __name__ == "__main__":
    unittest.main()
