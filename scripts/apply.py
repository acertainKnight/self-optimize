"""apply / rollback / reject. Every applicable action type (tier-A plus diff),
per-item human approval upstream — manual is the only permanent hand-off.
Per-rec file snapshots; post-apply smoke validation with automatic restore (the ONLY
auto-rollback case — metric regressions are flagged by verify, never auto-reverted);
metric baseline captured at apply time so verification is possible at all."""
import argparse
import hashlib
import json
import os
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "adapters" / "claude_code"))
import templates  # noqa: E402
import ledger as ledger_mod  # noqa: E402
import metriclib  # noqa: E402
import schema as so_schema  # noqa: E402
import synth  # noqa: E402


def _sha(p: Path):
    p = Path(p)
    return hashlib.sha256(p.read_bytes()).hexdigest() if p.exists() else None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _restore(meta, snap):
    for m in meta:
        p = Path(m["path"])
        if m["pre_exists"]:
            shutil.copy2(snap / m["snap_name"], p)
        elif p.exists():
            p.unlink()


def cmd_apply(ids, state, data_root, evidence):
    ids = list(dict.fromkeys(ids))  # dedupe: in-memory ledger is stale within one run
    state = Path(state)
    lpath = state / "state" / "ledger.jsonl"
    led = ledger_mod.load(lpath)
    sessions_file = Path(evidence) / "sessions.json"
    if not sessions_file.exists():
        print(f"evidence pack incomplete: {sessions_file} missing — run a collect first")
        return
    sessions = json.loads(sessions_file.read_text())["sessions"]
    inv_f = Path(evidence) / "inventory.json"
    inv = json.loads(inv_f.read_text()) if inv_f.exists() else None
    extra_roots = so_schema.derive_extra_roots(inv, data_root)
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
        if not so_schema.is_applicable(rec["action"]):
            print(f"{rid}: manual action — complete it yourself (see the report)")
            continue
        try:
            edits = templates.render(rec["action"], Path(data_root), extra_roots)
        except (ValueError, KeyError, TypeError, AttributeError, OSError) as err:
            # KeyError/TypeError/AttributeError alongside ValueError: templates.render
            # indexes payload fields directly (p["value"], p["path"], dict lookups keyed
            # by key_path elements, re.escape(p["key"]), Path(p["path"]), a non-str
            # diff's .split() in _apply_unified_diff, ...), so a malformed payload —
            # schema/guard only check shape, not every field's presence or type — must
            # fail cleanly here rather than crash the whole decide/apply run (mirrors
            # cmd_decide's amend guard wrapper, which catches the same class).
            # OSError: the diff branch's render-time target.read_text() can hit a
            # directory named CLAUDE.md or an unreadable file — fail just this rec,
            # not the whole batch.
            # ValueError is the renderer's DELIBERATE refusal (anchor missed, path
            # outside the sanctioned roots, diff didn't apply) and already says why —
            # calling that a malformed payload would misreport a working guard. The
            # other classes really are payload damage.
            reason = str(err) if isinstance(err, ValueError) else f"malformed payload: {err}"
            ledger_mod.append(lpath, {"id": rid, "status": "apply_failed",
                                      "errors": [reason],
                                      "evidence_hash": e.get("evidence_hash")})
            print(f"{rid}: refused by policy — {reason}")
            continue
        # UTC timestamp (not local date): same-day re-applies must not share a dir,
        # or the second apply would overwrite the true pre-state snapshot
        snap = (state / "state" / "snapshots"
                / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") / rid)
        snap.mkdir(parents=True, exist_ok=True)
        meta = []
        for i, (path, _) in enumerate(edits):
            snap_name = f"{i}-{path.name}"  # index prefix: basenames can repeat across dirs
            entry = {"path": str(path), "pre_exists": path.exists(),
                     "pre_sha": _sha(path), "snap_name": snap_name}
            if entry["pre_exists"]:
                shutil.copy2(path, snap / snap_name)
            meta.append(entry)
        try:
            for path, content in edits:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content)
            errs = templates.smoke_check([p for p, _ in edits], Path(data_root))
            if errs:
                _restore(meta, snap)
                ledger_mod.append(lpath, {"id": rid, "status": "apply_failed", "errors": errs,
                                          "evidence_hash": e.get("evidence_hash")})
                print(f"{rid}: APPLY FAILED (restored) — {errs}")
                continue
            for m in meta:
                m["post_sha"] = _sha(Path(m["path"]))
        except (OSError, TypeError) as err:
            # TypeError alongside OSError: a malformed payload can carry non-str
            # `content` (path.write_text(content) then raises TypeError, not OSError)
            try:
                _restore(meta, snap)
            except OSError as rerr:
                print(f"{rid}: RESTORE FAILED ({rerr}) — recover manually from {snap}")
            kind = "io" if isinstance(err, OSError) else "write"
            ledger_mod.append(lpath, {"id": rid, "status": "apply_failed",
                                      "errors": [f"{kind}: {err}"],
                                      "evidence_hash": e.get("evidence_hash")})
            print(f"{rid}: APPLY FAILED ({kind}) — {err}")
            continue
        val, n = metriclib.compute_metric(rec["metric"], sessions, inv)
        samples = metriclib.metric_samples(rec["metric"], sessions, inv)[-50:]
        ledger_mod.append(lpath, {"id": rid, "status": "applied", "rec": rec,
                                  "applied_at": _now(), "snapshot": str(snap), "files": meta,
                                  "baseline": {"value": val, "n_sessions": n, "samples": samples},
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
    try:
        _restore(e["files"], snap)
    except OSError as err:
        # no ledger append: status stays "applied", which remains accurate
        print(f"{rid}: ROLLBACK FAILED ({err}) — restore manually from {snap}")
        return
    ledger_mod.append(lpath, {"id": rid, "status": "rolled_back",
                              "evidence_hash": e.get("evidence_hash")})
    print(f"{rid}: rolled back")


def cmd_reject(rid, reason, state):
    lpath = Path(state) / "state" / "ledger.jsonl"
    e = ledger_mod.load(lpath).get(rid, {})
    ledger_mod.append(lpath, {"id": rid, "status": "rejected", "reason": reason,
                              "evidence_hash": e.get("evidence_hash")})
    print(f"{rid}: rejected ({reason})")


def _downloads_dir() -> Path:
    return Path.home() / "Downloads"


def _find_decisions_file(path_or_none, downloads_dir: Path):
    if path_or_none:
        return Path(path_or_none)
    if not downloads_dir.exists():
        return None
    matches = sorted(downloads_dir.glob("self-optimize-decisions-*.json"),
                     key=lambda p: p.stat().st_mtime)
    return matches[-1] if matches else None


def cmd_decide(path_or_none, state, data_root, evidence) -> dict:
    """Apply/reject the decisions dumped by the HTML dashboard's "Download
    decisions.json" button, then report the tier-B assist selections — those are
    NEVER executed here, only surfaced for the calling Claude session to carry out
    with the user (see docs/evidence-schema.md for the decisions.json shape)."""
    dfile = _find_decisions_file(path_or_none, _downloads_dir())
    if dfile is None or not dfile.exists():
        msg = (f"decisions file not found: {path_or_none}" if path_or_none else
               "no self-optimize-decisions-*.json found in ~/Downloads — "
               "download decisions.json from the dashboard first")
        print(msg)
        return {}
    try:
        data = json.loads(dfile.read_text())
    except json.JSONDecodeError:
        print(f"{dfile}: not valid JSON")
        return {}
    if not isinstance(data, dict):
        print(f"{dfile}: expected a JSON object")
        return {}
    # provenance gate: ~/Downloads is browser-writable, and even an honest file can
    # be stale — refuse anything not minted for the evidence run we're acting on
    run_id, ev_run = data.get("run_id"), Path(evidence).name
    if run_id != ev_run:
        print(f"{dfile}: decisions are for run {run_id!r} but the evidence run is "
              f"{ev_run!r} — regenerate the dashboard for the current run and re-download")
        return {}
    # authorization set: only ids THIS run actually proposed. The run_id string match
    # alone can't stop a file for the right run from naming any open-backlog id, so we
    # scope every apply/reject/assist to findings.json. No findings = nothing to
    # authorize against = invalid decide.
    fpath = Path(evidence) / "findings.json"
    if not fpath.exists():
        print(f"{dfile}: no findings.json in the evidence run {ev_run!r} — cannot "
              f"authorize any decision; run a full /self-optimize first")
        return {}
    # amend AUTHORS a new action (unlike apply/reject/assist, which select recs the
    # pipeline already proposed). It is NOT treated specially at file-resolution time:
    # decide is human-invoked on the user's own downloaded file, the authored action
    # is re-validated through validate_rec + guard + templates (sanctioned roots,
    # allowlisted settings keys, no symlink escape, tier-A only) exactly like any
    # apply, and every effect is snapshotted/rollback-able. A planted ~/Downloads file
    # would need local home-dir write access — which already permits far easier attacks
    # (editing settings.json, planting a hook) — so an explicit-path requirement here
    # buys no real safety while breaking the download→decide flow.
    run_ids = {f["id"] for f in json.loads(fpath.read_text()).get("findings", [])
               if isinstance(f, dict) and isinstance(f.get("id"), str)}
    apply_ids = [i for i in (data.get("apply") or []) if isinstance(i, str)]
    reject_items = [r for r in (data.get("reject") or [])
                    if isinstance(r, dict) and isinstance(r.get("id"), str)
                    and isinstance(r.get("reason"), str)]
    assist_ids = [i for i in (data.get("assist") or []) if isinstance(i, str)]
    # only structurally-anchored entries (has a string id) get scoped below; a
    # totally shapeless entry has no id to authorize or report against
    amend_raw = [a for a in (data.get("amend") or [])
                if isinstance(a, dict) and isinstance(a.get("id"), str)]
    refused = ([i for i in apply_ids if i not in run_ids]
               + [r["id"] for r in reject_items if r["id"] not in run_ids]
               + [i for i in assist_ids if i not in run_ids]
               + [a["id"] for a in amend_raw if a["id"] not in run_ids])
    apply_ids = [i for i in apply_ids if i in run_ids]
    reject_items = [r for r in reject_items if r["id"] in run_ids]
    assist_ids = [i for i in assist_ids if i in run_ids]
    amend_raw = [a for a in amend_raw if a["id"] in run_ids]
    # in-run but malformed (blank reason / non-dict action) amend entries must be
    # visible, not silently vanish — decisions.json is hand-editable, semi-trusted
    # input, and a "check me, not trust me" gate that drops entries without a trace
    # defeats the audit trail the decision log promises
    amend_items, amend_malformed = [], []
    for a in amend_raw:
        reason_ok = isinstance(a.get("reason"), str) and a["reason"].strip()
        action_ok = isinstance(a.get("action"), dict)
        if reason_ok and action_ok:
            amend_items.append(a)
        else:
            why = ([] if reason_ok else ["amend requires a non-empty reason"]) + (
                [] if action_ok else ["amend action must be an object"])
            # carry the attempted action so a refused/malformed amend is forensically
            # replayable from the decision log, not just {id, reason}
            amend_malformed.append({"id": a["id"], "action": a.get("action"),
                                    "reason": "; ".join(why)})
    # dedupe by id: the loop below reloads the ledger fresh each iteration (so a
    # repeated id would just be re-evaluated against its own prior outcome rather
    # than corrupting anything), but a spurious duplicate "not amendable" refusal
    # for an id already amended earlier in this same batch is still misleading —
    # keep only the first occurrence (mirrors cmd_apply's matching ids dedupe above)
    seen_amend, deduped = set(), []
    for a in amend_items:
        if a["id"] not in seen_amend:
            seen_amend.add(a["id"])
            deduped.append(a)
    amend_items = deduped
    print(f"decisions: {dfile} (run {run_id}: {len(apply_ids)} apply, "
          f"{len(reject_items)} reject, {len(assist_ids)} assist, {len(amend_items)} amend)")
    if refused:
        print(f"REFUSED (not part of run {ev_run}): {', '.join(sorted(set(refused)))}")
    lpath = Path(state) / "state" / "ledger.jsonl"
    if apply_ids:
        cmd_apply(apply_ids, state, data_root, evidence)
    for r in reject_items:
        cmd_reject(r["id"], r["reason"], state)

    # amend: reject the original in favor of a different, re-validated action. The
    # replacement action is user-supplied from a semi-trusted Downloads file, so it
    # gets the exact same schema + guard gate as any analyst-proposed action — never
    # trusted just because it arrived in a decisions.json for the right run.
    amended, amend_refused = [], list(amend_malformed)
    if amend_items:
        inv_f = Path(evidence) / "inventory.json"
        inv = json.loads(inv_f.read_text()) if inv_f.exists() else None
        extra_roots = so_schema.derive_extra_roots(inv, data_root)
        for item in amend_items:
            aid, reason, action = item["id"], item["reason"], item["action"]
            # reload fresh every iteration, not once before the loop: a rec_id
            # collision can make one item's replacement land on another item's id
            # (e.g. amending A into "disable plugin X" mints exactly finding B's
            # id) — a stale snapshot would let a later item in this same batch
            # amend that now-applied id as if it were still merely proposed
            e = ledger_mod.load(lpath).get(aid)
            if not e or e.get("status") not in ("proposed", "approved"):
                amend_refused.append({"id": aid, "action": action, "reason":
                                      f"not amendable (status={e['status'] if e else 'unknown'})"})
                continue
            replacement = dict(e["rec"])
            replacement["action"] = action
            replacement["title"] = "Amended: " + e["rec"].get("title", "")
            errs = so_schema.validate_rec(replacement)
            # amend always auto-applies its replacement, so it must be an
            # applicable action (every tier-A type, plus diff) — manual is
            # report-only and can never be what "amended" means here, even
            # though guard() legitimately allows it through for the ordinary
            # manual (never-executed) path
            if not errs and not so_schema.is_applicable(action):
                errs = ["amend replacement must be an applicable action (manual is report-only)"]
            if not errs:
                # guard()/rec_id assume a reasonably well-typed payload (true for
                # synth's own analyst-generated recs); amend feeds it a hand-typed
                # decisions.json instead, so a payload shaped like {"key_path": []}
                # or with unhashable/non-path elements can raise AttributeError/
                # TypeError/KeyError/IndexError deep in dict/Path lookups — must
                # fail cleanly into amend_refused, never crash the whole decide run
                try:
                    if not synth.guard(replacement, Path(data_root), extra_roots):
                        errs = ["guard refused the amended action"]
                except (AttributeError, TypeError, KeyError, IndexError) as gerr:
                    errs = [f"malformed action payload: {gerr}"]
            if errs:
                # leave the original untouched: refused amends must be retryable
                amend_refused.append({"id": aid, "action": action, "reason": "; ".join(errs)})
                continue
            new_id = so_schema.rec_id(replacement)
            # rec_id hashes only category|action.type|payload, so a replacement can
            # collide with an existing ledger id: the original itself (a no-op
            # amend), a LIVE still-undecided finding from this run (status
            # 'proposed' — hijacking it to applied would suppress a real rec with no
            # human decision), or any committed history. The ledger is append-only/
            # last-entry-wins, so blindly appending under a colliding id would
            # clobber that id's real row. The ONLY id safe to reuse is a dangling
            # remnant of a PRIOR failed/rolled-back attempt at this exact amend —
            # nothing of value lives under it, and refusing would make "fix and
            # retry" a permanent dead end. Every other status must be refused.
            if new_id == aid:
                amend_refused.append({"id": aid, "action": action, "reason":
                                      "replacement action is identical to the original — nothing to amend"})
                continue
            existing = ledger_mod.load(lpath).get(new_id)
            if existing and existing.get("status") not in ("apply_failed", "rolled_back"):
                amend_refused.append({"id": aid, "action": action, "reason":
                                      f"replacement action collides with existing recommendation {new_id}; "
                                      "choose a different action"})
                continue
            replacement["id"] = new_id
            replacement["evidence_hash"] = e.get("evidence_hash")
            replacement.pop("delta_tokens", None)
            replacement.pop("prior_rejection", None)
            # apply the replacement BEFORE touching the original: only a replacement
            # that actually reaches "applied" earns rejecting the original — a
            # render/smoke/io failure must leave the original untouched, not rejected
            # out from under a failed amend
            ledger_mod.append(lpath, {"id": new_id, "status": "proposed", "rec": replacement,
                                      "evidence_hash": replacement["evidence_hash"]})
            cmd_apply([new_id], state, data_root, evidence)
            post = (ledger_mod.load(lpath).get(new_id) or {}).get("status")
            if post == "applied":
                cmd_reject(aid, f"amended: {reason}", state)
                amended.append({"orig": aid, "new": new_id, "title": replacement["title"]})
            else:
                # cmd_apply can return early WITHOUT writing a terminal status (e.g.
                # sessions.json missing), leaving new_id dangling at 'proposed'. The
                # collision guard only treats apply_failed/rolled_back as retryable
                # remnants, so demote a still-'proposed' dangler to apply_failed —
                # otherwise the "fix and retry" this refusal promises would be
                # permanently blocked for this exact action by its own leftover row.
                if post == "proposed":
                    ledger_mod.append(lpath, {"id": new_id, "status": "apply_failed",
                                              "errors": ["replacement apply produced no status"],
                                              "evidence_hash": replacement["evidence_hash"]})
                amend_refused.append({"id": aid, "action": action, "reason":
                                      "replacement action failed to apply — original left untouched"})
    # outside the `if amend_items:` gate: amend_refused can be non-empty purely
    # from malformed entries even when amend_items itself is empty, and that must
    # still be visible on the console, not just in the returned dict / decision log
    if amended:
        print("AMENDED (original rejected, replacement applied):")
        for item in amended:
            print(f"- {item['orig']} -> {item['new']}: {item['title']}")
    if amend_refused:
        print("AMEND REFUSED (left untouched — fix and retry):")
        for item in amend_refused:
            print(f"- {item['id']}: {item['reason']}")

    led = ledger_mod.load(lpath)
    applied = [i for i in apply_ids if (led.get(i) or {}).get("status") == "applied"]
    assist = []
    for aid in assist_ids:
        rec = (led.get(aid) or {}).get("rec") or {}
        assist.append({"id": aid, "title": rec.get("title", "(unknown)"),
                       "payload_type": (rec.get("action") or {}).get("type", "?")})
    if assist:
        print("ASSISTED WORK SELECTED (tier B — complete these with the user):")
        for item in assist:
            print(f"- {item['id']}: {item['title']} [{item['payload_type']}]")
    result = {"applied": applied, "rejected": [r["id"] for r in reject_items],
              "assist": assist, "refused": sorted(set(refused)),
              "amended": amended, "amend_refused": amend_refused}
    # durable log: the decisions.json in ~/Downloads is transient, this survives.
    # applies/rejects are already committed to the ledger above, so a write failure
    # here must not raise — it would lose the result dict the caller acts on.
    now = datetime.now(timezone.utc)
    log_dir = Path(state) / "state" / "decisions"
    # ponytail: second-granularity name, same collision class as cmd_apply's snapshot
    # dirs above — two decides for one run within the same second overwrite each
    # other. Add sub-second precision or O_EXCL+suffix if scripted re-decides show up.
    log_path = log_dir / f"{run_id}-{now.strftime('%Y%m%dT%H%M%S')}.json"
    log_body = json.dumps({
        "run_id": run_id, "decided_at": now.isoformat().replace("+00:00", "Z"),
        "source_file": str(dfile), "apply": result["applied"],
        "reject": [{"id": r["id"], "reason": r["reason"]} for r in reject_items],
        "refused": result["refused"], "assist": assist,
        "amend": [{"id": a["id"], "reason": a["reason"], "action": a["action"]} for a in amend_items],
        "amended": amended, "amend_refused": amend_refused,
    }, indent=2) + "\n"
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        fd = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(log_body)
        print(f"decision log: {log_path}")
        result["log"] = str(log_path)
    except OSError as err:
        print(f"decision log FAILED (not persisted) — {err}")
    return result


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
    a4 = sub.add_parser("decide")
    a4.add_argument("path", nargs="?", default=None)
    a4.add_argument("--evidence", required=True)
    for x in (a1, a2, a3, a4):
        x.add_argument("--state", default=None)
        x.add_argument("--data-root", default=None)
    a = ap.parse_args(argv)
    data_root, state = so_config.resolve(a.data_root, a.state)
    if a.cmd == "apply":
        cmd_apply([i.strip() for i in a.ids.split(",") if i.strip()],
                  state, data_root, a.evidence)
    elif a.cmd == "rollback":
        cmd_rollback(a.id, state, a.force)
    elif a.cmd == "decide":
        cmd_decide(a.path, state, data_root, a.evidence)
    else:
        cmd_reject(a.id, a.reason, state)


if __name__ == "__main__":
    main()
