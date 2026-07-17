"""claude.ai conversation-export adapter: turns the official data export's
conversations.json (Settings → Privacy → Export data) into the shared evidence
shape, stamped harness "claude-chat". Chat conversations are cloud-side and the
desktop/web apps keep no minable local transcript store, so the export file is
the supported path — user-triggered, no cache scraping.

Token counts don't exist in the export; sessions carry message counts only and
usage.parse.collector_limits says so. Correction mining reuses the shared regex
and redaction from scripts/."""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
import redact  # noqa: E402
from collect import DEFAULT_CORRECTION_RE  # noqa: E402

EVIDENCE_VERSION = "1"
HARNESS = "claude-chat"


def _stamp(obj: dict) -> dict:
    return {"schema_version": EVIDENCE_VERSION, "harness": HARNESS, **obj}


def parse_export(path: Path) -> list:
    try:
        data = json.loads(Path(path).read_text(errors="replace"))
    except (OSError, json.JSONDecodeError):
        return []
    return data if isinstance(data, list) else []


def collect_conversations(convs: list, since_iso: str | None = None):
    sessions, samples = [], []
    for conv in convs:
        if not isinstance(conv, dict):
            continue
        msgs = [m for m in (conv.get("chat_messages") or []) if isinstance(m, dict)]
        started = conv.get("created_at")
        if since_iso and (started or "") < since_iso:
            continue
        corrections = 0
        prior_assistant = ""
        ended_on_correction = 0
        for m in msgs:
            text = m.get("text") or ""
            if m.get("sender") == "assistant":
                prior_assistant = text[:800]
                ended_on_correction = 0
                continue
            if m.get("sender") != "human" or not text:
                continue
            if DEFAULT_CORRECTION_RE.search(text[:200]):
                corrections += 1
                ended_on_correction = 1
                ut, _ = redact.scrub(text[:1200])
                pa, _ = redact.scrub(prior_assistant)
                samples.append({"session": conv.get("uuid", ""),
                                "project": "claude-chat",
                                "ts": m.get("created_at"),
                                "kind": "correction",
                                "pattern": DEFAULT_CORRECTION_RE.search(text[:200]).group(0).strip(),
                                "user_text": ut, "prior_assistant_text": pa})
            else:
                ended_on_correction = 0
        sessions.append({
            "id": conv.get("uuid", ""), "project": "claude-chat",
            "cwd": None, "harness_version": None,
            "started_at": started, "ended_at": conv.get("updated_at"),
            "turns": len(msgs), "input_tokens": 0, "output_tokens": 0,
            "cache_read": 0, "cache_write": 0, "sidechain_output_tokens": 0,
            "models": {}, "corrections_count": corrections,
            "duplicate_reads": 0, "repeated_calls": 0, "permission_stalls": 0,
            "revert_events": 0, "reasks": 0,
            "ended_on_correction": ended_on_correction, "redactions": 0})
    return sessions, samples


def build_usage(sessions: list, since_iso: str | None) -> dict:
    n = len(sessions)
    corr_total = sum(s["corrections_count"] for s in sessions)
    return {"window": {"since": since_iso,
                       "until": max((s["ended_at"] or "" for s in sessions), default=None)},
            "totals": {"sessions": n, "turns": sum(s["turns"] for s in sessions),
                       "input_tokens": 0, "output_tokens": 0,
                       "cache_read": 0, "cache_write": 0},
            "per_project": {"claude-chat": {"sessions": n, "output_tokens": 0}},
            "per_model": {}, "corrections_by_model": {},
            "waste": {"duplicate_reads_total": 0, "repeated_calls_total": 0,
                      "permission_stalls_total": 0, "main_model_heavy_sessions": 0,
                      "revert_events_total": 0, "reasks_total": 0,
                      "ended_on_correction_total": sum(s["ended_on_correction"]
                                                       for s in sessions),
                      "top_duplicate_read_paths": [], "top_stalled_tools": [],
                      "stall_examples": []},
            "corrections": {"total": corr_total,
                            "rate_per_session": (corr_total / n) if n else 0.0},
            "parse": {"skipped_lines": 0, "files": n, "redactions": 0,
                      "collector_limits": [
                          "claude.ai export carries no token usage or tool calls: "
                          "token totals are zero and tool-derived waste metrics are empty"]}}


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--export", required=True,
                    help="path to conversations.json from the claude.ai data export")
    ap.add_argument("--out", required=True)
    ap.add_argument("--since", default=None)
    a = ap.parse_args(argv)
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    sessions, samples = collect_conversations(parse_export(Path(a.export)), a.since)
    usage = build_usage(sessions, a.since)
    (out / "sessions.json").write_text(json.dumps(_stamp({"sessions": sessions}), indent=1))
    (out / "samples.json").write_text(json.dumps(_stamp({"samples": samples}), indent=1))
    (out / "usage.json").write_text(json.dumps(_stamp(usage), indent=1))
    for f in out.glob("*.json"):
        f.chmod(0o600)
    print(f"sessions={len(sessions)} corrections={usage['corrections']['total']} "
          f"samples={len(samples)}")


if __name__ == "__main__":
    main()
