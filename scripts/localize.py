"""Binary-search failure localization: for the handful of highest-friction sessions,
bisect the transcript with the pluggable judge backend to bracket roughly where
things went off track, in ceil(log2(turns)) judge calls per session instead of
grading every turn. Exact-step attribution is unsolved (published best is ~11-14%:
TRAIL, Who&When) so this settles for a bracketed turn range plus the judge's
rationale, meant to be attached to findings as advisory supporting evidence — it is
never its own finding category.

Opt-in via config `deep_localize: {enabled, top_n}`, off by default. Reuses gym.py's
subprocess judge driver (gym._invoke_judge) and its --max-budget token accounting
(gym._est_tokens) — one judge backend, one budget, configured the same way
everywhere in this plugin. No default backend, model, or vendor here either.
"""
import argparse
import json
import re
import sys
from pathlib import Path

import collect
import gym
import redact

# go-off-track signals already collected per session (docs/evidence-schema.md's
# sessions.json shape). duplicate_reads/repeated_calls are token waste, not
# derailment, so they're excluded from the friction score below.
FRICTION_FIELDS = ("corrections_count", "permission_stalls", "revert_events",
                   "reasks", "ended_on_correction")


def friction_score(session: dict) -> int:
    return sum(int(session.get(k) or 0) for k in FRICTION_FIELDS)


def top_friction_sessions(sessions: list, top_n: int) -> list:
    """Sessions scoring above the friction threshold (score > 0), highest first,
    capped at top_n. A run where every session scores 0 returns [] — this is what
    gives 'enabling adds no calls when no session crosses the threshold'."""
    scored = [(friction_score(s), s) for s in sessions]
    scored = [(sc, s) for sc, s in scored if sc > 0]
    scored.sort(key=lambda pair: (-pair[0], pair[1].get("id", "")))
    return [s for _, s in scored[:top_n]]


def load_turns(path: Path) -> list:
    """One entry per top-level (non-sidechain) assistant reply, paired with the
    user text that preceded it — the domain bisection searches over. This is
    transcript content leaving the file for a judge subprocess, so it is
    redaction-scrubbed and capped the same as the gym's case excerpts."""
    turns = []
    pending_user = ""
    counters = {"skipped_lines": 0}
    for rec in collect.iter_records(path, counters):
        if rec.get("isSidechain"):
            continue
        rtype = rec.get("type")
        msg = rec.get("message") or {}
        if rtype == "user":
            if rec.get("toolUseResult") is not None or rec.get("isMeta"):
                continue
            text = collect._text(msg)
            if text:
                pending_user, _ = redact.scrub(text[:gym.CASE_USER_CHARS])
        elif rtype == "assistant":
            text, _ = redact.scrub(collect._text(msg)[:gym.CASE_ASSISTANT_CHARS])
            turns.append({"user": pending_user, "assistant": text})
    return turns


def build_prompt(session_id: str, turns: list, idx: int) -> str:
    """Deterministic prompt for one bisection probe: the exchange at turn idx
    (0-based), with up to two turns of preceding context so the judge isn't
    guessing from a single exchange in isolation."""
    lead = []
    for i in range(max(0, idx - 2), idx):
        t = turns[i]
        lead += [f"[turn {i + 1}] USER: {t['user']}", f"[turn {i + 1}] ASSISTANT: {t['assistant']}"]
    cur = turns[idx]
    head = ["You are checking one point in an agent session transcript to help bracket",
            "where it went off track. Answer with JSON only, no other output.",
            "", f"SESSION: {session_id}", f"TURN: {idx + 1} of {len(turns)}", ""]
    if lead:
        head += ["--- PRECEDING CONTEXT ---", *lead, "--- END PRECEDING CONTEXT ---", ""]
    head += ["--- TURN UNDER REVIEW ---",
             f"USER: {cur['user']}", f"ASSISTANT: {cur['assistant']}",
             "--- END TURN UNDER REVIEW ---", "",
             "QUESTION: at this point, was the agent still on track toward what the user",
             "actually wanted?",
             'Reply with exactly {"on_track": true, "rationale": "<one sentence>"} or',
             '{"on_track": false, "rationale": "<one sentence>"}.']
    return "\n".join(head) + "\n"


def parse_bisect_verdict(stdout: str) -> tuple:
    """Accept a bare JSON object, or the first JSON object embedded in chatter —
    same tolerance as gym.parse_verdict."""
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
    if not isinstance(obj, dict) or not isinstance(obj.get("on_track"), bool):
        raise gym.JudgeError(f"judge returned no 'on_track' verdict: {stdout.strip()[:200]!r}")
    return bool(obj["on_track"]), str(obj.get("rationale", ""))[:400]


def bisect_session(session_id: str, turns: list, judge: dict, budget=None, spent=0) -> tuple:
    """Binary search over [0, len(turns)) for the first turn judged off track,
    assuming a session is on-track then off-track and never back (the same
    monotonic assumption Who&When's bisection strategy makes). Hard capped at
    ceil(log2(len(turns))) judge calls by construction: the loop halves the
    bracket every call and stops once it collapses to one turn. Returns
    (None, spent) if there's nothing to bisect (<=1 turn) or the budget runs out
    before a single call — a partially localized session is worse than none, so
    this run just skips it."""
    n = len(turns)
    if n <= 1:
        return None, spent
    lo, hi = 0, n - 1
    calls, rationale, errors = 0, "", 0
    while lo < hi:
        mid = (lo + hi) // 2
        prompt = build_prompt(session_id, turns, mid)
        est = gym._est_tokens(prompt)
        if budget and spent + est > budget:
            break
        spent += est
        try:
            on_track, why = parse_bisect_verdict(gym._invoke_judge(judge, prompt))
        except gym.JudgeError:
            errors += 1
            break
        calls += 1
        rationale = why or rationale
        lo = mid + 1 if on_track else lo
        hi = mid if not on_track else hi
    if calls == 0:
        return None, spent
    return {"bracket": [lo, hi], "turns_total": n, "calls": calls,
            "rationale": rationale, "errors": errors}, spent


def run(evidence_dir, data_root, cfg: dict, budget=None) -> dict:
    """Bisects the top-N friction sessions from EV/sessions.json, reading each
    one's raw transcript from data_root/projects/<project>/<id>.jsonl. Returns
    {<session_id>: bisect_session result, ...} — empty when deep_localize is
    disabled or no session crosses the friction threshold (zero judge calls in
    either case), and raises gym.JudgeNotConfigured only once a session actually
    needs bisecting."""
    lcfg = cfg.get("deep_localize") or {}
    if not lcfg.get("enabled"):
        return {}
    sessions = json.loads((Path(evidence_dir) / "sessions.json").read_text())["sessions"]
    top = top_friction_sessions(sessions, int(lcfg.get("top_n", 3)))
    if not top:
        return {}
    judge = (cfg.get("gym") or {}).get("judge") or {}
    if not judge.get("command"):
        raise gym.JudgeNotConfigured("deep_localize")
    out, spent = {}, 0
    for s in top:
        path = Path(data_root) / "projects" / s["project"] / f"{s['id']}.jsonl"
        if not path.exists():
            continue
        turns = load_turns(path)
        row, spent = bisect_session(s["id"], turns, judge, budget=budget, spent=spent)
        if row is None:
            continue
        row["friction_score"] = friction_score(s)
        out[s["id"]] = row
    return out


def main(argv=None) -> int:
    import so_config
    ap = argparse.ArgumentParser()
    ap.add_argument("--evidence", required=True)
    ap.add_argument("--data-root", default=None)
    ap.add_argument("--state", default=None)
    ap.add_argument("--out", required=True)
    ap.add_argument("--max-budget", type=int, default=None)
    a = ap.parse_args(argv)
    data_root, state = so_config.resolve(a.data_root, a.state)
    cfg = so_config.load_config(state)
    budget = a.max_budget if a.max_budget is not None else cfg.get("max_budget_tokens", 0)
    try:
        result = run(a.evidence, data_root, cfg, budget=budget)
    except gym.JudgeNotConfigured:
        print(gym.JUDGE_UNCONFIGURED.format(path=Path(state) / "config.json"), file=sys.stderr)
        return 2
    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, indent=1))
    out.chmod(0o600)
    print(f"localize: {len(result)} session(s) bisected")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
