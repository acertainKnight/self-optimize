# Evidence-pack schema v1 — the harness seam

Everything downstream of collection (analysts, synth, report, ledger, verify, gym)
consumes ONLY these JSON files. Supporting another harness = writing one collector that
emits them. Every file carries `schema_version: "1"` and `harness`.

One `collect.py` run covers every installed harness. It walks the Claude Code corpus
itself, runs each discovered adapter as a subprocess into `EV/harness/<name>/`, and merges
those packs into the canonical top-level files. The merge is deterministic (harnesses fold
in sorted-name order) and idempotent (same inputs, same bytes). Every session, sample, and
working row gains a `harness` field naming where it came from; that field is additive, so
analysts, synth, and the gym read a merged pack unchanged. The doc-level `harness` stamp
still names the collector that wrote the file (`claude-code`), and `usage.per_harness` is
the per-harness breakdown.

| file | shape |
|---|---|
| sessions.json | `{sessions:[{id, project, cwd, harness_version, started_at, ended_at, turns, input_tokens, output_tokens, cache_read, cache_write, sidechain_output_tokens, models:{<model_id>:{input,output}}, corrections_count, duplicate_reads, repeated_calls, permission_stalls, revert_events, reasks, ended_on_correction, redactions, harness, activation:{"skill:<name>"\|"agent:<name>"\|…:count}}]}` — `harness` names the collector the row came from; `activation` is the same id space as activation.json but scoped to that one session, which is what lets the gym attach a case to the artifacts that were actually live; adapters that cannot attribute activation per session omit it |
| usage.json | `{window:{since,until}, totals:{sessions,turns,input_tokens,output_tokens,cache_read,cache_write}, per_project:{p:{sessions,output_tokens}}, per_model:{m:{input,output,sessions}}, corrections_by_model:{m:count}, waste:{duplicate_reads_total,repeated_calls_total,permission_stalls_total,main_model_heavy_sessions,revert_events_total,reasks_total,ended_on_correction_total,top_duplicate_read_paths,top_stalled_tools,stall_examples}, corrections:{total,rate_per_session}, parse:{skipped_lines,files,redactions, collector_limits?}, per_harness:{<name>:{status:"ok"\|"failed", sessions, corrections, samples, error?}}}` — adapters that cannot supply a field emit it zeroed/empty and say so in `parse.collector_limits`; on a merged pack every numeric field is the sum across harnesses, `collector_limits` entries are prefixed with the harness that declared them, and `per_harness` carries the breakdown plus any harness that failed this run |
| activation.json | `{items:{"skill:<name>"|"agent:<name>"|"mcp:<server>"|"mcp_tool:<tool>"|"plugin:<id>":{count,last_used,projects}}}` |
| samples.json | `{samples:[{session,project,ts,kind:"correction",pattern,user_text,prior_assistant_text,harness}]}` — all text pre-redacted; the sample caps apply to the merged pool, split across harnesses in proportion to their session counts with a floor of one, so a chatty harness cannot crowd a quiet one out |
| working.json | `{samples:[{session, project, ts, kind:"working", user_text, assistant_text, harness}]}` — ask/answer excerpts (≤2 per session, pre-redacted) from sessions that drew NO correction, the "it worked" counterpart to samples.json. Gym input only: no analyst reads it, so it does not consume the analyst token budget |
| inventory.json | harness-specific config surface (Claude Code: plugins/skills/agents/mcp_servers/claude_md/hooks/guidance/settings-allowlist/available_plugins/base_context_est/unused/rare) — `metadata` is opaque to the portable core; `hooks` is `[{id, source, est_context_tokens}]` from settings.json's `hooks` block plus each enabled plugin's hook file(s); `guidance` is `[{id:"guidance:<path>", path, kind:"claude_md" or "memory", bytes, est_tokens, mtime, body}]` — the global CLAUDE.md plus auto-memory notes, bodies capped and redaction-scrubbed, for the guidance-debt audit |
| artifacts.json | `{artifacts:[{id, kind:"skill" or "agent", source, path, activation_count, symlinked, body}]}` — the ≤10 most-activated user-owned skills + any-source agents, bodies truncated to 8000 chars; `symlinked` is true when the file or a parent dir (≤3 levels) is a symlink |
| constraints.json | `{rejected:[{title, reason, ts}]}` — last 20 rejected ledger entries; feeds the analysts' standing-constraints instruction |
| papercuts.json | `{lines:[{id, date, harness, text}]}` — the live (unarchived) lines of the papercut file named by `papercuts_path` in config.json (default `$HOME/papercuts.md`; see README's Papercut channel), read once per run at the merged level since the file isn't tied to any one harness; `id` is a content hash of the raw line, stable across runs so a citation keeps resolving as later lines are appended; `text` is redaction-scrubbed; a missing file reads as `{lines: []}`, never an error |
| findings.json | `{findings:[rec + id + evidence_hash + delta_tokens + deep_localize?], dropped:{invalid,citations,guard,suppressed,citation_detail:[{analyst,title,failed_refs}]}}` — `citation_detail` drives the runner's single citation-retry round; `deep_localize` (optional) is synth.py's attachment of any matching localize.json rows, advisory supporting evidence, never a finding category of its own |
| localize.json (LLM step output) | `{<session-id>:{bracket:[lo,hi], turns_total, calls, rationale, errors, friction_score}}` — `scripts/localize.py`'s bisection of the top-N friction sessions (`deep_localize.enabled` in config, off by default) against the raw transcript with the gym's judge backend; synth.py matches session/sample evidence_refs to attach the row onto findings.deep_localize |
| labels.json (LLM step output) | written by the runner from the sample-labeler agent, then normalized by `scripts/labels.py` to `{counts:{<category>:n}, dropped:n}`; validated categories only, per-category counts land in metrics.jsonl as `corrections_by_category` |
| shadow.json (LLM step output) | `{<finding-id>:{prevented,total}}` — judge verdicts on evolver rewrites vs their motivating correction samples; rendered on the finding, never an auto-gate |
| gym.json (gym gate output) | `{<finding-id>:{artifact, prevented:{n,of}, preserved:{n,of}, unscorable, reason, errors, skipped_budget, tokens_est, per_case:[{case, side, verdict, prompt_hash, error?}]}}` — written by `scripts/gym.py gate`, one result per `skill-improve` finding; the candidate it judges is built deterministically (a `file_ops` finding is scored on the artifact's current text with its ops applied, so an op set that no longer anchors comes back unscorable). Any candidate it could not score is downgraded from tier A to tier B in findings.json. report.py renders BOTH sides on the finding. Judged evidence for the human decision, never an auto-gate |
| security.json (G1 deterministic + G2 LLM step output) | `{<finding-id>:{g1:{scanned, passed, hits:[{category, snippet}]}, g2:{scanned, verdict:"match" or "mismatch", reason} or {scanned:false, reason}}}` — written by `scripts/security_gate.py`, one result per finding whose payload adds or edits executable content (a fenced script block, or any `category: "hooks"` finding). G1 is a deterministic pattern scan for network egress, credential-file reads, destructive commands, and encoded payloads; a failing finding is downgraded from tier A to tier B in findings.json, same fail-closed mechanism as the gym gate. G2 reuses the gym's judge backend (`gym.judge.command`, via `gym._invoke_judge`) to check declared purpose (the finding's title) against actual content; unconfigured judge = G2 skipped and recorded as such, G1 still runs. report.py and dashboard.py both render the result; an instruction-only finding gets no entry at all — zero pattern-scan and zero judge calls |
| analyst_tokens.json | `{<analyst>:tokens}` written by the runner; report.py reads it when `--analyst-tokens` is absent |
| analysts/&lt;name&gt;/ | per-analyst copies of exactly the files that analyst reads (miner/auditor/evolver/labeler, written by inventory.py) so parallel analysts never share read paths |
| metrics.jsonl (state, not evidence) | one append-only row per run: `{run_id, n_sessions, tokens_per_session, correction_rate, duplicate_read_rate, permission_stalls, parse_skipped, zero_correction_session_rate, mean_session_minutes, turns_per_session, base_context_est, unused_surface_count, corrections_by_category?}` — the collector has no per-task segmentation, so there is no `tokens/task` or "first-try success" metric; `zero_correction_session_rate` is the honest, implementable proxy for the latter |
| capture-queue.jsonl (state, not evidence) | `<state>/capture-queue.jsonl`, one JSON object per line: `{ts, harness, session_id, trigger, artifact_hint?, consumed?}` — appended by the opt-in, never-auto-enabled Stop hook in `hooks/capture_trigger.py` (see `hooks/README.md`); pointers only, no transcript content or secrets; `collect.py` reads it each run, samples flagged sessions first within the existing sample caps, then marks consumed entries in place rather than deleting them; missing or empty file = identical behavior to not having the hook installed |
| decisions.json (dashboard output, not evidence) | `{run_id, apply:[id...], reject:[{id, reason}...], assist:[id...], amend:[{id, reason, action}...]}` — produced by the browser-side dashboard (`scripts/dashboard.py`, downloaded by hand), consumed by `self-optimize decide` (`scripts/apply.py::cmd_decide`); unknown top-level keys are ignored, ids must be strings, and `run_id` must match the evidence run being decided against (stale/foreign files are refused); `amend` rejects the original rec in favor of a different, re-validated `action` — the replacement is never trusted just because it came from a decisions.json for the right run: it goes through `schema.validate_rec` + `synth.guard` exactly like an analyst-proposed action, and a refused amend leaves the original rec untouched |
| gym/registry.json + gym/corpus/\*.json (state, not evidence) | the eval gym's derived artifact registry and per-artifact case corpus, written by `scripts/gym.py update` from the files above; registry is `{last_run, artifacts:{<id>:{id, kind, name, path, source, activation_key, first_seen_run, last_seen_run, absent_runs, retired}}}`, each corpus file is `{artifact, failure:[case], working:[case]}` with case `{id, harness, session, project, ts, first_seen_run, user_text, assistant_text}` — mode 0600, never committed |
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

Harness discovery probes the default data roots — the Claude config dir, `~/.codex`,
`~/.local/share/opencode` — and runs the adapter for each root that exists. The defaults
live in `so_config.HARNESS_DEFAULTS` and nowhere else; `config.json` overrides them:

    "harnesses": {"codex": {"home": "/opt/codex"}, "opencode": {"enabled": false}}

A harness block replaces the default block one level deep, so a missing key falls back to
its default and a missing `"enabled"` still means enabled — you turn a harness off by
writing `"enabled": false`. `adapters/claude_chat/` is not discovered: the claude.ai export
is a file you produce by hand, so it stays a manual adapter run.

One adapter crashing marks that harness `failed` in `usage.per_harness`, prints the reason
on stderr, and leaves the rest of the run intact; `collect.py` exits non-zero only when
every harness failed.
