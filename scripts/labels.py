"""Validate the sample-labeler's output and persist per-category correction
counts to the metrics trend. Labels attribute correction movement to specific
behavior categories — the per-rule attribution a global correction_rate can't
give. Invalid labels are dropped and counted, never guessed."""
import argparse
import json
from pathlib import Path

import inventory as inventory_mod
import synth as synth_mod

CATEGORIES = ("scope-creep", "wrong-target", "over-engineering", "verbosity",
              "style", "wrong-assumption", "premature-action", "other")


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
