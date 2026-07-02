"""Config + state-dir handling for self-optimize. Stdlib only.
The running Claude instance's config dir is the root of everything:
$CLAUDE_CONFIG_DIR if set, else ~/.claude — this module is the ONLY place
that fallback literal appears."""
import json
import os
from pathlib import Path

DEFAULTS = {
    "config_version": 1,
    "report_dir": "",  # empty -> <state>/reports
    "project_include": ["*"],
    "project_exclude": [],
    "project_weights": "auto",
    "since_days": 30,
    "sample_caps": {"excerpts": 40, "tokens_per_excerpt": 1500, "total_tokens": 60000},
    "correction_regex": "",  # empty -> collector default
    "max_budget_tokens": 400000,
    "retain_runs": 10,
    "verify": {"min_sessions": 10, "min_rel_change": 0.10},
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
