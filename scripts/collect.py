"""Transcript collector: walks Claude Code session JSONL into the evidence pack.
Collector contract (format is explicitly NOT stability-guaranteed by Anthropic docs):
skip unknown record types; never crash on a malformed line (count it); stamp the
transcript `version`; exclude isSidechain turns from correction mining but keep
their tokens; use parentUuid threading (not file order) for correction adjacency."""
import hashlib
import json
import re
import sys
from collections import Counter
from pathlib import Path

import redact

DEFAULT_CORRECTION_RE = re.compile(
    r"(?i)^\s*(no\b|nope\b|not (what|that|right)|that'?s (wrong|not)|actually\b|"
    r"instead\b|undo\b|revert\b|stop\b|don'?t\b|i meant\b|wrong\b|why did you)")
REJECTION_RE = re.compile(
    r"(?i)(user (declined|doesn'?t want)|permission.{0,30}denied|rejected this)")
REVERT_RE = re.compile(r"git\s+(revert\b|reset\s+--hard|checkout\s+--)")
EDIT_TOOLS = ("Edit", "Write", "NotebookEdit")
# working excerpts: ask/answer pairs from sessions that drew no correction, kept for
# the eval gym's "preserved" side. Two per session is enough to characterise a
# session and keeps working.json small; the gym caps again per artifact.
WORKING_PER_SESSION = 2
WORKING_USER_CHARS = 1200
WORKING_ASSISTANT_CHARS = 600


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
         "revert_events": 0, "reasks": 0, "ended_on_correction": 0,
         "redactions": 0, "activation": Counter(), "_stall_details": [],
         "_working": []}
    read_paths, call_keys = Counter(), Counter()
    pending_ask = None   # (ts, text) of the last non-correction user ask
    texts_by_uuid = {}  # uuid -> (role, text sample) for parentUuid adjacency
    tool_ids = {}       # tool_use id -> (name, trimmed detail) for stall attribution
    user_texts = []     # real user asks this session, for re-ask detection
    edits_seen = False
    last_user_was_correction = False

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
            texts_by_uuid[rec["uuid"]] = (rtype, _text(msg)[:800], rec.get("parentUuid"),
                                          msg.get("model") if rtype == "assistant" else None)

        if rtype == "assistant":
            if pending_ask and not side and len(s["_working"]) < WORKING_PER_SESSION:
                atext = _text(msg).strip()
                if atext:
                    ask_ts, ask_text = pending_ask
                    ut, r1 = redact.scrub(ask_text[:WORKING_USER_CHARS])
                    at, r2 = redact.scrub(atext[:WORKING_ASSISTANT_CHARS])
                    s["redactions"] += r1 + r2
                    s["_working"].append({"ts": ask_ts, "user_text": ut, "assistant_text": at})
                    pending_ask = None
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
                detail = str(inp.get("command") or inp.get("file_path") or "")[:80]
                if blk.get("id"):
                    tool_ids[blk["id"]] = (name, detail)
                if name in EDIT_TOOLS:
                    edits_seen = True
                if name == "Bash" and edits_seen and REVERT_RE.search(str(inp.get("command", ""))):
                    s["revert_events"] += 1
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
                    tid = next((b.get("tool_use_id") for b in
                                (msg.get("content") or []) if isinstance(b, dict)
                                and b.get("type") == "tool_result"), None)
                    name, detail = tool_ids.get(tid, ("unknown", ""))
                    s["_stall_details"].append({"tool": name,
                                                "detail": redact.scrub(detail)[0]})
                continue
            if side or rec.get("isMeta"):
                continue
            text = _text(msg)
            if text:
                user_texts.append(text[:400])
                last_user_was_correction = bool(correction_re.search(text[:200]))
                if not last_user_was_correction:
                    pending_ask = (ts, text)
            if text and correction_re.search(text[:200]):
                prior, prior_model = "", None
                parent = rec.get("parentUuid")
                for _ in range(5):  # walk up to the nearest assistant text
                    if parent not in texts_by_uuid:
                        break
                    role, ptext, gp, pmodel = texts_by_uuid[parent]
                    if role == "assistant" and ptext:
                        prior, prior_model = ptext, pmodel
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
                    "model": prior_model,
                })

    s["duplicate_reads"] = sum(c - 1 for c in read_paths.values() if c > 1)
    s["_dup_read_paths"] = {k: c for k, c in read_paths.items() if c > 1}
    s["repeated_calls"] = sum(c - 2 for c in call_keys.values() if c > 2)
    # ponytail: O(n²) similarity over user asks; sessions have tens of asks, and
    # a sequence-hash prefilter is the upgrade path if that ever stops holding
    import difflib
    for j, tj in enumerate(user_texts):
        if len(tj) < 20:
            continue
        if any(len(ti) >= 20
               and difflib.SequenceMatcher(None, ti, tj).ratio() > 0.85
               for ti in user_texts[:j]):
            s["reasks"] += 1
    s["ended_on_correction"] = int(last_user_was_correction)
    s["activation"] = dict(s["activation"])
    return s


# ---------------------------------------------------------------- corpus walk
import argparse
from datetime import date, datetime, timedelta, timezone
from fnmatch import fnmatch

import schema as so_schema


def collect_corpus(data_root: Path, since_iso, include, exclude, correction_re, caps,
                   flagged=frozenset()) -> dict:
    counters = {"skipped_lines": 0, "files": 0}
    sessions, act, samples, working, dup_paths, corr_by_model = [], {}, [], [], {}, {}
    stall_tools, stall_examples = Counter(), []
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
        # activation stays on the session record too: the eval gym attaches cases to
        # the artifacts that were active in that specific session
        for item, n in s["activation"].items():
            e = act.setdefault(item, {"count": 0, "last_used": None, "projects": []})
            e["count"] += n
            e["last_used"] = max(e["last_used"] or "", s["ended_at"] or "")
            if project not in e["projects"]:
                e["projects"].append(project)
        for k, c in s.pop("_dup_read_paths", {}).items():
            dup_paths[k] = dup_paths.get(k, 0) + c
        for d in s.pop("_stall_details", []):
            stall_tools[d["tool"]] += 1
            if len(stall_examples) < 8:
                stall_examples.append(d)
        corrs = s.pop("corrections")
        for c in corrs:
            samples.append({"session": s["id"], "project": project, "ts": c["ts"],
                            "kind": "correction", "pattern": c["pattern"],
                            "user_text": c["user_text"],
                            "prior_assistant_text": c["prior_assistant_text"]})
            if c.get("model"):
                corr_by_model[c["model"]] = corr_by_model.get(c["model"], 0) + 1
        s["corrections_count"] = len(corrs)
        # a session with any correction is not evidence that anything worked, so its
        # excerpts are dropped rather than stored as working cases
        excerpts = s.pop("_working", [])
        for w in (excerpts if not corrs else []):
            working.append({"session": s["id"], "project": project, "ts": w["ts"],
                            "kind": "working", "user_text": w["user_text"],
                            "assistant_text": w["assistant_text"]})
        sessions.append(s)

    samples = _cap_samples(samples, sessions, caps, flagged)
    n = len(sessions)
    per_project, per_model = {}, {}
    tot = {"sessions": n, "turns": 0, "input_tokens": 0, "output_tokens": 0,
           "cache_read": 0, "cache_write": 0}
    waste = {"duplicate_reads_total": 0, "repeated_calls_total": 0, "permission_stalls_total": 0,
             "main_model_heavy_sessions": 0, "revert_events_total": 0, "reasks_total": 0,
             "ended_on_correction_total": 0}
    corr_total = 0
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
        max_model_output = max((v["output"] for v in s["models"].values()), default=0)
        if max_model_output > 50000 and (s["duplicate_reads"] + s["repeated_calls"]) >= 5:
            waste["main_model_heavy_sessions"] += 1
        waste["duplicate_reads_total"] += s["duplicate_reads"]
        waste["repeated_calls_total"] += s["repeated_calls"]
        waste["permission_stalls_total"] += s["permission_stalls"]
        waste["revert_events_total"] += s["revert_events"]
        waste["reasks_total"] += s["reasks"]
        waste["ended_on_correction_total"] += s["ended_on_correction"]
        corr_total += s["corrections_count"]
    usage = {"window": {"since": since_iso, "until": max((s["ended_at"] or "" for s in sessions), default=None)},
             "totals": tot, "per_project": per_project, "per_model": per_model,
             "corrections_by_model": corr_by_model,
             "waste": {**waste, "top_duplicate_read_paths":
                       sorted(dup_paths.items(), key=lambda kv: -kv[1])[:10],
                       "top_stalled_tools": stall_tools.most_common(10),
                       "stall_examples": stall_examples},
             "corrections": {"total": corr_total,
                             "rate_per_session": (corr_total / n) if n else 0.0},
             "parse": {**counters, "redactions": sum(s.get("redactions", 0) for s in sessions)}}
    return {"sessions": sessions, "usage": usage,
            "activation": {"items": act}, "samples": samples, "working": working}


def _cap_samples(samples, sessions, caps, flagged=frozenset()):
    per_char = caps["tokens_per_excerpt"] * 4
    total_char = caps["total_tokens"] * 4
    n_sessions = max(len(sessions), 1)
    shares = {}
    for s in sessions:
        shares[s["project"]] = shares.get(s["project"], 0) + 1
    budget_per_project = {p: max(2, round(caps["excerpts"] * c / n_sessions))
                          for p, c in shares.items()}
    out, used_chars, taken = [], 0, {}
    ordered = sorted(samples, key=lambda x: x["ts"] or "", reverse=True)
    if flagged:
        # Second, stable sort: capture-queue-flagged sessions bubble to the
        # front while the recency order from the first sort survives within
        # each group. No flags -> ordered is untouched -> identical to the
        # pre-capture-queue behavior (uninstalling the hook changes nothing).
        ordered = sorted(ordered, key=lambda x: x["session"] not in flagged)
    for smp in ordered:
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


def scale_caps_to_budget(caps: dict, budget: int | None, reserve: int = 8000, floor: int = 2000) -> dict:
    """Shrinks sample_caps proportionally so total_tokens fits inside budget minus a
    fixed overhead reserve (system prompt + rules + inventory the analysts also read).
    Refuses (exit 2) rather than silently sampling too thin to be useful."""
    if not budget:
        return caps
    allowed = max(0, budget - reserve)
    if allowed < floor:
        print(f"refusing: --max-budget {budget} leaves only {allowed} tokens for sampling "
              f"(minimum {floor}) — raise the budget or omit --max-budget", file=sys.stderr)
        raise SystemExit(2)
    if allowed >= caps["total_tokens"]:
        return caps
    scale = allowed / caps["total_tokens"]
    out = dict(caps)
    out["excerpts"] = max(1, round(caps["excerpts"] * scale))
    out["tokens_per_excerpt"] = max(1, round(caps["tokens_per_excerpt"] * scale))
    out["total_tokens"] = allowed
    return out


def _stamp(obj: dict) -> dict:
    return {"schema_version": so_schema.EVIDENCE_VERSION, "harness": so_schema.HARNESS, **obj}


def constraints_pack(lpath: Path, limit: int = 20) -> dict:
    """Standing constraints for analysts: the most recently rejected recommendations.
    Reads the ledger jsonl directly rather than ledger.load() — load() collapses to
    one entry per id, and the 'rejected' entry (appended after 'proposed') carries no
    title of its own, so the title has to be picked up from the earlier line."""
    lpath = Path(lpath)
    rejected, id_title = [], {}
    if lpath.exists():
        for line in lpath.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                e = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(e, dict) or "id" not in e:
                continue
            rec = e.get("rec") or {}
            if rec.get("title"):
                id_title[e["id"]] = rec["title"]
            if e.get("status") == "rejected":
                rejected.append(e)
    rejected = rejected[-limit:]
    return _stamp({"rejected": [{"title": redact.scrub(str(id_title.get(e["id"], "")))[0],
                                 "reason": redact.scrub(str(e.get("reason", "")))[0],
                                 "ts": e.get("ts", "")}
                                for e in rejected]})


def read_capture_queue(path: Path) -> tuple[list, int]:
    """Reads <state>/capture-queue.jsonl, appended to by the opt-in (never
    auto-enabled) Stop hook in hooks/capture_trigger.py. A line that isn't a
    JSON object with a session_id is skipped and counted, never raised — the
    queue is best-effort pointers, and one bad line must not break collect."""
    path = Path(path)
    if not path.exists():
        return [], 0
    entries, malformed = [], 0
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            e = json.loads(line)
        except json.JSONDecodeError:
            malformed += 1
            continue
        if not isinstance(e, dict) or not e.get("session_id"):
            malformed += 1
            continue
        entries.append(e)
    return entries, malformed


def flagged_session_ids(entries: list) -> set:
    return {e["session_id"] for e in entries if not e.get("consumed")}


def mark_queue_consumed(path: Path, consumed_ids: set) -> None:
    """Marks queue lines for the given session ids as consumed, in place —
    an audit trail, never a deletion. No-op if the queue is missing or
    nothing was flagged this run."""
    path = Path(path)
    if not consumed_ids or not path.exists():
        return
    out = []
    for line in path.read_text().splitlines():
        s = line.strip()
        if not s:
            continue
        try:
            e = json.loads(s)
        except json.JSONDecodeError:
            out.append(s)  # malformed lines survive untouched, byte for byte
            continue
        if isinstance(e, dict) and e.get("session_id") in consumed_ids and not e.get("consumed"):
            e["consumed"] = True
            out.append(json.dumps(e))
        else:
            out.append(s)
    path.write_text("\n".join(out) + "\n")
    path.chmod(0o600)


def write_pack(out_dir: Path, pack: dict, constraints: dict | None = None) -> None:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "sessions.json").write_text(json.dumps(_stamp({"sessions": pack["sessions"]}), indent=1))
    (out_dir / "usage.json").write_text(json.dumps(_stamp(pack["usage"]), indent=1))
    (out_dir / "activation.json").write_text(json.dumps(_stamp(pack["activation"]), indent=1))
    (out_dir / "samples.json").write_text(json.dumps(_stamp({"samples": pack["samples"]}), indent=1))
    (out_dir / "working.json").write_text(
        json.dumps(_stamp({"samples": pack.get("working", [])}), indent=1))
    if constraints is not None:
        (out_dir / "constraints.json").write_text(json.dumps(constraints, indent=1))
    for f in out_dir.glob("*.json"):
        f.chmod(0o600)


def _session_minutes(s: dict):
    a, b = s.get("started_at"), s.get("ended_at")
    if not a or not b:
        return None
    try:
        start = datetime.fromisoformat(a.replace("Z", "+00:00"))
        end = datetime.fromisoformat(b.replace("Z", "+00:00"))
    except (ValueError, TypeError, AttributeError):  # non-string timestamps from odd records
        return None
    return max(0.0, (end - start).total_seconds() / 60.0)


def metrics_row(run_id: str, pack: dict) -> dict:
    t = pack["usage"]["totals"]
    n = max(t["sessions"], 1)
    sessions = pack["sessions"]
    zero_corr = sum(1 for s in sessions if s.get("corrections_count", 0) == 0)
    durations = [d for d in (_session_minutes(s) for s in sessions) if d is not None]
    return {"run_id": run_id, "n_sessions": t["sessions"],
            "tokens_per_session": (t["input_tokens"] + t["output_tokens"]) / n,
            "correction_rate": pack["usage"]["corrections"]["rate_per_session"],
            "duplicate_read_rate": pack["usage"]["waste"]["duplicate_reads_total"] / n,
            "permission_stalls": pack["usage"]["waste"]["permission_stalls_total"] / n,
            "parse_skipped": pack["usage"]["parse"]["skipped_lines"],
            "zero_correction_session_rate": zero_corr / n,
            "mean_session_minutes": (sum(durations) / len(durations)) if durations else None,
            "turns_per_session": t["turns"] / n,
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
    ap.add_argument("--max-budget", type=int, default=None)
    a = ap.parse_args(argv)
    data_root, state = so_config.resolve(a.data_root, a.state)
    cfg = so_config.load_config(state)
    correction_re = re.compile(cfg["correction_regex"]) if cfg["correction_regex"] else DEFAULT_CORRECTION_RE
    since = a.since
    if since is None and cfg["since_days"]:
        since = (datetime.now(timezone.utc) - timedelta(days=cfg["since_days"])).isoformat()
    budget = a.max_budget if a.max_budget is not None else cfg.get("max_budget_tokens", 0)
    caps = scale_caps_to_budget(cfg["sample_caps"], budget)
    queue_path = state / "capture-queue.jsonl"
    queue_entries, queue_malformed = read_capture_queue(queue_path)
    flagged = flagged_session_ids(queue_entries)
    pack = collect_corpus(data_root, since, cfg["project_include"],
                          cfg["project_exclude"], correction_re, caps, flagged)
    constraints = constraints_pack(state / "state" / "ledger.jsonl")
    write_pack(Path(a.out), pack, constraints)
    append_metrics(state, metrics_row(a.run_id, pack))
    mark_queue_consumed(queue_path, flagged)
    t = pack["usage"]["totals"]
    print(f"sessions={t['sessions']} corrections={pack['usage']['corrections']['total']} "
          f"dup_reads={pack['usage']['waste']['duplicate_reads_total']} "
          f"stalls={pack['usage']['waste']['permission_stalls_total']} "
          f"parse_skipped={pack['usage']['parse']['skipped_lines']} "
          f"queue_flagged={len(flagged)} queue_malformed={queue_malformed}")


if __name__ == "__main__":
    main()
