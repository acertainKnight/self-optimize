"""Variant archive: every candidate version of an artifact the gym scored, kept with
its parent, its edit ops, its two-sided score and where it came from.

Single-lineage editing keeps one live version and discards everything else, so a
candidate that was better on one side than anything proposed since is simply gone, and
the loop can only climb from wherever it happens to stand. The archive keeps them all:
`<state>/gym/archive/<artifact>/<variant>.json`, one file per scored candidate, mode
0600 under 0700 dirs like the rest of the gym state. A variant holds artifact text
rather than transcript excerpts, but it lives beside the corpus and inherits its posture.

Lineage is a DAG, not a list. Each variant records `parent` — the variant its edits were
written against, or null for edits written against the live artifact — and two op lists:
`ops`, the edits relative to that parent, and `chain_ops`, the flattened set that applies
to the live artifact. Keeping the flattened set is what lets a proposal branch from an
archived variant that was never applied: the branch is its parent's chain plus its own
delta, so the whole thing still applies to the file on disk as one ordered bounded edit,
and still fails closed if the file moved on since.

Retention: `gym.max_variants_per_artifact` caps the archive per artifact. Dominated
variants are evicted first — one that some other variant beats on BOTH prevented and
preserved has nothing left to contribute to a front. After that it is oldest-first. The
variant matching what the artifact file holds right now is never evicted, and neither is
the newest: the archive's job is to remember, and losing the live version or the run's
own candidate would defeat it.

Nothing here applies, promotes or rejects anything. It records what was scored.
"""
import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path

ARCHIVE_VERSION = "1"
DEFAULT_CAP = 20


# ---------------------------------------------------------------- state layout
def _slug(artifact_id: str) -> str:
    """Same readable-but-safe naming the gym corpus uses: sanitized head, truncated,
    disambiguated with a hash of the whole id."""
    head = re.sub(r"[^A-Za-z0-9_.-]", "_", artifact_id)[:60]
    return f"{head}-{hashlib.sha256(artifact_id.encode()).hexdigest()[:8]}"


def archive_root(state, create: bool = False) -> Path:
    d = Path(state) / "gym" / "archive"
    if create:
        d.mkdir(parents=True, exist_ok=True)
        d.chmod(0o700)
    return d


def artifact_dir(state, artifact_id: str, create: bool = False) -> Path:
    d = archive_root(state, create) / _slug(artifact_id)
    if create:
        d.mkdir(parents=True, exist_ok=True)
        d.chmod(0o700)
    return d


def text_sha(text: str) -> str:
    return hashlib.sha256((text or "").encode()).hexdigest()[:16]


def file_sha(path) -> str:
    """The artifact file's current content hash, or "" when it cannot be read — a
    missing or unreadable artifact means "nothing is live", never an exception."""
    try:
        return text_sha(Path(str(path)).expanduser().read_text(errors="replace"))
    except (OSError, TypeError):
        return ""


def variant_id(artifact_id: str, parent, sha: str) -> str:
    """Content-addressed, so re-proposing the same candidate from the same parent
    re-writes one node instead of growing a duplicate lineage every run."""
    basis = "|".join([artifact_id, str(parent or ""), sha])
    return hashlib.sha256(basis.encode()).hexdigest()[:12]


def _write(path: Path, obj: dict) -> None:
    path.write_text(json.dumps(obj, indent=1))
    path.chmod(0o600)


def load(state, artifact_id: str) -> list:
    """Every archived variant for one artifact, oldest first."""
    d = artifact_dir(state, artifact_id)
    if not d.is_dir():
        return []
    out = []
    for f in sorted(d.glob("*.json")):
        try:
            rec = json.loads(f.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        if isinstance(rec, dict) and rec.get("id"):
            out.append(rec)
    return sorted(out, key=lambda v: (v.get("seq", 0), v["id"]))


def load_all(state) -> dict:
    """{artifact_id: [variants]} across the whole archive. The directory name is a
    slug, so the artifact id is read back out of the records themselves."""
    root = archive_root(state)
    if not root.is_dir():
        return {}
    out = {}
    for d in sorted(root.iterdir()):
        if not d.is_dir():
            continue
        recs = []
        for f in sorted(d.glob("*.json")):
            try:
                rec = json.loads(f.read_text())
            except (json.JSONDecodeError, OSError):
                continue
            if isinstance(rec, dict) and rec.get("id") and rec.get("artifact"):
                recs.append(rec)
        if recs:
            recs.sort(key=lambda v: (v.get("seq", 0), v["id"]))
            out[recs[0]["artifact"]] = recs
    return out


def get(state, artifact_id: str, vid: str):
    f = artifact_dir(state, artifact_id) / f"{vid}.json"
    try:
        rec = json.loads(f.read_text())
    except (json.JSONDecodeError, OSError):
        return None
    return rec if isinstance(rec, dict) else None


# ---------------------------------------------------------------- scores
def rates(v: dict):
    """(prevented rate, preserved rate) for a scored variant, or None when it has no
    usable score. An unscorable variant is not a bad variant — it is an unmeasured one,
    so it never dominates and is never dominated."""
    s = v.get("scores") or {}
    if s.get("unscorable", True):
        return None
    p, w = s.get("prevented") or {}, s.get("preserved") or {}
    if not p.get("of") or not w.get("of"):
        return None
    return (p["n"] / p["of"], w["n"] / w["of"])


def dominates(a: dict, b: dict) -> bool:
    """a is strictly better than b on BOTH sides. Strict on both is deliberate: a
    variant that ties on one side still carries a trade-off worth keeping."""
    ra, rb = rates(a), rates(b)
    if ra is None or rb is None:
        return False
    return ra[0] > rb[0] and ra[1] > rb[1]


def dominated_ids(records: list) -> list:
    return [v["id"] for v in records if any(dominates(o, v) for o in records if o["id"] != v["id"])]


# ---------------------------------------------------------------- pareto front
def pareto_dominates(a: dict, b: dict) -> bool:
    """Standard Pareto domination on the case-count-weighted score: at least as good on
    both sides, strictly better on one.

    Both sides are read as rates — verdicts that came back true over cases actually
    judged — rather than raw counts, because two candidates for the same artifact are
    not always scored on the same number of cases: a judge error or a budget skip drops
    a case for one and not the next, and 3/3 is not worse than 4/8.

    This is deliberately not the same rule as `dominates` above. Eviction asks "is this
    variant worthless?" and answers conservatively, strictly worse on both sides. The
    front asks "is this variant on the frontier?" and uses the sharper standard rule. A
    variant that ties another on both sides is on the front beside it, and neither
    evicts the other.
    """
    ra, rb = rates(a), rates(b)
    if ra is None or rb is None:
        return False
    return ra[0] >= rb[0] and ra[1] >= rb[1] and (ra[0] > rb[0] or ra[1] > rb[1])


def pareto_front(records: list) -> list:
    """The non-dominated scored variants, best-preventing first. An unscorable variant
    is never on the front: an unmeasured candidate has no position to defend."""
    scored = [v for v in records if rates(v) is not None]
    front = [v for v in scored
             if not any(pareto_dominates(o, v) for o in scored if o["id"] != v["id"])]
    return sorted(front, key=lambda v: (-rates(v)[0], -rates(v)[1], v.get("seq", 0), v["id"]))


def front_members(records: list, max_members: int = 4) -> list:
    """The front, shaped for an analyst's context and for the report: both score sides
    with their rates, and the edits that produced each member.

    Trimming keeps the extremes. On a front, ordering by prevented rate descending puts
    the best preserver last — no two members can lead on both sides at once, or the
    loser would not be on the front — so the first and last members are exactly the pair
    the reflective question is asked about, and the middle fills by combined score.
    """
    ordered = pareto_front(records)
    if max_members >= 2 and len(ordered) > max_members:
        fill = sorted(ordered[1:-1],
                      key=lambda v: (-(rates(v)[0] + rates(v)[1]), v.get("seq", 0)))
        keep = ({ordered[0]["id"], ordered[-1]["id"]}
                | {v["id"] for v in fill[:max_members - 2]})
        ordered = [v for v in ordered if v["id"] in keep]
    return [_member(v) for v in ordered]


def _member(v: dict) -> dict:
    r = rates(v)
    s = v.get("scores") or {}
    prov = v.get("provenance") or {}
    return {"variant": v["id"], "parent": v.get("parent"), "run_id": v.get("run_id"),
            "prevented": {**(s.get("prevented") or {}), "rate": round(r[0], 3)},
            "preserved": {**(s.get("preserved") or {}), "rate": round(r[1], 3)},
            "title": prov.get("title", ""),
            "expected_improvement": prov.get("expected_improvement", ""),
            "ops": [{"op": o.get("op"), "anchor": o.get("anchor"), "text": o.get("text", "")}
                    for o in (v.get("ops") or []) if isinstance(o, dict)]}


def trade_off(members: list) -> str:
    """One sentence naming what the front actually trades, so nobody has to read two
    rate columns to see it."""
    if len(members) < 2:
        return ""
    best_prevent = max(members, key=lambda m: m["prevented"]["rate"])
    best_preserve = max(members, key=lambda m: m["preserved"]["rate"])
    if best_prevent["variant"] == best_preserve["variant"]:
        return (f"{best_prevent['variant']} leads on both sides; the others are here "
                f"because they tie it on one of them.")
    return (f"{best_prevent['variant']} prevents the most: "
            f"{best_prevent['prevented']['rate']:.2f} of its failure cases, but it keeps "
            f"only {best_prevent['preserved']['rate']:.2f} of what already worked. "
            f"{best_preserve['variant']} is the other end: it keeps "
            f"{best_preserve['preserved']['rate']:.2f} of what worked and prevents "
            f"{best_preserve['prevented']['rate']:.2f}. Which trade to take is yours "
            f"to pick.")


def all_fronts(state, max_members: int = 4) -> dict:
    """{artifact: {members, trade_off}} for every artifact whose archive states a
    trade-off. A one-member front states none, so it is left out."""
    out = {}
    for artifact, records in load_all(state).items():
        members = front_members(records, max_members)
        if len(members) >= 2:
            out[artifact] = {"members": members, "trade_off": trade_off(members)}
    return out


# ---------------------------------------------------------------- retention
def evict(records: list, cap: int, live: str | None = None) -> tuple:
    """(kept, evicted) for one artifact's archive. Dominated variants go first, oldest
    first within each group; the live variant and the newest one are never evicted."""
    if cap <= 0 or len(records) <= cap:
        return list(records), []
    protected = {records[-1]["id"]}
    if live:
        protected.add(live)
    dominated = set(dominated_ids(records))
    droppable = [v for v in records if v["id"] not in protected]
    order = ([v for v in droppable if v["id"] in dominated]
             + [v for v in droppable if v["id"] not in dominated])
    n_drop = min(len(records) - cap, len(order))
    evicted = {v["id"] for v in order[:n_drop]}
    return [v for v in records if v["id"] not in evicted], [v for v in order[:n_drop]]


# ---------------------------------------------------------------- writing
def record(state, artifact_id: str, text: str, score: dict, *, base_sha: str = "",
           ops=(), chain_ops=None, parent=None, run_id=None, finding_id=None,
           path=None, provenance=None, cap: int = DEFAULT_CAP) -> dict:
    """Archive one scored candidate and enforce the cap. `base_sha` is the hash of the
    text the ops were applied to: when no parent is declared, the archived variant
    holding exactly that text IS the parent, which is how an applied variant becomes
    the trunk the next run branches from without anyone writing the edge by hand."""
    existing = load(state, artifact_id)
    if parent is None and base_sha:
        parent = next((v["id"] for v in existing if v.get("text_sha") == base_sha), None)
    sha = text_sha(text)
    vid = variant_id(artifact_id, parent, sha)
    prior = next((v for v in existing if v["id"] == vid), None)
    s = score or {}
    rec = {
        "schema_version": ARCHIVE_VERSION,
        "id": vid,
        "artifact": artifact_id,
        "parent": parent,
        "seq": prior["seq"] if prior else (max((v.get("seq", 0) for v in existing), default=0) + 1),
        "run_id": run_id,
        "finding_id": finding_id,
        "created_at": (prior or {}).get("created_at")
                      or datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "path": str(path) if path else None,
        "base_sha": base_sha,
        "text_sha": sha,
        "text": text,
        "ops": list(ops or []),
        "chain_ops": list(chain_ops if chain_ops is not None else (ops or [])),
        "scores": {"prevented": s.get("prevented") or {"n": 0, "of": 0},
                   "preserved": s.get("preserved") or {"n": 0, "of": 0},
                   "unscorable": bool(s.get("unscorable", True)),
                   "reason": s.get("reason") or ""},
        "provenance": provenance or {},
    }
    d = artifact_dir(state, artifact_id, create=True)
    _write(d / f"{vid}.json", rec)

    records = [v for v in existing if v["id"] != vid] + [rec]
    records.sort(key=lambda v: (v.get("seq", 0), v["id"]))
    _, evicted = evict(records, cap, live=live_id(records))
    for v in evicted:
        (d / f"{v['id']}.json").unlink(missing_ok=True)
    return rec


# ---------------------------------------------------------------- reading
def live_id(records: list):
    """The archived variant whose text is what the artifact file holds right now, if
    any. Read from disk rather than tracked through apply: what is live is a fact about
    the file, not a claim this module would have to keep in sync."""
    path = next((v.get("path") for v in reversed(records) if v.get("path")), None)
    if not path:
        return None
    sha = file_sha(path)
    if not sha:
        return None
    return next((v["id"] for v in records if v.get("text_sha") == sha), None)


def score_text(v: dict) -> str:
    s = v.get("scores") or {}
    if s.get("unscorable", True):
        return f"unscorable — {s.get('reason') or 'no score'}"
    p, w = s.get("prevented") or {}, s.get("preserved") or {}
    return (f"prevented {p.get('n', 0)}/{p.get('of', 0)}  "
            f"preserved {w.get('n', 0)}/{w.get('of', 0)}")


def _op_text(op: dict) -> str:
    kind = op.get("op")
    line = f"{kind} @ {str(op.get('anchor', ''))[:60]!r}"
    if op.get("text"):
        line += f" -> {str(op['text'])[:60]!r}"
    return line


def lineage_lines(artifact_id: str, records: list, live: str | None = None) -> list:
    """Depth-first lineage view: parents before children, each variant with its score,
    its edits relative to its parent, and where it came from."""
    if not records:
        return [f"{artifact_id}: no archived variants yet"]
    children = {}
    known = {v["id"] for v in records}
    for v in records:
        parent = v.get("parent") if v.get("parent") in known else None
        children.setdefault(parent, []).append(v)
    lines = [f"{artifact_id} — {len(records)} archived variant(s)"]

    def walk(parent, depth):
        for v in children.get(parent, []):
            pad = "  " * (depth + 1)
            marks = []
            if v["id"] == live:
                marks.append("LIVE")
            if v.get("parent") and v.get("parent") not in known:
                marks.append(f"parent {v['parent']} evicted")
            head = f"{pad}{v['id']}  run {v.get('run_id') or '-'}  {score_text(v)}"
            if marks:
                head += "  [" + ", ".join(marks) + "]"
            lines.append(head)
            prov = v.get("provenance") or {}
            if prov.get("title"):
                lines.append(f"{pad}  {prov['title']}")
            if prov.get("expected_improvement"):
                lines.append(f"{pad}  expected: {prov['expected_improvement']}")
            for op in v.get("ops") or []:
                if isinstance(op, dict):
                    lines.append(f"{pad}  {_op_text(op)}")
            walk(v["id"], depth + 1)

    walk(None, 0)
    return lines
