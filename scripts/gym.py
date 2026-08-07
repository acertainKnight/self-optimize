"""Eval gym: the acceptance substrate every candidate artifact version is scored on.

For each optimizable artifact the gym keeps a corpus of graded cases mined from real
usage, so a proposed rewrite can be checked against evidence instead of taste.

Registry — DERIVED, never curated. Every `update` re-reads the inventory step's
output and registers whatever it reports: skills, agents, hooks, guidance blocks.
A new skill on disk shows up with an empty corpus on the next run. An artifact
missing from the inventory for `retire_after_absent_runs` consecutive runs is
marked retired and excluded from scoring; if it comes back, it un-retires with its
corpus intact. (Commands are not enumerated by the inventory step, so the gym does
not see them yet — when inventory grows a `commands` list, they register for free.)

Cases — mined at SESSION granularity, which is the honest resolution of the
evidence pack (step-level failure attribution is unsolved):
  failure case: the artifact was active in a session where the user corrected the
                assistant — the correction excerpt from samples.json.
  working case: the artifact was active in a session that drew no correction at
                all — an ask/answer excerpt from working.json.
Attribution needs a per-session activation signal (`sessions[].activation`), so only
skills and agents accrue cases today. Hooks and guidance blocks are registered and
reported, but stay unscorable until some harness reports them firing per session.

Case floor: below `min_cases_per_side` on either side the gym reports `unscorable`
and refuses to emit a score at all — a number off two cases is noise, not evidence.

Retention: cases are deduplicated by content (collection windows overlap, so runs
re-see the same sessions) and FIFO-trimmed to `max_cases_per_side`, oldest first.

State lives under `<state>/gym/` (registry.json + corpus/*.json, mode 0600) and never
enters a repo: real cases are real transcript excerpts.

Scoring (`gym.py score`) judges a candidate artifact text against that corpus and
reports BOTH sides — how many failure cases it would have prevented AND how many
working cases it preserves — and nothing here ever applies a change on a score. A
loop that optimizes one number will find ways to game that number: Prime Intellect's
prime-agent edits its own harness mid-session with no held-out validation, and in
their Factorio study the refine loop climbed the production metric partly by
discovering reward hacks (spawning resources outright). The two-sided score plus a
human ratifying every apply is the defense; a single auto-gated number is the failure
mode. Scores are decision support, never an auto-gate.

The judge backend is configured, never hardcoded: `gym.judge.command` in config.json
is any CLI that reads a prompt on stdin and writes a JSON verdict on stdout. There is
no default backend, model, or vendor anywhere in this file — an unconfigured judge is
a refusal with instructions, not a silent fallback to somebody's API.
"""
import argparse
import hashlib
import json
import re
import subprocess
import sys
from datetime import date
from pathlib import Path

import redact

GYM_VERSION = "1"
# cases are judge input, so excerpts are capped at the same size collect.py stores
CASE_USER_CHARS = 1200
CASE_ASSISTANT_CHARS = 600
SIDES = ("failure", "working")


# ---------------------------------------------------------------- state layout
def gym_dir(state) -> Path:
    d = Path(state) / "gym"
    (d / "corpus").mkdir(parents=True, exist_ok=True)
    d.chmod(0o700)
    (d / "corpus").chmod(0o700)
    return d


def _slug(artifact_id: str) -> str:
    """Readable-but-safe corpus filename. Guidance ids carry a full path, so the
    sanitized head is truncated and disambiguated with a hash of the whole id."""
    head = re.sub(r"[^A-Za-z0-9_.-]", "_", artifact_id)[:60]
    return f"{head}-{hashlib.sha256(artifact_id.encode()).hexdigest()[:8]}"


def _write_json(path: Path, obj: dict) -> None:
    path.write_text(json.dumps(obj, indent=1))
    path.chmod(0o600)


def _read_json(path: Path) -> dict:
    try:
        data = json.loads(Path(path).read_text())
    except (json.JSONDecodeError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def load_registry(gdir: Path) -> dict:
    reg = _read_json(Path(gdir) / "registry.json")
    reg.setdefault("schema_version", GYM_VERSION)
    reg.setdefault("artifacts", {})
    reg.setdefault("last_run", None)
    return reg


def save_registry(gdir: Path, reg: dict) -> None:
    _write_json(Path(gdir) / "registry.json", reg)


def load_corpus(gdir: Path, artifact_id: str) -> dict:
    c = _read_json(Path(gdir) / "corpus" / f"{_slug(artifact_id)}.json")
    c.setdefault("schema_version", GYM_VERSION)
    c["artifact"] = artifact_id
    for side in SIDES:
        c.setdefault(side, [])
    return c


def save_corpus(gdir: Path, corpus: dict) -> None:
    _write_json(Path(gdir) / "corpus" / f"{_slug(corpus['artifact'])}.json", corpus)


# ---------------------------------------------------------------- registry
def derive_artifacts(inv: dict) -> dict:
    """Optimizable artifacts as the inventory step sees them. `activation_key` is the
    id the collector reports per session; None means no activation signal exists for
    that kind, so it can never accrue cases (and never scores)."""
    out = {}
    for item in inv.get("skills") or []:
        out[item["id"]] = {"kind": "skill", "name": item.get("name"),
                           "path": item.get("path"), "source": item.get("source"),
                           "activation_key": item["id"]}
    for item in inv.get("agents") or []:
        out[item["id"]] = {"kind": "agent", "name": item.get("name"),
                           "path": item.get("path"), "source": item.get("source"),
                           "activation_key": item["id"]}
    for item in inv.get("hooks") or []:
        out[item["id"]] = {"kind": "hook", "name": item["id"], "path": None,
                           "source": item.get("source"), "activation_key": None}
    for item in inv.get("guidance") or []:
        out[item["id"]] = {"kind": "guidance", "name": item.get("path"),
                           "path": item.get("path"), "source": item.get("kind"),
                           "activation_key": None}
    return out


def update_registry(reg: dict, derived: dict, run_id: str, retire_after: int) -> dict:
    """Upsert everything inventory reported; tick absence for everything it didn't.
    Absence is counted once per run id (re-running `update` for the same run is
    idempotent) and never when the inventories were empty — a failed or partial
    collect must not retire the whole registry."""
    arts = reg["artifacts"]
    for aid, fields in sorted(derived.items()):
        entry = arts.setdefault(aid, {"id": aid, "first_seen_run": run_id})
        entry.update(fields)
        entry["last_seen_run"] = run_id
        entry["absent_runs"] = 0
        entry["retired"] = False
    if derived and run_id != reg.get("last_run"):
        for aid, entry in arts.items():
            if aid in derived:
                continue
            entry["absent_runs"] = entry.get("absent_runs", 0) + 1
            entry["retired"] = entry["absent_runs"] >= retire_after
    reg["last_run"] = run_id
    return reg


# ---------------------------------------------------------------- case accrual
def case_id(harness: str, session: str, ts, side: str, user_text: str, assistant_text: str) -> str:
    basis = "|".join([harness, str(session), str(ts), side,
                      hashlib.sha256((user_text + assistant_text).encode()).hexdigest()])
    return hashlib.sha256(basis.encode()).hexdigest()[:12]


def read_pack(evidence_dir) -> dict:
    """One harness's evidence pack, reduced to what the gym needs. Missing files are
    empty, not fatal: adapters declare their gaps rather than faking fields."""
    ev = Path(evidence_dir)
    inv = _read_json(ev / "inventory.json")
    sess_doc = _read_json(ev / "sessions.json")
    return {"harness": inv.get("harness") or sess_doc.get("harness") or "unknown",
            "inventory": inv, "sessions": sess_doc.get("sessions") or [],
            "samples": _read_json(ev / "samples.json").get("samples") or [],
            "working": _read_json(ev / "working.json").get("samples") or []}


def _make_case(harness: str, side: str, row: dict, user_text: str, assistant_text: str,
               run_id: str) -> dict:
    user_text = redact.scrub(user_text or "")[0][:CASE_USER_CHARS]
    assistant_text = redact.scrub(assistant_text or "")[0][:CASE_ASSISTANT_CHARS]
    return {"id": case_id(harness, row.get("session"), row.get("ts"), side,
                          user_text, assistant_text),
            "harness": harness, "session": row.get("session"),
            "project": row.get("project"), "ts": row.get("ts"),
            "first_seen_run": run_id, "user_text": user_text,
            "assistant_text": assistant_text}


def _merge(existing: list, new: list, cap: int) -> tuple:
    """Dedupe by case id, then FIFO-trim to the cap (oldest timestamps drop first).
    Returns (cases, added)."""
    by_id = {c["id"]: c for c in existing}
    added = 0
    for case in new:
        if case["id"] in by_id:
            continue
        by_id[case["id"]] = case
        added += 1
    ordered = sorted(by_id.values(), key=lambda c: (c.get("ts") or "", c["id"]))
    return ordered[-cap:] if cap > 0 else ordered, added


def accrue(state, evidence_dirs: list, run_id: str, cfg: dict) -> dict:
    gcfg = cfg.get("gym") or {}
    cap = int(gcfg.get("max_cases_per_side", 20))
    retire_after = int(gcfg.get("retire_after_absent_runs", 3))
    gdir = gym_dir(state)
    reg = load_registry(gdir)
    known_before = set(reg["artifacts"])

    packs = [read_pack(d) for d in evidence_dirs]
    derived = {}
    for pack in packs:
        derived.update(derive_artifacts(pack["inventory"]))
    update_registry(reg, derived, run_id, retire_after)

    live = {aid: e for aid, e in reg["artifacts"].items()
            if not e.get("retired") and e.get("activation_key")}
    by_key = {e["activation_key"]: aid for aid, e in live.items()}
    pending = {aid: {"failure": [], "working": []} for aid in live}

    for pack in packs:
        sess = {s.get("id"): s for s in pack["sessions"]}
        for smp in pack["samples"]:
            if smp.get("kind") not in (None, "correction"):
                continue
            for aid in _active_artifacts(sess.get(smp.get("session")), by_key):
                pending[aid]["failure"].append(
                    _make_case(pack["harness"], "failure", smp, smp.get("user_text", ""),
                               smp.get("prior_assistant_text", ""), run_id))
        for wrk in pack["working"]:
            s = sess.get(wrk.get("session"))
            if s and s.get("corrections_count"):
                continue
            for aid in _active_artifacts(s, by_key):
                pending[aid]["working"].append(
                    _make_case(pack["harness"], "working", wrk, wrk.get("user_text", ""),
                               wrk.get("assistant_text", ""), run_id))

    added = {"failure": 0, "working": 0}
    for aid, sides in pending.items():
        if not (sides["failure"] or sides["working"]):
            continue
        corpus = load_corpus(gdir, aid)
        for side in SIDES:
            corpus[side], n = _merge(corpus[side], sides[side], cap)
            added[side] += n
        save_corpus(gdir, corpus)
    save_registry(gdir, reg)
    return {"registered": len(reg["artifacts"]),
            "new": sorted(set(derived) - known_before),
            "retired": sorted(a for a, e in reg["artifacts"].items() if e.get("retired")),
            "added_failure": added["failure"], "added_working": added["working"]}


def _active_artifacts(session: dict | None, by_key: dict) -> list:
    if not session:
        return []
    keys = session.get("activation") or {}
    return sorted(by_key[k] for k in keys if k in by_key)


# ---------------------------------------------------------------- status
def artifact_status(state, cfg: dict) -> list:
    """One row per registered artifact: case counts and whether it can be scored."""
    floor = int((cfg.get("gym") or {}).get("min_cases_per_side", 3))
    gdir = gym_dir(state)
    reg = load_registry(gdir)
    rows = []
    for aid in sorted(reg["artifacts"]):
        entry = reg["artifacts"][aid]
        corpus = load_corpus(gdir, aid)
        counts = {side: len(corpus[side]) for side in SIDES}
        scorable, reason = _scorable(entry, counts, floor)
        rows.append({"id": aid, "kind": entry.get("kind"), "retired": bool(entry.get("retired")),
                     "absent_runs": entry.get("absent_runs", 0),
                     "failure_cases": counts["failure"], "working_cases": counts["working"],
                     "scorable": scorable, "reason": reason})
    return rows


def _scorable(entry: dict, counts: dict, floor: int) -> tuple:
    if entry.get("retired"):
        return False, f"retired (absent {entry.get('absent_runs', 0)} runs)"
    if not entry.get("activation_key"):
        return False, "no per-session activation signal for this artifact kind"
    thin = [s for s in SIDES if counts[s] < floor]
    if thin:
        return False, (f"below case floor: {', '.join(f'{s} {counts[s]}/{floor}' for s in thin)}")
    return True, ""


# ---------------------------------------------------------------- judge driver
JUDGE_UNCONFIGURED = """refusing to score: no judge backend is configured.

Set gym.judge.command in {path} to a CLI that reads a prompt on stdin and writes a
JSON verdict on stdout, e.g.

  "gym": {{"judge": {{"command": ["<your-cli>", "run", "--model", "{{model}}"],
                    "model": "<your-model-id>", "timeout_s": 120}}}}

"{{model}}" in any argument is replaced with gym.judge.model. This plugin ships no
default judge: the backend, the model and the vendor are your choice, and it will
not pick one for you."""

VERDICT_KEY = {"failure": "prevented", "working": "preserved"}


class JudgeError(RuntimeError):
    pass


class JudgeNotConfigured(JudgeError):
    """Raised instead of guessing a backend. Never caught by the per-case handler:
    it is raised before any case is judged."""


def build_prompt(artifact_id: str, candidate_text: str, case: dict, side: str) -> str:
    """Deterministic by construction: fixed sections, fixed order, no timestamps and
    no run ids, so the same candidate and case always produce a byte-identical prompt
    (and therefore a cacheable prompt hash)."""
    if side == "failure":
        exchange = [
            "--- ASSISTANT (what it did) ---", case.get("assistant_text", ""),
            "--- END ASSISTANT ---", "",
            "--- USER (the correction that followed) ---", case.get("user_text", ""),
            "--- END USER ---", "",
            "QUESTION: if the assistant had been operating under the candidate artifact",
            "text above, would this correction still have been needed?",
            'Reply with exactly {"prevented": true} if the candidate addresses what the',
            'user corrected, or {"prevented": false} if it does not.']
    else:
        exchange = [
            "--- USER (the request) ---", case.get("user_text", ""),
            "--- END USER ---", "",
            "--- ASSISTANT (the response that drew no correction) ---",
            case.get("assistant_text", ""), "--- END ASSISTANT ---", "",
            "QUESTION: under the candidate artifact text above, would this exchange still",
            "have gone well, with no correction needed?",
            'Reply with exactly {"preserved": true} if the candidate keeps this working,',
            'or {"preserved": false} if it would have derailed it.']
    head = ["You are grading one candidate version of an agent-harness artifact against",
            "one recorded case from real usage. Answer with JSON only, no other output.",
            "",
            f"ARTIFACT: {artifact_id}",
            f"CASE SIDE: {side}",
            f"CASE ID: {case['id']}",
            "",
            "--- CANDIDATE ARTIFACT TEXT ---", candidate_text.strip(),
            "--- END CANDIDATE ARTIFACT TEXT ---", ""]
    return "\n".join(head + exchange) + "\n"


def parse_verdict(stdout: str, side: str) -> bool:
    """Accept a bare JSON object, or the first JSON object embedded in chatter — a
    judge that wraps its answer in prose is common and not worth failing over."""
    key = VERDICT_KEY[side]
    obj = None
    try:
        obj = json.loads(stdout)
    except json.JSONDecodeError:
        m = re.search(r"\{[^{}]*\}", stdout, re.S)
        if m:
            try:
                obj = json.loads(m.group(0))
            except json.JSONDecodeError:
                obj = None
    if not isinstance(obj, dict) or key not in obj:
        raise JudgeError(f"judge returned no '{key}' verdict: {stdout.strip()[:200]!r}")
    if not isinstance(obj[key], bool):
        raise JudgeError(f"'{key}' must be a boolean, got {obj[key]!r}")
    return obj[key]


def _invoke_judge(judge: dict, prompt: str) -> str:
    """The one subprocess driver every judge call goes through: a CLI that reads
    the prompt on stdin and writes its verdict JSON on stdout. Shared by run_judge
    (gym scoring) and localize.py (bisection localization) so there is exactly one
    place in this plugin that shells out to the configured backend."""
    cmd = [str(part).replace("{model}", str(judge.get("model") or ""))
           for part in judge["command"]]
    try:
        proc = subprocess.run(cmd, input=prompt, capture_output=True, text=True,
                              timeout=float(judge.get("timeout_s", 120)))
    except (OSError, subprocess.SubprocessError) as e:
        raise JudgeError(f"judge command failed: {e}") from e
    if proc.returncode != 0:
        raise JudgeError(f"judge exited {proc.returncode}: {proc.stderr.strip()[:200]}")
    return proc.stdout


def run_judge(judge: dict, prompt: str, side: str) -> bool:
    return parse_verdict(_invoke_judge(judge, prompt), side)


# ---------------------------------------------------------------- scoring
def _est_tokens(text: str) -> int:
    return len(text) // 4


def score_artifact(state, artifact_id: str, candidate_text: str, cfg: dict,
                   budget: int | None = None, run_id: str | None = None) -> dict:
    """Two-sided score for one candidate artifact text. Judge calls are the expensive
    step, so everything that can refuse before spending does: an unknown, retired or
    below-floor artifact short-circuits to `unscorable` without invoking the judge at
    all, and cases that do not fit the token budget are skipped rather than truncated."""
    gcfg = cfg.get("gym") or {}
    floor = int(gcfg.get("min_cases_per_side", 3))
    gdir = gym_dir(state)
    reg = load_registry(gdir)
    result = {"artifact": artifact_id, "run_id": run_id,
              "prevented": {"n": 0, "of": 0}, "preserved": {"n": 0, "of": 0},
              "unscorable": True, "reason": "", "errors": 0, "skipped_budget": 0,
              "tokens_est": 0, "per_case": []}

    entry = reg["artifacts"].get(artifact_id)
    if entry is None:
        result["reason"] = f"{artifact_id} is not in the gym registry"
        return result
    corpus = load_corpus(gdir, artifact_id)
    counts = {side: len(corpus[side]) for side in SIDES}
    ok, reason = _scorable(entry, counts, floor)
    if not ok:
        result["reason"] = reason
        return result

    judge = gcfg.get("judge") or {}
    if not judge.get("command"):
        raise JudgeNotConfigured(artifact_id)

    spent = 0
    judged = {"failure": [0, 0], "working": [0, 0]}   # [true verdicts, verdicts total]
    for side in SIDES:
        for case in sorted(corpus[side], key=lambda c: c["id"]):
            prompt = build_prompt(artifact_id, candidate_text, case, side)
            row = {"case": case["id"], "side": side, "verdict": None,
                   "prompt_hash": hashlib.sha256(prompt.encode()).hexdigest()[:12]}
            est = _est_tokens(prompt)
            if budget and spent + est > budget:
                row["error"] = "skipped: token budget exhausted"
                result["skipped_budget"] += 1
                result["per_case"].append(row)
                continue
            spent += est
            try:
                row["verdict"] = run_judge(judge, prompt, side)
                judged[side][1] += 1
                judged[side][0] += int(row["verdict"])
            except JudgeError as e:
                row["error"] = str(e)
                result["errors"] += 1
            result["per_case"].append(row)
    result["tokens_est"] = spent
    result["prevented"] = {"n": judged["failure"][0], "of": judged["failure"][1]}
    result["preserved"] = {"n": judged["working"][0], "of": judged["working"][1]}
    thin = [s for s in SIDES if judged[s][1] < floor]
    if thin:
        result["reason"] = ("too few usable verdicts to score: "
                            + ", ".join(f"{s} {judged[s][1]}/{floor}" for s in thin)
                            + f" ({result['errors']} judge errors, "
                            f"{result['skipped_budget']} skipped for budget)")
    else:
        result["unscorable"] = False
    return result


# ---------------------------------------------------------------- CLI
def cmd_update(a) -> int:
    import so_config
    _, state = so_config.resolve(None, a.state)
    cfg = so_config.load_config(state)
    summary = accrue(state, a.evidence, a.run_id, cfg)
    print(f"gym: registered={summary['registered']} new={len(summary['new'])} "
          f"retired={len(summary['retired'])} "
          f"+failure_cases={summary['added_failure']} +working_cases={summary['added_working']}")
    return 0


def cmd_status(a) -> int:
    import so_config
    _, state = so_config.resolve(None, a.state)
    cfg = so_config.load_config(state)
    rows = artifact_status(state, cfg)
    if a.json:
        print(json.dumps({"artifacts": rows}, indent=1))
        return 0
    print(f"{'artifact':<48} {'kind':<9} {'fail':>5} {'work':>5}  status")
    for r in rows:
        status = "scorable" if r["scorable"] else r["reason"]
        print(f"{r['id'][:48]:<48} {str(r['kind']):<9} {r['failure_cases']:>5} "
              f"{r['working_cases']:>5}  {status}")
    print(f"{len(rows)} artifacts, {sum(1 for r in rows if r['scorable'])} scorable")
    return 0


def cmd_score(a) -> int:
    import so_config
    _, state = so_config.resolve(None, a.state)
    cfg = so_config.load_config(state)
    candidate = Path(a.candidate).expanduser()
    if not candidate.exists():
        print(f"refusing to score: candidate file not found: {candidate}", file=sys.stderr)
        return 2
    budget = a.max_budget if a.max_budget is not None else cfg.get("max_budget_tokens", 0)
    try:
        result = score_artifact(state, a.artifact, candidate.read_text(errors="replace"),
                                cfg, budget=budget, run_id=a.run_id)
    except JudgeNotConfigured:
        print(JUDGE_UNCONFIGURED.format(path=Path(state) / "config.json"), file=sys.stderr)
        return 2
    result["candidate"] = str(candidate)
    if a.out:
        out = Path(a.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        _write_json(out, result)
    else:
        print(json.dumps(result, indent=1))
    if result["unscorable"]:
        print(f"gym score: {a.artifact} unscorable — {result['reason']}")
    else:
        p, w = result["prevented"], result["preserved"]
        print(f"gym score: {a.artifact} prevented {p['n']}/{p['of']} failure cases, "
              f"preserved {w['n']}/{w['of']} working cases "
              f"(~{result['tokens_est']} judge input tokens; evidence for your decision, "
              f"never an auto-gate)")
    return 0


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description="eval gym: per-artifact case corpus")
    sub = ap.add_subparsers(dest="cmd", required=True)

    up = sub.add_parser("update", help="derive the registry and accrue cases from a run")
    up.add_argument("--evidence", action="append", required=True,
                    help="evidence pack dir; repeat once per harness")
    up.add_argument("--state", default=None)
    up.add_argument("--run-id", default=date.today().isoformat())
    up.set_defaults(func=cmd_update)

    st = sub.add_parser("status", help="per-artifact case counts and scorable status")
    st.add_argument("--state", default=None)
    st.add_argument("--json", action="store_true")
    st.set_defaults(func=cmd_status)

    sc = sub.add_parser("score", help="score a candidate artifact text against its corpus")
    sc.add_argument("--artifact", required=True, help="registry id, e.g. skill:<name>")
    sc.add_argument("--candidate", required=True, help="file holding the candidate text")
    sc.add_argument("--state", default=None)
    sc.add_argument("--out", default=None, help="write the score JSON here (default: stdout)")
    sc.add_argument("--max-budget", type=int, default=None,
                    help="cap judge input tokens; cases past the cap are skipped, not truncated")
    sc.add_argument("--run-id", default=date.today().isoformat())
    sc.set_defaults(func=cmd_score)
    return ap


def main(argv=None):
    a = build_parser().parse_args(argv)
    raise SystemExit(a.func(a))


if __name__ == "__main__":
    main()
