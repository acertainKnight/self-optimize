"""Evidence-pack + recommendation schema: constants, validation, id hashing,
and the shared minimal frontmatter parser (stdlib-only, flat key: value)."""
import hashlib
import json

EVIDENCE_VERSION = "1"
HARNESS = "claude-code"

ACTION_TYPES_A = {"setting_change", "frontmatter_edit", "file_create"}
ACTION_TYPES_B = {"diff", "manual"}
CATEGORIES = {"bloat", "model-routing", "skill-edit", "new-skill", "claude-md",
              "hooks", "permissions", "settings", "waste"}
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
    if errs:
        return errs
    t = a.get("type")
    if t not in ACTION_TYPES_A | ACTION_TYPES_B:
        errs.append(f"bad action type: {t}")
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
