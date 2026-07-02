"""Step 0 of every run: for each applied recommendation, recompute its declared
metric over sessions AFTER the apply timestamp only. Pure arithmetic, zero LLM cost.
Verdicts flag; they never auto-rollback."""
import argparse
import json
import math
from pathlib import Path

import ledger as ledger_mod
import metriclib


def _welch_p(a: list, b: list):
    """Welch's t-test on two unequal-variance samples; the p-value is a normal
    approximation to the (unknown-df) t-distribution via math.erfc, not an exact
    Student-t CDF — adequate for a directional 'is this real' gate, not a paper."""
    na, nb = len(a), len(b)
    mean_a, mean_b = sum(a) / na, sum(b) / nb
    var_a = sum((x - mean_a) ** 2 for x in a) / (na - 1)
    var_b = sum((x - mean_b) ** 2 for x in b) / (nb - 1)
    se = math.sqrt(var_a / na + var_b / nb)
    if se == 0:
        return 0.0 if mean_a != mean_b else 1.0
    t = (mean_a - mean_b) / se
    return math.erfc(abs(t) / math.sqrt(2))


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
               "direction": m.get("direction"),
               "baseline": base, "value": val, "n": n,
               "verdict": "inconclusive", "rel_change": None, "p_value": None}
        floor_ok = (n >= cfg["verify"]["min_sessions"]
                    or m.get("key") in ("base_context_est", "unused_surface_count"))
        if floor_ok and val is not None and base not in (None, 0):
            rel = (val - base) / abs(base)
            row["rel_change"] = rel
            t = cfg["verify"]["min_rel_change"]
            want = m.get("direction")
            base_samples = (e.get("baseline") or {}).get("samples") or []
            cur_samples = metriclib.metric_samples(m, sessions, inventory,
                                                    after_ts=e.get("applied_at"))
            sig_ok = True
            if len(base_samples) >= 2 and len(cur_samples) >= 2:
                p = _welch_p([float(x) for x in base_samples], [float(x) for x in cur_samples])
                row["p_value"] = p
                sig_ok = p < 0.05
            if sig_ok and ((want == "down" and rel <= -t) or (want == "up" and rel >= t)):
                row["verdict"] = "verified"
            elif sig_ok and ((want == "down" and rel >= t) or (want == "up" and rel <= -t)):
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
    out_path = Path(a.out)
    out_path.write_text(json.dumps({"rows": rows}, indent=1))
    out_path.chmod(0o600)
    for r in rows:
        if r["verdict"] in ("verified", "regressed"):
            # carry forward apply context: ledger.load is last-entry-wins, so the
            # verdict entry must keep files/snapshot or rollback finds nothing
            prior = entries.get(r["id"], {})
            ledger_mod.append(lpath, {"id": r["id"], "status": r["verdict"],
                                      "measured": {"value": r["value"], "n": r["n"],
                                                   "rel_change": r["rel_change"],
                                                   "p_value": r["p_value"]},
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
