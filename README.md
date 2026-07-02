# self-optimize

A recurring, measured optimization loop for Claude Code. It mines your own session
transcripts and config surface, produces an evidence-cited report where every number
is re-derivable from a local evidence pack, applies the safe tier of changes only with
your per-item approval, and verifies each applied change against your next real
sessions — with one-command rollback.

Personal-first and private. See SECURITY.md for the threat model before running.

## Install

    /plugin marketplace add /Users/nick/Documents/python/self-optimize     (or the private GitHub URL)
    /plugin install self-optimize@self-optimize

## Use

    /self-optimize --stats-only     # first run: deterministic numbers, zero LLM tokens
    /self-optimize                  # full run: collect -> verify -> 2 analysts -> report
    /self-optimize apply <id>       # apply a tier-A finding (snapshot + smoke-check)
    /self-optimize reject <id> "reason"   # suppress it, with memory of why
    /self-optimize rollback <id>    # restore the snapshot

Reports land in `<config-dir>/self-optimize/reports/` (override `report_dir` in
`<config-dir>/self-optimize/config.json` — note reports contain redacted transcript
excerpts, so think before pointing this at a synced folder).

## Instances

The config dir is resolved per running instance: `$CLAUDE_CONFIG_DIR` if set, else
`~/.claude`. Separate work/personal instances (e.g. `~/.claude-work` vs `~/.claude`)
are fully independent: install and run `/self-optimize` inside each; every instance
keeps its own state, evidence, ledger, and reports in `<config-dir>/self-optimize/`.

## Config (`<config-dir>/self-optimize/config.json`, auto-created)

| key | default | meaning |
|---|---|---|
| report_dir | `<state>/reports` | where reports are written |
| project_include / project_exclude | `["*"]` / `[]` | fnmatch globs over project dir names |
| since_days | 30 | analysis window |
| sample_caps | 40 excerpts / 1500 tok / 60k total | bounds analyst input (and cost) |
| max_budget_tokens | 400000 | advisory cap; sample caps are the enforcement |
| retain_runs | 10 | evidence dirs kept |
| verify.min_sessions / min_rel_change | 10 / 0.10 | verification floors |

## Cost

`--stats-only` uses zero LLM tokens. A full run's LLM cost is bounded by the sample
caps (≤ ~60k tokens of analyst input by default) plus two sonnet-class analyst
outputs; the report footer records actual analyst tokens per run.

## What it reads and writes

Reads: `<config-dir>/projects/**/*.jsonl` (locally, through a secret scrubber),
settings/plugin/skill/agent metadata (settings `env` blocks are never read).
Writes: only under `<config-dir>/self-optimize/` — plus, on `apply`, exactly the config
files a finding names, snapshotted first. Nothing leaves the machine.
