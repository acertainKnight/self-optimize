"""Shared metric computation over session records. Used to capture apply-time
baselines and to verify applied recommendations on the next run."""


def _mean(vals):
    return (sum(vals) / len(vals)) if vals else None


def compute_metric(metric: dict, sessions: list, inventory: dict | None = None,
                   after_ts: str | None = None):
    key = metric.get("key")
    if key == "none":
        return None, 0
    if key == "base_context_est":
        return (inventory or {}).get("base_context_est"), 1
    if key == "unused_surface_count":
        return len((inventory or {}).get("unused", [])), 1

    rows = [s for s in sessions if not after_ts or (s.get("started_at") or "") > after_ts]
    n = len(rows)
    if n == 0:
        return None, 0
    if key == "tokens_per_session":
        return _mean([s.get("input_tokens", 0) + s.get("output_tokens", 0) for s in rows]), n
    if key == "correction_rate":
        return _mean([s.get("corrections_count", 0) for s in rows]), n
    if key == "duplicate_read_rate":
        return _mean([s.get("duplicate_reads", 0) for s in rows]), n
    if key == "permission_stalls":
        return _mean([s.get("permission_stalls", 0) for s in rows]), n
    if key == "model_output_tokens":
        scope = metric.get("scope") or ""
        if ":" not in scope:
            return None, 0
        prefix = scope.split(":", 1)[1]
        vals = []
        for s in rows:
            vals.append(sum(v.get("output", 0) for m, v in (s.get("models") or {}).items()
                            if m.startswith(prefix)))
        return _mean(vals), n
    return None, 0
