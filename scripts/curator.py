"""Curator: dedupe, retire, and lifecycle-metadata findings for the shared skill
library (Claude Code adapter). Stage 1 (always runs) is fully deterministic --
difflib token-ratio similarity over skill bodies, name/description overlap,
activation counts, frontmatter presence -- zero LLM calls. Stage 2 (only for
duplicate candidates, only when a backend is configured) asks an LLM to draft
merged skill text, through the exact same pluggable subprocess driver gym.py's
judge uses (gym.judge.command in config.json, via gym._invoke_judge): any CLI
that reads a prompt on stdin and writes to stdout. No default backend, model,
or vendor here either -- an unconfigured backend just skips stage 2, never a
silent fallback to a picked one.

Findings are emitted in the same shape the LLM analysts return (title,
category, evidence_refs, impact, risk, metric, action), so they flow through
the ordinary synth.py pipeline -- citation checks, guard, tiers, ledger --
completely unchanged. Metadata additions are tier-A frontmatter_edit. A
duplicate pair with a successful merge proposal becomes a tier-B diff on the
surviving skill plus a tier-B retire on the redundant one; a duplicate pair
with no merge proposal (stage 2 skipped or failed) is a tier-B manual note for
a human to resolve by hand. retire is always reversible: it moves the file
into a `.retired` archive dir under its own root rather than deleting it (see
adapters/claude_code/templates.py's render() for the "retire" action type).
"""
import argparse
import difflib
import json
from datetime import datetime, timezone
from pathlib import Path

import gym
import schema as so_schema

# lifecycle frontmatter every user skill is expected to carry; the default is
# what gets proposed when a key is missing (explicit "none" beats silent absence
# for superseded_by/requires-tools -- you can tell "checked, not superseded"
# apart from "never evaluated").
LIFECYCLE_KEYS = {"version": "1", "superseded_by": "none", "requires-tools": "none"}


def _read_text(path) -> str | None:
    try:
        return Path(path).read_text(errors="replace")
    except OSError:
        return None


def load_user_skills(inventory: dict) -> list:
    """Skills this instance owns (source == 'user') with body text read fresh
    from disk. inventory.json only carries a truncated description, and
    artifacts.json only carries the top 10 skills by activation count -- which
    would silently exclude the zero-activation skills curation cares about
    most. Plugin-owned skills are out of scope: they live outside data_root's
    skills dir, so no curator action here could touch them anyway (synth's
    guard confines every write to data_root/skills or data_root/agents)."""
    out = []
    for item in inventory.get("skills") or []:
        if item.get("source") != "user":
            continue
        body = _read_text(item.get("path", ""))
        if body is None:
            continue
        out.append({**item, "body": body})
    return out


# ---------------------------------------------------------------- stage 1: duplicates
def _ratio(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, a, b).ratio()


def find_duplicates(skills: list, floor: float = 0.6) -> list:
    """Pairwise body-similarity over every user skill.
    # ponytail: O(n^2) SequenceMatcher over full bodies -- fine at personal
    # skill-library scale (tens of skills), the same tradeoff collect.py
    # already makes for within-session re-ask detection. Add a length/shingle
    # prefilter if a library ever grows into the hundreds and this gets slow.
    """
    out = []
    for i in range(len(skills)):
        for j in range(i + 1, len(skills)):
            a, b = skills[i], skills[j]
            body_ratio = _ratio(a["body"], b["body"])
            if body_ratio < floor:
                continue
            desc_ratio = _ratio(a.get("description", ""), b.get("description", ""))
            out.append({"a": a, "b": b, "body_ratio": round(body_ratio, 3),
                        "desc_ratio": round(desc_ratio, 3)})
    out.sort(key=lambda d: -d["body_ratio"])
    return out


def _pick_survivor(a: dict, b: dict, activation: dict) -> tuple:
    """Deterministic tie-break: more-activated skill survives; ties break on
    name so the outcome does not depend on inventory scan order."""
    def key(s):
        return (activation.get(s["id"], {}).get("count", 0), s["name"])
    return (a, b) if key(a) >= key(b) else (b, a)


# ---------------------------------------------------------------- stage 1: retirement
def find_never_fired(skills: list, activation: dict) -> list:
    return [s for s in skills if activation.get(s["id"], {}).get("count", 0) == 0]


def find_long_unfired(skills: list, activation: dict, days: int, now=None) -> list:
    now = now or datetime.now(timezone.utc)
    out = []
    for s in skills:
        entry = activation.get(s["id"]) or {}
        count, last = entry.get("count", 0), entry.get("last_used")
        if count <= 0 or not last:
            continue
        try:
            last_dt = datetime.fromisoformat(str(last).replace("Z", "+00:00"))
        except ValueError:
            continue
        age_days = (now - last_dt).days
        if age_days >= days:
            out.append({**s, "days_unfired": age_days})
    return out


# ---------------------------------------------------------------- stage 1: metadata
def find_metadata_gaps(skills: list) -> list:
    out = []
    for s in skills:
        fm = so_schema.parse_frontmatter(s["body"])
        for key, default in LIFECYCLE_KEYS.items():
            if key not in fm:
                out.append({"skill": s, "key": key, "default": default})
    return out


# ---------------------------------------------------------------- stage 2: LLM merge (optional)
def build_merge_prompt(a: dict, b: dict) -> str:
    """Deterministic by construction (fixed sections, fixed order), mirroring
    gym.build_prompt's own rationale: the same pair always produces the same
    prompt."""
    lines = [
        "You are drafting one merged skill file from two near-duplicate skills in",
        "an agent-harness skill library. Output ONLY the complete merged file text",
        "(frontmatter + body) and nothing else -- no explanation, no code fences.",
        "",
        f"SURVIVING NAME (keep this in the merged frontmatter's name: field): {a['name']}",
        "",
        "--- SKILL A (survives) ---", a["body"].strip(), "--- END SKILL A ---", "",
        "--- SKILL B (folds into A; B is retired afterward) ---", b["body"].strip(),
        "--- END SKILL B ---", "",
        "Keep everything from A that is not redundant with B. Fold in anything B",
        "covers that A does not. Do not invent behavior neither skill already has.",
    ]
    return "\n".join(lines) + "\n"


def propose_merge(judge: dict, a: dict, b: dict) -> str | None:
    """None means stage 2 did not produce text for this pair -- no backend
    configured, or the configured one errored. Both fall back to the
    deterministic manual finding; neither crashes the whole scan."""
    if not judge.get("command"):
        return None
    try:
        text = gym._invoke_judge(judge, build_merge_prompt(a, b))
    except gym.JudgeError:
        return None
    text = text.strip()
    return text or None


# ---------------------------------------------------------------- finding builders
def _inv_ref(skill: dict) -> str:
    return f"inventory:{skill['id']}"


def _act_ref(skill: dict) -> str:
    return f"activation:{skill['id']}"


def metadata_finding(skill: dict, key: str, default: str) -> dict:
    return {"title": f"Add missing lifecycle field '{key}' to {skill['name']}",
            "category": "skill-edit", "evidence_refs": [_inv_ref(skill)],
            "impact": {"ordinal": "low"},
            "risk": "additive frontmatter only, no behavior change",
            "metric": {"key": "none"},
            "action": {"harness": "claude-code", "tier": "A", "type": "frontmatter_edit",
                       "payload": {"file": skill["path"], "key": key, "value": default}}}


def retire_finding(skill: dict, reason: str, evidence_refs: list) -> dict:
    return {"title": f"Retire {reason} skill: {skill['name']}",
            "category": "skill-retire", "evidence_refs": evidence_refs,
            "impact": {"ordinal": "med"},
            "risk": "moved to an archive dir, never deleted -- reversible with "
                    "/self-optimize rollback",
            "metric": {"key": "base_context_est", "direction": "down", "scope": "global"},
            "action": {"harness": "claude-code", "tier": "B", "type": "retire",
                       "payload": {"path": skill["path"]}}}


def dedupe_diff_finding(survivor: dict, retiree: dict, merged_text: str) -> dict:
    diff = "".join(difflib.unified_diff(survivor["body"].splitlines(keepends=True),
                                        merged_text.splitlines(keepends=True)))
    return {"title": f"Merge near-duplicate skills: {survivor['name']} + {retiree['name']}",
            "category": "skill-dedupe",
            "evidence_refs": [_inv_ref(survivor), _inv_ref(retiree)],
            "impact": {"ordinal": "med"},
            "risk": "review the merged text before relying on it in place of both originals",
            "metric": {"key": "base_context_est", "direction": "down", "scope": "global"},
            "action": {"harness": "claude-code", "tier": "B", "type": "diff",
                       "payload": {"file": survivor["path"], "diff": diff}}}


def dedupe_manual_finding(a: dict, b: dict, pair: dict) -> dict:
    return {"title": f"Near-duplicate skills: {a['name']} and {b['name']}",
            "category": "skill-dedupe", "evidence_refs": [_inv_ref(a), _inv_ref(b)],
            "impact": {"ordinal": "med"},
            "risk": "no merge backend configured -- review and merge or retire by hand",
            "metric": {"key": "none"},
            "action": {"harness": "claude-code", "tier": "B", "type": "manual",
                       "payload": {"description":
                                   f"{a['name']} ({a['path']}) and {b['name']} ({b['path']}) "
                                   f"are {round(pair['body_ratio'] * 100)}% similar by body text "
                                   "-- merge the overlap or retire the redundant one"}}}


# ---------------------------------------------------------------- driver
def scan(inventory: dict, activation: dict, cfg: dict) -> list:
    ccfg = cfg.get("curator") or {}
    body_floor = float(ccfg.get("dup_body_floor", 0.6))
    unfired_days = int(ccfg.get("long_unfired_days", 60))
    act = activation.get("items") or {}
    skills = load_user_skills(inventory)

    findings = []
    for s in find_never_fired(skills, act):
        findings.append(retire_finding(s, "never-fired", [_inv_ref(s)]))
    for s in find_long_unfired(skills, act, unfired_days):
        findings.append(retire_finding(s, f"long-unfired ({s['days_unfired']}d)", [_act_ref(s)]))
    for gap in find_metadata_gaps(skills):
        findings.append(metadata_finding(gap["skill"], gap["key"], gap["default"]))

    judge = (cfg.get("gym") or {}).get("judge") or {}
    for pair in find_duplicates(skills, body_floor):
        a, b = pair["a"], pair["b"]
        survivor, retiree = _pick_survivor(a, b, act)
        merged = propose_merge(judge, survivor, retiree)
        if merged:
            findings.append(dedupe_diff_finding(survivor, retiree, merged))
            findings.append(retire_finding(retiree, "merged-duplicate",
                                           [_inv_ref(survivor), _inv_ref(retiree)]))
        else:
            findings.append(dedupe_manual_finding(a, b, pair))
    return findings


def main(argv=None):
    import so_config
    ap = argparse.ArgumentParser()
    ap.add_argument("--evidence", required=True)
    ap.add_argument("--state", default=None)
    ap.add_argument("--out", required=True)
    a = ap.parse_args(argv)
    _, state = so_config.resolve(None, a.state)
    cfg = so_config.load_config(state)
    ev = Path(a.evidence)
    inv_f, act_f = ev / "inventory.json", ev / "activation.json"
    inventory = json.loads(inv_f.read_text()) if inv_f.exists() else {}
    activation = json.loads(act_f.read_text()) if act_f.exists() else {}
    findings = scan(inventory, activation, cfg)
    out = Path(a.out)
    out.write_text(json.dumps(findings, indent=1))
    out.chmod(0o600)
    dedupe = sum(1 for f in findings if f["category"] == "skill-dedupe")
    retire = sum(1 for f in findings if f["category"] == "skill-retire")
    metadata = sum(1 for f in findings if f["category"] == "skill-edit")
    print(f"curator: findings={len(findings)} dedupe={dedupe} retire={retire} metadata={metadata}")


if __name__ == "__main__":
    main()
