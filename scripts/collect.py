"""Transcript collector: walks Claude Code session JSONL into the evidence pack.
Collector contract (format is explicitly NOT stability-guaranteed by Anthropic docs):
skip unknown record types; never crash on a malformed line (count it); stamp the
transcript `version`; exclude isSidechain turns from correction mining but keep
their tokens; use parentUuid threading (not file order) for correction adjacency."""
import hashlib
import json
import re
from collections import Counter
from pathlib import Path

import redact

DEFAULT_CORRECTION_RE = re.compile(
    r"(?i)^\s*(no\b|nope\b|not (what|that|right)|that'?s (wrong|not)|actually\b|"
    r"instead\b|undo\b|revert\b|stop\b|don'?t\b|i meant\b|wrong\b|why did you)")
REJECTION_RE = re.compile(
    r"(?i)(user (declined|doesn'?t want)|permission.{0,30}denied|rejected this)")


def iter_records(path: Path, counters: dict):
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                counters["skipped_lines"] += 1
                continue
            if not isinstance(rec, dict):
                counters["skipped_lines"] += 1
                continue
            yield rec


def _text(msg: dict) -> str:
    c = msg.get("content")
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        return "\n".join(b.get("text", "") for b in c
                         if isinstance(b, dict) and b.get("type") == "text")
    return ""


def _args_hash(inp) -> str:
    return hashlib.sha256(json.dumps(inp, sort_keys=True, default=str).encode()).hexdigest()[:8]


def _bare(name) -> str:
    """Plugin items are invoked namespaced ('plugin:skill'); inventory ids are bare."""
    return str(name).split(":")[-1]


def parse_session(path: Path, project: str, correction_re, counters: dict) -> dict:
    s = {"id": Path(path).stem, "project": project, "cwd": None,
         "harness_version": None, "started_at": None, "ended_at": None, "turns": 0,
         "input_tokens": 0, "output_tokens": 0, "cache_read": 0, "cache_write": 0,
         "sidechain_output_tokens": 0, "models": {}, "corrections": [],
         "duplicate_reads": 0, "repeated_calls": 0, "permission_stalls": 0,
         "redactions": 0, "activation": Counter()}
    read_paths, call_keys = Counter(), Counter()
    texts_by_uuid = {}  # uuid -> (role, text sample) for parentUuid adjacency

    for rec in iter_records(path, counters):
        rtype = rec.get("type")
        ts = rec.get("timestamp")
        if ts:
            s["started_at"] = min(s["started_at"] or ts, ts)
            s["ended_at"] = max(s["ended_at"] or ts, ts)
        s["harness_version"] = s["harness_version"] or rec.get("version")
        s["cwd"] = s["cwd"] or rec.get("cwd")
        if rec.get("attributionSkill"):
            s["activation"][f"skill:{_bare(str(rec['attributionSkill']))}"] += 1
        for attr, prefix in (("attributionPlugin", "plugin"), ("attributionMcpServer", "mcp")):
            if rec.get(attr):
                s["activation"][f"{prefix}:{rec[attr]}"] += 1
        if rtype not in ("user", "assistant"):
            continue
        s["turns"] += 1
        msg = rec.get("message") or {}
        side = bool(rec.get("isSidechain"))
        if rec.get("uuid"):
            texts_by_uuid[rec["uuid"]] = (rtype, _text(msg)[:800], rec.get("parentUuid"))

        if rtype == "assistant":
            u = msg.get("usage") or {}
            s["input_tokens"] += u.get("input_tokens", 0) or 0
            out = u.get("output_tokens", 0) or 0
            s["output_tokens"] += out
            s["cache_read"] += u.get("cache_read_input_tokens", 0) or 0
            s["cache_write"] += u.get("cache_creation_input_tokens", 0) or 0
            if side:
                s["sidechain_output_tokens"] += out
            model = msg.get("model")
            if model:
                m = s["models"].setdefault(model, {"input": 0, "output": 0})
                m["input"] += u.get("input_tokens", 0) or 0
                m["output"] += out
            for blk in (msg.get("content") or []) if isinstance(msg.get("content"), list) else []:
                if not (isinstance(blk, dict) and blk.get("type") == "tool_use"):
                    continue
                name, inp = blk.get("name", ""), blk.get("input") or {}
                call_keys[(name, _args_hash(inp))] += 1
                if name == "Read" and inp.get("file_path"):
                    read_paths[inp["file_path"]] += 1
                if name == "Skill" and inp.get("skill"):
                    s["activation"][f"skill:{_bare(inp['skill'])}"] += 1
                if name in ("Agent", "Task") and inp.get("subagent_type"):
                    s["activation"][f"agent:{_bare(inp['subagent_type'])}"] += 1
                if name.startswith("mcp__"):
                    s["activation"][f"mcp_tool:{name}"] += 1
                    parts = name.split("__")
                    if len(parts) >= 2:
                        s["activation"][f"mcp:{parts[1]}"] += 1

        elif rtype == "user":
            if rec.get("toolUseResult") is not None:
                if REJECTION_RE.search(str(rec["toolUseResult"])):
                    s["permission_stalls"] += 1
                continue
            if side or rec.get("isMeta"):
                continue
            text = _text(msg)
            if text and correction_re.search(text[:200]):
                prior = ""
                parent = rec.get("parentUuid")
                for _ in range(5):  # walk up to the nearest assistant text
                    if parent not in texts_by_uuid:
                        break
                    role, ptext, gp = texts_by_uuid[parent]
                    if role == "assistant" and ptext:
                        prior = ptext
                        break
                    parent = gp
                ut, r1 = redact.scrub(text[:1200])
                pa, r2 = redact.scrub(prior)
                s["redactions"] += r1 + r2
                s["corrections"].append({
                    "uuid": rec.get("uuid"), "ts": ts,
                    "pattern": correction_re.search(text[:200]).group(0).strip(),
                    "user_text": ut,
                    "prior_assistant_text": pa,
                })

    s["duplicate_reads"] = sum(c - 1 for c in read_paths.values() if c > 1)
    s["repeated_calls"] = sum(c - 2 for c in call_keys.values() if c > 2)
    s["activation"] = dict(s["activation"])
    return s


# ---------------------------------------------------------------- corpus walk
import argparse
from datetime import date
from fnmatch import fnmatch

import schema as so_schema


def collect_corpus(data_root: Path, since_iso, include, exclude, correction_re, caps) -> dict:
    counters = {"skipped_lines": 0, "files": 0}
    sessions, act, samples = [], {}, []
    proj_root = Path(data_root) / "projects"
    for f in sorted(proj_root.glob("*/*.jsonl")) if proj_root.exists() else []:
        project = f.parent.name
        if not any(fnmatch(project, g) for g in include):
            continue
        if any(fnmatch(project, g) for g in exclude):
            continue
        counters["files"] += 1
        s = parse_session(f, project, correction_re, counters)
        if since_iso and (s["started_at"] or "") < since_iso:
            continue
        for item, n in s.pop("activation").items():
            e = act.setdefault(item, {"count": 0, "last_used": None, "projects": []})
            e["count"] += n
            e["last_used"] = max(e["last_used"] or "", s["ended_at"] or "")
            if project not in e["projects"]:
                e["projects"].append(project)
        corrs = s.pop("corrections")
        for c in corrs:
            samples.append({"session": s["id"], "project": project, "ts": c["ts"],
                            "kind": "correction", "pattern": c["pattern"],
                            "user_text": c["user_text"],
                            "prior_assistant_text": c["prior_assistant_text"]})
        s["corrections_count"] = len(corrs)
        sessions.append(s)

    samples = _cap_samples(samples, sessions, caps)
    n = len(sessions)
    per_project, per_model, dup_paths = {}, {}, {}
    tot = {"sessions": n, "turns": 0, "input_tokens": 0, "output_tokens": 0,
           "cache_read": 0, "cache_write": 0}
    waste = {"duplicate_reads_total": 0, "repeated_calls_total": 0, "permission_stalls_total": 0}
    corr_total = 0
    for s in sessions:
        for k in ("turns", "input_tokens", "output_tokens", "cache_read", "cache_write"):
            tot[k] += s[k]
        p = per_project.setdefault(s["project"], {"sessions": 0, "output_tokens": 0})
        p["sessions"] += 1
        p["output_tokens"] += s["output_tokens"]
        for m, v in s["models"].items():
            e = per_model.setdefault(m, {"input": 0, "output": 0})
            e["input"] += v["input"]
            e["output"] += v["output"]
        waste["duplicate_reads_total"] += s["duplicate_reads"]
        waste["repeated_calls_total"] += s["repeated_calls"]
        waste["permission_stalls_total"] += s["permission_stalls"]
        corr_total += s["corrections_count"]
    usage = {"window": {"since": since_iso, "until": max((s["ended_at"] or "" for s in sessions), default=None)},
             "totals": tot, "per_project": per_project, "per_model": per_model,
             "waste": {**waste, "top_duplicate_read_paths": []},
             "corrections": {"total": corr_total,
                             "rate_per_session": (corr_total / n) if n else 0.0},
             "parse": {**counters, "redactions": sum(s.get("redactions", 0) for s in sessions)}}
    return {"sessions": sessions, "usage": usage,
            "activation": {"items": act}, "samples": samples}


def _cap_samples(samples, sessions, caps):
    per_char = caps["tokens_per_excerpt"] * 4
    total_char = caps["total_tokens"] * 4
    n_sessions = max(len(sessions), 1)
    shares = {}
    for s in sessions:
        shares[s["project"]] = shares.get(s["project"], 0) + 1
    budget_per_project = {p: max(2, round(caps["excerpts"] * c / n_sessions))
                          for p, c in shares.items()}
    out, used_chars, taken = [], 0, {}
    for smp in sorted(samples, key=lambda x: x["ts"] or "", reverse=True):
        p = smp["project"]
        if taken.get(p, 0) >= budget_per_project.get(p, 2) or len(out) >= caps["excerpts"]:
            continue
        smp = dict(smp)
        smp["user_text"] = smp["user_text"][:per_char]
        smp["prior_assistant_text"] = smp["prior_assistant_text"][:per_char // 2]
        cost = len(smp["user_text"]) + len(smp["prior_assistant_text"])
        if used_chars + cost > total_char:
            break
        used_chars += cost
        taken[p] = taken.get(p, 0) + 1
        out.append(smp)
    return out


def _stamp(obj: dict) -> dict:
    return {"schema_version": so_schema.EVIDENCE_VERSION, "harness": so_schema.HARNESS, **obj}


def write_pack(out_dir: Path, pack: dict) -> None:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "sessions.json").write_text(json.dumps(_stamp({"sessions": pack["sessions"]}), indent=1))
    (out_dir / "usage.json").write_text(json.dumps(_stamp(pack["usage"]), indent=1))
    (out_dir / "activation.json").write_text(json.dumps(_stamp(pack["activation"]), indent=1))
    (out_dir / "samples.json").write_text(json.dumps(_stamp({"samples": pack["samples"]}), indent=1))
    for f in out_dir.glob("*.json"):
        f.chmod(0o600)


def metrics_row(run_id: str, pack: dict) -> dict:
    t = pack["usage"]["totals"]
    n = max(t["sessions"], 1)
    return {"run_id": run_id, "n_sessions": t["sessions"],
            "tokens_per_session": (t["input_tokens"] + t["output_tokens"]) / n,
            "correction_rate": pack["usage"]["corrections"]["rate_per_session"],
            "duplicate_read_rate": pack["usage"]["waste"]["duplicate_reads_total"] / n,
            "permission_stalls": pack["usage"]["waste"]["permission_stalls_total"] / n,
            "parse_skipped": pack["usage"]["parse"]["skipped_lines"],
            "base_context_est": None, "unused_surface_count": None}


def append_metrics(state_dir: Path, row: dict) -> None:
    p = Path(state_dir) / "state"
    p.mkdir(parents=True, exist_ok=True)
    with open(p / "metrics.jsonl", "a") as f:
        f.write(json.dumps(row) + "\n")


def main(argv=None):
    import so_config
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-root", default=None)
    ap.add_argument("--state", default=None)
    ap.add_argument("--out", required=True)
    ap.add_argument("--since", default=None)
    ap.add_argument("--run-id", default=date.today().isoformat())
    a = ap.parse_args(argv)
    data_root, state = so_config.resolve(a.data_root, a.state)
    cfg = so_config.load_config(state)
    correction_re = re.compile(cfg["correction_regex"]) if cfg["correction_regex"] else DEFAULT_CORRECTION_RE
    since = a.since
    if since is None and cfg["since_days"]:
        from datetime import datetime, timedelta, timezone
        since = (datetime.now(timezone.utc) - timedelta(days=cfg["since_days"])).isoformat()
    pack = collect_corpus(data_root, since, cfg["project_include"],
                          cfg["project_exclude"], correction_re, cfg["sample_caps"])
    write_pack(Path(a.out), pack)
    append_metrics(state, metrics_row(a.run_id, pack))
    t = pack["usage"]["totals"]
    print(f"sessions={t['sessions']} corrections={pack['usage']['corrections']['total']} "
          f"dup_reads={pack['usage']['waste']['duplicate_reads_total']} "
          f"stalls={pack['usage']['waste']['permission_stalls_total']} "
          f"parse_skipped={pack['usage']['parse']['skipped_lines']}")


if __name__ == "__main__":
    main()
