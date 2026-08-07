# self-optimize

A recurring, measured optimization loop for Claude Code. It mines your own session
transcripts and config surface, produces an evidence-cited report where every number
is re-derivable from a local evidence pack, applies the safe tier of changes only with
your per-item approval, and verifies each applied change against your next real
sessions — with one-command rollback.

Personal-first and private. See SECURITY.md for the threat model before running.

## Install

    /plugin marketplace add https://github.com/acertainKnight/self-optimize     (or a local clone path)
    /plugin install self-optimize@self-optimize

## Use

    /self-optimize --stats-only     # first run: deterministic numbers, zero LLM tokens
    /self-optimize                  # full run: collect -> verify -> 3 analysts -> report
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

## Eval gym

Every full run also updates an eval gym under `<config-dir>/self-optimize/gym/`: for
each optimizable artifact (skill, agent, hook, guidance block) it keeps a corpus of
graded cases mined from your own sessions — *failure cases*, where the artifact was
active and you corrected the assistant, and *working cases*, where it was active and
nothing needed correcting. That corpus is what a proposed rewrite gets checked
against, instead of taste.

    python3 scripts/gym.py status --state <config-dir>/self-optimize

The registry is derived from the inventory step, never hand-maintained: a new skill on
disk registers itself on the next run with an empty corpus, and an artifact missing
from three consecutive runs is retired and dropped from scoring (it un-retires with
its corpus intact if it comes back). Below the case floor — fewer than 3 cases on
either side — the gym reports `unscorable` and refuses to emit a number, because a
score off two cases is noise. Cases deduplicate across overlapping collection windows
and age out FIFO past the per-artifact cap.

The corpus holds real transcript excerpts (redaction-scrubbed, mode 0600). It lives in
your state dir and is never committed anywhere.

## Instances

The config dir is resolved per running instance: `$CLAUDE_CONFIG_DIR` if set, else
`~/.claude`. Separate work/personal instances (e.g. `~/.claude-work` vs `~/.claude`)
are fully independent: install and run `/self-optimize` inside each; every instance
keeps its own state, evidence, ledger, and reports in `<config-dir>/self-optimize/`.

Exception: if a second config dir symlinks `projects/` and `self-optimize/` back to
the first (agent-home-style unification), that is ONE corpus and ONE ledger — run
the loop once, from either instance; running it "per instance" would just repeat
the same run against the same data.

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
| gym.min_cases_per_side | 3 | fewer cases than this on either side and the artifact is reported unscorable |
| gym.max_cases_per_side | 20 | per-artifact corpus cap; older cases age out FIFO |
| gym.retire_after_absent_runs | 3 | consecutive runs an artifact can be missing from the inventory before it is retired |

Session metrics verify against the post-apply session distribution. Snapshot metrics
(`base_context_est`, `unused_surface_count`) are one global number that any later
config change moves, so those verify by checking the applied setting is still in
effect — not by comparing the global number.

## Cost

`--stats-only` uses zero LLM tokens. A full run's LLM cost is bounded by the sample
caps (≤ ~60k tokens of analyst input by default) plus three sonnet-class analyst
outputs; the report footer records actual analyst tokens per run.

## Running it on a schedule

Not enabled by default — schedule it locally with a plain crontab line (adjust the
time and `CLAUDE_CONFIG_DIR` for the instance you want optimized):

    0 9 * * MON  CLAUDE_CONFIG_DIR=$HOME/.claude claude -p "/self-optimize" >> $HOME/.claude/self-optimize/cron.log 2>&1

Note: Claude Code's `/schedule` cloud routines are NOT suitable here — they execute
in the cloud, without access to this machine's transcripts and state. Schedule locally.

It's the same full run described above — collect, verify, three analysts, report —
so review the report and `apply`/`reject` by hand; nothing auto-applies.

## Adapters

Claude Code is the primary harness (plugin + runner + apply templates). Claude
Code sessions launched from the desktop app land in the same
`<config-dir>/projects/` store, so they are covered automatically.
`adapters/codex/` additionally collects evidence from a Codex CLI install —
sessions from its state DB plus the config.toml/AGENTS.md surface; evidence
only, no apply automation (see `adapters/codex/README.md`).
`adapters/claude_chat/` ingests the official claude.ai data export
(`conversations.json`) to mine chat corrections from the web/desktop chat apps,
whose conversations are cloud-side and have no local transcript store.

## What it reads and writes

Reads: `<config-dir>/projects/**/*.jsonl` (locally, through a secret scrubber),
settings/plugin/skill/agent metadata (settings `env` blocks are never read).
Writes: only under `<config-dir>/self-optimize/` — plus, on `apply`, exactly the config
files a finding names, snapshotted first. Nothing leaves the machine.
