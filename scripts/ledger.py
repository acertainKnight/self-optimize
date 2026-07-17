"""Append-only recommendation ledger + run log. The spine of the loop:
proposed -> approved/applied -> verified|regressed|inconclusive, plus rejected
(with reason) and apply_failed. Rejected items stay suppressed until their
evidence hash changes."""
import json
from datetime import datetime, timezone
from pathlib import Path

APPLIED_LIKE = {"applied", "verified", "regressed", "inconclusive", "apply_failed", "rolled_back"}


def load(path: Path) -> dict:
    path = Path(path)
    if not path.exists():
        return {}
    out = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            e = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(e, dict) and "id" in e:
            out[e["id"]] = e
    return out


def append(path: Path, entry: dict) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    entry = dict(entry)
    entry.setdefault("ts", datetime.now(timezone.utc).isoformat())
    with open(path, "a") as f:
        f.write(json.dumps(entry) + "\n")


def suppress_reason(rec: dict, entries: dict) -> str | None:
    e = entries.get(rec["id"])
    if not e:
        return None
    if e["status"] in APPLIED_LIKE:
        return "already applied"
    if e["status"] == "rejected":
        if e.get("evidence_hash") == rec.get("evidence_hash"):
            return f"rejected: {e.get('reason', '')}"
        return None  # evidence changed -> resurface (synth attaches prior_rejection)
    return None  # proposed/approved -> re-propose freely
