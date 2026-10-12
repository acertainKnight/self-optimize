"""Optional writer that sends what the loop learned to a cortex brain.

Off unless CORTEX_BRAIN_URL and CORTEX_API_TOKEN are both set. When they are, each
run sends two kinds of item to the brain's POST /v1/memory/learned:

1. A rule or warning for every finding that carries a `learned` statement and
   that the user applied or adopted. A finding that was rolled back or regressed
   is sent again as archived. Proposals nobody approved are never sent, so the
   brain only learns what passed the same human gate as a config change.
2. A skill pointer for every model-invocable skill in this run's inventory: the
   skill's name, the path of its SKILL.md and its description as the "use when"
   text. A skill that leaves the inventory is archived.

Only the derived statement and reference ids leave the machine: the finding id,
the session ids it cites, and dates. Transcript text, excerpts and skill bodies
are never sent. The brain dedups on its side, and a local record of what was
sent (`<state>/state/cortex_sent.json`) skips unchanged items, so a run repeats
no work. A failed call is reported and the run continues.
"""
import argparse
import hashlib
import json
import os
import urllib.error
import urllib.request
from pathlib import Path

URL_ENV = "CORTEX_BRAIN_URL"
TOKEN_ENV = "CORTEX_API_TOKEN"
KINDS = ("rule", "warning")
MAX_LEN = 300
ACTIVE = {"applied", "verified", "inconclusive", "adopted"}
ARCHIVED = {"rolled_back", "regressed"}
TIMEOUT_S = 10


def validate(learned) -> list:
    """Shape of a finding's optional `learned` object: the rule or warning the
    finding encodes, stated as one instruction, plus when it applies."""
    if not isinstance(learned, dict):
        return ["learned must be an object"]
    errs = []
    if learned.get("kind") not in KINDS:
        errs.append(f"learned.kind must be rule|warning, got {learned.get('kind')!r}")
    for key in ("statement", "trigger"):
        value = learned.get(key)
        if not isinstance(value, str) or not value.strip():
            errs.append(f"learned.{key} must be a non-empty string")
        elif len(value) > MAX_LEN or "\n" in value:
            errs.append(f"learned.{key} must be one line of at most {MAX_LEN} chars")
    return errs


def _ledger_rows(path: Path):
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict) and "id" in row:
            yield row


def finding_items(state: Path) -> list:
    """One payload per ledger finding that carries `learned` and was applied,
    adopted, rolled back or regressed. The rec comes from the newest ledger row
    that has one; the status from the newest row of all."""
    recs, status, since = {}, {}, {}
    for row in _ledger_rows(Path(state) / "state" / "ledger.jsonl"):
        rid = row["id"]
        if isinstance(row.get("rec"), dict):
            recs[rid] = row["rec"]
        status[rid] = row.get("status")
        since.setdefault(rid, str(row.get("ts", ""))[:10])
    items = []
    for rid, st in status.items():
        rec = recs.get(rid) or {}
        learned = rec.get("learned")
        if st not in ACTIVE | ARCHIVED or validate(learned):
            continue
        sessions = sorted({r for r in rec.get("evidence_refs", [])
                           if isinstance(r, str) and r.startswith("session:")})
        finding = {"type": "finding", "ref": f"self-optimize:{rid}"}
        if since[rid]:
            finding["date"] = since[rid]
        items.append({
            "kind": learned["kind"], "statement": learned["statement"].strip(),
            "trigger": learned["trigger"].strip(), "key": f"self-optimize:{rid}",
            "created_by": "self-optimize",
            "provenance": [finding] + [{"type": "session", "ref": s} for s in sessions],
            "evidence_count": max(1, len(sessions)), "archived": st in ARCHIVED,
        })
    return items


def skill_items(evidence: Path) -> list:
    """One skill pointer per model-invocable skill in this run's inventory."""
    p = Path(evidence) / "inventory.json"
    if not p.exists():
        return []
    items = []
    for s in json.loads(p.read_text()).get("skills", []):
        desc = " ".join(str(s.get("description") or "").split())
        if (not desc or str(s.get("disable_model_invocation")).lower() == "true"
                or s.get("override") == "off"):
            continue
        items.append({"kind": "skill_pointer", "skill_name": s["name"],
                      "skill_path": s["path"], "trigger": desc,
                      "key": f"self-optimize:skill:{s['name']}",
                      "created_by": "self-optimize"})
    return items


def _post(url: str, token: str, payload: dict) -> dict:
    req = urllib.request.Request(
        url.rstrip("/") + "/v1/memory/learned", data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
        method="POST")
    with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:
        return json.loads(resp.read() or b"{}")


def _digest(payload: dict) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def sync(items: list, state: Path, url: str, token: str, post=_post) -> dict:
    """Send every item whose payload changed since the last successful send. A
    skill pointer sent before and absent now is archived. Returns counts."""
    sent_path = Path(state) / "state" / "cortex_sent.json"
    try:
        sent = json.loads(sent_path.read_text())
    except (OSError, json.JSONDecodeError):
        sent = {}
    keys = {i["key"] for i in items}
    items = items + [{"kind": "skill_pointer", "key": key, "created_by": "self-optimize",
                      "archived": True}
                     for key in sorted(sent)
                     if key.startswith("self-optimize:skill:") and key not in keys]
    counts = {"sent": 0, "unchanged": 0, "failed": 0}
    errors = []
    for item in items:
        digest = _digest(item)
        if sent.get(item["key"]) == digest:
            counts["unchanged"] += 1
            continue
        try:
            post(url, token, item)
        except urllib.error.HTTPError as exc:
            if exc.code == 404 and item.get("archived"):
                sent[item["key"]] = digest  # never stored there: nothing to archive
                counts["unchanged"] += 1
                continue
            counts["failed"] += 1
            errors.append(f"{item['key']}: HTTP {exc.code}")
            continue
        except (OSError, ValueError) as exc:
            counts["failed"] += 1
            errors.append(f"{item['key']}: {exc}")
            continue
        counts["sent"] += 1
        sent[item["key"]] = digest
    sent_path.parent.mkdir(parents=True, exist_ok=True)
    sent_path.write_text(json.dumps(sent, indent=1, sort_keys=True) + "\n")
    sent_path.chmod(0o600)
    counts["errors"] = errors
    return counts


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--evidence", required=True)
    ap.add_argument("--state", required=True)
    a = ap.parse_args(argv)
    url, token = os.environ.get(URL_ENV, ""), os.environ.get(TOKEN_ENV, "")
    if not (url and token):
        print(f"cortex: off ({URL_ENV} and {TOKEN_ENV} not both set)")
        return 0
    items = finding_items(Path(a.state)) + skill_items(Path(a.evidence))
    c = sync(items, Path(a.state), url, token)
    print(f"cortex: sent={c['sent']} unchanged={c['unchanged']} failed={c['failed']}")
    for err in c["errors"][:5]:
        print(f"cortex: {err}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
