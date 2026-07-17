"""Config-surface collector: plugins, skills, agents, MCP servers, CLAUDE.md files,
allowlisted settings. Joins activation counts into an unused/rare list.
NEVER reads settings 'env' or other secret-bearing keys — allowlist only."""
import argparse
import json
from pathlib import Path

import redact
import schema as so_schema

SETTINGS_ALLOWLIST = ["model", "outputStyle", "effortLevel", "autoMemoryDirectory"]


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


def _available_plugins(data_root: Path, installed: dict) -> list:
    """Guarded parse: the catalog cache is Claude Code's own cache file, not ours —
    any shape drift must degrade to an empty list, never crash the collector."""
    cache_f = data_root / "plugins" / "plugin-catalog-cache.json"
    if not cache_f.exists():
        return []
    try:
        catalog = json.loads(cache_f.read_text(errors="replace"))["catalog"]["plugins"]
    except (json.JSONDecodeError, OSError, KeyError, TypeError):
        return []
    if not isinstance(catalog, dict):
        return []
    out = []
    for pid, entry in catalog.items():
        if pid in installed or not isinstance(entry, dict):
            continue
        name, _, marketplace = pid.rpartition("@")
        me = entry.get("marketplace_entry") or {}
        out.append({"id": f"availplugin:{pid}", "name": name or pid,
                    "marketplace": marketplace, "description": (me.get("description") or "")[:200]})
    return out


def build_artifacts(inv: dict, evidence_dir) -> dict:
    act = {}
    if evidence_dir and (Path(evidence_dir) / "activation.json").exists():
        act = json.loads((Path(evidence_dir) / "activation.json").read_text())["items"]
    pool = [("skill", s) for s in inv["skills"] if s["source"] == "user"]
    pool += [("agent", a) for a in inv["agents"]]
    scored = sorted(((act.get(item["id"], {}).get("count", 0), kind, item) for kind, item in pool),
                    key=lambda t: -t[0])
    artifacts = []
    for count, kind, item in scored[:10]:
        p = Path(item["path"])
        try:
            body = p.read_text(errors="replace")[:8000]
        except OSError:
            continue
        # ponytail: 3 parent levels covers skills/<name>/SKILL.md under any root;
        # the scanners never nest artifacts deeper than that
        symlinked = p.is_symlink() or any(par.is_symlink() for par in list(p.parents)[:3])
        artifacts.append({"id": f"artifact:{kind}:{item['name']}", "kind": kind,
                          "source": item["source"], "path": item["path"],
                          "activation_count": count, "symlinked": symlinked, "body": body})
    return {"schema_version": so_schema.EVIDENCE_VERSION, "harness": so_schema.HARNESS,
            "artifacts": artifacts}


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
            try:
                text = hf.read_text(errors="replace") if hf.exists() else ""
            except OSError:
                text = ""
        if text:
            out.append({"id": f"hook:plugin:{pid}", "source": f"plugin:{pid}",
                        "est_context_tokens": _est(text)})
    return out


def _scan_guidance(data_root: Path, settings: dict) -> list:
    """The guidance surface (global CLAUDE.md + auto-memory notes) with
    redaction-scrubbed bodies, so the auditor can find rules and memories that
    no longer earn their tokens. Bodies are capped and scrubbed — memory notes
    can quote anything the user ever asked to remember."""
    from datetime import datetime, timezone
    out = []

    def add(path: Path, kind: str, cap: int):
        try:
            text = path.read_text(errors="replace")[:cap]
            mtime = path.stat().st_mtime
        except OSError:
            return
        out.append({"id": f"guidance:{path}", "path": str(path), "kind": kind,
                    "bytes": path.stat().st_size, "est_tokens": path.stat().st_size // 4,
                    "mtime": datetime.fromtimestamp(mtime, tz=timezone.utc).isoformat(),
                    "body": redact.scrub(text)[0]})

    gmd = data_root / "CLAUDE.md"
    if gmd.exists():
        add(gmd, "claude_md", 8000)
    mem_dir = Path(settings.get("autoMemoryDirectory")
                   or data_root / "auto-memory").expanduser()
    if mem_dir.is_dir():
        for md in sorted(mem_dir.glob("*.md")):
            add(md, "memory", 2000)
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
    available_plugins = _available_plugins(data_root, installed)
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
    guidance = _scan_guidance(data_root, settings)
    return {"schema_version": so_schema.EVIDENCE_VERSION, "harness": so_schema.HARNESS,
            "plugins": plugins, "skills": skills, "agents": agents, "mcp_servers": mcp,
            "claude_md": claude_md, "hooks": hooks, "guidance": guidance,
            "available_plugins": available_plugins,
            "settings": {**{k: settings.get(k) for k in SETTINGS_ALLOWLIST},
                         "permissions_default_mode": perms.get("defaultMode")},
            "base_context_est": base, "unused": unused, "rare": rare}


# each analyst reads its own copies: shared canonical paths let one analyst's
# read poison another's through session-scoped interference (dedupe hooks,
# permission rules) since parallel subagents share the parent session id
ANALYST_FILES = {
    "miner": ["usage.json", "samples.json", "sessions.json", "constraints.json"],
    "auditor": ["inventory.json", "activation.json", "usage.json", "constraints.json"],
    "evolver": ["artifacts.json", "samples.json", "constraints.json"],
    "labeler": ["samples.json"],
}


def write_analyst_copies(evidence_dir: Path, rules_path: Path | None = None) -> None:
    import shutil
    evidence_dir = Path(evidence_dir)
    for analyst, names in ANALYST_FILES.items():
        adir = evidence_dir / "analysts" / analyst
        adir.mkdir(parents=True, exist_ok=True)
        for name in names:
            src = evidence_dir / name
            if not src.exists():
                continue
            dst = adir / name
            shutil.copyfile(src, dst)
            dst.chmod(0o600)
        if rules_path and analyst in ("auditor", "evolver") and Path(rules_path).exists():
            dst = adir / "rules.json"
            shutil.copyfile(rules_path, dst)
            dst.chmod(0o600)


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
    ap.add_argument("--rules", default=None)
    a = ap.parse_args(argv)
    data_root, state = so_config.resolve(a.data_root, a.state)
    inv = build_inventory(data_root, Path(a.out))
    out_file = Path(a.out) / "inventory.json"
    out_file.write_text(json.dumps(inv, indent=1))
    out_file.chmod(0o600)
    art = build_artifacts(inv, a.out)
    art_file = Path(a.out) / "artifacts.json"
    art_file.write_text(json.dumps(art, indent=1))
    art_file.chmod(0o600)
    write_analyst_copies(Path(a.out), Path(a.rules) if a.rules else None)
    update_last_metrics(state, base_context_est=inv["base_context_est"],
                        unused_surface_count=len(inv["unused"]))
    print(f"skills={len(inv['skills'])} agents={len(inv['agents'])} mcp={len(inv['mcp_servers'])} "
          f"unused={len(inv['unused'])} base_context_est={inv['base_context_est']} "
          f"artifacts={len(art['artifacts'])}")


if __name__ == "__main__":
    main()
