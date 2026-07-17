# Codex CLI adapter (evidence collection only)

Collects session and config-surface evidence from a Codex CLI installation into
the shared self-optimize evidence shape, stamped `harness: "codex"`.

    python3 adapters/codex/collect_codex.py --out <evidence-dir> [--codex-home ~/.codex] [--since ISO]

Writes `sessions.json`, `usage.json`, `inventory.json`, `samples.json` (chmod 600).

## What it reads (Codex CLI 0.144.x)

- `state_<N>.sqlite` `threads` table (highest N wins): thread id, cwd, model,
  model_provider, tokens_used, created/updated timestamps. Read-only connection.
- `config.toml`: model_providers, profiles, mcp_servers, features (via stdlib
  `tomllib`, Python 3.11+). Never reads auth material.
- `AGENTS.md`, `prompts/*.md`, `skills/**/SKILL.md`.

## Known limits

- Turn-level content lives in per-thread rollout files (`threads.rollout_path`)
  whose format has not been observed against a real session yet. Corrections,
  samples, and waste metrics are therefore emitted empty, and `tokens_used` is
  carried as `output_tokens` without an input/output split. `usage.parse.
  collector_limits` records this in-band. Extend `parse_rollout()` against a
  populated rollout file before trusting those fields.
- Evidence only: there are no Codex apply templates. Recommendations against a
  Codex config surface are tier-B manual until an allowlisted `config.toml`
  edit template exists.
- No runner: Codex has no plugin-agent equivalent; invoke the collector by hand
  or from a prompt in `~/.codex/prompts/`.
