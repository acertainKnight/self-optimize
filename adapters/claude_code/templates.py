"""Applicable-action renderers (every tier-A type, plus diff) + post-apply smoke
checks. Actions are rendered from typed payloads only — never from free analyst
text. Second line of defense after synth.py's guards, because this module is what
actually touches files. manual is never rendered — it has no branch here at all."""
import json
import os
import re
from pathlib import Path

import schema as so_schema

ALLOWED_SETTING_ROOTS = {"skillOverrides", "enabledPlugins", "model", "outputStyle", "effortLevel"}
KNOWN_MODELS = {"haiku", "sonnet", "opus", "fable", "inherit"}


def _model_ok(v: str) -> bool:
    return v in KNOWN_MODELS or v.startswith("claude-")


def _require_user_md(f: Path, data_root: Path, extra_roots=None) -> bool:
    """Confine f to a sanctioned root; returns True when the match is an extra root.
    Lexical normalization (collapses ..) + separator boundary; the base itself may be
    a symlink (legitimately symlinked skills dirs keep working), but components BELOW
    it must not be — a live symlink would redirect the write outside confinement."""
    target = os.path.normpath(str(f))
    bases = [(os.path.normpath(str(data_root / sub)), False)
             for sub in ("skills", "agents", "workflows")]
    bases += [(os.path.normpath(str(Path(root))), True) for root in (extra_roots or [])]
    matched = next(((b, extra) for b, extra in bases
                    if target == b or target.startswith(b + os.sep)), None)
    if matched is None:
        raise ValueError(f"path outside sanctioned roots under {data_root}: {f}")
    if not target.endswith(".md"):
        raise ValueError(f"not a .md file: {f}")
    base, is_extra = matched
    cur = Path(base)
    for part in Path(target).relative_to(base).parts:
        cur = cur / part
        if cur.is_symlink():
            raise ValueError(f"refusing machine write through symlink: {cur}")
    return is_extra


def _in_extra_root(f: Path, extra_roots) -> bool:
    target = os.path.normpath(str(f))
    for root in (extra_roots or []):
        base = os.path.normpath(str(Path(root)))
        if target == base or target.startswith(base + os.sep):
            return True
    return False


_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def _apply_unified_diff(original: str, diff: str) -> str:
    """Stdlib strict unified-diff applier: every context/removed hunk line must
    match the original exactly at the stated position, or refuse — no fuzzy
    matching, so a stale/malformed diff fails safe to a human. Supports an
    empty original (new file, hunks like '@@ -0,0 +1,N @@') and multiple hunks.
    ponytail: LF-only, no '\\ No newline at end of file' handling — sufficient
    for the .md content this feeds; add if a CRLF/no-trailing-newline source
    ever needs it."""
    def _lines(text):
        parts = text.split("\n")
        if parts and parts[-1] == "":
            parts.pop()
        return parts

    src = _lines(original)
    hunk_lines = _lines(diff)
    out, pos, i, n, applied = [], 0, 0, len(hunk_lines), False
    while i < n:
        line = hunk_lines[i]
        if not line.startswith("@@"):
            # only a real file-header preamble (---/+++), and only before the
            # first hunk, is tolerable filler — everything else that isn't a
            # hunk header must refuse. (A line that itself starts with "@@" but
            # fails the strict regex below — e.g. a header missing its trailing
            # space — is NOT skipped here; it falls through to the explicit
            # "malformed hunk header" raise instead of being silently dropped
            # along with its now-orphaned body lines.)
            if applied or not (line.startswith("---") or line.startswith("+++")):
                raise ValueError("diff did not apply cleanly")
            i += 1
            continue
        m = _HUNK_RE.match(line)
        if not m:
            raise ValueError("diff did not apply cleanly")  # malformed hunk header
        old_start, old_count = int(m.group(1)), m.group(2)
        old_count = int(old_count) if old_count is not None else 1
        # a 0-count old-side (pure insertion, e.g. "@@ -5,0 +6,1 @@") means
        # "insert after old line 5" -> 0-indexed boundary 5, NOT 4: unlike a
        # normal hunk, the header line number here is not itself an old-file
        # line being touched, so it must not get the usual -1. Verified against
        # GNU patch's actual behavior for this hunk shape.
        start_idx = old_start if old_count == 0 else max(old_start - 1, 0)
        if start_idx < pos or start_idx > len(src):
            raise ValueError("diff did not apply cleanly")
        out.extend(src[pos:start_idx])
        pos = start_idx
        applied = True
        i += 1
        while i < n and not hunk_lines[i].startswith("@@"):
            line = hunk_lines[i]
            if line.startswith(" ") or line.startswith("-"):
                if pos >= len(src) or src[pos] != line[1:]:
                    raise ValueError("diff did not apply cleanly")
                if line.startswith(" "):
                    out.append(src[pos])
                pos += 1
            elif line.startswith("+"):
                out.append(line[1:])
            else:
                raise ValueError("diff did not apply cleanly")
            i += 1
    if not applied:
        raise ValueError("diff did not apply cleanly")
    out.extend(src[pos:])
    return "\n".join(out) + ("\n" if out else "")


def render(action: dict, data_root: Path, extra_roots=None) -> list:
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
        _require_user_md(f, data_root, extra_roots)
        if f.exists():
            raise ValueError(f"refusing to overwrite existing file: {f}")
        return [(f, p["content"])]
    if t == "file_replace":
        f = Path(p["path"]).expanduser()
        if _require_user_md(f, data_root, extra_roots):
            # kills the rewrite-MEMORY.md injection amplifier: memory is create-only
            raise ValueError("file_replace not permitted in extra roots (memory) — create new files only")
        if not f.exists():
            raise ValueError(f"cannot replace nonexistent file: {f}")
        return [(f, p["content"])]
    if t == "diff":
        # BOUND: the only place a machine write may leave the sanctioned roots.
        # basename=="CLAUDE.md" -> follow to its realpath, but refuse unless the
        # resolved basename is STILL CLAUDE.md (blocks a symlinked CLAUDE.md
        # redirecting to e.g. ~/.zshrc; allows a legit ~/.claude-work/CLAUDE.md
        # -> ~/.claude/CLAUDE.md merge symlink). Anything else must pass the
        # ordinary sanctioned-root check — no other escape hatch exists.
        f = Path(p["file"]).expanduser()
        # memory (extra roots) is create-only, same rule file_replace enforces
        # unconditionally — checked BEFORE the CLAUDE.md carve-out below so that
        # naming an existing memory note "CLAUDE.md" can't bypass it: a diff
        # against an EXISTING extra-root file is a rewrite of existing memory,
        # the exact injection amplifier that rule defends against. A diff
        # against a not-yet-existing extra-root path is fine (== file_create).
        if _in_extra_root(f, extra_roots) and f.exists():
            raise ValueError("diff not permitted on existing extra-root "
                              "(memory) files — create-only")
        if os.path.basename(str(f)) == "CLAUDE.md":
            real = os.path.realpath(str(f))
            if os.path.basename(real) != "CLAUDE.md":
                raise ValueError(f"CLAUDE.md symlink resolves outside CLAUDE.md: {real}")
            target = Path(real)
        else:
            _require_user_md(f, data_root, extra_roots)
            target = f
        base = target.read_text() if target.exists() else ""
        new = _apply_unified_diff(base, p["diff"])
        return [(target, new)]
    raise ValueError(f"not a renderable action type: {t}")


def smoke_check(paths: list, data_root: Path) -> list:
    # frontmatter is only required under skills/agents (frontmatter-bearing artifact
    # dirs) — a CLAUDE.md or any other .md (workflows, arbitrary sanctioned paths)
    # must not be failed for missing frontmatter, it was never expected to have any.
    fm_roots = [os.path.normpath(str(Path(data_root) / sub)) for sub in ("skills", "agents")]
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
            target = os.path.normpath(str(p))
            if not any(target == b or target.startswith(b + os.sep) for b in fm_roots):
                continue
            fm = so_schema.parse_frontmatter(text)
            if not fm:
                errs.append(f"{p}: missing or unparseable frontmatter")
            elif "model" in fm and not _model_ok(fm["model"]):
                errs.append(f"{p}: unknown model '{fm['model']}'")
    return errs
