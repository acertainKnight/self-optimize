"""Durable corrections compiled into runtime enforcement proposals.

A correction the user has had to give more than once is a preference the model
keeps failing to read. Writing it down again produces more prose to ignore; the
alternative is a check the harness runs, so the call fails instead of the
preference being re-violated and re-reported every run.

Two properties make that safe enough to propose automatically:

1. The analyst never writes shell. It names a RULE from the table below and
   supplies that rule's parameters. The event, the matcher, and the command are
   rendered here; the check's logic lives in `hooks/enforce.py`, which is repo
   code a human reviewed. Parameters reach it as quoted argv strings.
2. The proposal is permanently tier B (`schema.validate_rec` pins it, and
   `apply.py` refuses it outright). A loop that can install its own checks can
   install a check that silences its own evidence, so a human installs every one
   of these by hand and records it with `self-optimize adopt`.

Every proposal also carries a prediction — which correction category should fall
after the check is installed — and `verify.py` scores it against the labeled
correction counts on later runs.
"""
import json
import shlex
from pathlib import Path

# rule -> what it needs, and where it hooks. The event and the matcher are
# DERIVED from the rule here, never supplied by the analyst: a proposal chooses
# a rule, not a place to run code.
RULES = {
    "forbid_bash_substring": {
        "params": ("value",),
        "event": "PreToolUse", "matcher": "Bash",
        "summary": "block any Bash command containing {value}",
    },
    "require_bash_flag": {
        "params": ("program", "flag"),
        "event": "PreToolUse", "matcher": "Bash",
        "summary": "require {flag} on every {program} command",
    },
    "forbid_write_path": {
        "params": ("prefix",),
        "event": "PreToolUse", "matcher": "Write|Edit|NotebookEdit",
        "summary": "block file writes under {prefix}",
    },
}
KINDS = ("hook", "check")
MAX_PARAM_LEN = 200
# A correction is durable when it recurred across sessions. synth.py checks the
# finding's own citations against this before letting a proposal through.
MIN_SESSIONS = 2
# Predictions are scored on a category's SHARE of labeled corrections, so a run
# that labeled almost nothing must not produce a verdict. ponytail: flat floor,
# not a power calculation — raise it if verdicts read as noisy.
MIN_LABELED = 10
CHECKER = "${CLAUDE_PLUGIN_ROOT}/hooks/enforce.py"


def correction_categories() -> set:
    # local import: labels imports synth, which imports schema, which imports
    # this module — importing labels at module level would close that loop.
    import labels
    return set(labels.CATEGORIES)


def _param_errors(key: str, value) -> list:
    if not isinstance(value, str) or not value.strip():
        return [f"enforcement.params.{key} must be a non-empty string"]
    if len(value) > MAX_PARAM_LEN:
        return [f"enforcement.params.{key} is longer than {MAX_PARAM_LEN} chars"]
    if any(ord(c) < 0x20 or ord(c) == 0x7F for c in value):
        return [f"enforcement.params.{key} must be a single line of printable text"]
    return []


def _prediction_errors(pred) -> list:
    """The proposal states its own success metric up front: which labeled
    correction category should fall. Magnitude is deliberately NOT the analyst's
    to set — the threshold comes from config, so a proposal cannot grade itself
    on a bar it chose."""
    if not isinstance(pred, dict):
        return ["enforcement.prediction must be an object"]
    errs = []
    if pred.get("category") not in correction_categories():
        errs.append(f"enforcement.prediction.category must be a correction category, "
                    f"got {pred.get('category')!r}")
    if pred.get("direction") != "down":
        errs.append("enforcement.prediction.direction must be 'down' — an enforcement "
                    "check predicts fewer corrections, never more")
    return errs


def validate(enf) -> list:
    if not isinstance(enf, dict):
        return ["enforcement must be an object"]
    errs = []
    if enf.get("kind") not in KINDS:
        errs.append(f"enforcement.kind must be hook|check, got {enf.get('kind')!r}")
    rule = enf.get("rule")
    spec = RULES.get(rule) if isinstance(rule, str) else None
    if spec is None:
        errs.append(f"enforcement.rule must be one of {sorted(RULES)}, got {rule!r}")
    params = enf.get("params")
    if not isinstance(params, dict):
        errs.append("enforcement.params must be an object")
    elif spec is not None:
        if set(params) != set(spec["params"]):
            errs.append(f"enforcement.params for {rule} must be exactly "
                        f"{list(spec['params'])}, got {sorted(params)}")
        for key in sorted(params):
            errs += _param_errors(key, params[key])
    errs += _prediction_errors(enf.get("prediction"))
    return errs


def render(enf: dict, checker: str = CHECKER) -> dict:
    """Template-render the proposed check. Only the analyst-supplied parameters
    are interpolated, and each one is shell-quoted; the program, the flag names,
    the event and the matcher all come from the RULES table. The checker path is
    deliberately NOT quoted — `${CLAUDE_PLUGIN_ROOT}` is a placeholder the
    harness substitutes, and quoting it would ship a literal dollar sign."""
    spec = RULES[enf["rule"]]
    params = enf["params"]
    args = [f"--rule {shlex.quote(enf['rule'])}"]
    args += [f"--arg {shlex.quote(f'{k}={params[k]}')}" for k in spec["params"]]
    command = f"python3 {checker} " + " ".join(args)
    summary = spec["summary"].format(**{k: repr(params[k]) for k in spec["params"]})
    out = {"rule": enf["rule"], "kind": enf["kind"], "event": spec["event"],
           "matcher": spec["matcher"], "command": command, "summary": summary}
    if enf["kind"] == "hook":
        out["settings_block"] = {"hooks": {spec["event"]: [
            {"matcher": spec["matcher"],
             "hooks": [{"type": "command", "command": command, "timeout": 5}]}]}}
    return out


def read_metrics_rows(state_dir) -> list:
    p = Path(state_dir) / "state" / "metrics.jsonl"
    if not p.exists():
        return []
    rows = []
    for line in p.read_text().splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def label_counts(evidence, state) -> tuple:
    """(counts, run_id) for the most recent labeled correction sample — this
    run's validated labels.json when the labeler ran, else the newest metrics
    row that carries `corrections_by_category`. (None, None) when nothing in
    this instance's history has ever been labeled."""
    lf = Path(evidence) / "labels.json"
    if lf.exists():
        try:
            data = json.loads(lf.read_text())
        except (OSError, json.JSONDecodeError):
            data = {}
        if isinstance(data, dict) and isinstance(data.get("counts"), dict):
            return data["counts"], Path(evidence).name
    for row in reversed(read_metrics_rows(state)):
        counts = row.get("corrections_by_category")
        if isinstance(counts, dict):
            return counts, row.get("run_id")
    return None, None


def observed_counts(rows: list, baseline_run_id) -> tuple:
    """(counts, run_id) from the newest labeled metrics row that comes STRICTLY
    AFTER the run the baseline was taken in — by position in the file, not by
    comparing run-id strings. verify runs before this run's labeler, so the
    newest labeled row is normally the previous run's; on the run right after an
    adoption that row IS the baseline row, and measuring the check against
    sessions that predate it would be a free fake verdict."""
    base_idx = next((i for i, r in enumerate(rows)
                     if r.get("run_id") == baseline_run_id), None)
    if base_idx is None:
        return None, None
    for row in reversed(rows[base_idx + 1:]):
        counts = row.get("corrections_by_category")
        if isinstance(counts, dict):
            return counts, row.get("run_id")
    return None, None


def _share(counts: dict, category: str):
    total = sum(v for v in counts.values() if isinstance(v, (int, float)))
    if total <= 0:
        return None, 0, 0
    return counts.get(category, 0) / total, counts.get(category, 0), total


def score(baseline: dict, observed: dict, category: str, min_rel_change: float) -> dict:
    """Predicted-vs-observed for one adopted proposal. The comparison is the
    category's SHARE of labeled corrections, not its raw count: sample caps make
    the count a function of how chatty the window was, so a quiet month would
    otherwise read as every prediction coming true at once."""
    b_share, b_count, b_total = _share(baseline or {}, category)
    o_share, o_count, o_total = _share(observed or {}, category)
    row = {"baseline": {"count": b_count, "total": b_total, "share": b_share},
           "observed": {"count": o_count, "total": o_total, "share": o_share},
           "rel_change": None, "verdict": "inconclusive", "note": ""}
    if o_share is None:
        row["note"] = "no labeled correction run since adoption yet"
        return row
    if o_total < MIN_LABELED:
        row["note"] = f"only {o_total} labeled corrections since adoption (floor {MIN_LABELED})"
        return row
    if not b_share:
        row["note"] = "category was not seen at adoption — nothing to fall"
        return row
    rel = (o_share - b_share) / b_share
    row["rel_change"] = rel
    if rel <= -min_rel_change:
        row["verdict"] = "verified"
    elif rel >= min_rel_change:
        row["verdict"] = "regressed"
    else:
        row["note"] = "moved less than the configured threshold"
    return row
