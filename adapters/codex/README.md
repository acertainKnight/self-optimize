# Codex CLI adapter (evidence collection only)

Collects session and config-surface evidence from a Codex CLI installation into
the shared self-optimize evidence shape, stamped `harness: "codex"`.

    python3 adapters/codex/collect_codex.py --out <evidence-dir> [--codex-home ~/.codex] [--since ISO]

Writes `sessions.json`, `usage.json`, `inventory.json`, `samples.json` (chmod 600).

## What it reads (Codex CLI 0.144.x)

- `state_<N>.sqlite` `threads` table (highest N wins): thread id, cwd, model,
  model_provider, tokens_used, created/updated timestamps. Read-only connection.
- Per-thread rollout JSONL (`threads.rollout_path`, falling back to a scan of
  `<codex-home>/sessions/<year>/...` by thread id if that path is stale):
  user/assistant turns, tool calls, token usage, and corrections. See
  `parse_rollout()`'s docstring for the record shapes this is grounded in.
- `config.toml`: model_providers, profiles, mcp_servers, features (via stdlib
  `tomllib`, Python 3.11+). Never reads auth material.
- `AGENTS.md`, `prompts/*.md`, `skills/**/SKILL.md`.

## Known limits

- `duplicate_reads` and `permission_stalls` stay zero: the rollout format has
  no distinct read-tool call or approval/sandbox-denial event to key either
  signal off of. `usage.parse.collector_limits` records this in-band.
- Threads whose rollout file can't be found (path stale and no match under
  `sessions/`) fall back to the sqlite `tokens_used` total as an unsplit
  `output_tokens` figure — the pre-rollout-parsing behavior.
- Unknown top-level rollout record types and malformed lines are counted in
  `usage.parse.skipped_lines`, never fatal — schema drift degrades gracefully
  rather than crashing the collector.
- Evidence only: there are no Codex apply templates. Recommendations against a
  Codex config surface are tier-B manual until an allowlisted `config.toml`
  edit template exists.
- No runner: Codex has no plugin-agent equivalent; invoke the collector by hand
  or from a prompt in `~/.codex/prompts/`.
