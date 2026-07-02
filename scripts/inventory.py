"""Config-surface collector: plugins, skills, agents, MCP servers, CLAUDE.md files,
allowlisted settings. Joins activation counts into an unused/rare list.
NEVER reads settings 'env' or other secret-bearing keys — allowlist only."""
import argparse
import json
from pathlib import Path

import schema as so_schema

SETTINGS_ALLOWLIST = ["model", "outputStyle", "effortLevel"]


def _est(text: str) -> int:
    return len(text) // 4


def _frontmatter_block(text: str) -> str:
    if not text.startswith("---"):
        return ""
    end = text.find("\n---", 3)
    return text[:end] if end != -1 else ""


def _scan_skill_dir(base: Path, source: str, overrides: dict) -> list:
    out = []
    for md in sorted(base.glob("*/SKILL.md")):
        text = md.read_text(errors="replace")
        fm = so_schema.parse_frontmatter(text)
        name = fm.get("name", md.parent.name)
        out.append({"id": f"skill:{name}", "name": name, "source": source,
                    "path": str(md),
                    "description": fm.get("description", "")[:200],
                    "disable_model_invocation": fm.get("disable-model-invocation", "false"),
                    "override": overrides.get(name),
                    "est_context_tokens": _est(_frontmatter_block(text))})
    return out


def _scan_agent_dir(base: Path, source: str) -> list:
    out = []
    for md in sorted(base.glob("*.md")):
        text = md.read_text(errors="replace")
        fm = so_schema.parse_frontmatter(text)
        name = fm.get("name", md.stem)
        out.append({"id": f"agent:{name}", "name": name, "source": source,
                    "path": str(md), "model": fm.get("model", "inherit"),
                    "est_context_tokens": _est(text)})
    return out


def _dedupe(items: list) -> list:
    """User-level entries shadow plugin entries of the same id (documented precedence)."""
    by_id = {}
    for it in items:
        prev = by_id.get(it["id"])
        if prev is None or (prev["source"] != "user" and it["source"] == "user"):
            by_id[it["id"]] = it
    return list(by_id.values())


def _scan_hooks(settings: dict, enabled: dict, installed: dict) -> list:
    """Token-cost visibility only — hook bodies are never rendered into a rec or
    report, just estimated, since a command's own arguments could carry secrets."""
    out = []
    hooks_block = settings.get("hooks") or {}
    if hooks_block:
        out.append({"id": "hook:settings", "source": "user",
                    "est_context_tokens": _est(json.dumps(hooks_block))})
    for pid, entries in installed.items():
        if not enabled.get(pid, False):
            continue
        entry = entries[0] if isinstance(entries, list) and entries else {}
        install_path = entry.get("installPath") or ""
        install = Path(install_path)
        if not install_path or not install.exists():
            continue
        pj = install / ".claude-plugin" / "plugin.json"
        if not pj.exists():
            continue
        try:
            manifest = json.loads(pj.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        hooks_field = manifest.get("hooks")
        if not hooks_field:
            continue
        text = json.dumps(hooks_field)
        if isinstance(hooks_field, str):
            hf = install / hooks_field
            text = hf.read_text(errors="replace") if hf.exists() else ""
        if text:
            out.append({"id": f"hook:plugin:{pid}", "source": f"plugin:{pid}",
                        "est_context_tokens": _est(text)})
    return out


def build_inventory(data_root: Path, evidence_dir: Path | None) -> dict:
    data_root = Path(data_root)
    settings = {}
    sfile = data_root / "settings.json"
    if sfile.exists():
        try:
            settings = json.loads(sfile.read_text())
        except (json.JSONDecodeError, OSError):
            settings = {}
    overrides = settings.get("skillOverrides", {}) or {}
    enabled = settings.get("enabledPlugins", {}) or {}

    plugins, skills, agents, mcp = [], [], [], []
    ipath = data_root / "plugins" / "installed_plugins.json"
    installed = json.loads(ipath.read_text())["plugins"] if ipath.exists() else {}
    for pid, entries in installed.items():
        entry = entries[0] if isinstance(entries, list) and entries else {}
        install_path = entry.get("installPath") or ""
        install = Path(install_path)
        is_on = bool(enabled.get(pid, False))
        plugins.append({"id": f"plugin:{pid}", "enabled": is_on,
                        "version": entry.get("version"),
                        "install_path": install_path or None})
        if not is_on or not install_path or not install.exists():
            continue
        skills += _scan_skill_dir(install / "skills", f"plugin:{pid}", overrides)
        agents += _scan_agent_dir(install / "agents", f"plugin:{pid}")
        mcp_file = install / ".mcp.json"
        if mcp_file.exists():
            for name in (json.loads(mcp_file.read_text()).get("mcpServers") or {}):
                mcp.append({"id": f"mcp:{name}", "name": name, "source": f"plugin:{pid}",
                            "est_context_tokens": _est(mcp_file.read_text())})
    skills += _scan_skill_dir(data_root / "skills", "user", overrides)
    agents += _scan_agent_dir(data_root / "agents", "user")
    skills = _dedupe(skills)
    agents = _dedupe(agents)

    # .claude.json sits INSIDE the config dir when CLAUDE_CONFIG_DIR is set,
    # else as ~/.claude.json next to the default ~/.claude
    cj = data_root / ".claude.json"
    if not cj.exists():
        cj = data_root.parent / ".claude.json"
    if cj.exists():
        try:
            servers = json.loads(cj.read_text()).get("mcpServers", {}) or {}
            for name, scfg in servers.items():
                mcp.append({"id": f"mcp:{name}", "name": name, "source": "user",
                            "est_context_tokens": _est(json.dumps(scfg))})
        except (json.JSONDecodeError, OSError):
            pass
    mcp = _dedupe(mcp)

    claude_md = []
    gmd = data_root / "CLAUDE.md"
    if gmd.exists():
        claude_md.append({"id": f"claude_md:{str(gmd)}", "path": str(gmd),
                          "bytes": gmd.stat().st_size, "est_tokens": gmd.stat().st_size // 4})
    if evidence_dir and (Path(evidence_dir) / "sessions.json").exists():
        cwds = {s.get("cwd") for s in
                json.loads((Path(evidence_dir) / "sessions.json").read_text())["sessions"]}
        for cwd in sorted(c for c in cwds if c):
            cmd = Path(cwd) / "CLAUDE.md"
            if cmd.exists():
                claude_md.append({"id": f"claude_md:{str(cmd)}", "path": str(cmd),
                                  "bytes": cmd.stat().st_size, "est_tokens": cmd.stat().st_size // 4})

    act = {}
    if evidence_dir and (Path(evidence_dir) / "activation.json").exists():
        act = json.loads((Path(evidence_dir) / "activation.json").read_text())["items"]
    unused, rare = [], []
    for item in skills + agents + mcp:
        n = act.get(item["id"], {}).get("count", 0)
        if n == 0:
            unused.append(item["id"])
        elif n <= 2:
            rare.append(item["id"])

    base = (sum(s["est_context_tokens"] for s in skills if s.get("override") != "off")
            + sum(a["est_context_tokens"] for a in agents)
            + sum(m["est_context_tokens"] for m in mcp)
            + sum(c["est_tokens"] for c in claude_md))

    perms = settings.get("permissions", {}) or {}
    hooks = _scan_hooks(settings, enabled, installed)
    return {"schema_version": so_schema.EVIDENCE_VERSION, "harness": so_schema.HARNESS,
            "plugins": plugins, "skills": skills, "agents": agents, "mcp_servers": mcp,
            "claude_md": claude_md, "hooks": hooks,
            "settings": {**{k: settings.get(k) for k in SETTINGS_ALLOWLIST},
                         "permissions_default_mode": perms.get("defaultMode")},
            "base_context_est": base, "unused": unused, "rare": rare}


def update_last_metrics(state_dir: Path, **fields) -> None:
    p = Path(state_dir) / "state" / "metrics.jsonl"
    if not p.exists():
        return
    lines = p.read_text().splitlines()
    if not lines:
        return
    row = json.loads(lines[-1])
    row.update(fields)
    lines[-1] = json.dumps(row)
    p.write_text("\n".join(lines) + "\n")


def main(argv=None):
    import so_config
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-root", default=None)
    ap.add_argument("--state", default=None)
    ap.add_argument("--out", required=True)
    a = ap.parse_args(argv)
    data_root, state = so_config.resolve(a.data_root, a.state)
    inv = build_inventory(data_root, Path(a.out))
    out_file = Path(a.out) / "inventory.json"
    out_file.write_text(json.dumps(inv, indent=1))
    out_file.chmod(0o600)
    update_last_metrics(state, base_context_est=inv["base_context_est"],
                        unused_surface_count=len(inv["unused"]))
    print(f"skills={len(inv['skills'])} agents={len(inv['agents'])} mcp={len(inv['mcp_servers'])} "
          f"unused={len(inv['unused'])} base_context_est={inv['base_context_est']}")


if __name__ == "__main__":
    main()
