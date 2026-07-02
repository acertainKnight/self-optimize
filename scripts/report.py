"""Report renderer: outcomes of previously-applied changes FIRST, then ranked
findings, trend, and an honest footer (what was dropped, what was redacted,
what the run cost). Appends the run row and prunes old evidence dirs."""
import argparse
import json
import shutil
from pathlib import Path

import ledger as ledger_mod


def _cell(x) -> str:
    return str(x).replace("|", "\\|").replace("\n", " ")


def _num(x):
    return f"{x:.2f}" if isinstance(x, (int, float)) else "-"


def _impact(rec):
    d = rec.get("delta_tokens")
    return f"~{d:,} tok/window" if d else rec["impact"].get("ordinal", "?")


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


def render(run_id, findings, dropped, verify_rows, trend_rows, usage, footer) -> str:
    L = [f"# self-optimize report — {run_id}", ""]
    if verify_rows:
        L += ["## Applied changes: outcomes", "",
              "| id | title | metric | verdict | baseline | now | n |",
              "|---|---|---|---|---|---|---|"]
        for v in verify_rows:
            verdict_cell = f"**{v['verdict']}**"
            if v["verdict"] == "regressed":
                verdict_cell += f" · rollback: /self-optimize rollback {v['id']}"
            L.append(f"| `{v['id']}` | {_cell(v['title'])} | {_cell(v['metric'])} | {verdict_cell} | "
                     f"{_num(v.get('baseline'))} | {_num(v.get('value'))} | {v.get('n')} |")
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
        if r.get("prior_rejection"):
            L.append(f"- previously rejected: {r['prior_rejection']} — evidence has changed since")
        if r["action"]["tier"] == "A":
            L.append(f"- apply: `/self-optimize apply {r['id']}` · reject: "
                     f"`/self-optimize reject {r['id']} \"<reason>\"`")
        else:
            p = r["action"].get("payload", {})
            body = p.get("diff") or p.get("description") or json.dumps(p, indent=1)
            L += ["- manual action:", "", "```", body, "```"]
        L.append("")
    if trend_rows:
        L += ["## Trend", "",
              "| run | sessions | tok/session | corrections | dup reads | base ctx | unused |",
              "|---|---|---|---|---|---|---|"]
        for t in trend_rows:
            L.append(f"| {t['run_id']} | {t['n_sessions']} | {_num(t.get('tokens_per_session'))} | "
                     f"{_num(t.get('correction_rate'))} | {_num(t.get('duplicate_read_rate'))} | "
                     f"{t.get('base_context_est') if t.get('base_context_est') is not None else '-'} | "
                     f"{t.get('unused_surface_count') if t.get('unused_surface_count') is not None else '-'} |")
        L.append("")
    L += ["## Run footer", "",
          f"- window sessions: {usage['totals']['sessions']}; "
          f"parse-skipped lines: {usage['parse']['skipped_lines']}",
          f"- redactions applied to stored excerpts: {usage['parse'].get('redactions', 0)}",
          f"- findings dropped — invalid: {dropped['invalid']}, "
          f"failed citations: {dropped['citations']}, guard: {dropped['guard']}, "
          f"suppressed by ledger: {len(dropped['suppressed'])}",
          f"- analyst tokens: {_analyst_tokens_line(footer.get('analyst_tokens'))}", ""]
    return "\n".join(L) + "\n"


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
    md = render(a.run_id, fdata["findings"], fdata["dropped"], verify_rows,
                _trend(state), usage, {"analyst_tokens": tokens})
    rdir = Path(cfg["report_dir"])
    rdir.mkdir(parents=True, exist_ok=True)
    out = rdir / f"{a.run_id}.md"
    out.write_text(md)
    ledger_mod.append_run(state / "state" / "runs.jsonl",
                          {"run_id": a.run_id, "n_sessions": usage["totals"]["sessions"],
                           "findings": len(fdata["findings"]),
                           "analyst_tokens": tokens})
    _prune_evidence(state, cfg["retain_runs"])
    print(f"REPORT: {out}")
    for i, r in enumerate(fdata["findings"][:5], 1):
        print(f"{i}. [{r['category']}/{r['action']['tier']}] {r['title']} "
              f"({_impact(r)}) — id {r['id']}")


if __name__ == "__main__":
    main()
