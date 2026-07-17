"""Step 0 of every run: for each applied recommendation, recompute its declared
metric over sessions AFTER the apply timestamp only. Pure arithmetic, zero LLM cost.
Verdicts flag; they never auto-rollback."""
import argparse
import json
import math
from pathlib import Path

import ledger as ledger_mod
import metriclib


INVENTORY_METRICS = ("base_context_est", "unused_surface_count")


def _setting_in_effect(payload: dict, settings: dict | None):
    """None = cannot determine; True/False = the applied value is/isn't still set."""
    if settings is None or payload.get("file") != "settings.json":
        return None
    node = settings
    for k in payload.get("key_path") or []:
        if not isinstance(node, dict) or k not in node:
            return False
        node = node[k]
    return node == payload.get("value")


COUNT_METRICS = ("correction_rate", "duplicate_read_rate", "permission_stalls")


def _binom_two_sided(k: int, n: int, p: float) -> float:
    """Exact two-sided binomial test: total probability of outcomes no more likely
    than the observed k under Binomial(n, p). This is the conditional two-sample
    Poisson rate test (k2 | k1+k2 ~ Binom under equal rates) — far better powered
    on sparse per-session counts than a t-test on their means. lgamma keeps the
    pmf finite at the thousands-of-events totals duplicate reads reach."""
    if n == 0:
        return 1.0
    logp, logq = math.log(p), math.log(1.0 - p)

    def log_pmf(i):
        return (math.lgamma(n + 1) - math.lgamma(i + 1) - math.lgamma(n - i + 1)
                + i * logp + (n - i) * logq)

    obs = log_pmf(k)
    return min(1.0, sum(math.exp(lp) for lp in map(log_pmf, range(n + 1))
                        if lp <= obs + 1e-9))


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


def verify_entries(entries: dict, sessions: list, inventory, cfg: dict,
                   settings: dict | None = None) -> list:
    rows = []
    for rid, e in entries.items():
        rec = e.get("rec") or {}
        m = rec.get("metric") or {"key": "none"}
        # snapshot verdicts are re-checked every run — the still-in-effect test is
        # a dict walk, and a hand-reverted setting must not stay "verified" forever.
        # Session-metric verdicts stay terminal.
        recheck = (m.get("key") in INVENTORY_METRICS
                   and e.get("status") in ("verified", "regressed"))
        if e.get("status") != "applied" and not recheck:
            continue
        if m.get("key") == "none":
            continue
        val, n = metriclib.compute_metric(m, sessions, inventory, after_ts=e.get("applied_at"))
        base = (e.get("baseline") or {}).get("value")
        row = {"id": rid, "title": rec.get("title", ""), "metric": m["key"],
               "direction": m.get("direction"),
               "baseline": base, "value": val, "n": n,
               "verdict": "inconclusive", "rel_change": None, "p_value": None}
        if m.get("key") in INVENTORY_METRICS:
            # A snapshot metric is one global number: any config change made since
            # the apply moves it, so baseline-vs-now cannot attribute the movement
            # to this rec. Verdict comes from whether the applied setting still
            # holds; the snapshot values stay in the row as context only.
            action = rec.get("action") or {}
            if action.get("type") == "setting_change":
                held = _setting_in_effect(action.get("payload") or {}, settings)
                if held is True:
                    row["verdict"] = "verified"
                    row["note"] = "applied setting still in effect"
                elif held is False:
                    row["verdict"] = "regressed"
                    row["note"] = "applied setting no longer in effect"
                else:
                    row["note"] = "settings unreadable; snapshot metric cannot attribute"
            else:
                row["note"] = "snapshot metric cannot attribute movement to this change"
            rows.append(row)
            continue
        floor_ok = n >= cfg["verify"]["min_sessions"]
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
                if m["key"] in COUNT_METRICS:
                    k1, n1 = round(sum(base_samples)), len(base_samples)
                    k2, n2 = round(sum(cur_samples)), len(cur_samples)
                    p = _binom_two_sided(k2, k1 + k2, n2 / (n1 + n2))
                else:
                    p = _welch_p([float(x) for x in base_samples],
                                 [float(x) for x in cur_samples])
                row["p_value"] = p
                sig_ok = p < 0.05
            if sig_ok and ((want == "down" and rel <= -t) or (want == "up" and rel >= t)):
                row["verdict"] = "verified"
            elif sig_ok and ((want == "down" and rel >= t) or (want == "up" and rel <= -t)):
                row["verdict"] = "regressed"
        rows.append(row)
    _mark_cohorts(rows, entries)
    return rows


def _mark_cohorts(rows: list, entries: dict) -> None:
    """N applied recs sharing (metric, scope) all measure the same post-apply
    sessions, so their identical numbers are one joint measurement — mark them
    as a cohort rather than implying N independent per-rec verdicts."""
    groups = {}
    for row in rows:
        if row["metric"] in INVENTORY_METRICS:
            continue
        rec = (entries.get(row["id"]) or {}).get("rec") or {}
        scope = (rec.get("metric") or {}).get("scope", "global")
        groups.setdefault((row["metric"], scope), []).append(row)
    for grp in groups.values():
        if len(grp) < 2:
            continue
        ids = [r["id"] for r in grp]
        for r in grp:
            r["cohort"] = ids
            r["note"] = (f"cohort of {len(ids)} sharing this metric — "
                         "one joint measurement, not attributable per-rec")


def main(argv=None):
    import so_config
    ap = argparse.ArgumentParser()
    ap.add_argument("--evidence", required=True)
    ap.add_argument("--state", default=None)
    ap.add_argument("--data-root", default=None)
    ap.add_argument("--out", required=True)
    a = ap.parse_args(argv)
    data_root, state = so_config.resolve(a.data_root, a.state)
    cfg = so_config.load_config(state)
    lpath = state / "state" / "ledger.jsonl"
    entries = ledger_mod.load(lpath)
    ev = Path(a.evidence)
    sessions = json.loads((ev / "sessions.json").read_text())["sessions"]
    inv_f = ev / "inventory.json"
    inventory = json.loads(inv_f.read_text()) if inv_f.exists() else None
    try:
        settings = json.loads((data_root / "settings.json").read_text())
    except (OSError, json.JSONDecodeError):
        settings = None
    rows = verify_entries(entries, sessions, inventory, cfg, settings=settings)
    out_path = Path(a.out)
    out_path.write_text(json.dumps({"rows": rows}, indent=1))
    out_path.chmod(0o600)
    for r in rows:
        if r["verdict"] in ("verified", "regressed"):
            # carry forward apply context: ledger.load is last-entry-wins, so the
            # verdict entry must keep files/snapshot or rollback finds nothing
            prior = entries.get(r["id"], {})
            if prior.get("status") == r["verdict"]:
                continue  # re-check with unchanged verdict: no duplicate entry
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
