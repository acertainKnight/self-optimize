"""opencode adapter collector: session + config-surface evidence for the shared
self-optimize pipeline, stamped harness "opencode".

Grounded in an observed opencode install (CLI 1.17.x): sessions live in a single
SQLite database, <opencode-home>/opencode.db (table `session`), with turn content
externalized to `message` (one row per user/assistant turn, `data` is a JSON blob)
and `part` (one row per content block within a turn — text, reasoning, tool call,
etc. — linked by `message_id`). Config lives separately under
<opencode-config>/opencode.jsonc plus AGENTS.md, agent/, command/.

Correction mining and sample capping reuse the Claude Code collector's shared
logic directly (scripts/collect.py) rather than re-implementing it.

Scope, stated honestly: duplicate-read, repeated-call, permission-stall, revert,
and re-ask mining are not implemented for opencode (no local tool-call transcript
walk beyond text/correction extraction). Those evidence fields are emitted zero
and usage.parse.collector_limits says so.
"""
import argparse
import json
import re
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
import redact  # noqa: E402
from collect import DEFAULT_CORRECTION_RE, _cap_samples  # noqa: E402

EVIDENCE_VERSION = "1"
HARNESS = "opencode"
# same defaults as so_config.py's sample_caps, since this adapter runs standalone
# (no self-optimize config.json in the loop)
DEFAULT_SAMPLE_CAPS = {"excerpts": 40, "tokens_per_excerpt": 1500, "total_tokens": 60000}


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


def _load_json(text) -> dict:
    if not text:
        return {}
    try:
        d = json.loads(text)
    except json.JSONDecodeError:
        return {}
    return d if isinstance(d, dict) else {}


def _strip_jsonc(text: str) -> str:
    """Strips // and /* */ comments outside string literals, and trailing commas
    before ] or } — enough to parse opencode's jsonc config, not a full JSON5
    parser (no single-quoted strings, no unquoted keys)."""
    out = []
    in_str = False
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if in_str:
            out.append(c)
            if c == "\\" and i + 1 < n:
                out.append(text[i + 1])
                i += 2
                continue
            if c == '"':
                in_str = False
            i += 1
            continue
        if c == '"':
            in_str = True
            out.append(c)
            i += 1
            continue
        if c == "/" and i + 1 < n and text[i + 1] == "/":
            while i < n and text[i] != "\n":
                i += 1
            continue
        if c == "/" and i + 1 < n and text[i + 1] == "*":
            i += 2
            while i + 1 < n and not (text[i] == "*" and text[i + 1] == "/"):
                i += 1
            i += 2
            continue
        out.append(c)
        i += 1
    stripped = "".join(out)
    return re.sub(r",(\s*[}\]])", r"\1", stripped)


def _session_turns(conn, session_id: str) -> list:
    """Returns [(role, text, time_created_ms), ...] for a session's user/assistant
    message rows, in order. Text is the concatenation of that message's "text"
    parts — reasoning and tool-call parts are not user-visible turn content."""
    msg_rows = conn.execute(
        "SELECT id, time_created, data FROM message WHERE session_id=? "
        "ORDER BY time_created, id", (session_id,)).fetchall()
    part_rows = conn.execute(
        "SELECT message_id, data FROM part WHERE session_id=? "
        "AND json_extract(data,'$.type')='text' ORDER BY id", (session_id,)).fetchall()
    text_by_msg = {}
    for r in part_rows:
        text_by_msg.setdefault(r["message_id"], []).append(_load_json(r["data"]).get("text") or "")
    turns = []
    for r in msg_rows:
        role = _load_json(r["data"]).get("role")
        if role not in ("user", "assistant"):
            continue
        turns.append((role, "".join(text_by_msg.get(r["id"], [])), r["time_created"]))
    return turns


def _mine_corrections(turns: list) -> tuple:
    """Same correction pattern and text caps as scripts/collect.py's parse_session:
    a real user turn matching DEFAULT_CORRECTION_RE in its first 200 chars, paired
    with the nearest-preceding assistant turn's text."""
    corrections = []
    prior_assistant = ""
    ended_on_correction = 0
    redactions = 0
    for role, text, ts_ms in turns:
        if role == "assistant":
            if text:
                prior_assistant = text[:800]
            ended_on_correction = 0
            continue
        if not text:
            continue
        m = DEFAULT_CORRECTION_RE.search(text[:200])
        if not m:
            ended_on_correction = 0
            continue
        ended_on_correction = 1
        ut, r1 = redact.scrub(text[:1200])
        pa, r2 = redact.scrub(prior_assistant)
        redactions += r1 + r2
        corrections.append({"ts": _iso(ts_ms), "pattern": m.group(0).strip(),
                            "user_text": ut, "prior_assistant_text": pa})
    return corrections, ended_on_correction, redactions


def collect_sessions(opencode_home, since_iso: str | None = None) -> tuple:
    """Returns (sessions, samples). Missing db or empty session table both
    degrade to ([], []) rather than raising — main() is what enforces the
    hard "db must exist" CLI contract."""
    db = Path(opencode_home) / "opencode.db"
    if not db.exists():
        return [], []
    try:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT id, directory, version, model, time_created, time_updated,"
            " tokens_input, tokens_output, tokens_cache_read, tokens_cache_write"
            " FROM session ORDER BY time_created").fetchall()
    except sqlite3.Error:
        return [], []

    sessions, samples = [], []
    for r in rows:
        started = _iso(r["time_created"])
        if since_iso and (started or "") < since_iso:
            continue
        model_id = _load_json(r["model"]).get("id") or "unknown"
        turns = _session_turns(conn, r["id"])
        corrections, ended_on_correction, redactions = _mine_corrections(turns)
        project = r["directory"] or ""
        for c in corrections:
            samples.append({"session": r["id"], "project": project, "ts": c["ts"],
                            "kind": "correction", "pattern": c["pattern"],
                            "user_text": c["user_text"],
                            "prior_assistant_text": c["prior_assistant_text"]})
        in_tok, out_tok = r["tokens_input"] or 0, r["tokens_output"] or 0
        sessions.append({
            "id": r["id"], "project": project, "cwd": r["directory"],
            "harness_version": r["version"],
            "started_at": started, "ended_at": _iso(r["time_updated"]),
            "turns": len(turns), "input_tokens": in_tok, "output_tokens": out_tok,
            "cache_read": r["tokens_cache_read"] or 0, "cache_write": r["tokens_cache_write"] or 0,
            "sidechain_output_tokens": 0,
            "models": {model_id: {"input": in_tok, "output": out_tok}},
            "corrections_count": len(corrections),
            "duplicate_reads": 0, "repeated_calls": 0, "permission_stalls": 0,
            "revert_events": 0, "reasks": 0,
            "ended_on_correction": ended_on_correction, "redactions": redactions,
        })
    conn.close()
    return sessions, samples


def build_usage(sessions: list, since_iso: str | None) -> dict:
    n = len(sessions)
    per_project, per_model, corr_by_model = {}, {}, {}
    tot = {"sessions": n, "turns": 0, "input_tokens": 0, "output_tokens": 0,
           "cache_read": 0, "cache_write": 0}
    corr_total, ended_on_correction_total = 0, 0
    for s in sessions:
        for k in ("turns", "input_tokens", "output_tokens", "cache_read", "cache_write"):
            tot[k] += s[k]
        p = per_project.setdefault(s["project"], {"sessions": 0, "output_tokens": 0})
        p["sessions"] += 1
        p["output_tokens"] += s["output_tokens"]
        for m, v in s["models"].items():
            e = per_model.setdefault(m, {"input": 0, "output": 0, "sessions": 0})
            e["input"] += v["input"]
            e["output"] += v["output"]
            e["sessions"] += 1
            if s["corrections_count"]:
                corr_by_model[m] = corr_by_model.get(m, 0) + s["corrections_count"]
        corr_total += s["corrections_count"]
        ended_on_correction_total += s["ended_on_correction"]
    return {"window": {"since": since_iso,
                       "until": max((s["ended_at"] or "" for s in sessions), default=None)},
            "totals": tot, "per_project": per_project, "per_model": per_model,
            "corrections_by_model": corr_by_model,
            "waste": {"duplicate_reads_total": 0, "repeated_calls_total": 0,
                      "permission_stalls_total": 0, "main_model_heavy_sessions": 0,
                      "revert_events_total": 0, "reasks_total": 0,
                      "ended_on_correction_total": ended_on_correction_total,
                      "top_duplicate_read_paths": [], "top_stalled_tools": [],
                      "stall_examples": []},
            "corrections": {"total": corr_total, "rate_per_session": (corr_total / n) if n else 0.0},
            "parse": {"skipped_lines": 0, "files": n,
                      "redactions": sum(s["redactions"] for s in sessions),
                      "collector_limits": [
                          "duplicate reads, repeated tool calls, permission stalls, revert "
                          "events, and re-asks are not mined for opencode: those fields are "
                          "always zero",
                          "input/output tokens are the session table's own running totals, "
                          "attributed to session.model (the model active when collected); a "
                          "mid-session model switch is not split out per turn"]}}


def build_inventory(config_root) -> dict:
    config_root = Path(config_root)
    cfg = {}
    cfile = config_root / "opencode.jsonc"
    if cfile.exists():
        try:
            cfg = json.loads(_strip_jsonc(cfile.read_text(errors="replace")))
        except (json.JSONDecodeError, OSError):
            cfg = {}
    if not isinstance(cfg, dict):
        cfg = {}

    mcp = [{"id": f"mcp:{name}", "name": name, "source": "user",
            "est_context_tokens": _est(json.dumps(scfg, default=str))}
           for name, scfg in (cfg.get("mcp") or {}).items() if isinstance(scfg, dict)]

    skills = []
    for root in (cfg.get("skills") or {}).get("paths") or []:
        for md in sorted(Path(root).expanduser().glob("*/SKILL.md")):
            name = md.parent.name
            skills.append({"id": f"skill:{name}", "name": name, "source": "user",
                           "path": str(md),
                           "est_context_tokens": _est(md.read_text(errors="replace")[:2000])})

    agents = [{"id": f"agent:{p.stem}", "name": p.stem, "path": str(p),
               "est_context_tokens": _est(p.read_text(errors="replace"))}
              for p in sorted((config_root / "agent").glob("*.md"))]
    commands = [{"id": f"command:{p.stem}", "name": p.stem, "path": str(p),
                "est_context_tokens": _est(p.read_text(errors="replace"))}
                for p in sorted((config_root / "command").glob("*.md"))]

    agents_md = []
    am = config_root / "AGENTS.md"
    if am.exists():
        agents_md.append({"id": f"agents_md:{am}", "path": str(am),
                          "bytes": am.stat().st_size, "est_tokens": am.stat().st_size // 4})

    settings = {"model": cfg.get("model"), "plugin": cfg.get("plugin") or []}
    base = (sum(s["est_context_tokens"] for s in skills)
            + sum(m["est_context_tokens"] for m in mcp)
            + sum(a["est_context_tokens"] for a in agents)
            + sum(c["est_context_tokens"] for c in commands)
            + sum(a["est_tokens"] for a in agents_md))
    return {"plugins": [], "skills": skills, "agents": agents, "mcp_servers": mcp,
            "commands": commands, "agents_md": agents_md, "settings": settings,
            "base_context_est": base, "unused": [], "rare": []}


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--opencode-home", default=str(Path.home() / ".local" / "share" / "opencode"))
    ap.add_argument("--opencode-config", default=str(Path.home() / ".config" / "opencode"))
    ap.add_argument("--out", required=True)
    ap.add_argument("--since", default=None)
    a = ap.parse_args(argv)

    home = Path(a.opencode_home)
    if not (home / "opencode.db").exists():
        print(f"error: no opencode.db found at {home / 'opencode.db'} — is opencode installed, "
              f"or is --opencode-home pointed at the right data root?", file=sys.stderr)
        raise SystemExit(1)

    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    sessions, samples = collect_sessions(home, a.since)
    samples = _cap_samples(samples, sessions, DEFAULT_SAMPLE_CAPS)
    usage = build_usage(sessions, a.since)
    inv = build_inventory(Path(a.opencode_config))
    (out / "sessions.json").write_text(json.dumps(_stamp({"sessions": sessions}), indent=1))
    (out / "usage.json").write_text(json.dumps(_stamp(usage), indent=1))
    (out / "inventory.json").write_text(json.dumps(_stamp(inv), indent=1))
    (out / "samples.json").write_text(json.dumps(_stamp({"samples": samples}), indent=1))
    for f in out.glob("*.json"):
        f.chmod(0o600)
    print(f"sessions={usage['totals']['sessions']} corrections={usage['corrections']['total']} "
          f"mcp={len(inv['mcp_servers'])} skills={len(inv['skills'])} "
          f"base_context_est={inv['base_context_est']}")


if __name__ == "__main__":
    main()
