"""Config + state-dir handling for self-optimize. Stdlib only.
The running Claude instance's config dir is the root of everything:
$CLAUDE_CONFIG_DIR if set, else ~/.claude — this module is the ONLY place
that fallback literal appears."""
import json
import os
from pathlib import Path

# Default data roots for the non-Claude-Code harnesses. This module is the only
# place these literals live; collect.py maps them onto each adapter's CLI flags.
HARNESS_DEFAULTS = {
    "codex": {"home": "~/.codex"},
    "opencode": {"home": "~/.local/share/opencode", "config": "~/.config/opencode"},
}

DEFAULTS = {
    "config_version": 1,
    "report_dir": "",  # empty -> <state>/reports
    "project_include": ["*"],
    "project_exclude": [],
    "since_days": 30,
    "sample_caps": {"excerpts": 40, "tokens_per_excerpt": 1500, "total_tokens": 60000},
    "correction_regex": "",  # empty -> collector default
    "completion_claim_regex": "",  # empty -> collector default (silent-failure prefilter)
    "silent_failure_regex": "",  # empty -> collector default (silent-failure prefilter)
    "papercuts_path": "",  # empty -> <home>/papercuts.md — see README's Papercut channel
    "harnesses": {name: {"enabled": True, **roots}
                  for name, roots in HARNESS_DEFAULTS.items()},
    "max_budget_tokens": 400000,
    "retain_runs": 10,
    "verify": {"min_sessions": 10, "min_rel_change": 0.10},
    # judge.command is deliberately empty: the gym has NO default backend or provider.
    # You name the CLI that judges cases; missing config is a refusal, not a fallback.
    # max_variants_per_artifact caps the variant archive; over the cap, variants beaten
    # on BOTH prevented and preserved are evicted before anything else. `front` is the
    # budget on seeding an analyst with an artifact's Pareto front: only artifacts
    # carrying at least min_failure_cases recorded failures get one, and it names at
    # most max_members versions.
    "gym": {"min_cases_per_side": 3, "max_cases_per_side": 20,
            "retire_after_absent_runs": 3, "max_variants_per_artifact": 20,
            "front": {"min_failure_cases": 5, "max_members": 4},
            "judge": {"command": [], "model": "", "timeout_s": 120}},
    # opt-in, off by default: bisects the top_n highest-friction sessions with
    # the gym's judge backend to bracket roughly where each went off track.
    "deep_localize": {"enabled": False, "top_n": 3},
    # curator's stage 2 (merge-text drafting) reuses gym.judge above -- no backend
    # of its own to configure.
    "curator": {"dup_body_floor": 0.6, "long_unfired_days": 60},
}


def config_dir() -> Path:
    return Path(os.environ.get("CLAUDE_CONFIG_DIR") or (Path.home() / ".claude")).expanduser()


def resolve(data_root=None, state=None) -> tuple:
    d = Path(data_root).expanduser() if data_root else config_dir()
    st = Path(state).expanduser() if state else d / "self-optimize"
    return d, st


def load_config(state_dir: Path) -> dict:
    state_dir = Path(state_dir)
    (state_dir / "state").mkdir(parents=True, exist_ok=True)
    path = state_dir / "config.json"
    if not path.exists():
        path.write_text(json.dumps(DEFAULTS, indent=2) + "\n")
    cfg = json.loads(json.dumps(DEFAULTS))  # deep copy
    for k, v in json.loads(path.read_text()).items():
        if isinstance(v, dict) and isinstance(cfg.get(k), dict):
            cfg[k].update(v)
        else:
            cfg[k] = v
    if not cfg["report_dir"]:
        cfg["report_dir"] = str(state_dir / "reports")
    return cfg


def papercuts_path(cfg: dict) -> Path:
    """The papercut file this instance reads: cfg["papercuts_path"] if set, else
    the home directory — the one location every harness can reach, since each
    harness's own config dir (~/.claude, ~/.codex, ...) is otherwise private to
    it. See README.md's Papercut channel section for the instruction snippet."""
    p = cfg.get("papercuts_path") or ""
    return Path(p).expanduser() if p else Path.home() / "papercuts.md"


def harness_roots(cfg: dict) -> dict:
    """Resolved data roots per non-Claude-Code harness: {name: {key: Path}}.
    A harness block in config.json replaces the default block wholesale (the
    merge in load_config is one level deep), so a missing key falls back to the
    default here and a missing "enabled" means enabled — you turn a harness off
    by writing "enabled": false, never by omitting it."""
    out = {}
    for name, roots in HARNESS_DEFAULTS.items():
        over = (cfg.get("harnesses") or {}).get(name) or {}
        if not over.get("enabled", True):
            continue
        out[name] = {k: Path(str(over.get(k) or default)).expanduser()
                     for k, default in roots.items()}
    return out
