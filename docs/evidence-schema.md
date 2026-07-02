# Evidence-pack schema v1 — the harness seam

Everything downstream of collection (analysts, synth, report, ledger, verify) consumes
ONLY these six JSON files. Supporting another harness = writing one collector that
emits them. Every file carries `schema_version: "1"` and `harness`.

| file | shape |
|---|---|
| sessions.json | `{sessions:[{id, project, cwd, harness_version, started_at, ended_at, turns, input_tokens, output_tokens, cache_read, cache_write, sidechain_output_tokens, models:{<model_id>:{input,output}}, corrections_count, duplicate_reads, repeated_calls, permission_stalls, redactions}]}` |
| usage.json | `{window:{since,until}, totals:{sessions,turns,input_tokens,output_tokens,cache_read,cache_write}, per_project:{p:{sessions,output_tokens}}, per_model:{m:{input,output}}, waste:{duplicate_reads_total,repeated_calls_total,permission_stalls_total,top_duplicate_read_paths}, corrections:{total,rate_per_session}, parse:{skipped_lines,files,redactions}}` |
| activation.json | `{items:{"skill:<name>"|"agent:<name>"|"mcp:<server>"|"mcp_tool:<tool>"|"plugin:<id>":{count,last_used,projects}}}` |
| samples.json | `{samples:[{session,project,ts,kind:"correction",pattern,user_text,prior_assistant_text}]}` — all text pre-redacted |
| inventory.json | harness-specific config surface (Claude Code: plugins/skills/agents/mcp_servers/claude_md/settings-allowlist/base_context_est/unused/rare) — `metadata` is opaque to the portable core |
| findings.json | `{findings:[rec + id + evidence_hash + delta_tokens], dropped:{invalid,citations,guard,suppressed}}` |

Recommendation objects and evidence-ref grammar are defined in `scripts/schema.py`
and the analyst agent definitions. Portable analyzers: transcript-miner
(`agents/transcript-miner.md` — its content is harness-neutral), synth, report,
ledger, verify. Harness-scoped: config-auditor + rules.json + templates.py (the
Claude Code adapter).
