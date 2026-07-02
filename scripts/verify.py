"""Step 0 of every run: for each applied recommendation, recompute its declared
metric over sessions AFTER the apply timestamp only. Pure arithmetic, zero LLM cost.
Verdicts flag; they never auto-rollback."""
import argparse
import json
from pathlib import Path

import ledger as ledger_mod
import metriclib


def verify_entries(entries: dict, sessions: list, inventory, cfg: dict) -> list:
    rows = []
    for rid, e in entries.items():
        if e.get("status") != "applied":
            continue
        rec = e.get("rec") or {}
        m = rec.get("metric") or {"key": "none"}
        if m.get("key") == "none":
            continue
        val, n = metriclib.compute_metric(m, sessions, inventory, after_ts=e.get("applied_at"))
        base = (e.get("baseline") or {}).get("value")
        row = {"id": rid, "title": rec.get("title", ""), "metric": m["key"],
               "baseline": base, "value": val, "n": n,
               "verdict": "inconclusive", "rel_change": None}
        if n >= cfg["verify"]["min_sessions"] and val is not None and base not in (None, 0):
            rel = (val - base) / abs(base)
            row["rel_change"] = rel
            t = cfg["verify"]["min_rel_change"]
            want = m.get("direction")
            if (want == "down" and rel <= -t) or (want == "up" and rel >= t):
                row["verdict"] = "verified"
            elif (want == "down" and rel >= t) or (want == "up" and rel <= -t):
                row["verdict"] = "regressed"
        rows.append(row)
    return rows


def main(argv=None):
    import so_config
    ap = argparse.ArgumentParser()
    ap.add_argument("--evidence", required=True)
    ap.add_argument("--state", default=None)
    ap.add_argument("--out", required=True)
    a = ap.parse_args(argv)
    _, state = so_config.resolve(None, a.state)
    cfg = so_config.load_config(state)
    lpath = state / "state" / "ledger.jsonl"
    entries = ledger_mod.load(lpath)
    ev = Path(a.evidence)
    sessions = json.loads((ev / "sessions.json").read_text())["sessions"]
    inv_f = ev / "inventory.json"
    inventory = json.loads(inv_f.read_text()) if inv_f.exists() else None
    rows = verify_entries(entries, sessions, inventory, cfg)
    Path(a.out).write_text(json.dumps({"rows": rows}, indent=1))
    for r in rows:
        if r["verdict"] in ("verified", "regressed"):
            # carry forward apply context: ledger.load is last-entry-wins, so the
            # verdict entry must keep files/snapshot or rollback finds nothing
            prior = entries.get(r["id"], {})
            ledger_mod.append(lpath, {"id": r["id"], "status": r["verdict"],
                                      "measured": {"value": r["value"], "n": r["n"],
                                                   "rel_change": r["rel_change"]},
                                      "rec": prior.get("rec"),
                                      "files": prior.get("files"),
                                      "snapshot": prior.get("snapshot"),
                                      "applied_at": prior.get("applied_at"),
                                      "baseline": prior.get("baseline"),
                                      "evidence_hash": prior.get("evidence_hash")})
    print(f"verified={sum(r['verdict'] == 'verified' for r in rows)} "
          f"regressed={sum(r['verdict'] == 'regressed' for r in rows)} "
          f"inconclusive={sum(r['verdict'] == 'inconclusive' for r in rows)}")


if __name__ == "__main__":
    main()
