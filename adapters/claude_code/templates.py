"""Tier-A action renderers + post-apply smoke checks. Actions are rendered from typed
payloads only — never from free analyst text. Second line of defense after synth.py's
guards, because this module is what actually touches files."""
import json
import os
import re
from pathlib import Path

import schema as so_schema

ALLOWED_SETTING_ROOTS = {"skillOverrides", "enabledPlugins", "model", "outputStyle", "effortLevel"}
KNOWN_MODELS = {"haiku", "sonnet", "opus", "fable", "inherit"}


def _model_ok(v: str) -> bool:
    return v in KNOWN_MODELS or v.startswith("claude-")


def _require_user_md(f: Path, data_root: Path):
    # lexical normalization (collapses ..) + separator boundary; deliberately NOT
    # resolve() — that follows symlinks and would break legitimately symlinked skills
    target = os.path.normpath(str(f))
    ok = False
    for sub in ("skills", "agents"):
        base = os.path.normpath(str(data_root / sub))
        if target == base or target.startswith(base + os.sep):
            ok = True
    if not ok:
        raise ValueError(f"path outside {data_root}/skills|agents: {f}")
    if not target.endswith(".md"):
        raise ValueError(f"not a .md file: {f}")


def render(action: dict, data_root: Path) -> list:
    t = action["type"]
    p = action.get("payload", {})
    data_root = Path(data_root)
    if t == "setting_change":
        kp = p["key_path"]
        if not kp or kp[0] not in ALLOWED_SETTING_ROOTS:
            raise ValueError(f"settings root not allowed: {kp[:1]}")
        f = data_root / "settings.json"
        obj = json.loads(f.read_text()) if f.exists() else {}
        node = obj
        for k in kp[:-1]:
            nxt = node.get(k)
            if nxt is None:
                nxt = node[k] = {}
            elif not isinstance(nxt, dict):
                raise ValueError(f"settings path collides with non-object at '{k}'")
            node = nxt
        node[kp[-1]] = p["value"]
        return [(f, json.dumps(obj, indent=2) + "\n")]
    if t == "frontmatter_edit":
        f = Path(p["file"]).expanduser()
        _require_user_md(f, data_root)
        if not f.exists():
            raise ValueError(f"no such file: {f}")
        text = f.read_text()
        end = text.find("\n---", 3)
        if not text.startswith("---") or end == -1:
            raise ValueError(f"no frontmatter in {f}")
        head, rest = text[:end], text[end:]
        if any(c in str(p["key"]) or c in str(p["value"]) for c in ("\n", "\r")):
            raise ValueError("frontmatter key/value must be single-line")
        line = f"{p['key']}: {p['value']}"
        pat = re.compile(rf"^{re.escape(p['key'])}:.*$", re.M)
        head = pat.sub(line, head) if pat.search(head) else head + "\n" + line
        return [(f, head + rest)]
    if t == "file_create":
        f = Path(p["path"]).expanduser()
        _require_user_md(f, data_root)
        if f.exists():
            raise ValueError(f"refusing to overwrite existing file: {f}")
        return [(f, p["content"])]
    raise ValueError(f"not a tier-A action: {t}")


def smoke_check(paths: list, data_root: Path) -> list:
    errs = []
    for p in paths:
        p = Path(p)
        if p.name == "settings.json":
            try:
                json.loads(p.read_text())
            except Exception as e:  # noqa: BLE001 - any parse failure must restore
                errs.append(f"{p}: invalid JSON ({e})")
        elif p.suffix == ".md":
            try:
                text = p.read_text()
            except OSError as e:
                errs.append(f"{p}: unreadable ({e})")
                continue
            fm = so_schema.parse_frontmatter(text)
            if not fm:
                errs.append(f"{p}: missing or unparseable frontmatter")
            elif "model" in fm and not _model_ok(fm["model"]):
                errs.append(f"{p}: unknown model '{fm['model']}'")
    return errs
