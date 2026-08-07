#!/usr/bin/env python3
"""Stop hook: cheap, pattern/state-based detection of skill-worthy moments,
fired at the moment each turn completes instead of re-discovered later by
batch mining. Appends pointer-only lines to <state>/capture-queue.jsonl: a
timestamp, harness, session id, and a trigger label — never transcript
content or secrets, no LLM calls. See hooks/README.md for why Stop was
chosen over SessionEnd, and how to opt in (this script ships inert; nothing
in the plugin registers it automatically)."""
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import schema as so_schema                                # noqa: E402
import so_config                                           # noqa: E402
from collect import DEFAULT_CORRECTION_RE, REJECTION_RE    # noqa: E402

MAX_PER_SESSION = 5      # writes at most a few lines per session
TASK_TOOL_THRESHOLD = 5  # "task completed after 5+ tool calls"
CURSOR_SUBDIR = "capture-cursor"


def _text(msg: dict) -> str:
    c = msg.get("content") if isinstance(msg, dict) else None
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        return "\n".join(b.get("text", "") for b in c
                         if isinstance(b, dict) and b.get("type") == "text")
    return ""


def _looks_like_tool_error(tool_use_result) -> bool:
    """A real tool failure, not a permission decline — that's a separate
    signal collect.py already tracks as permission_stalls."""
    if isinstance(tool_use_result, dict):
        err = tool_use_result.get("error")
        return bool(err) and not REJECTION_RE.search(str(err))
    if isinstance(tool_use_result, str):
        return (bool(re.search(r"(?i)\berror\b", tool_use_result))
                and not REJECTION_RE.search(tool_use_result))
    return False


def detect_triggers(records: list) -> list:
    """Cheap single pass over one turn's new transcript records (everything
    appended since the previous Stop). Returns [(trigger, artifact_hint), ...]
    — structural pointers only (a tool-call count, or the fixed keyword the
    correction regex matched), never free-form user or assistant text."""
    triggers = []
    tool_calls = 0
    error_seen = False
    recovered = False
    for rec in records:
        if rec.get("isSidechain") or rec.get("isMeta"):
            continue
        rtype = rec.get("type")
        msg = rec.get("message") or {}
        if rtype == "assistant":
            content = msg.get("content")
            for blk in (content if isinstance(content, list) else []):
                if isinstance(blk, dict) and blk.get("type") == "tool_use":
                    tool_calls += 1
                    if error_seen:
                        recovered = True
        elif rtype == "user":
            tr = rec.get("toolUseResult")
            if tr is not None:
                if _looks_like_tool_error(tr):
                    error_seen = True
                    recovered = False
                continue
            text = _text(msg)
            if not text:
                continue
            m = DEFAULT_CORRECTION_RE.search(text[:200])
            if m:
                triggers.append(("correction", m.group(0).strip().lower()[:20]))
    if tool_calls >= TASK_TOOL_THRESHOLD:
        triggers.append(("task_completed", f"{tool_calls}_tool_calls"))
    if error_seen and recovered:
        triggers.append(("dead_end_then_working", None))
    return triggers


def _cursor_path(state: Path, session_id: str) -> Path:
    return state / "state" / CURSOR_SUBDIR / f"{session_id}.json"


def _load_cursor(path: Path) -> dict:
    try:
        d = json.loads(path.read_text())
        return {"offset": int(d.get("offset", 0)), "emitted": int(d.get("emitted", 0))}
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return {"offset": 0, "emitted": 0}


def _save_cursor(path: Path, cursor: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cursor))
    path.chmod(0o600)


def _read_delta(transcript_path: Path, offset: int) -> tuple[list, int]:
    """Reads only the bytes appended since `offset` — keeps every Stop
    invocation O(this turn), not O(the whole session transcript so far),
    which is what keeps a hook that fires every turn cheap."""
    try:
        with open(transcript_path, "rb") as f:
            f.seek(offset)
            chunk = f.read()
            new_offset = f.tell()
    except OSError:
        return [], offset
    records = []
    for line in chunk.decode("utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(rec, dict):
            records.append(rec)
    return records, new_offset


def _append_queue(queue_path: Path, entries: list) -> None:
    queue_path.parent.mkdir(parents=True, exist_ok=True)
    with open(queue_path, "a") as f:
        for e in entries:
            f.write(json.dumps(e) + "\n")
    queue_path.chmod(0o600)


def main() -> None:
    payload = json.loads(sys.stdin.read() or "{}")
    session_id = payload.get("session_id")
    transcript_path = payload.get("transcript_path")
    if not session_id or not transcript_path:
        return
    _, state = so_config.resolve()
    cpath = _cursor_path(state, session_id)
    cursor = _load_cursor(cpath)
    if cursor["emitted"] >= MAX_PER_SESSION:
        return  # capped for this session: no transcript I/O at all
    transcript_path = Path(transcript_path)
    if not transcript_path.exists():
        return
    records, new_offset = _read_delta(transcript_path, cursor["offset"])
    cursor["offset"] = new_offset
    triggers = detect_triggers(records)[:MAX_PER_SESSION - cursor["emitted"]]
    if triggers:
        ts = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        entries = []
        for trigger, hint in triggers:
            entry = {"ts": ts, "harness": so_schema.HARNESS,
                     "session_id": session_id, "trigger": trigger}
            if hint:
                entry["artifact_hint"] = hint
            entries.append(entry)
        _append_queue(state / "capture-queue.jsonl", entries)
        cursor["emitted"] += len(triggers)
    _save_cursor(cpath, cursor)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass  # capture-only, best-effort: never let this hook affect the session
    sys.exit(0)
