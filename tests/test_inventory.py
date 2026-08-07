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

    def test_settings_allowlist_includes_auto_memory_directory(self):
        with tempfile.TemporaryDirectory() as d:
            root = pathlib.Path(d) / "claude"
            make_fake_claude(root)
            s = json.loads((root / "settings.json").read_text())
            s["autoMemoryDirectory"] = "/Users/someone/.claude/memory"
            (root / "settings.json").write_text(json.dumps(s))
            inv = inventory.build_inventory(root, None)
            self.assertEqual(inv["settings"]["autoMemoryDirectory"], "/Users/someone/.claude/memory")

    def test_available_plugins_excludes_installed_and_parses_catalog(self):
        with tempfile.TemporaryDirectory() as d:
            root = pathlib.Path(d) / "claude"
            make_fake_claude(root)
            (root / "plugins" / "plugin-catalog-cache.json").write_text(json.dumps({
                "catalog": {"plugins": {
                    "toolkit@mp": {"marketplace_entry": {"description": "installed already"}},
                    "shiny@mp2": {"marketplace_entry": {"name": "shiny", "description": "does shiny things"}},
                }}}))
            inv = inventory.build_inventory(root, None)
            ids = {p["id"] for p in inv["available_plugins"]}
            self.assertIn("availplugin:shiny@mp2", ids)
            self.assertNotIn("availplugin:toolkit@mp", ids)   # already installed
            shiny = next(p for p in inv["available_plugins"] if p["id"] == "availplugin:shiny@mp2")
            self.assertEqual(shiny["marketplace"], "mp2")
            self.assertEqual(shiny["description"], "does shiny things")

    def test_available_plugins_guarded_parse_on_malformed_cache(self):
        with tempfile.TemporaryDirectory() as d:
            root = pathlib.Path(d) / "claude"
            make_fake_claude(root)
            (root / "plugins" / "plugin-catalog-cache.json").write_text("{not json")
            inv = inventory.build_inventory(root, None)
            self.assertEqual(inv["available_plugins"], [])

    def test_build_artifacts_top_activated_user_skills_and_any_source_agents(self):
        with tempfile.TemporaryDirectory() as d:
            root = pathlib.Path(d) / "claude"
            ev = pathlib.Path(d) / "ev"
            ev.mkdir()
            make_fake_claude(root)
            (ev / "activation.json").write_text(json.dumps(
                {"schema_version": "1", "harness": "claude-code",
                 "items": {"skill:my-skill": {"count": 9, "last_used": "2026-06-30", "projects": ["p"]},
                           "skill:used-skill": {"count": 2, "last_used": "2026-06-30", "projects": ["p"]},
                           "agent:helper": {"count": 4, "last_used": "2026-06-30", "projects": ["p"]}}}))
            inv = inventory.build_inventory(root, ev)
            art = inventory.build_artifacts(inv, ev)
            ids = [a["id"] for a in art["artifacts"]]
            self.assertEqual(len(art["artifacts"]), 2)
            self.assertEqual(ids[0], "artifact:skill:my-skill")     # highest activation first
            self.assertIn("artifact:agent:helper", ids)
            self.assertNotIn("artifact:skill:used-skill", ids)      # plugin-owned skill excluded
            my = next(a for a in art["artifacts"] if a["id"] == "artifact:skill:my-skill")
            self.assertEqual(my["source"], "user")
            self.assertLessEqual(len(my["body"]), 8000)

    def test_build_artifacts_symlinked_flag(self):
        with tempfile.TemporaryDirectory() as d:
            root = pathlib.Path(d) / "claude"
            ev = pathlib.Path(d) / "ev"
            ev.mkdir()
            make_fake_claude(root)
            real = pathlib.Path(d) / "elsewhere" / "sneaky"
            real.mkdir(parents=True)
            (real / "SKILL.md").write_text(SKILL_MD.replace("used-skill", "sneaky"))
            (root / "skills" / "sneaky").symlink_to(real, target_is_directory=True)
            (ev / "activation.json").write_text(json.dumps(
                {"schema_version": "1", "harness": "claude-code", "items": {}}))
            inv = inventory.build_inventory(root, ev)
            art = inventory.build_artifacts(inv, ev)
            by_id = {a["id"]: a for a in art["artifacts"]}
            self.assertTrue(by_id["artifact:skill:sneaky"]["symlinked"])
            self.assertFalse(by_id["artifact:skill:my-skill"]["symlinked"])

    def test_main_writes_artifacts_with_600_perms(self):
        with tempfile.TemporaryDirectory() as d:
            root = pathlib.Path(d) / "claude"
            ev = pathlib.Path(d) / "ev"
            ev.mkdir()
            make_fake_claude(root)
            (ev / "activation.json").write_text(json.dumps(
                {"schema_version": "1", "harness": "claude-code", "items": {}}))
            inventory.main(["--data-root", str(root), "--state", str(pathlib.Path(d) / "st"),
                            "--out", str(ev)])
            mode = (ev / "artifacts.json").stat().st_mode & 0o777
            self.assertEqual(mode, 0o600)
            art = json.loads((ev / "artifacts.json").read_text())
            self.assertEqual(art["schema_version"], "1")

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

    def test_hooks_manifest_pointing_at_directory_degrades_gracefully(self):
        with tempfile.TemporaryDirectory() as d:
            root = pathlib.Path(d) / "claude"
            ev = pathlib.Path(d) / "ev"
            ev.mkdir()
            make_fake_claude(root)
            plug = root / "plugins" / "cache" / "mp" / "toolkit" / "1.0.0"
            # manifest's hooks path is a DIRECTORY — read_text would raise IsADirectoryError
            (plug / ".claude-plugin" / "plugin.json").write_text(json.dumps({"hooks": "hooks"}))
            inv = inventory.build_inventory(root, ev)
            ids = {h["id"] for h in inv["hooks"]}
            self.assertIn("hook:settings", ids)               # rest of inventory intact
            self.assertNotIn("hook:plugin:toolkit@mp", ids)   # unreadable → entry skipped

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

    def test_guidance_surface_scrubbed_and_listed(self):
        with tempfile.TemporaryDirectory() as d:
            root = pathlib.Path(d) / "claude"
            make_fake_claude(root)
            (root / "CLAUDE.md").write_text("## Rules\n- be terse\n")
            mem = root / "auto-memory"
            mem.mkdir()
            (mem / "old-note.md").write_text(
                "api key sk-ant-api03-" + "a" * 90 + " stays secret\n")
            inv = inventory.build_inventory(root, None)
            kinds = {g["kind"] for g in inv["guidance"]}
            self.assertEqual(kinds, {"claude_md", "memory"})
            mem_entry = next(g for g in inv["guidance"] if g["kind"] == "memory")
            self.assertNotIn("sk-ant-api03", mem_entry["body"])
            self.assertTrue(mem_entry["id"].startswith("guidance:"))

    def test_analyst_copies_are_per_analyst_and_600(self):
        with tempfile.TemporaryDirectory() as d:
            root = pathlib.Path(d) / "claude"
            ev = pathlib.Path(d) / "ev"
            ev.mkdir()
            make_fake_claude(root)
            (ev / "sessions.json").write_text('{"sessions": []}')
            (ev / "activation.json").write_text('{"items": {}}')
            for name in ("usage", "samples", "constraints", "papercuts"):
                (ev / f"{name}.json").write_text("{}")
            rules = pathlib.Path(d) / "rules.json"
            rules.write_text('{"rules": []}')
            inventory.main(["--data-root", str(root), "--state", str(pathlib.Path(d) / "st"),
                            "--out", str(ev), "--rules", str(rules)])
            for analyst, names in inventory.ANALYST_FILES.items():
                for name in names:
                    f = ev / "analysts" / analyst / name
                    self.assertTrue(f.exists(), f"{analyst}/{name} missing")
                    self.assertEqual(f.stat().st_mode & 0o777, 0o600)
            self.assertTrue((ev / "analysts" / "auditor" / "rules.json").exists())
            self.assertTrue((ev / "analysts" / "evolver" / "rules.json").exists())
            self.assertFalse((ev / "analysts" / "miner" / "rules.json").exists())
            self.assertFalse((ev / "analysts" / "miner" / "inventory.json").exists())


if __name__ == "__main__":
    unittest.main()
