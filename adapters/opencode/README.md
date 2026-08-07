# opencode adapter (evidence collection only)

Collects session and config-surface evidence from an opencode installation into
the shared self-optimize evidence shape, stamped `harness: "opencode"`.

    python3 adapters/opencode/collect_opencode.py --out <evidence-dir> [--opencode-home ~/.local/share/opencode] [--opencode-config ~/.config/opencode] [--since ISO]

Writes `sessions.json`, `usage.json`, `inventory.json`, `samples.json` (chmod 600).
Exits non-zero with a message on stderr if `opencode.db` is missing.

## What it reads (observed against opencode 1.17.x)

- `<opencode-home>/opencode.db`, a single SQLite database, opened read-only.
  - `session` table: one row per session — `id`, `directory` (cwd), `version`,
    `model` (a JSON string: `{id, providerID, variant}`), `time_created`/
    `time_updated` (epoch ms), and running totals `tokens_input`,
    `tokens_output`, `tokens_cache_read`, `tokens_cache_write`.
  - `message` table: one row per user/assistant turn — `session_id`,
    `time_created`, `data` (JSON with at least `role`).
  - `part` table: one row per content block within a turn, linked by
    `message_id` — `data.type` is `"text"`, `"reasoning"`, `"tool"`,
    `"step-start"`, `"step-finish"`, or `"patch"`. Only `"text"` parts are read
    for turn content; a message's turns text is every one of its text parts
    concatenated in part-id order.
  - The legacy `session_message` table (seen empty on this install) is never
    queried — `message` + `part` is the live schema.
- `<opencode-config>/opencode.jsonc`: comments and trailing commas are stripped
  (a small hand-rolled pass, not a full JSON5 parser) before `json.loads`.
  `mcp.*` becomes `mcp_servers`, `skills.paths` is globbed for `*/SKILL.md`,
  `model`/`plugin` land in `settings`.
- `<opencode-config>/AGENTS.md`, `<opencode-config>/agent/*.md`,
  `<opencode-config>/command/*.md`.
- Never opens `auth.json` or `mcp-auth.json` (or anything else in the data
  root besides `opencode.db`) — the inventory scan only ever globs `*.md` and
  `*/SKILL.md` under the config root, or reads `opencode.jsonc`/`AGENTS.md` by
  exact name, so credential files in the data root are structurally
  unreachable, not just skipped by a check.

## Correction mining

Reuses `scripts/collect.py`'s `DEFAULT_CORRECTION_RE` and `_cap_samples`
directly (imported, not copied) rather than re-implementing them. A user turn
matching the regex in its first 200 characters is paired with the nearest
preceding assistant turn's text, redacted via `scripts/redact.py`, and
recorded as a `samples.json` entry — same convention as the Claude Code and
claude.ai-export collectors.

## Known limits

- Duplicate reads, repeated tool calls, permission stalls, revert events, and
  re-asks are not mined — opencode's `part` table does carry tool-call rows
  (`type: "tool"`, with `tool`, `callID`, `state.status`), so this is doable
  as a follow-up, but it is out of scope here. Those `sessions.json` fields
  are always zero, and `usage.parse.collector_limits` says so.
- `input_tokens`/`output_tokens` are the `session` table's own running
  totals, attributed to `session.model` (whichever model was active when the
  row was read). If a session's model was switched mid-session, the token
  split is not reconstructed per turn.
- The correction regex only scans a turn's first 200 characters, matching
  every other collector in this repo. Validated against a real install: one
  local install's `message`/`part` data had a median *single* user turn of
  roughly 29,000 characters (one `part` row per turn, not several), evidently
  because full file/log content gets pasted inline into the same turn as the
  actual request. On that install `samples.json` came back empty — not
  because the mining logic is broken (the fixture test in
  `tests/test_opencode_adapter.py` exercises a short, realistic turn and
  passes), but because a real short correction, if any, would be past
  character 200 on a turn shaped like that. Narrowing further or widening the
  window for opencode specifically is a real option, but it would diverge
  from the shared, tested convention on a single data point — worth
  revisiting with more installs to look at.
## Apply templates

`opencode.jsonc` and `AGENTS.md` are both tier-B apply surfaces in
`adapters/claude_code/templates.py` (`jsonc_ops`, `agents_md_ops`) — a
recommendation against either renders and applies through the same
snapshot/rollback flow as the Claude Code templates, human-approved per item.
`jsonc_ops` edits are anchor-matched line edits (same mechanism as file_ops),
so existing `//` and `/* */` comments and layout survive untouched; the
result is re-validated against `_strip_jsonc` + `json.loads` before it is
written, and an edit that would produce invalid JSON is refused.
