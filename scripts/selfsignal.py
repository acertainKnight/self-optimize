"""Self-application: the analyst instruction files as gym artifacts.

The four analysts (`agents/*.md`) are markdown steering an agent — the same thing
the gym scores and bounded edits improve everywhere else. This module is the seam
that lets the loop point at itself: it registers those files as gym artifacts and
builds their case corpus.

The self-signal is NOT session transcripts. An analyst's own output is already
graded by the rest of the pipeline, and those grades are recorded:

  failure  a finding the human rejected, a finding that regressed after it was
           applied, a finding synth.py dropped because its citations did not
           resolve, and (evolver only) a candidate the gym scored poorly.
  working  a finding that was applied and later verified, and a candidate the
           gym scored well.

"Scored poorly" is the two-sided rule the gym already reports, stated as a
binary: a candidate that prevented nothing, or that broke a working case, is a
poor proposal; one that prevented at least one failure case while preserving
every working case is a good one. Nothing here reads a single number.

Timescale is deliberately slow. Cases only accrue from runs that have already
happened, an analyst edit is permanently tier B (`schema.validate_rec` pins it),
synth caps it at one per analyst per run, and `self_edit_rows` re-reads the next
run's citation-resolution and verified-outcome rates so an edit that made the
pipeline worse is flagged for rollback instead of quietly staying in.
"""
import hashlib
import json
from pathlib import Path

import ledger as ledger_mod
import redact

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
KIND = "analyst"
HARNESS = "self-optimize"

# The runner writes each analyst's output to EV/<stem>.json (plus <stem>-retry.json
# for the citation-repair round). This is the only place that mapping is stated.
ANALYST_BY_STEM = {"miner": "transcript-miner", "auditor": "config-auditor",
                   "evolver": "evolver", "labeler": "sample-labeler"}

# mirrors gym.CASE_USER_CHARS / CASE_ASSISTANT_CHARS — cases are judge input, and
# gym imports this module, so the caps are restated rather than imported back
CASE_USER_CHARS = 1200
CASE_ASSISTANT_CHARS = 600

FAILURE_STATUSES = {"rejected": "rejected by the human",
                    "regressed": "applied, then measured worse"}
WORKING_STATUSES = {"verified": "applied, then measured better"}


# ---------------------------------------------------------------- the artifacts
def analyst_files(root=None) -> dict:
    """{artifact id: path} for every analyst instruction file present on disk."""
    base = Path(root or PLUGIN_ROOT) / "agents"
    out = {}
    for name in sorted(set(ANALYST_BY_STEM.values())):
        path = base / f"{name}.md"
        if path.is_file():
            out[f"{KIND}:{name}"] = path
    return out


def artifact_entries(root=None) -> dict:
    """Registry rows in the shape gym.derive_artifacts produces. `activation_key`
    is None — an analyst never fires in a user session, so it can accrue nothing
    from transcripts; `self_signal` is what makes it scorable anyway."""
    return {aid: {"kind": KIND, "name": aid.split(":", 1)[1], "path": str(path),
                  "source": HARNESS, "activation_key": None, "self_signal": True}
            for aid, path in analyst_files(root).items()}


def artifact_for_stem(stem) -> str | None:
    """'miner' -> 'analyst:transcript-miner'. A citation-retry file ('miner-retry')
    is the same analyst, so its suffix is stripped first."""
    name = ANALYST_BY_STEM.get(str(stem or "").removesuffix("-retry"))
    return f"{KIND}:{name}" if name else None


def analyst_of(rec: dict) -> str | None:
    """The artifact id of the analyst that proposed this finding, from the
    provenance synth.py stamps on it. Older findings carry no stamp and are
    simply unattributable — never guessed at from the category."""
    return artifact_for_stem((rec or {}).get("analyst"))


def is_analyst_path(path, root=None) -> bool:
    """True only for one of the analyst instruction files themselves. Symlinks are
    resolved on both sides, so an installed plugin behind a symlinked directory
    still matches and nothing else can be aimed at this carve-out by naming."""
    if not isinstance(path, str) or not path.strip():
        return False
    try:
        target = Path(path).expanduser().resolve()
        return any(p.resolve() == target for p in analyst_files(root).values())
    except (OSError, RuntimeError, ValueError):
        return False


def target_artifact(rec: dict) -> str | None:
    """The analyst artifact a finding proposes to edit, or None if it edits
    something else. Bounded edits only — a self-edit is never a whole-file
    rewrite, a diff, or a retire."""
    action = (rec or {}).get("action") or {}
    if action.get("type") != "file_ops":
        return None
    path = (action.get("payload") or {}).get("path")
    if not is_analyst_path(path):
        return None
    for aid, known in analyst_files().items():
        try:
            if known.resolve() == Path(path).expanduser().resolve():
                return aid
        except (OSError, RuntimeError, ValueError):
            return None
    return None


# ---------------------------------------------------------------- case building
def _case(aid: str, side: str, signal: str, key: str, assistant_text: str,
          user_text: str, run_id) -> dict:
    """One graded case in the shape gym.load_corpus stores and gym.build_prompt
    judges. The id is derived from what happened, not from when it was read, so
    re-reading the same ledger every run adds nothing the second time."""
    basis = "|".join([aid, side, signal, key])
    return {"id": hashlib.sha256(basis.encode()).hexdigest()[:12],
            "harness": HARNESS, "session": None, "project": None,
            "ts": str(run_id), "first_seen_run": str(run_id),
            "origin": "self", "signal": signal,
            "user_text": redact.scrub(user_text or "")[0][:CASE_USER_CHARS],
            "assistant_text": redact.scrub(assistant_text or "")[0][:CASE_ASSISTANT_CHARS]}


def finding_text(rec: dict) -> str:
    """What the analyst produced, as the judge sees it."""
    action = (rec or {}).get("action") or {}
    refs = ", ".join(str(r) for r in (rec or {}).get("evidence_refs") or []) or "none"
    return "\n".join([f"proposed: {(rec or {}).get('title', '')}",
                      f"category: {(rec or {}).get('category')}; "
                      f"action: {action.get('type')} (tier {action.get('tier')})",
                      f"evidence cited: {refs}",
                      f"stated risk: {(rec or {}).get('risk', '')}"])


def score_side(score: dict) -> str | None:
    """'failure' for a poor gym score, 'working' for a good one, None when the
    candidate was never scored. Both sides are read: a candidate that prevents
    corrections by breaking what already worked is a poor proposal, not a good
    one, and grading it on the prevented side alone is how a self-improving loop
    learns to game itself."""
    if not isinstance(score, dict) or score.get("unscorable"):
        return None
    prevented, preserved = score.get("prevented") or {}, score.get("preserved") or {}
    if not prevented.get("of") or not preserved.get("of"):
        return None
    if prevented["n"] == 0 or preserved["n"] < preserved["of"]:
        return "failure"
    return "working"


def _score_text(score: dict) -> str:
    p, w = score.get("prevented") or {}, score.get("preserved") or {}
    return (f"the gym scored this candidate on the artifact's recorded cases: "
            f"prevented {p.get('n', 0)}/{p.get('of', 0)} failure cases, "
            f"preserved {w.get('n', 0)}/{w.get('of', 0)} working cases.")


def _run_dirs(state) -> list:
    ev = Path(state) / "evidence"
    return sorted((d for d in ev.iterdir() if d.is_dir()), key=lambda d: d.name) \
        if ev.is_dir() else []


def _read_json(path: Path):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, json.JSONDecodeError):
        return None


def collect(state, run_id) -> dict:
    """{artifact id: {"failure": [case], "working": [case]}} for every analyst.

    Three sources, all of them records the pipeline already keeps: the ledger's
    outcome for each finding, each past run's citation drops, and each past run's
    gym scores. Only runs that already finished contribute — this is called at the
    start of a run, before its own analysts have said anything."""
    pending = {aid: {"failure": [], "working": []} for aid in analyst_files()}
    if not pending:
        return {}

    def add(aid, side, signal, key, assistant_text, user_text):
        if aid in pending:
            pending[aid][side].append(
                _case(aid, side, signal, key, assistant_text, user_text, run_id))

    for rid, entry in ledger_mod.load(Path(state) / "state" / "ledger.jsonl").items():
        rec = entry.get("rec") or {}
        aid = analyst_of(rec)
        status = entry.get("status")
        if not aid:
            continue
        if status in FAILURE_STATUSES:
            reason = entry.get("reason") or ""
            add(aid, "failure", status, rid, finding_text(rec),
                f"this recommendation was {FAILURE_STATUSES[status]}"
                + (f": {reason}" if reason else "."))
        elif status in WORKING_STATUSES:
            add(aid, "working", status, rid, finding_text(rec),
                f"this recommendation was {WORKING_STATUSES[status]}.")

    for rdir in _run_dirs(state):
        findings_doc = _read_json(rdir / "findings.json") or {}
        for drop in ((findings_doc.get("dropped") or {}).get("citation_detail") or []):
            aid = artifact_for_stem(drop.get("analyst"))
            refs = ", ".join(str(r) for r in drop.get("failed_refs") or [])
            title = str(drop.get("title", ""))
            if aid:
                add(aid, "failure", "citations", f"{rdir.name}|{title}|{refs}",
                    f"proposed: {title}",
                    "this recommendation was dropped before anyone read it: its "
                    f"evidence refs did not resolve against the evidence pack ({refs}).")
        scores = _read_json(rdir / "gym.json")
        if not isinstance(scores, dict):
            continue
        by_id = {f["id"]: f for f in findings_doc.get("findings") or []
                 if isinstance(f, dict) and isinstance(f.get("id"), str)}
        for fid, score in scores.items():
            side = score_side(score)
            rec = by_id.get(fid)
            aid = analyst_of(rec or {})
            if side and aid:
                add(aid, side, "gym-score", f"{rdir.name}|{fid}",
                    finding_text(rec), _score_text(score))
    return pending


# ---------------------------------------------------------------- the guard
def run_rates(rundir) -> dict:
    """The two pipeline-health rates for one completed run.

    citation_rate    share of analyst recommendations whose citations all resolved.
    verified_rate    share of terminal verify verdicts that came out verified.

    Either is None when that run has nothing to measure it on — an absent number
    is reported as absent, never as a zero."""
    rundir = Path(rundir)
    row = {"run_id": rundir.name, "citation_rate": None, "verified_rate": None}
    doc = _read_json(rundir / "findings.json") or {}
    dropped = doc.get("dropped") or {}
    failed = dropped.get("citations")
    if isinstance(failed, int):
        checked = dropped.get("citation_checked")
        if not isinstance(checked, int) or checked < failed:
            # runs written before synth counted the denominator: the recs that
            # survived plus the ones this check dropped is the best it can say
            checked = len(doc.get("findings") or []) + failed
        if checked > 0:
            row["citation_rate"] = (checked - failed) / checked
    vdoc = _read_json(rundir / "verify.json") or {}
    verdicts = [r.get("verdict") for r in vdoc.get("rows") or []]
    terminal = sum(v in ("verified", "regressed") for v in verdicts)
    if terminal:
        row["verified_rate"] = sum(v == "verified" for v in verdicts) / terminal
    return row


RATES = ("citation_rate", "verified_rate")


def self_edit_rows(entries: dict, state, min_rel_change: float) -> list:
    """Score every applied self-edit against the run that followed it.

    A self-edit changes how an analyst reasons, so its effect shows up in the
    pipeline's own numbers rather than in a session metric: fewer of its findings
    surviving the citation check, or fewer of the applied ones verifying. Either
    one falling by more than the configured threshold is flagged for rollback.
    Flagged, not reverted — every rollback in this plugin is a human's call."""
    rows = []
    runs = _run_dirs(state)
    for rid, entry in sorted(entries.items()):
        if entry.get("status") not in ("applied", "verified", "regressed"):
            continue
        rec = entry.get("rec") or {}
        aid = target_artifact(rec)
        if not aid:
            continue
        applied_day = str(entry.get("applied_at") or "")[:10]
        before = next((r for r in reversed(runs) if r.name <= applied_day), None)
        after = next((r for r in runs if r.name > applied_day), None)
        row = {"id": rid, "title": rec.get("title", ""), "artifact": aid,
               "applied_at": entry.get("applied_at"),
               "before_run": before.name if before else None,
               "after_run": after.name if after else None,
               "verdict": "inconclusive", "note": "", "rates": {}}
        if before is None or after is None:
            row["note"] = "no completed run after this edit yet"
            rows.append(row)
            continue
        pre, post = run_rates(before), run_rates(after)
        degraded, measured = [], 0
        for key in RATES:
            b, a = pre[key], post[key]
            if b in (None, 0) or a is None:
                continue
            measured += 1
            rel = (a - b) / b
            row["rates"][key] = {"before": b, "after": a, "rel_change": rel}
            if rel <= -min_rel_change:
                degraded.append(key)
        if degraded:
            row["verdict"] = "regressed"
            row["note"] = (", ".join(degraded) + " fell after this edit — "
                           f"roll it back with /self-optimize rollback {rid}")
        elif measured:
            row["verdict"] = "held"
            row["note"] = "no measured degradation in the run after this edit"
        else:
            row["note"] = "neither rate is measurable across these two runs"
        rows.append(row)
    return rows
