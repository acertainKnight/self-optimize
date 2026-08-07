"""Papercut channel: a standing instruction (see README.md) tells agents to append
one line to a plain-text file whenever they hit and work around friction, instead of
stopping to fix it. collect.py reads this file once per run at the merged level —
harness-neutral, since the file's location isn't tied to any one harness's config
dir. When a finding built from a papercut cluster is applied AND verified, verify.py
calls archive_lines() to move exactly the cited lines under an archive heading,
leaving every other line untouched. Absent file = silently empty, never an error."""
import hashlib
import re
from pathlib import Path

import redact

ARCHIVE_HEADING = "## Archive"
LINE_RE = re.compile(r"^-\s+(\d{4}-\d{2}-\d{2})\s+(\S+)\s+(.+)$")


def _line_id(stripped_line: str) -> str:
    # hashed from the RAW line (pre-redaction) so the id stays stable and always
    # matches what archive_lines() finds on disk, regardless of what read_papercuts()
    # scrubs out of the displayed text
    return hashlib.sha256(stripped_line.encode()).hexdigest()[:12]


def read_papercuts(path) -> list:
    """Live (pre-archive) papercut lines as citable evidence rows: [{id, date,
    harness, text}]. Only lines above the '## Archive' heading are read; a line
    that doesn't match the '- YYYY-MM-DD <harness> <text>' convention is skipped,
    not fatal. A missing file returns [] — this channel is opt-in and most
    installs will never have written one."""
    path = Path(path)
    if not path.exists():
        return []
    out = []
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        stripped = raw.strip()
        if stripped == ARCHIVE_HEADING:
            break
        m = LINE_RE.match(stripped)
        if not m:
            continue
        date, harness, text = m.groups()
        out.append({"id": _line_id(stripped), "date": date, "harness": harness,
                    "text": redact.scrub(text)[0]})
    return out


def archive_lines(path, line_ids) -> int:
    """Moves every live line whose content-hash id is in line_ids under the
    '## Archive' heading (created if absent), appended after anything already
    archived there. Every other line — live or already archived — is left
    byte-for-byte untouched. No-op (returns 0, no write) on a missing file, an
    empty id set, or no matching lines; safe to call again on already-archived
    ids since a moved line no longer appears in the live section to re-match."""
    path = Path(path)
    line_ids = set(line_ids)
    if not path.exists() or not line_ids:
        return 0
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    arch_idx = next((i for i, ln in enumerate(lines) if ln.strip() == ARCHIVE_HEADING), None)
    live = lines if arch_idx is None else lines[:arch_idx]
    tail = [] if arch_idx is None else lines[arch_idx:]
    kept, moved = [], []
    for raw in live:
        stripped = raw.strip()
        if LINE_RE.match(stripped) and _line_id(stripped) in line_ids:
            moved.append(stripped)
        else:
            kept.append(raw)
    if not moved:
        return 0
    if arch_idx is None:
        tail = ["", ARCHIVE_HEADING, ""]
    path.write_text("\n".join(kept + tail + moved) + "\n", encoding="utf-8")
    return len(moved)
