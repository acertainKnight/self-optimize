"""Codex CLI adapter collector: session + config-surface evidence for the shared
self-optimize pipeline, stamped harness "codex".

Grounded in Codex CLI 0.144.x on-disk formats: threads live in
<codex-home>/state_<N>.sqlite (table `threads`; per-thread turn content is
externalized to the file named by `rollout_path`), config in config.toml,
instructions in AGENTS.md, custom prompts in prompts/*.md, skills in skills/.

Rollout files are newline-delimited records shaped `{timestamp, type, payload}`.
Observed top-level `type` values: `session_meta` (cli_version, cwd, git),
`turn_context` (active model + turn settings), `response_item` (a Responses-API
item: `payload.type == "message"` for user/assistant/developer turns, content
blocks typed `input_text`/`output_text`; `payload.type` ending in `_call` for a
tool invocation), `event_msg` (control-plane events: `payload.type` values seen
include `task_started`, `user_message`, `task_complete`; `token_count` and
`error` are handled defensively since a populated example of either has not
been observed). Any other top-level `type`, and any line that isn't valid JSON,
is counted and skipped rather than treated as fatal — see parse_rollout().

This module intentionally imports nothing from scripts/ (self-contained
adapter): the correction regex, the revert-command regex, and the redaction
patterns are local copies of the same conventions scripts/collect.py and
scripts/redact.py use elsewhere in this repo.
"""
import argparse
import difflib
import hashlib
import json
import math
import re
import sqlite3
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

EVIDENCE_VERSION = "1"
HARNESS = "codex"

DEFAULT_CORRECTION_RE = re.compile(
    r"(?i)^\s*(no\b|nope\b|not (what|that|right)|that'?s (wrong|not)|actually\b|"
    r"instead\b|undo\b|revert\b|stop\b|don'?t\b|i meant\b|wrong\b|why did you)")
REVERT_RE = re.compile(r"git\s+(revert\b|reset\s+--hard|checkout\s+--)")
SAMPLE_CAPS = {"excerpts": 40, "tokens_per_excerpt": 1500, "total_tokens": 60000}
KNOWN_RECORD_TYPES = {"session_meta", "event_msg", "response_item", "turn_context",
                       "world_state"}

# local copy of scripts/redact.py's patterns (see module docstring: no scripts/ import)
_REDACT_PATTERNS = [
    ("private_key", re.compile(
        r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----")),
    ("anthropic_key", re.compile(r"sk-ant-[A-Za-z0-9_\-]{8,}")),
    ("openai_key", re.compile(r"sk-[A-Za-z0-9]{20,}")),
    ("github_token", re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}")),
    ("slack_token", re.compile(r"xox[baprs]-[A-Za-z0-9\-]{8,}")),
    ("aws_key", re.compile(r"AKIA[0-9A-Z]{16}")),
    ("bearer", re.compile(r"(?i)bearer\s+[A-Za-z0-9._\-]{16,}")),
    ("email", re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")),
]
_REDACT_CANDIDATE = re.compile(r"[A-Za-z0-9+/_\-]{32,}")
_REDACT_ENTROPY_THRESHOLD = 4.5


def _scrub(text: str) -> tuple:
    count = 0
    for name, pat in _REDACT_PATTERNS:
        text, n = pat.subn(f"[REDACTED:{name}]", text)
        count += n

    def _maybe(m: re.Match) -> str:
        nonlocal count
        freq = {c: m.group(0).count(c) for c in set(m.group(0))}
        entropy = -sum(n / len(m.group(0)) * math.log2(n / len(m.group(0))) for n in freq.values())
        if entropy > _REDACT_ENTROPY_THRESHOLD:
            count += 1
            return "[REDACTED:entropy]"
        return m.group(0)

    text = _REDACT_CANDIDATE.sub(_maybe, text)
    return text, count


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


def _find_rollout(codex_home: Path, thread_id: str, rollout_path) -> Path | None:
    """threads.rollout_path first; if that file is gone, fall back to scanning
    <codex-home>/sessions/<year>/... for a filename carrying the thread id."""
    if rollout_path:
        p = Path(rollout_path)
        if p.is_file():
            return p
    sessions_dir = Path(codex_home) / "sessions"
    if sessions_dir.exists():
        matches = sorted(sessions_dir.glob(f"**/*{thread_id}*.jsonl"))
        if matches:
            return matches[0]
    return None


def _content_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(b.get("text", "") for b in content
                         if isinstance(b, dict) and b.get("type") in ("input_text", "output_text")
                         and isinstance(b.get("text"), str))
    return ""


def _args_hash(args) -> str:
    return hashlib.sha256(json.dumps(args, sort_keys=True, default=str).encode()).hexdigest()[:8]


def _token_usage(payload: dict) -> tuple:
    """Best-effort extraction across plausible token_count payload shapes. No populated
    example has been observed yet, so this checks a nested info.last/total_token_usage
    shape first, then flat fields, and returns zeros rather than guessing further."""
    info = payload.get("info") if isinstance(payload.get("info"), dict) else payload
    usage = info.get("last_token_usage") or info.get("total_token_usage") or info
    if not isinstance(usage, dict):
        return 0, 0
    inp = usage.get("input_tokens") or usage.get("prompt_tokens") or 0
    out = usage.get("output_tokens") or usage.get("completion_tokens") or 0
    try:
        return int(inp or 0), int(out or 0)
    except (TypeError, ValueError):
        return 0, 0


def parse_rollout(path: Path, correction_re=DEFAULT_CORRECTION_RE) -> dict:
    """Reads one rollout JSONL file and extracts turn-level evidence. Never raises on a
    malformed line or an unrecognized top-level record type — both are counted in
    skipped_lines rather than treated as fatal (schema drift is expected, not exceptional)."""
    out = {"turns": 0, "input_tokens": 0, "output_tokens": 0, "models": {},
           "repeated_calls": 0, "revert_events": 0, "reasks": 0,
           "ended_on_correction": 0, "redactions": 0, "corrections_count": 0,
           "cli_version": None, "samples": [], "skipped_lines": 0}
    try:
        fh = open(path, encoding="utf-8", errors="replace")
    except OSError:
        return out

    call_keys = Counter()
    user_texts = []
    last_assistant_text = ""
    current_model = None
    last_user_was_correction = False

    with fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                out["skipped_lines"] += 1
                continue
            if not isinstance(rec, dict):
                out["skipped_lines"] += 1
                continue
            rtype = rec.get("type")
            if rtype not in KNOWN_RECORD_TYPES:
                out["skipped_lines"] += 1
                continue
            payload = rec.get("payload")
            payload = payload if isinstance(payload, dict) else {}
            ts = rec.get("timestamp")

            try:
                if rtype == "session_meta":
                    out["cli_version"] = out["cli_version"] or payload.get("cli_version")

                elif rtype == "turn_context":
                    current_model = payload.get("model") or current_model

                elif rtype == "event_msg":
                    if payload.get("type") == "token_count":
                        inp, outp = _token_usage(payload)
                        out["input_tokens"] += inp
                        out["output_tokens"] += outp
                        m = out["models"].setdefault(current_model or "unknown",
                                                      {"input": 0, "output": 0})
                        m["input"] += inp
                        m["output"] += outp

                elif rtype == "response_item":
                    ptype = payload.get("type")
                    if ptype == "message":
                        role = payload.get("role")
                        text = _content_text(payload.get("content"))
                        if not text:
                            pass
                        elif role == "assistant":
                            out["turns"] += 1
                            last_assistant_text = text[:800]
                        elif role == "user":
                            out["turns"] += 1
                            user_texts.append(text[:400])
                            is_corr = bool(correction_re.search(text[:200]))
                            last_user_was_correction = is_corr
                            if is_corr:
                                ut, r1 = _scrub(text[:1200])
                                pa, r2 = _scrub(last_assistant_text)
                                out["redactions"] += r1 + r2
                                out["samples"].append({
                                    "ts": ts, "kind": "correction",
                                    "pattern": correction_re.search(text[:200]).group(0).strip(),
                                    "user_text": ut, "prior_assistant_text": pa})
                        # role in ("developer", "system"): injected context, not a turn
                    elif isinstance(ptype, str) and ptype.endswith("_call"):
                        name = payload.get("name") or ptype[:-len("_call")]
                        args = (payload.get("arguments") or payload.get("action")
                               or payload.get("command") or "")
                        call_keys[(name, _args_hash(args))] += 1
                        if REVERT_RE.search(str(args)):
                            out["revert_events"] += 1
                    # other response_item payload types (reasoning, *_call_output, ...)
                    # carry no turn/tool-call signal this collector mines yet — no-op
            except Exception:
                # a single malformed/unexpected record must never abort the whole file
                out["skipped_lines"] += 1

    out["repeated_calls"] = sum(c - 2 for c in call_keys.values() if c > 2)
    for j, tj in enumerate(user_texts):
        if len(tj) < 20:
            continue
        if any(len(ti) >= 20 and difflib.SequenceMatcher(None, ti, tj).ratio() > 0.85
               for ti in user_texts[:j]):
            out["reasks"] += 1
    out["ended_on_correction"] = int(last_user_was_correction)
    out["corrections_count"] = len(out["samples"])
    return out


def collect_sessions(codex_home: Path) -> list:
    codex_home = Path(codex_home)
    db = _state_db(codex_home)
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
        # threads.tokens_used is a single total; kept as the output_tokens fallback
        # whenever the rollout file has no token_count events to split it with
        toks = r["tokens_used"] or 0
        s = {
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
            "_samples": [], "_skipped_lines": 0,
        }
        rollout = _find_rollout(codex_home, str(r["id"]), r["rollout_path"])
        if rollout is not None:
            parsed = parse_rollout(rollout)
            s["turns"] = parsed["turns"]
            if parsed["input_tokens"] or parsed["output_tokens"]:
                s["input_tokens"] = parsed["input_tokens"]
                s["output_tokens"] = parsed["output_tokens"]
                s["models"] = parsed["models"]
            s["corrections_count"] = parsed["corrections_count"]
            s["repeated_calls"] = parsed["repeated_calls"]
            s["revert_events"] = parsed["revert_events"]
            s["reasks"] = parsed["reasks"]
            s["ended_on_correction"] = parsed["ended_on_correction"]
            s["redactions"] = parsed["redactions"]
            if parsed["cli_version"]:
                s["harness_version"] = parsed["cli_version"]
            s["_samples"] = parsed["samples"]
            s["_skipped_lines"] = parsed["skipped_lines"]
        sessions.append(s)
    return sessions


def _cap_samples(samples: list, caps: dict = SAMPLE_CAPS) -> list:
    per_char = caps["tokens_per_excerpt"] * 4
    total_char = caps["total_tokens"] * 4
    out, used_chars = [], 0
    for smp in sorted(samples, key=lambda x: x["ts"] or "", reverse=True):
        if len(out) >= caps["excerpts"]:
            break
        smp = dict(smp)
        smp["user_text"] = smp["user_text"][:per_char]
        smp["prior_assistant_text"] = smp["prior_assistant_text"][:per_char // 2]
        cost = len(smp["user_text"]) + len(smp["prior_assistant_text"])
        if used_chars + cost > total_char:
            break
        used_chars += cost
        out.append(smp)
    return out


def build_usage(sessions: list, since_iso: str | None) -> dict:
    rows = [s for s in sessions if not since_iso or (s["started_at"] or "") >= since_iso]
    per_project, per_model = {}, {}
    tot = {"sessions": len(rows), "turns": 0, "input_tokens": 0, "output_tokens": 0,
           "cache_read": 0, "cache_write": 0}
    waste = {"duplicate_reads_total": 0, "repeated_calls_total": 0,
             "permission_stalls_total": 0, "main_model_heavy_sessions": 0,
             "revert_events_total": 0, "reasks_total": 0, "ended_on_correction_total": 0}
    corr_total = 0
    for s in rows:
        tot["turns"] += s["turns"]
        tot["input_tokens"] += s["input_tokens"]
        tot["output_tokens"] += s["output_tokens"]
        p = per_project.setdefault(s["project"], {"sessions": 0, "output_tokens": 0})
        p["sessions"] += 1
        p["output_tokens"] += s["output_tokens"]
        for m, v in s["models"].items():
            e = per_model.setdefault(m, {"input": 0, "output": 0, "sessions": 0})
            e["input"] += v["input"]
            e["output"] += v["output"]
            e["sessions"] += 1
        waste["duplicate_reads_total"] += s["duplicate_reads"]
        waste["repeated_calls_total"] += s["repeated_calls"]
        waste["permission_stalls_total"] += s["permission_stalls"]
        waste["revert_events_total"] += s["revert_events"]
        waste["reasks_total"] += s["reasks"]
        waste["ended_on_correction_total"] += s["ended_on_correction"]
        corr_total += s["corrections_count"]
    return {"window": {"since": since_iso,
                       "until": max((s["ended_at"] or "" for s in rows), default=None)},
            "totals": tot, "per_project": per_project, "per_model": per_model,
            "corrections_by_model": {},
            "waste": {**waste, "top_duplicate_read_paths": [], "top_stalled_tools": [],
                      "stall_examples": []},
            "corrections": {"total": corr_total,
                            "rate_per_session": (corr_total / len(rows)) if rows else 0.0},
            "parse": {"skipped_lines": sum(s.get("_skipped_lines", 0) for s in sessions),
                      "files": len(rows),
                      "redactions": sum(s["redactions"] for s in rows),
                      "collector_limits": [
                          "duplicate_reads stays zero: the observed rollout format has no "
                          "distinct read-tool call to key a repeated-path signal off of",
                          "permission_stalls stays zero: no approval/sandbox-denial event "
                          "has been observed in a rollout file to detect this from",
                          "threads whose rollout file is missing (deleted, or path not yet "
                          "scannable under sessions/) fall back to the sqlite tokens_used "
                          "total as an unsplit output_tokens figure",
                      ]}}


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
    samples = []
    for s in sessions:
        for smp in s.pop("_samples", []):
            samples.append({**smp, "session": s["id"], "project": s["project"]})
    samples = _cap_samples(samples)
    usage = build_usage(sessions, a.since)
    for s in sessions:
        s.pop("_skipped_lines", None)
    inv = build_inventory(Path(a.codex_home))
    (out / "sessions.json").write_text(json.dumps(_stamp({"sessions": sessions}), indent=1))
    (out / "usage.json").write_text(json.dumps(_stamp(usage), indent=1))
    (out / "inventory.json").write_text(json.dumps(_stamp(inv), indent=1))
    (out / "samples.json").write_text(json.dumps(_stamp({"samples": samples}), indent=1))
    for f in out.glob("*.json"):
        f.chmod(0o600)
    print(f"sessions={usage['totals']['sessions']} corrections={usage['corrections']['total']} "
          f"samples={len(samples)} mcp={len(inv['mcp_servers'])} skills={len(inv['skills'])} "
          f"base_context_est={inv['base_context_est']}")


if __name__ == "__main__":
    main()
