"""Codex CLI adapter collector: session + config-surface evidence for the shared
self-optimize pipeline, stamped harness "codex".

Grounded in Codex CLI 0.144.x on-disk formats: threads live in
<codex-home>/state_<N>.sqlite (table `threads`; per-thread turn content is
externalized to the file named by `rollout_path`), config in config.toml,
instructions in AGENTS.md, custom prompts in prompts/*.md, skills in skills/.

Scope, stated honestly: this collector reads the threads table and the config
surface only. The rollout file's turn-level format has not been observed on a
real session yet, so corrections/samples/waste mining is NOT implemented —
those evidence fields are emitted empty and usage.parse.collector_limits says
so. Extend parse_rollout() once a populated rollout file exists to
reverse-engineer against.
"""
import argparse
import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

EVIDENCE_VERSION = "1"
HARNESS = "codex"


def _stamp(obj: dict) -> dict:
    return {"schema_version": EVIDENCE_VERSION, "harness": HARNESS, **obj}


def _est(text: str) -> int:
    return len(text) // 4


def _iso(ms) -> str | None:
    if not ms:
        return None
    try:
        return datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc).isoformat()
    except (OverflowError, OSError, ValueError):
        return None


def _state_db(codex_home: Path) -> Path | None:
    """Highest-numbered state_<N>.sqlite wins — Codex bumps N on schema migrations."""
    dbs = sorted(codex_home.glob("state_*.sqlite"),
                 key=lambda p: int(re.search(r"state_(\d+)", p.name).group(1)))
    return dbs[-1] if dbs else None


def collect_sessions(codex_home: Path) -> list:
    db = _state_db(Path(codex_home))
    if db is None:
        return []
    try:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT id, rollout_path, cwd, model, model_provider, tokens_used,"
            " created_at_ms, updated_at_ms FROM threads").fetchall()
        conn.close()
    except sqlite3.Error:
        return []
    sessions = []
    for r in rows:
        model = r["model"] or "unknown"
        # threads.tokens_used is a single total; the input/output split lives in
        # the unobserved rollout file, so it is carried as output_tokens and the
        # split left at zero rather than guessed
        toks = r["tokens_used"] or 0
        sessions.append({
            "id": str(r["id"]), "project": r["cwd"] or "", "cwd": r["cwd"],
            "harness_version": None,
            "started_at": _iso(r["created_at_ms"]), "ended_at": _iso(r["updated_at_ms"]),
            "turns": 0, "input_tokens": 0, "output_tokens": toks,
            "cache_read": 0, "cache_write": 0, "sidechain_output_tokens": 0,
            "models": {model: {"input": 0, "output": toks}},
            "corrections_count": 0, "duplicate_reads": 0, "repeated_calls": 0,
            "permission_stalls": 0, "revert_events": 0, "reasks": 0,
            "ended_on_correction": 0, "redactions": 0,
            "rollout_path": r["rollout_path"],
        })
    return sessions


def build_usage(sessions: list, since_iso: str | None) -> dict:
    rows = [s for s in sessions if not since_iso or (s["started_at"] or "") >= since_iso]
    per_project, per_model = {}, {}
    tot = {"sessions": len(rows), "turns": 0, "input_tokens": 0, "output_tokens": 0,
           "cache_read": 0, "cache_write": 0}
    for s in rows:
        tot["output_tokens"] += s["output_tokens"]
        p = per_project.setdefault(s["project"], {"sessions": 0, "output_tokens": 0})
        p["sessions"] += 1
        p["output_tokens"] += s["output_tokens"]
        for m, v in s["models"].items():
            e = per_model.setdefault(m, {"input": 0, "output": 0, "sessions": 0})
            e["input"] += v["input"]
            e["output"] += v["output"]
            e["sessions"] += 1
    return {"window": {"since": since_iso,
                       "until": max((s["ended_at"] or "" for s in rows), default=None)},
            "totals": tot, "per_project": per_project, "per_model": per_model,
            "corrections_by_model": {},
            "waste": {"duplicate_reads_total": 0, "repeated_calls_total": 0,
                      "permission_stalls_total": 0, "main_model_heavy_sessions": 0,
                      "revert_events_total": 0, "reasks_total": 0,
                      "ended_on_correction_total": 0,
                      "top_duplicate_read_paths": [], "top_stalled_tools": [],
                      "stall_examples": []},
            "corrections": {"total": 0, "rate_per_session": 0.0},
            "parse": {"skipped_lines": 0, "files": len(rows), "redactions": 0,
                      "collector_limits": [
                          "turn-level rollout files not parsed (format unobserved): "
                          "corrections, samples, and waste metrics are empty, and "
                          "tokens_used is carried as output_tokens without a split"]}}


def build_inventory(codex_home: Path) -> dict:
    codex_home = Path(codex_home)
    cfg = {}
    cfile = codex_home / "config.toml"
    if cfile.exists():
        try:
            import tomllib
            cfg = tomllib.loads(cfile.read_text(errors="replace"))
        except Exception:
            cfg = {}

    mcp = [{"id": f"mcp:{name}", "name": name, "source": "user",
            "est_context_tokens": _est(json.dumps(scfg, default=str))}
           for name, scfg in (cfg.get("mcp_servers") or {}).items()]

    skills = []
    for md in sorted((codex_home / "skills").glob("**/SKILL.md")):
        name = md.parent.name
        skills.append({"id": f"skill:{name}", "name": name,
                       "source": "system" if ".system" in md.parts else "user",
                       "path": str(md), "description": "",
                       "est_context_tokens": _est(md.read_text(errors="replace")[:2000])})
    prompts = [{"id": f"prompt:{p.stem}", "name": p.stem, "path": str(p),
                "est_context_tokens": _est(p.read_text(errors="replace"))}
               for p in sorted((codex_home / "prompts").glob("*.md"))]

    agents_md = []
    am = codex_home / "AGENTS.md"
    if am.exists():
        agents_md.append({"id": f"agents_md:{am}", "path": str(am),
                          "bytes": am.stat().st_size,
                          "est_tokens": am.stat().st_size // 4})

    profiles = cfg.get("profiles") or {}
    settings = {"forced_login_method": cfg.get("forced_login_method"),
                "profiles": {k: {"model": v.get("model"),
                                 "model_provider": v.get("model_provider")}
                             for k, v in profiles.items() if isinstance(v, dict)},
                "features": cfg.get("features") or {}}
    base = (sum(s["est_context_tokens"] for s in skills)
            + sum(m["est_context_tokens"] for m in mcp)
            + sum(p["est_context_tokens"] for p in prompts)
            + sum(a["est_tokens"] for a in agents_md))
    return {"plugins": [], "skills": skills, "agents": [], "mcp_servers": mcp,
            "prompts": prompts, "agents_md": agents_md, "settings": settings,
            "base_context_est": base, "unused": [], "rare": []}


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--codex-home", default=str(Path.home() / ".codex"))
    ap.add_argument("--out", required=True)
    ap.add_argument("--since", default=None)
    a = ap.parse_args(argv)
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    sessions = collect_sessions(Path(a.codex_home))
    usage = build_usage(sessions, a.since)
    inv = build_inventory(Path(a.codex_home))
    (out / "sessions.json").write_text(json.dumps(_stamp({"sessions": sessions}), indent=1))
    (out / "usage.json").write_text(json.dumps(_stamp(usage), indent=1))
    (out / "inventory.json").write_text(json.dumps(_stamp(inv), indent=1))
    (out / "samples.json").write_text(json.dumps(_stamp({"samples": []}), indent=1))
    for f in out.glob("*.json"):
        f.chmod(0o600)
    print(f"sessions={usage['totals']['sessions']} "
          f"mcp={len(inv['mcp_servers'])} skills={len(inv['skills'])} "
          f"base_context_est={inv['base_context_est']}")


if __name__ == "__main__":
    main()
