"""Report renderer: outcomes of previously-applied changes FIRST, then ranked
findings, trend, and an honest footer (what was dropped, what was redacted,
what the run cost). Appends the run row and prunes old evidence dirs."""
import argparse
import json
import shutil
from pathlib import Path

import ledger as ledger_mod
import labels as labels_mod
import variants


def _cell(x) -> str:
    return str(x).replace("|", "\\|").replace("\n", " ")


def _num(x):
    return f"{x:.2f}" if isinstance(x, (int, float)) else "-"


def _impact(rec):
    d = rec.get("delta_tokens")
    return f"~{d:,} tok/window" if d else rec["impact"].get("ordinal", "?")


def _cumulative_savings(entries: dict) -> list:
    """Per-metric cumulative verified improvement across every run: sums the
    signed improvement over every ledger entry whose CURRENT status is
    'verified' (ledger.load is last-entry-wins, so a later regression on the
    same id drops it out of this sum automatically). Improvement is positive
    regardless of the metric's better-direction."""
    totals = {}
    for e in entries.values():
        if e.get("status") != "verified":
            continue
        rec = e.get("rec") or {}
        metric = (rec.get("metric") or {}).get("key")
        base = (e.get("baseline") or {}).get("value")
        val = (e.get("measured") or {}).get("value")
        if not metric or base is None or val is None:
            continue
        direction = (rec.get("metric") or {}).get("direction")
        t = totals.setdefault(metric, [0.0, 0])
        t[0] += (val - base) if direction == "up" else (base - val)
        t[1] += 1
    return sorted(totals.items())


def _verified_deltas(verify_rows: list) -> dict:
    """This run's verified-metric improvements (positive = better, using the
    row's direction from verify.py) for the runs.jsonl row — NOT the all-time
    cumulative total in the Trend section, which reads the full ledger instead."""
    deltas = {}
    for v in verify_rows:
        if v.get("verdict") != "verified":
            continue
        base, val, metric = v.get("baseline"), v.get("value"), v.get("metric")
        if base is None or val is None or not metric:
            continue
        d = (val - base) if v.get("direction") == "up" else (base - val)
        deltas[metric] = deltas.get(metric, 0.0) + d
    return deltas


def _analyst_tokens_line(tokens) -> str:
    if not tokens:
        return "n/a"
    parts = ", ".join(f"{k}={v:,}" for k, v in tokens.items())
    return f"{parts} (total {sum(tokens.values()):,})"


def _parse_analyst_tokens(raw: str | None) -> dict | None:
    """'miner=1200,auditor=800' -> {'miner': 1200, 'auditor': 800}; malformed
    pairs are skipped rather than raising — a bad flag value shouldn't kill the report."""
    if not raw:
        return None
    out = {}
    for part in raw.split(","):
        part = part.strip()
        if not part or "=" not in part:
            continue
        k, _, v = part.partition("=")
        try:
            out[k.strip()] = int(v.strip())
        except ValueError:
            continue
    return out or None


def _gym_line(g: dict) -> str:
    """Both sides of the gym score, always together. A candidate that prevents more
    corrections by breaking what already worked is a regression, and only showing the
    number that improved is how a self-improving loop learns to game itself."""
    if g.get("unscorable"):
        return f"- gym score: unscorable — {g.get('reason') or 'below the case floor'}"
    p, w = g.get("prevented") or {}, g.get("preserved") or {}
    return (f"- gym score: candidate prevented {p.get('n', 0)}/{p.get('of', 0)} failure "
            f"cases and preserved {w.get('n', 0)}/{w.get('of', 0)} working cases "
            f"(judged evidence for your decision, not an auto-gate)")


def _security_line(sec: dict) -> str:
    """G1 is the actual gate (rendered as a blocking warning when it failed — the
    finding is already forced to tier B by security_gate.gate by the time this
    renders). G2 is judged evidence like the gym score and shadow eval: shown
    either way, never itself a reason the tier changed."""
    g1, g2 = sec.get("g1") or {}, sec.get("g2") or {}
    if g1.get("passed"):
        parts = ["G1 pattern scan passed"]
    else:
        hits = ", ".join(f"{h['category']} (`{h['snippet']}`)" for h in g1.get("hits") or [])
        parts = [f"**G1 BLOCKED** — {hits} — ineligible for tier A"]
    if g2.get("scanned"):
        if g2.get("verdict") == "mismatch":
            parts.append(f"G2 purpose mismatch: {g2.get('reason') or 'no reason given'}")
        else:
            parts.append("G2 purpose check: match")
    else:
        parts.append(f"G2 not run — {g2.get('reason', 'unknown')}")
    return "- security: " + "; ".join(parts)


def _ops_block(payload: dict) -> list:
    """Bounded edits are proposed to be READ before they are applied, so the report
    shows each operation in full: what it does, the exact line it anchors on, the new
    text, and the evidence that motivated that one edit."""
    ops = payload.get("ops")
    if not isinstance(ops, list):
        return []
    L = [f"- bounded edits ({len(ops)}):", "", "```"]
    for i, op in enumerate(ops, 1):
        if not isinstance(op, dict):
            continue
        L.append(f"{i}. {op.get('op')} @ {str(op.get('anchor', ''))!r}")
        if op.get("text"):
            L.append(f"   -> {str(op['text'])!r}")
        refs = [str(r) for r in (op.get("motivated_by") or [])]
        if refs:
            L.append(f"   motivated_by: {', '.join(refs)}")
    return L + ["```", ""]


def _fronts_block(fronts: dict) -> list:
    """Per-artifact Pareto front: the candidate versions no other version beats on both
    sides at once. Rendered as evidence with the trade-off stated, and nothing else —
    there is no promote button here and no auto-promotion behind it. A two-dimensional
    score has no single winner, so the loop is not allowed to invent one."""
    if not fronts:
        return []
    L = ["## Variant fronts", "",
         "Candidate versions already scored on each artifact's cases, with none of them "
         "beaten on both sides at once. Choosing among them is your call — nothing here "
         "promotes a version.", ""]
    for artifact in sorted(fronts):
        block = fronts[artifact]
        L += [f"### {_cell(artifact)}", "",
              "| variant | from | prevented | preserved | edits | proposed as |",
              "|---|---|---|---|---|---|"]
        for m in block["members"]:
            p, w = m["prevented"], m["preserved"]
            L.append(f"| `{m['variant']}` | {_cell(m.get('parent') or 'live artifact')} | "
                     f"{p.get('n', 0)}/{p.get('of', 0)} ({p['rate']:.2f}) | "
                     f"{w.get('n', 0)}/{w.get('of', 0)} ({w['rate']:.2f}) | "
                     f"{len(m['ops'])} | {_cell(m.get('title') or '-')} |")
        L += ["", f"Trade-off: {block['trade_off']}", ""]
    return L


def render(run_id, findings, dropped, verify_rows, trend_rows, usage, footer,
           cumulative=None, shadow=None, roi=None, gym=None, security=None, fronts=None) -> str:
    L = [f"# self-optimize report — {run_id}", ""]
    if verify_rows:
        L += ["## Applied changes: outcomes", "",
              "| id | title | metric | verdict | baseline | now | n | note |",
              "|---|---|---|---|---|---|---|---|"]
        for v in verify_rows:
            verdict_cell = f"**{v['verdict']}**"
            if v["verdict"] == "regressed":
                verdict_cell += f" · rollback: /self-optimize rollback {v['id']}"
            L.append(f"| `{v['id']}` | {_cell(v['title'])} | {_cell(v['metric'])} | {verdict_cell} | "
                     f"{_num(v.get('baseline'))} | {_num(v.get('value'))} | {v.get('n')} | "
                     f"{_cell(v.get('note') or '')} |")
        L.append("")
    L += [f"## Findings ({len(findings)})", ""]
    if findings:
        L += ["| # | id | category | impact | tier | title |", "|---|---|---|---|---|---|"]
        for i, r in enumerate(findings, 1):
            L.append(f"| {i} | `{r['id']}` | {_cell(r['category'])} | {_impact(r)} | "
                     f"{r['action']['tier']} | {_cell(r['title'])} |")
        L.append("")
    for i, r in enumerate(findings, 1):
        L += [f"### {i}. {r['title']}  `{r['id']}`", "",
              f"- category: {r['category']} · tier {r['action']['tier']} · impact: {_impact(r)}",
              f"- evidence: {', '.join('`' + e + '`' for e in r['evidence_refs'])}",
              f"- risk: {r['risk']}",
              f"- metric: {r['metric']['key']} "
              f"({r['metric'].get('direction', '-')}, {r['metric'].get('scope', 'global')})"]
        sh = (shadow or {}).get(r["id"])
        if sh:
            L.append(f"- shadow eval: rewrite would have prevented "
                     f"{sh.get('prevented', 0)}/{sh.get('total', 0)} of its "
                     f"motivating corrections (judged, not measured)")
        gs = (gym or {}).get(r["id"])
        if gs:
            L.append(_gym_line(gs))
        sec = (security or {}).get(r["id"])
        if sec:
            L.append(_security_line(sec))
        for loc in r.get("deep_localize") or []:
            b = loc.get("bracket") or [0, 0]
            L.append(f"- deep localize: session went off track around turn {b[0] + 1}-{b[1] + 1} "
                     f"of {loc.get('turns_total')} ({loc.get('calls')} judge calls) — "
                     f"{loc.get('rationale') or 'no rationale given'}")
        if r.get("prior_rejection"):
            L.append(f"- previously rejected: {r['prior_rejection']} — evidence has changed since")
        if r["action"].get("type") == "file_ops":
            L += _ops_block(r["action"].get("payload") or {})
        if r["action"]["tier"] == "A":
            L.append(f"- apply: `/self-optimize apply {r['id']}` · reject: "
                     f"`/self-optimize reject {r['id']} \"<reason>\"`")
        elif r["action"].get("type") == "file_ops":
            # the ops block above already shows the whole payload in readable form —
            # dumping the same JSON again under "manual action" would be noise, and
            # a bounded edit is not manual work
            L.append("- tier B: read the edits above, then decide from the dashboard")
        else:
            p = r["action"].get("payload", {})
            body = p.get("diff") or p.get("description") or json.dumps(p, indent=1)
            L += ["- manual action:", "", "```", body, "```"]
        L.append("")
    L += _fronts_block(fronts or {})
    cumulative = cumulative or []
    if trend_rows or cumulative:
        L.append("## Trend")
        L.append("")
        if trend_rows:
            L += ["| run | sessions | tok/session | corrections | dup reads | base ctx | unused |",
                  "|---|---|---|---|---|---|---|"]
            for t in trend_rows:
                L.append(f"| {t['run_id']} | {t['n_sessions']} | {_num(t.get('tokens_per_session'))} | "
                         f"{_num(t.get('correction_rate'))} | {_num(t.get('duplicate_read_rate'))} | "
                         f"{t.get('base_context_est') if t.get('base_context_est') is not None else '-'} | "
                         f"{t.get('unused_surface_count') if t.get('unused_surface_count') is not None else '-'} |")
            L.append("")
        cat_rows = [t for t in trend_rows if t.get("corrections_by_category")]
        if cat_rows:
            # migrate_categories folds any pre-taxonomy-v2 rows onto today's
            # category names so a mid-history migration doesn't read as the
            # trend resetting to zero.
            cur = labels_mod.migrate_categories(cat_rows[-1]["corrections_by_category"])
            prev = labels_mod.migrate_categories(
                cat_rows[-2]["corrections_by_category"]) if len(cat_rows) > 1 else {}
            parts = []
            for k in sorted(set(cur) | set(prev)):
                p = f"{k} {cur.get(k, 0)}"
                if prev:
                    p += f" (prev {prev.get(k, 0)})"
                parts.append(p)
            L += ["**Correction categories**: " + ", ".join(parts), ""]
        if cumulative:
            L.append("**Cumulative verified improvement**")
            L.append("")
            for metric, (delta, count) in cumulative:
                plural = "change" if count == 1 else "changes"
                L.append(f"- {metric}: {_num(delta)} ({count} verified {plural})")
            L.append("")
    if usage.get("per_model"):
        L += ["## Model performance", "",
              "| model | sessions | output tokens | corrections |", "|---|---|---|---|"]
        corrections_by_model = usage.get("corrections_by_model", {})
        for model in sorted(usage["per_model"]):
            pm = usage["per_model"][model]
            L.append(f"| {_cell(model)} | {pm.get('sessions', 0)} | "
                     f"{pm.get('output', 0):,} | {corrections_by_model.get(model, 0)} |")
        L.append("")
    L += ["## Run footer", "",
          f"- window sessions: {usage['totals']['sessions']}; "
          f"parse-skipped lines: {usage['parse']['skipped_lines']}",
          f"- redactions applied to stored excerpts: {usage['parse'].get('redactions', 0)}",
          f"- findings dropped — invalid: {dropped['invalid']}, "
          f"failed citations: {dropped['citations']}, guard: {dropped['guard']}, "
          f"suppressed by ledger: {len(dropped['suppressed'])}",
          f"- analyst tokens: {_analyst_tokens_line(footer.get('analyst_tokens'))}"]
    if roi:
        saved, spent = roi.get("saved", 0), roi.get("spent", 0)
        verdict = "" if saved >= spent else " — not yet paying for itself"
        L.append(f"- program ROI: est. {saved:,} tok/window saved by currently-verified "
                 f"changes vs {spent:,} analyst tokens spent all-time{verdict}")
    L.append("")
    return "\n".join(L) + "\n"


def _optional_json(path: Path):
    """LLM-step outputs the runner may or may not have written; a malformed one must
    not take the whole report down."""
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return None


def _trend(state_dir: Path, limit=5) -> list:
    p = Path(state_dir) / "state" / "metrics.jsonl"
    if not p.exists():
        return []
    rows = [json.loads(x) for x in p.read_text().splitlines() if x.strip()]
    return rows[-limit:]


def _prune_evidence(state_dir: Path, retain: int):
    ev = Path(state_dir) / "evidence"
    if not ev.exists():
        return
    dirs = sorted(d for d in ev.iterdir() if d.is_dir())
    for d in dirs[:-retain] if retain > 0 else []:
        shutil.rmtree(d)


def main(argv=None):
    import so_config
    ap = argparse.ArgumentParser()
    ap.add_argument("--evidence", required=True)
    ap.add_argument("--state", default=None)
    ap.add_argument("--run-id", required=True)
    ap.add_argument("--analyst-tokens", default=None)
    a = ap.parse_args(argv)
    _, state = so_config.resolve(None, a.state)
    cfg = so_config.load_config(state)
    evdir = Path(a.evidence)
    fdata = json.loads((evdir / "findings.json").read_text())
    usage = json.loads((evdir / "usage.json").read_text())
    verify_rows = []
    if (evdir / "verify.json").exists():
        verify_rows = json.loads((evdir / "verify.json").read_text())["rows"]
    tokens = _parse_analyst_tokens(a.analyst_tokens)
    if tokens is None and (evdir / "analyst_tokens.json").exists():
        try:
            raw = json.loads((evdir / "analyst_tokens.json").read_text())
            tokens = {str(k): int(v) for k, v in raw.items()} or None
        except (json.JSONDecodeError, OSError, TypeError, ValueError):
            tokens = None
    ledger_entries = ledger_mod.load(state / "state" / "ledger.jsonl")
    cumulative = _cumulative_savings(ledger_entries)
    shadow = _optional_json(evdir / "shadow.json")
    gym = _optional_json(evdir / "gym.json")
    security = _optional_json(evdir / "security.json")
    saved = sum((e.get("rec") or {}).get("delta_tokens") or 0
                for e in ledger_entries.values() if e.get("status") == "verified")
    spent = sum(tokens.values()) if tokens else 0
    runs_f = state / "state" / "runs.jsonl"
    if runs_f.exists():
        for line in runs_f.read_text().splitlines():
            try:
                spent += sum((json.loads(line).get("analyst_tokens") or {}).values())
            except (json.JSONDecodeError, AttributeError, TypeError):
                continue
    max_members = int(((cfg.get("gym") or {}).get("front") or {}).get("max_members", 4))
    md = render(a.run_id, fdata["findings"], fdata["dropped"], verify_rows,
                _trend(state), usage, {"analyst_tokens": tokens}, cumulative=cumulative,
                shadow=shadow, roi={"saved": saved, "spent": spent}, gym=gym, security=security,
                fronts=variants.all_fronts(state, max_members))
    rdir = Path(cfg["report_dir"])
    rdir.mkdir(parents=True, exist_ok=True)
    out = rdir / f"{a.run_id}.md"
    out.write_text(md)
    ledger_mod.append(state / "state" / "runs.jsonl",
                          {"run_id": a.run_id, "n_sessions": usage["totals"]["sessions"],
                           "findings": len(fdata["findings"]),
                           "analyst_tokens": tokens,
                           "verified_deltas": _verified_deltas(verify_rows)})
    _prune_evidence(state, cfg["retain_runs"])
    print(f"REPORT: {out}")
    for i, r in enumerate(fdata["findings"][:5], 1):
        print(f"{i}. [{r['category']}/{r['action']['tier']}] {r['title']} "
              f"({_impact(r)}) — id {r['id']}")


if __name__ == "__main__":
    main()
