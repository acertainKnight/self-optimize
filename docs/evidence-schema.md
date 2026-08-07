# Evidence-pack schema v1 — the harness seam

Everything downstream of collection (analysts, synth, report, ledger, verify) consumes
ONLY these six JSON files. Supporting another harness = writing one collector that
emits them. Every file carries `schema_version: "1"` and `harness`.

| file | shape |
|---|---|
| sessions.json | `{sessions:[{id, project, cwd, harness_version, started_at, ended_at, turns, input_tokens, output_tokens, cache_read, cache_write, sidechain_output_tokens, models:{<model_id>:{input,output}}, corrections_count, duplicate_reads, repeated_calls, permission_stalls, revert_events, reasks, ended_on_correction, redactions}]}` |
| usage.json | `{window:{since,until}, totals:{sessions,turns,input_tokens,output_tokens,cache_read,cache_write}, per_project:{p:{sessions,output_tokens}}, per_model:{m:{input,output,sessions}}, corrections_by_model:{m:count}, waste:{duplicate_reads_total,repeated_calls_total,permission_stalls_total,main_model_heavy_sessions,revert_events_total,reasks_total,ended_on_correction_total,top_duplicate_read_paths,top_stalled_tools,stall_examples}, corrections:{total,rate_per_session}, parse:{skipped_lines,files,redactions, collector_limits?}}` — adapters that cannot supply a field emit it zeroed/empty and say so in `parse.collector_limits` |
| activation.json | `{items:{"skill:<name>"|"agent:<name>"|"mcp:<server>"|"mcp_tool:<tool>"|"plugin:<id>":{count,last_used,projects}}}` |
| samples.json | `{samples:[{session,project,ts,kind:"correction",pattern,user_text,prior_assistant_text}]}` — all text pre-redacted |
| inventory.json | harness-specific config surface (Claude Code: plugins/skills/agents/mcp_servers/claude_md/hooks/guidance/settings-allowlist/available_plugins/base_context_est/unused/rare) — `metadata` is opaque to the portable core; `hooks` is `[{id, source, est_context_tokens}]` from settings.json's `hooks` block plus each enabled plugin's hook file(s); `guidance` is `[{id:"guidance:<path>", path, kind:"claude_md" or "memory", bytes, est_tokens, mtime, body}]` — the global CLAUDE.md plus auto-memory notes, bodies capped and redaction-scrubbed, for the guidance-debt audit |
| artifacts.json | `{artifacts:[{id, kind:"skill" or "agent", source, path, activation_count, symlinked, body}]}` — the ≤10 most-activated user-owned skills + any-source agents, bodies truncated to 8000 chars; `symlinked` is true when the file or a parent dir (≤3 levels) is a symlink |
| constraints.json | `{rejected:[{title, reason, ts}]}` — last 20 rejected ledger entries; feeds the analysts' standing-constraints instruction |
| findings.json | `{findings:[rec + id + evidence_hash + delta_tokens], dropped:{invalid,citations,guard,suppressed,citation_detail:[{analyst,title,failed_refs}]}}` — `citation_detail` drives the runner's single citation-retry round |
| labels.json (LLM step output) | written by the runner from the sample-labeler agent, then normalized by `scripts/labels.py` to `{counts:{<category>:n}, dropped:n}`; validated categories only, per-category counts land in metrics.jsonl as `corrections_by_category` |
| shadow.json (LLM step output) | `{<finding-id>:{prevented,total}}` — judge verdicts on evolver rewrites vs their motivating correction samples; rendered on the finding, never an auto-gate |
| analyst_tokens.json | `{<analyst>:tokens}` written by the runner; report.py reads it when `--analyst-tokens` is absent |
| analysts/&lt;name&gt;/ | per-analyst copies of exactly the files that analyst reads (miner/auditor/evolver/labeler, written by inventory.py) so parallel analysts never share read paths |
| metrics.jsonl (state, not evidence) | one append-only row per run: `{run_id, n_sessions, tokens_per_session, correction_rate, duplicate_read_rate, permission_stalls, parse_skipped, zero_correction_session_rate, mean_session_minutes, turns_per_session, base_context_est, unused_surface_count, corrections_by_category?}` — the collector has no per-task segmentation, so there is no `tokens/task` or "first-try success" metric; `zero_correction_session_rate` is the honest, implementable proxy for the latter |
| decisions.json (dashboard output, not evidence) | `{run_id, apply:[id...], reject:[{id, reason}...], assist:[id...], amend:[{id, reason, action}...]}` — produced by the browser-side dashboard (`scripts/dashboard.py`, downloaded by hand), consumed by `self-optimize decide` (`scripts/apply.py::cmd_decide`); unknown top-level keys are ignored, ids must be strings, and `run_id` must match the evidence run being decided against (stale/foreign files are refused); `amend` rejects the original rec in favor of a different, re-validated `action` — the replacement is never trusted just because it came from a decisions.json for the right run: it goes through `schema.validate_rec` + `synth.guard` exactly like an analyst-proposed action, and a refused amend leaves the original rec untouched |
| state/decisions/\<run_id\>-\<timestamp\>.json (state, not evidence) | `{run_id, decided_at, source_file, apply:[id...], reject:[{id, reason}...], refused:[id...], assist:[{id, title, payload_type}...], amend:[{id, reason, action}...], amended:[{orig, new, title}...], amend_refused:[{id, reason}...]}` — one durable file per `self-optimize decide` run, mode 0600; the dashboard's decisions.json in `~/Downloads` is transient, this is the permanent record of what was applied/rejected/refused/assisted/amended |

`inventory.json`'s `available_plugins` is a guarded parse of the local plugin catalog
cache (`<data_root>/plugins/plugin-catalog-cache.json`), installed plugins excluded;
any shape drift in that cache degrades to an empty list rather than failing collection.

Recommendation objects and evidence-ref grammar are defined in `scripts/schema.py`
and the analyst agent definitions. Portable analyzers: transcript-miner and
sample-labeler (harness-neutral content), synth, report, ledger, verify.
Harness-scoped: config-auditor + rules.json + templates.py (the Claude Code
adapter). Additional collectors emitting this same shape: `adapters/codex/`
(Codex CLI threads DB + config surface), `adapters/claude_chat/` (claude.ai
data export), and `adapters/opencode/` (opencode's `opencode.db` SQLite store
+ config surface) — all evidence-only, gaps declared in `parse.collector_limits`.
