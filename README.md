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
    /self-optimize --max-budget 10000   # cap analyst input tokens; refuses below a 2000-token floor
    /self-optimize apply <id>       # apply a tier-A finding (snapshot + smoke-check)
    /self-optimize reject <id> "reason"   # suppress it, with memory of why
    /self-optimize rollback <id>    # restore the snapshot
    /self-optimize decide [path]    # apply/reject the decisions.json from the dashboard

Reports land in `<config-dir>/self-optimize/reports/` (override `report_dir` in
`<config-dir>/self-optimize/config.json` — note reports contain redacted transcript
excerpts, so think before pointing this at a synced folder).

## Reviewing in the dashboard

Every full run also writes `<report_dir>/latest.html` (and a per-run
`<run-id>.html`) — a self-contained page, open it straight from disk
(`open <config-dir>/self-optimize/reports/latest.html`). For each tier-A finding
pick Apply / Reject / Amend / Skip (Reject and Amend both need a reason); check
any tier-B item you want help with as "Select for assisted work". Amend rejects
the finding in favor of a different action: pick one of the dashboard's suggested
alternatives or paste your own replacement action JSON — either way it's
re-validated and guarded exactly like any other action before it's ever applied.
Your choices persist locally as you go. When ready, click **Download
decisions.json** (or **Copy command** for a quick apply/reject one-liner instead
— amendments require Download, they can't be expressed as a command), then run:

    /self-optimize decide

with no argument it picks up the newest `self-optimize-decisions-*.json` in
`~/Downloads` automatically; pass a path to use a specific file. It applies/rejects
your tier-A picks and lists the tier-B items you selected for assisted work — those
are never run automatically, you do them together with Claude afterward. Each decide
run also logs what happened to `<config-dir>/self-optimize/state/decisions/` (the
`~/Downloads` file itself is transient).

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
| max_budget_tokens | 400000 | default `--max-budget` in tokens; sample caps shrink to fit and collect.py refuses (exit 2) runs too small to sample usefully |
| retain_runs | 10 | evidence dirs kept |
| verify.min_sessions / min_rel_change | 10 / 0.10 | verification floors |

## Cost

`--stats-only` uses zero LLM tokens. A full run's LLM cost is bounded by the sample
caps (≤ ~60k tokens of analyst input by default) plus two sonnet-class analyst
outputs; the report footer records actual analyst tokens per run.

## Running it on a schedule

Not enabled by default — schedule it locally with a plain crontab line (adjust the
time and `CLAUDE_CONFIG_DIR` for the instance you want optimized):

    0 9 * * MON  CLAUDE_CONFIG_DIR=$HOME/.claude claude -p "/self-optimize" >> $HOME/.claude/self-optimize/cron.log 2>&1

Note: Claude Code's `/schedule` cloud routines are NOT suitable here — they execute
in the cloud, without access to this machine's transcripts and state. Schedule locally.

It's the same full run described above — collect, verify, two analysts, report —
so review the report and `apply`/`reject` by hand; nothing auto-applies.

## What it reads and writes

Reads: `<config-dir>/projects/**/*.jsonl` (locally, through a secret scrubber),
settings/plugin/skill/agent metadata (settings `env` blocks are never read).
Writes: only under `<config-dir>/self-optimize/` — plus, on `apply`, exactly the config
files a finding names, snapshotted first. Nothing leaves the machine.
