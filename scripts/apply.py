"""apply / rollback / reject. Tier-A only, per-item human approval upstream.
Per-rec file snapshots; post-apply smoke validation with automatic restore (the ONLY
auto-rollback case — metric regressions are flagged by verify, never auto-reverted);
metric baseline captured at apply time so verification is possible at all."""
import argparse
import hashlib
import json
import shutil
import sys
from datetime import date, datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "adapters" / "claude_code"))
import templates  # noqa: E402
import ledger as ledger_mod  # noqa: E402
import metriclib  # noqa: E402


def _sha(p: Path):
    p = Path(p)
    return hashlib.sha256(p.read_bytes()).hexdigest() if p.exists() else None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def cmd_apply(ids, state, data_root, evidence):
    state = Path(state)
    lpath = state / "state" / "ledger.jsonl"
    led = ledger_mod.load(lpath)
    sessions = json.loads((Path(evidence) / "sessions.json").read_text())["sessions"]
    inv_f = Path(evidence) / "inventory.json"
    inv = json.loads(inv_f.read_text()) if inv_f.exists() else None
    # staggered-apply warning: changes sharing a metric are unattributable to verify
    shared = {}
    for rid in ids:
        rec = (led.get(rid) or {}).get("rec")
        if rec:
            k = (rec["metric"].get("key"), rec["metric"].get("scope", "global"))
            shared.setdefault(k, []).append(rid)
    for (mkey, mscope), group in shared.items():
        if mkey != "none" and len(group) > 1:
            print(f"WARNING: {', '.join(group)} share metric {mkey}/{mscope} — "
                  f"verification cannot attribute the outcome; consider separate runs")
    for rid in ids:
        e = led.get(rid)
        if not e or e["status"] not in ("proposed", "approved"):
            print(f"{rid}: not applicable (status={e['status'] if e else 'unknown'})")
            continue
        rec = e["rec"]
        if rec["action"]["tier"] != "A":
            print(f"{rid}: tier B — apply manually (see the report)")
            continue
        try:
            edits = templates.render(rec["action"], Path(data_root))
        except ValueError as err:
            ledger_mod.append(lpath, {"id": rid, "status": "apply_failed",
                                      "errors": [str(err)], "evidence_hash": e.get("evidence_hash")})
            print(f"{rid}: refused by policy — {err}")
            continue
        snap = state / "state" / "snapshots" / date.today().isoformat() / rid
        snap.mkdir(parents=True, exist_ok=True)
        meta = []
        for path, _ in edits:
            entry = {"path": str(path), "pre_exists": path.exists(), "pre_sha": _sha(path)}
            if entry["pre_exists"]:
                shutil.copy2(path, snap / path.name)
            meta.append(entry)
        for path, content in edits:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
        errs = templates.smoke_check([p for p, _ in edits], Path(data_root))
        if errs:
            for m in meta:
                p = Path(m["path"])
                if m["pre_exists"]:
                    shutil.copy2(snap / p.name, p)
                elif p.exists():
                    p.unlink()
            ledger_mod.append(lpath, {"id": rid, "status": "apply_failed", "errors": errs,
                                      "evidence_hash": e.get("evidence_hash")})
            print(f"{rid}: APPLY FAILED (restored) — {errs}")
            continue
        for m in meta:
            m["post_sha"] = _sha(Path(m["path"]))
        val, n = metriclib.compute_metric(rec["metric"], sessions, inv)
        ledger_mod.append(lpath, {"id": rid, "status": "applied", "rec": rec,
                                  "applied_at": _now(), "snapshot": str(snap), "files": meta,
                                  "baseline": {"value": val, "n_sessions": n},
                                  "evidence_hash": e.get("evidence_hash")})
        print(f"{rid}: applied — baseline {rec['metric']['key']}={val} (n={n}). "
              f"Rollback: /self-optimize rollback {rid}")


def cmd_rollback(rid, state, force=False):
    state = Path(state)
    lpath = state / "state" / "ledger.jsonl"
    e = ledger_mod.load(lpath).get(rid)
    if not e or "files" not in e:
        print(f"{rid}: nothing to roll back")
        return
    snap = Path(e["snapshot"])
    for m in e["files"]:
        if _sha(Path(m["path"])) != m["post_sha"] and not force:
            print(f"{rid}: {m['path']} changed since apply — rerun with --force to override")
            return
    for m in e["files"]:
        p = Path(m["path"])
        if m["pre_exists"]:
            shutil.copy2(snap / p.name, p)
        elif p.exists():
            p.unlink()
    ledger_mod.append(lpath, {"id": rid, "status": "rolled_back",
                              "evidence_hash": e.get("evidence_hash")})
    print(f"{rid}: rolled back")


def cmd_reject(rid, reason, state):
    lpath = Path(state) / "state" / "ledger.jsonl"
    e = ledger_mod.load(lpath).get(rid, {})
    ledger_mod.append(lpath, {"id": rid, "status": "rejected", "reason": reason,
                              "evidence_hash": e.get("evidence_hash")})
    print(f"{rid}: rejected ({reason})")


def main(argv=None):
    import so_config
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    a1 = sub.add_parser("apply")
    a1.add_argument("--ids", required=True)
    a1.add_argument("--evidence", required=True)
    a2 = sub.add_parser("rollback")
    a2.add_argument("--id", required=True)
    a2.add_argument("--force", action="store_true")
    a3 = sub.add_parser("reject")
    a3.add_argument("--id", required=True)
    a3.add_argument("--reason", default="")
    for x in (a1, a2, a3):
        x.add_argument("--state", default=None)
        x.add_argument("--data-root", default=None)
    a = ap.parse_args(argv)
    data_root, state = so_config.resolve(a.data_root, a.state)
    if a.cmd == "apply":
        cmd_apply([i.strip() for i in a.ids.split(",") if i.strip()],
                  state, data_root, a.evidence)
    elif a.cmd == "rollback":
        cmd_rollback(a.id, state, a.force)
    else:
        cmd_reject(a.id, a.reason, state)


if __name__ == "__main__":
    main()
