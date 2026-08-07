"""Validate the sample-labeler's output and persist per-category correction
counts to the metrics trend. Labels attribute correction movement to specific
behavior categories — the per-rule attribution a global correction_rate can't
give. Invalid labels are dropped and counted, never guessed.

Taxonomy v2 adapts MAST (Cemri et al., "Why Do Multi-Agent LLM Systems Fail?",
arXiv:2503.13657) to single-agent tool use. Full mode-by-mode mapping and the
old-taxonomy migration rationale: agents/taxonomy-v2-mast-mapping.md."""
import argparse
import json
from pathlib import Path

import inventory as inventory_mod
import synth as synth_mod

# 14 MAST failure modes adapted to single-agent tool use, plus "other" — a
# catch-all MAST's exhaustive-by-construction taxonomy doesn't need but a
# real correction stream does.
CATEGORIES = (
    "spec-violation", "role-violation", "step-repetition", "context-loss",
    "no-stop-condition", "plan-reset", "no-clarification", "scope-creep",
    "info-withholding", "ignored-input", "reasoning-action-mismatch",
    "premature-termination", "no-verification", "incorrect-verification",
    "other",
)

# Static v1 (pre-MAST) -> v2 migration so historical metrics.jsonl rows render
# under today's names instead of the trend resetting to zero. Several v1
# categories collapse into one v2 category (MAST doesn't distinguish
# "assumed" from "acted without asking" the way the old set implied it did);
# migrate_categories sums counts on collision. See
# agents/taxonomy-v2-mast-mapping.md for the per-row rationale.
LEGACY_CATEGORY_MAP = {
    "scope-creep": "scope-creep",
    "wrong-target": "spec-violation",
    "over-engineering": "spec-violation",
    "verbosity": "no-stop-condition",
    "style": "role-violation",
    "wrong-assumption": "no-clarification",
    "premature-action": "no-clarification",
    "other": "other",
}


def migrate_categories(counts: dict) -> dict:
    """Fold a corrections_by_category dict keyed by v1 names into v2 names,
    summing counts that land on the same v2 category. A key already in v2's
    vocabulary (or any key the map doesn't recognize) passes through as-is,
    so this is safe to apply to both old and new rows alike."""
    out = {}
    for k, v in (counts or {}).items():
        nk = LEGACY_CATEGORY_MAP.get(k, k)
        out[nk] = out.get(nk, 0) + v
    return out


def validate_labels(raw: list, n_samples: int):
    counts = {}
    dropped = 0
    seen = set()
    for item in raw:
        if (not isinstance(item, dict)
                or not isinstance(item.get("sample"), int)
                or not 0 <= item["sample"] < n_samples
                or item.get("category") not in CATEGORIES
                or item["sample"] in seen):
            dropped += 1
            continue
        seen.add(item["sample"])
        counts[item["category"]] = counts.get(item["category"], 0) + 1
    return counts, dropped


def main(argv=None):
    import so_config
    ap = argparse.ArgumentParser()
    ap.add_argument("--evidence", required=True)
    ap.add_argument("--state", default=None)
    a = ap.parse_args(argv)
    _, state = so_config.resolve(None, a.state)
    ev = Path(a.evidence)
    lfile = ev / "labels.json"
    if not lfile.exists():
        print("labels=0 (no labels.json)")
        return
    raw = synth_mod.load_analyst_output(lfile)
    n = len(json.loads((ev / "samples.json").read_text()).get("samples", []))
    counts, dropped = validate_labels(raw, n)
    lfile.write_text(json.dumps({"counts": counts, "dropped": dropped}, indent=1))
    lfile.chmod(0o600)
    inventory_mod.update_last_metrics(state, corrections_by_category=counts)
    print(f"labels={sum(counts.values())} dropped={dropped} "
          + " ".join(f"{k}={v}" for k, v in sorted(counts.items())))


if __name__ == "__main__":
    main()
