"""Evidence-pack + recommendation schema: constants, validation, id hashing,
the shared minimal frontmatter parser (stdlib-only, flat key: value), and the
bounded-edit applier both the renderer and the gym build candidates with."""
import hashlib
import json
import os
from pathlib import Path

EVIDENCE_VERSION = "1"
HARNESS = "claude-code"

ACTION_TYPES_A = {"setting_change", "frontmatter_edit", "file_create", "file_replace",
                  "file_ops"}
# toml_key_edit/jsonc_ops/agents_md_ops: the non-Claude config surfaces (Codex's
# config.toml, opencode's opencode.jsonc, either harness's AGENTS.md) -- always
# tier B, human approves each item, never gym-eligible for A like file_ops is.
ACTION_TYPES_B = {"diff", "manual", "retire", "toml_key_edit", "jsonc_ops", "agents_md_ops"}
OP_KINDS = {"add", "delete", "replace"}
# Applicability is a function of action TYPE, not tier: every type machine-applies
# except manual (hooks/permissions/free-form hand-off, forever a human step).
# retire is tier B (reversible move to an archive dir, not a plain edit) but still
# machine-applicable, same as diff.
APPLICABLE_TYPES = (ACTION_TYPES_A | ACTION_TYPES_B) - {"manual"}
CATEGORIES = {"bloat", "model-routing", "skill-edit", "new-skill", "claude-md",
              "hooks", "permissions", "settings", "waste",
              "skill-improve", "new-agent", "new-workflow", "new-plugin", "memory",
              "skill-dedupe", "skill-retire"}
METRIC_KEYS = {"tokens_per_session", "correction_rate", "duplicate_read_rate",
               "permission_stalls", "base_context_est", "unused_surface_count",
               "model_output_tokens", "none"}
DIRECTIONS = {"up", "down", "none"}
REQUIRED = ["title", "category", "evidence_refs", "impact", "risk", "metric", "action"]


def rec_id(rec: dict) -> str:
    basis = "|".join([
        rec["category"], rec["action"]["type"],
        json.dumps(rec["action"].get("payload", {}), sort_keys=True),
    ])
    return hashlib.sha256(basis.encode()).hexdigest()[:12]


def evidence_hash(rec: dict) -> str:
    basis = json.dumps(sorted(rec.get("evidence_refs", [])))
    return hashlib.sha256(basis.encode()).hexdigest()[:12]


def derive_extra_roots(inventory: dict, data_root=None):
    """Extra sanctioned write roots from evidence inventory. Fail closed:
    only a non-empty, absolute (after ~ expansion) string qualifies, and it
    must not overlap the data-root's own sanctioned subdirs (a memory dir
    inside skills/agents/workflows would defeat the create-only rule there)."""
    settings = (inventory or {}).get("settings")
    amd = settings.get("autoMemoryDirectory") if isinstance(settings, dict) else None
    if not isinstance(amd, str) or not amd.strip():
        return None
    expanded = os.path.normpath(str(Path(amd).expanduser()))
    if not os.path.isabs(expanded):
        return None
    if data_root is not None:
        for sub in ("skills", "agents", "workflows"):
            base = os.path.normpath(str(Path(data_root) / sub))
            if expanded == base or expanded.startswith(base + os.sep):
                return None
    return [expanded]


def is_rewrite(action: dict) -> bool:
    """True when a payload carries a whole new artifact body instead of bounded edits.
    Whole-file rewrites are the degradation mode bounded edits exist to avoid, so an
    analyst that wants one must say so with `op: "rewrite"` in the payload — and it is
    always tier B, never the one-click path."""
    if not isinstance(action, dict):
        return False
    p = action.get("payload")
    return isinstance(p, dict) and p.get("op") == "rewrite"


def _validate_ops_shape(ops) -> list[str]:
    """The bounded-edit ops-list check shared by every op-based action type:
    file_ops (an explicit target path in the payload) and the non-Claude
    harness-surface types jsonc_ops/agents_md_ops (a fixed target, no path in
    the payload at all). Shape only — whether an anchor actually resolves is
    decided at apply time against the live file, where a miss or a duplicate
    refuses the whole set."""
    if not isinstance(ops, list) or not ops:
        return ["payload needs a non-empty ops list"]
    errs = []
    for i, op in enumerate(ops):
        if not isinstance(op, dict):
            errs.append(f"op {i}: must be an object")
            continue
        kind = op.get("op")
        if kind not in OP_KINDS:
            errs.append(f"op {i}: op must be add|delete|replace, got {kind!r}")
        anchor = op.get("anchor")
        if not isinstance(anchor, str) or not anchor.strip():
            errs.append(f"op {i}: anchor must be a non-empty existing line")
        elif "\n" in anchor:
            errs.append(f"op {i}: anchor must be a single line")
        text = op.get("text", "")
        if kind == "delete":
            if text:
                errs.append(f"op {i}: delete carries no text")
        elif not isinstance(text, str) or not text.strip():
            errs.append(f"op {i}: {kind} needs non-empty text")
        refs = op.get("motivated_by")
        if not isinstance(refs, list) or not refs or not all(isinstance(r, str) for r in refs):
            errs.append(f"op {i}: motivated_by must be a non-empty list of evidence refs")
    return errs


def validate_ops(payload: dict) -> list[str]:
    """Shape of a file_ops payload: a path plus an ordered list of bounded edits, each
    naming one exact existing line as its anchor and carrying its own evidence refs."""
    if not isinstance(payload, dict):
        return ["file_ops payload must be an object"]
    if not isinstance(payload.get("path"), str) or not payload["path"].strip():
        return ["file_ops payload needs a path"]
    return _validate_ops_shape(payload.get("ops"))


def apply_ops(original: str, ops: list) -> str:
    """Apply ordered bounded edits by EXACT full-line anchor match.

    Fail closed, in both directions. An anchor matching no line or more than one line
    refuses the whole set: a wrong guess about which bullet was meant is exactly the
    edit a human would have caught. Re-applying an already-applied set also refuses —
    an `add` whose text already follows its anchor, and a `delete`/`replace` whose
    anchor is gone, raise rather than edit twice. Every op sees the text as the
    previous ops left it, so the order in the payload is the order of record.

    ponytail: LF-only line splitting, same as the unified-diff applier — enough for
    the .md artifacts this edits.
    """
    lines = original.split("\n")
    for i, op in enumerate(ops):
        kind, anchor = op["op"], op["anchor"]
        text = op.get("text") or ""
        new = text.split("\n") if text else []
        hits = [n for n, line in enumerate(lines) if line == anchor]
        if len(hits) > 1:
            raise ValueError(f"op {i} ({kind}): ambiguous anchor — {len(hits)} lines "
                             f"match {anchor[:120]!r}")
        if not hits:
            raise ValueError(f"op {i} ({kind}): anchor not found — already applied, or the "
                             f"artifact changed since this was proposed: {anchor[:120]!r}")
        at = hits[0]
        if kind == "add":
            if lines[at + 1:at + 1 + len(new)] == new:
                raise ValueError(f"op {i} (add): already applied — this text already "
                                 f"follows {anchor[:120]!r}")
            lines[at + 1:at + 1] = new
        elif kind == "replace":
            lines[at:at + 1] = new
        elif kind == "delete":
            del lines[at]
        else:
            raise ValueError(f"op {i}: unknown op {kind!r}")
    return "\n".join(lines)


def validate_rec(rec: dict) -> list[str]:
    errs = [f"missing {k}" for k in REQUIRED if k not in rec]
    if errs:
        return errs
    a = rec["action"]
    m = rec["metric"]
    if not isinstance(a, dict):
        errs.append("action must be an object")
    if not isinstance(m, dict):
        errs.append("metric must be an object")
    if not isinstance(rec["impact"], dict):
        errs.append("impact must be an object")
    if errs:
        return errs
    if rec["impact"].get("ordinal") not in ("high", "med", "low"):
        errs.append("impact.ordinal must be high|med|low")
    t = a.get("type")
    if t not in ACTION_TYPES_A | ACTION_TYPES_B:
        errs.append(f"bad action type: {t}")
    elif is_rewrite(a):
        if a.get("tier") != "B":
            errs.append("tier must be B for a payload marked op: rewrite")
    elif t == "file_ops":
        # tier-A-ELIGIBLE, not tier-A-guaranteed: a bounded edit the gym could not
        # score is downgraded to B before it renders, so both values are valid here.
        if a.get("tier") not in ("A", "B"):
            errs.append("tier must be A or B for file_ops")
        errs += validate_ops(a.get("payload"))
    elif t in ("jsonc_ops", "agents_md_ops"):
        if a.get("tier") != "B":
            errs.append(f"tier must be B for {t}")
        payload = a.get("payload")
        if not isinstance(payload, dict):
            errs.append(f"{t} payload must be an object")
        else:
            errs += _validate_ops_shape(payload.get("ops"))
            if t == "agents_md_ops" and payload.get("harness") not in ("codex", "opencode"):
                errs.append("agents_md_ops payload needs harness: codex or opencode")
    elif t == "toml_key_edit":
        if a.get("tier") != "B":
            errs.append("tier must be B for toml_key_edit")
        payload = a.get("payload")
        if not isinstance(payload, dict):
            errs.append("toml_key_edit payload must be an object")
        else:
            kp = payload.get("key_path")
            if not isinstance(kp, list) or not kp or not all(isinstance(k, str) for k in kp):
                errs.append("toml_key_edit payload needs a non-empty key_path list of strings")
            if "value" not in payload:
                errs.append("toml_key_edit payload needs a value")
    else:
        want = "A" if t in ACTION_TYPES_A else "B"
        if a.get("tier") != want:
            errs.append(f"tier must be {want} for {t}")
    if rec["category"] not in CATEGORIES:
        errs.append(f"bad category: {rec['category']}")
    if m.get("key") not in METRIC_KEYS:
        errs.append(f"bad metric key: {m.get('key')}")
    if m.get("key") != "none" and m.get("direction") not in DIRECTIONS:
        errs.append("metric.direction required")
    if not isinstance(rec["evidence_refs"], list) or not rec["evidence_refs"]:
        errs.append("evidence_refs must be a non-empty list")
    return errs


def is_applicable(action: dict) -> bool:
    """True for any action type the machine may apply (snapshot/write/smoke/rollback):
    every tier-A type plus diff. Only manual is a permanent human hand-off."""
    return isinstance(action, dict) and action.get("type") in APPLICABLE_TYPES


def parse_frontmatter(text: str) -> dict:
    if not text.startswith("---"):
        return {}
    end = text.find("\n---", 3)
    if end == -1:
        return {}
    out = {}
    for line in text[3:end].strip().splitlines():
        if ":" in line and not line.startswith((" ", "\t", "#")):
            k, v = line.split(":", 1)
            out[k.strip()] = v.strip()
    return out
