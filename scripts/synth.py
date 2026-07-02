"""Synthesizer: validate analyst output, machine-check every citation against the
evidence pack (drop what doesn't resolve — 'check me', not 'trust me'), guard action
payloads (defense-in-depth vs prompt injection via transcripts), compute deterministic
Tier-1 token deltas, dedup against the ledger, rank."""
import argparse
import json
import os
from pathlib import Path

import ledger as ledger_mod
import schema as so_schema

ALLOWED_SETTING_ROOTS = {"skillOverrides", "enabledPlugins", "model", "outputStyle", "effortLevel"}
ORD = {"high": 3, "med": 2, "low": 1}


def load_analyst_output(path) -> list:
    text = Path(path).read_text().strip()
    if text.startswith("```"):
        parts = text.split("\n", 1)
        text = parts[1].rsplit("```", 1)[0] if len(parts) > 1 else ""
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return []
    return data if isinstance(data, list) else []


def _resolve_path(d: dict, dotted: str) -> bool:
    node = d
    for part in dotted.split("."):
        if isinstance(node, dict) and part in node:
            node = node[part]
        else:
            return False
    return True


def check_citation(ref: str, ev: dict) -> bool:
    if not isinstance(ref, str):
        return False
    kind, _, rest = ref.partition(":")
    if kind == "usage":
        return _resolve_path(ev["usage"], rest)
    if kind == "activation":
        return rest in ev["activation"]["items"]
    if kind == "sample":
        return rest.isdigit() and int(rest) < len(ev["samples"]["samples"])
    if kind == "session":
        return any(s["id"] == rest for s in ev["sessions"]["sessions"])
    if kind == "inventory":
        inv = ev.get("inventory", {})
        pool = {i["id"] for g in ("skills", "agents", "mcp_servers", "plugins")
                for i in inv.get(g, [])}
        pool |= {f"claude_md:{c['path']}" for c in inv.get("claude_md", [])}
        return rest in pool
    if kind == "rule":
        return rest in {r["id"] for r in ev.get("rules", {}).get("rules", [])}
    return False


def _under(f: str, data_root: Path, sub: str) -> bool:
    # ponytail: lexical normpath, not resolve() — resolve() follows symlinks and
    # would reject legitimately symlinked skills dirs; normpath collapses ".."
    # without touching the filesystem. Separator suffix blocks sibling-dir prefixes.
    target = os.path.normpath(str(Path(f).expanduser()))
    base = os.path.normpath(str(Path(data_root) / sub))
    return target == base or target.startswith(base + os.sep)


def guard(rec: dict, data_root: Path) -> bool:
    a = rec["action"]
    p = a.get("payload", {})
    t = a["type"]
    if t == "setting_change":
        kp = p.get("key_path") or []
        return bool(kp) and kp[0] in ALLOWED_SETTING_ROOTS
    if t == "frontmatter_edit":
        return ((_under(p.get("file", ""), data_root, "skills") or _under(p.get("file", ""), data_root, "agents"))
                and p.get("key") in {"model", "disable-model-invocation", "description"})
    if t == "file_create":
        f = p.get("path", "")
        return ((_under(f, data_root, "skills") or _under(f, data_root, "agents"))
                and str(f).endswith(".md"))
    return t in so_schema.ACTION_TYPES_B  # tier B is report-only: rendered, never executed


def tier1_delta(rec: dict, ev: dict):
    inv = ev.get("inventory", {})
    sessions = ev["usage"]["totals"]["sessions"]
    a = rec["action"]
    p = a.get("payload", {})
    kp = p.get("key_path") or [""]
    if a["type"] == "setting_change" and kp[0] == "skillOverrides" and p.get("value") in ("off", "name-only"):
        item = next((s for s in inv.get("skills", []) if s["name"] == kp[1]), None)
        return item["est_context_tokens"] * sessions if item else None
    if a["type"] == "setting_change" and kp[0] == "enabledPlugins" and p.get("value") is False:
        src = f"plugin:{kp[1]}"
        toks = sum(i["est_context_tokens"] for g in ("skills", "agents")
                   for i in inv.get(g, []) if i.get("source") == src)
        return toks * sessions if toks else None
    if a["type"] == "frontmatter_edit" and p.get("key") == "disable-model-invocation":
        f = str(Path(p.get("file", "")).expanduser())
        item = next((s for s in inv.get("skills", []) if s.get("path") == f), None)
        return item["est_context_tokens"] * sessions if item else None
    return None


def synthesize(analyst_recs: list, ev: dict, led_entries: dict, data_root: Path):
    dropped = {"invalid": 0, "citations": 0, "guard": 0, "suppressed": []}
    seen, findings = set(), []
    for rec in analyst_recs:
        if not isinstance(rec, dict) or so_schema.validate_rec(rec):
            dropped["invalid"] += 1
            continue
        if not all(check_citation(r, ev) for r in rec["evidence_refs"]):
            dropped["citations"] += 1
            continue
        if not guard(rec, data_root):
            dropped["guard"] += 1
            continue
        rec["id"] = so_schema.rec_id(rec)
        rec["evidence_hash"] = so_schema.evidence_hash(rec)
        if rec["id"] in seen:
            continue
        seen.add(rec["id"])
        reason = ledger_mod.suppress_reason(rec, led_entries)
        if reason:
            dropped["suppressed"].append({"id": rec["id"], "title": rec["title"], "reason": reason})
            continue
        prior = led_entries.get(rec["id"])
        if prior and prior.get("status") == "rejected":
            rec["prior_rejection"] = prior.get("reason", "")
        rec["delta_tokens"] = tier1_delta(rec, ev)
        findings.append(rec)
    findings.sort(key=lambda r: (r["delta_tokens"] is None, -(r["delta_tokens"] or 0),
                                 -ORD.get(r["impact"].get("ordinal", "low"), 1)))
    return findings, dropped


def main(argv=None):
    import so_config
    ap = argparse.ArgumentParser()
    ap.add_argument("--evidence", required=True)
    ap.add_argument("--data-root", default=None)
    ap.add_argument("--state", default=None)
    ap.add_argument("--rules", required=True)
    ap.add_argument("--analyst", action="append", required=True)
    ap.add_argument("--out", required=True)
    a = ap.parse_args(argv)
    data_root, state = so_config.resolve(a.data_root, a.state)
    evdir = Path(a.evidence)
    ev = {name: json.loads((evdir / f"{name}.json").read_text())
          for name in ("usage", "sessions", "activation", "samples")}
    inv_f = evdir / "inventory.json"
    ev["inventory"] = json.loads(inv_f.read_text()) if inv_f.exists() else {}
    ev["rules"] = json.loads(Path(a.rules).read_text())
    lpath = state / "state" / "ledger.jsonl"
    led = ledger_mod.load(lpath)
    recs = [r for f in a.analyst for r in load_analyst_output(f)]
    findings, dropped = synthesize(recs, ev, led, data_root)
    for rec in findings:
        ledger_mod.append(lpath, {"id": rec["id"], "status": "proposed", "rec": rec,
                                  "evidence_hash": rec["evidence_hash"]})
    out_path = Path(a.out)
    out_path.write_text(json.dumps({"findings": findings, "dropped": dropped}, indent=1))
    out_path.chmod(0o600)
    print(f"findings={len(findings)} dropped_invalid={dropped['invalid']} "
          f"dropped_citations={dropped['citations']} dropped_guard={dropped['guard']} "
          f"suppressed={len(dropped['suppressed'])}")


if __name__ == "__main__":
    main()
