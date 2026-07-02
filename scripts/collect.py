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
                yield json.loads(line)
            except json.JSONDecodeError:
                counters["skipped_lines"] += 1


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
        for attr, prefix in (("attributionSkill", "skill"), ("attributionPlugin", "plugin"),
                             ("attributionMcpServer", "mcp")):
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
                    s["activation"][f"skill:{inp['skill']}"] += 1
                if name in ("Agent", "Task") and inp.get("subagent_type"):
                    s["activation"][f"agent:{inp['subagent_type']}"] += 1
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
