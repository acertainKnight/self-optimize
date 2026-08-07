# self-optimize

A measured optimization loop for coding agents. It mines your own session transcripts
and config surface, produces a report where every number is re-derivable from a local
evidence pack, applies the safe tier of changes only with your per-item approval, and
scores each applied change against your next real sessions — with one-command rollback.

Personal-first and private. Nothing leaves the machine. Read SECURITY.md for the threat
model before you run it.

## The problem

Working with a coding agent produces friction that nobody writes down. You correct the
same habit in three different sessions. The agent re-reads a file it read twenty minutes
ago. A permission prompt stalls the same tool every day. A skill you wrote in March now
fires on the wrong things, and a CLAUDE.md rule you added in April is costing tokens in
every single session while being violated anyway.

Each of these is individually too small to act on. Collectively they are most of the
gap between the agent you have and the agent you thought you were configuring. They stay
invisible because the evidence is scattered across hundreds of JSONL transcripts nobody
reads, and because the only person who could act on it is the person too busy working to
go looking.

The obvious fix — let the agent edit its own configuration — is worse than the problem.
An agent that rewrites its own instructions from one session's impressions overfits to
that session, and there is no signal telling you whether the edit helped or quietly broke
something that used to work. So the loop stays open: friction accumulates, corrections
repeat, config drifts, and nothing closes it.

This project closes the loop the boring way. Collection is deterministic. Analysis is
evidence-cited and mechanically checked. Application is gated on a human. Verification
happens against your next real sessions, not against the analyst's own confidence.

## The design

Four stages that run today, and a fifth the roadmap adds. Each one is a seam you can
inspect on its own:

```
sessions + config surface        (every installed harness, read locally)
      │
      ▼
 collection ......... deterministic, no LLM: parse, redact, count
      │               evidence pack — six JSON files, every id citable
      ▼
 analysts ........... read-only agents that see the pack and nothing else;
      │               synth drops any finding whose citations don't resolve
      │
      ├──▶ [ eval gym ] ..... roadmap (#21, #22, #23): score a proposed
      │                       self-edit on held-out cases before a human
      │                       sees it — complaints prevented AND working
      │                       behavior preserved, both reported
      ▼
 human gate ......... report + local HTML dashboard; you apply, reject,
      │               or amend, per item, with reasons recorded
      │               tier A: snapshot → write → smoke check → rollback
      │               tier B: read closely; hooks and permissions never
      │               machine-apply at all
      ▼
 verification ....... the next run scores each applied change against your
      │               real sessions and prints a verdict per change
      └──▶ metrics.jsonl — one row per run, feeds the next run's evidence
```

**Collection is deterministic and cheap.** `scripts/collect.py` and
`scripts/inventory.py` parse transcripts and config with no model in the loop, so
`--stats-only` costs zero tokens and the numbers are reproducible. Everything downstream
consumes only the six-file evidence pack described in `docs/evidence-schema.md`. That
file is the harness seam: supporting a new agent means writing one collector that emits
those six files.

**Analysts cite or they are dropped.** Four subagents read the pack — a transcript miner,
a config auditor, an artifact evolver, and a correction labeler. Each gets the Read tool
and its own private copy of exactly the files it needs. Every recommendation must carry
`evidence_refs` pointing at ids that exist in the pack; `scripts/synth.py` resolves each
one and discards the finding if a reference does not resolve. There is exactly one retry
round for citation repair, then the run moves on.

**Applying is a human decision, per item.** Findings carry a typed action, not prose.
Every action is rendered by a template that refuses anything outside the sanctioned roots
and refuses to write through a symlink. Tier A is the set of writes the machine is trusted
to make on your say-so: a settings change against an allowlisted key, a frontmatter edit,
a new file, a full replacement of a user-owned skill or agent. Tier B is everything you
should read before it lands — a unified diff against a file you wrote by hand, which still
applies with a snapshot behind it, and free-form instructions the machine never executes
at all. Hooks and permission changes are always that last kind, permanently, with no path
to promotion.

**Verification uses your sessions, not the analyst's opinion.** Each applied change
predicts a metric and a direction. `scripts/verify.py` compares the post-apply session
distribution against the pre-apply one — a binomial test for sparse count metrics,
Welch's t-test for continuous ones — and refuses to rule below the configured session
floor rather than reporting noise. Changes that share a metric are judged as a cohort,
because they cannot be separated. Snapshot metrics like `base_context_est` are one global
number that any later edit moves, so those verify by checking the applied setting is
still in effect instead of by comparing the number.

**The gym is the missing fifth stage.** Verification tells you whether a change helped
after you shipped it. The gym is meant to tell you before: a per-artifact corpus of graded
cases drawn from real usage (failure cases where the artifact fired and a correction
followed, working cases where it fired and nothing followed), and a judge that scores a
candidate rewrite on both sides. It is not built yet. See the roadmap below.

## What ships today

Merged on `main` and working:

- Deterministic collection and config inventory for Claude Code, with secret redaction on
  every excerpt before it is stored.
- Friction signals per session: corrections, duplicate reads, repeated tool calls,
  permission stalls, reverts after an agent edit, re-asks, and sessions that ended on a
  correction.
- Four read-only analysts, mechanical citation checking with one repair round, and typed
  payload guards on every proposed action.
- Correction labeling into stable categories, with a per-category trend across runs.
- A guidance-debt audit over CLAUDE.md and auto-memory notes: rules earn their token cost
  or get proposed for trimming.
- Hook proposals: a prose rule that transcripts show being violated becomes a proposed
  enforced check — as a tier-B item you implement yourself.
- Shadow eval on evolver rewrites: a cheap judge is asked, per motivating correction,
  whether the rewritten artifact would have prevented it. The score renders on the
  finding as evidence for your decision. It is not an auto-gate, and it only covers
  failure cases — the "did it preserve working behavior" half is what the gym adds.
- Markdown report plus a self-contained HTML dashboard, per-item apply / reject / amend /
  assist, a durable decision record, snapshot-backed applies with a post-write smoke
  check and auto-restore, and one-command rollback.
- Next-run verification with cohort verdicts and a program ROI line: verified token
  savings against analyst tokens spent.
- Evidence-only adapters for the Codex CLI, the claude.ai conversation export, and opencode.

## Roadmap

Tracked as four waves plus a portfolio epic. Nothing below is implemented.

- [#40 — Wave 1: the spine](https://github.com/acertainKnight/self-optimize/issues/40).
  One data path across harnesses feeding an eval gym that gates self-edits: a turn-level
  Codex rollout parser (#19), one run that merges evidence from every adapter (#20), the
  self-maintaining gym registry and case corpus (#21), the judge runner and scoring (#22),
  an evolver that makes bullet-level incremental edits gated on gym scores instead of
  whole-file rewrites (#23), inline trigger capture (#24), and a papercut channel where
  agents self-report friction they worked around (#25).
- [#41 — Wave 2: gym consumers](https://github.com/acertainKnight/self-optimize/issues/41).
  Durable corrections compiled into enforcement proposals (#28), a curator that dedupes
  and retires skills (#29), a validated failure taxonomy (#30), a silent-failure detector
  (#31), failure localization (#32), apply templates for non-Claude config surfaces (#33),
  and security gates on generated skill content (#34).
- [#42 — Wave 3: search over variants](https://github.com/acertainKnight/self-optimize/issues/42).
  A versioned variant archive (#35) and Pareto selection over it (#36). Gated on the gym
  proving itself on real corpus data first.
- [#43 — Wave 4: self-application](https://github.com/acertainKnight/self-optimize/issues/43).
  The analyst instruction files become gym artifacts improved by the same machinery (#37),
  on a deliberately slow timescale.
- [#44 — Portfolio](https://github.com/acertainKnight/self-optimize/issues/44). A synthetic
  demo corpus (#38) and a CI benchmark over it (#39), so the loop is runnable end to end
  without real transcripts.

## Landscape and position

The ideas here are not new. Each part was taken from published work, adapted, or
deliberately left out. The reasons matter more than the list.

**SkillOpt** ([arXiv 2605.23904](https://arxiv.org/abs/2605.23904)). Adopt the validation
gate; adapt the signal. SkillOpt treats agent skills as trainable external state and
improves them with bounded add/delete/replace edits, keeping an edit only when validated
rollout feedback says it helped. That shape is the backbone here: bounded edits, and a
score that must clear a bar before the edit counts as an improvement. The signal differs
because it has to. SkillOpt validates against benchmark tasks with known answers, and no
benchmark exists for how one particular person works. The label here is the user's own
correction: a session where the artifact fired and the user immediately corrected the
agent is a labeled failure, and a session where it fired and nothing followed is a
labeled success.

**ACE** ([arXiv 2510.04618](https://arxiv.org/abs/2510.04618)). Adopt itemized incremental
edits. ACE evolves an agent's context through generation, reflection, and curation, and
its central finding is that repeated monolithic rewrites cause context collapse — detail
that mattered gets summarized away one rewrite at a time. Today's evolver proposes
full-file replacements and is exposed to exactly that failure. Replacing it with
bullet-level add, delete, and replace operations is issue #23, and ACE is the reason it is
scoped that way rather than as a better rewrite prompt.

**Trace2Skill** ([arXiv 2603.25158](https://arxiv.org/abs/2603.25158)). Adapt the causal
quality gate. Trace2Skill distills execution trajectories into transferable skills and
keeps only the ones that demonstrably change an outcome, rather than everything that
looks like a lesson. That gate is what the gym's two-sided score implements: a proposed
edit has to prevent complaints it was written for *and* preserve behavior that already
worked. An edit that only does the first is how you trade a known problem for an unknown
one.

**GEPA** ([arXiv 2507.19457](https://arxiv.org/abs/2507.19457)). Defer, do not reject.
GEPA evolves prompts by reflecting on execution traces in natural language and keeping a
Pareto front of candidates, and it beats reinforcement learning with far fewer rollouts.
Pareto search over variants is the right end state and it is filed as wave 3 (#35, #36).
It is deferred because a search is only as good as its scorer: with no gym, a Pareto front
would be a front over noise, and with thin per-artifact case counts it would be a front
over three examples. The entry condition is the gym scoring real corpus data through at
least one verified apply cycle.

**Hermes Agent** ([NousResearch/hermes-agent](https://github.com/NousResearch/hermes-agent)).
Adopt the triggers and the staging directory; reject the default. Hermes creates skills
from experience on concrete triggers — a complex task that succeeded after five or more
tool calls, a dead end followed by the working path, a user correction, a non-trivial
workflow — and that trigger set is better than anything derived from first principles, so
it is taken as-is for the capture queue in #24. Hermes can also stage skill writes for
review under a pending directory, which is the right mechanism. The difference is that in
Hermes staging is opt-in behind a config flag and writes are unstaged by default. Here
approval is not a setting. Nothing reaches a config file without a per-item human
decision, and there is no flag that changes that.

**prime-agent** ([PrimeIntellect-ai/prime-agent](https://github.com/PrimeIntellect-ai/prime-agent),
MIT). Adapt the harness formalization; reject mid-session auto-apply. prime-agent models
the harness as durable state — supplemental prompts, memories, skill descriptions, and
subagent specs — with one uniform create/read/update/delete surface per artifact kind.
That is a cleaner statement of the apply-op design than this repo's, and it is the shape
#33 generalizes to. The refine loop applies its edits in-session, on the evidence of the
trajectory it just ran. One trajectory is one sample, and their own Factorio write-up
shows what optimizing against it can find: the agent discovered it could spawn resources
directly through RCON commands, bypassing the game's rules, despite a standing instruction
not to cheat. Applying offline, against held-out cases, with a human ratifying, is the
whole point of the slow path.

**MAST** ([arXiv 2503.13657](https://arxiv.org/abs/2503.13657)). Adapt the taxonomy. MAST
is an empirically derived taxonomy of 14 failure modes in three categories, built from 150
annotated multi-agent traces with strong inter-annotator agreement. The correction labeler
currently uses a hand-written category list, which is a guess. Issue #30 replaces it with
the MAST subset that survives translation to a single-agent coding session — the
inter-agent misalignment category largely does not apply — plus the correction-specific
categories MAST has no reason to cover.

**TRACE, user corrections to runtime enforcement**
([arXiv 2606.13174](https://arxiv.org/abs/2606.13174)). Adopt the compilation step. The
paper's observation is that coding agents violate previously corrected preferences across
sessions, and its answer is to compile corrections into runtime enforcement rather than
into more prose the model may or may not read. That is the difference between a rule in
CLAUDE.md and a hook that fails the call. Shipping today: a prose rule the transcripts
show being violated gets proposed as an enforced check. Issue #28 extends this to
corrections with no existing rule behind them. Generated enforcement stays tier B forever
— a machine that can install its own checks can install a check that silences its own
evidence.

**Voyager** ([arXiv 2305.16291](https://arxiv.org/abs/2305.16291)) **and TroVE**
([arXiv 2401.12869](https://arxiv.org/abs/2401.12869)). Adapt curation over accretion.
Voyager's skill library grows monotonically: every new behavior is stored, nothing is ever
removed. TroVE grows a toolbox the same way but periodically trims it, and reports
toolboxes 79–98% smaller than the comparison methods without giving up accuracy. A personal skill library has the Voyager
failure mode by default — skills accumulate, near-duplicates pile up, retired workflows
keep loading. The curator in #29 takes TroVE's trimming discipline, and does the cheap
half deterministically: similarity and activation counts find candidate duplicates and
dead skills with no model calls at all.

## Safety posture

The full threat model is in SECURITY.md. The short version, in the order the data moves:

Transcripts contain secrets pasted in past sessions and third-party content fetched from
the web, so every excerpt passes through deterministic redaction before it is stored, and
settings are read through an allowlist that never touches `env` blocks. Excerpts may carry
prompt injection, so analysts get no tools beyond reading their own evidence files, all
content is framed to them as data, and their output must satisfy a strict schema. A
poisoned finding is an attack path, so citations are verified mechanically, typed payload
guards reject any write outside the sanctioned roots, and every action is rendered by a
template rather than executed as free-form instructions. Applies snapshot the file first,
run a syntax smoke check after writing, auto-restore on failure, and can be rolled back
with one command. Hooks and permission changes are never machine-applied, permanently.

Stated honestly: regex-and-entropy redaction is not perfect, injection framing is a
mitigation rather than a guarantee, and the transcript format carries no stability promise
— collectors skip what they cannot parse and report the skip count.

## Multi-harness

Hub and spoke, not a port. The runner stays in Claude Code, which is the only harness with
plugin, subagent, and apply-template support wired up. Every other harness contributes a
collector that emits the same six-file evidence pack, and everything downstream —
analysts, synthesis, report, ledger, verification — is harness-neutral by construction.
The alternative, a runner per harness, means maintaining the same loop three times.

Applies target shared artifacts. When a skill or agent file is a symlink, or sits under a
symlinked directory, the collector records that, the evolver proposes a diff instead of a
rewrite, and the machine write path refuses to follow the symlink — so a file that other
harnesses read gets edited by you, not overwritten in place. Apply templates for
non-Claude config surfaces are #33.

Where each harness stands today: Claude Code is complete, including sessions launched from
the desktop app, which land in the same `<config-dir>/projects/` store. `adapters/codex/`
collects sessions and config surface from a Codex CLI install, evidence only.
`adapters/claude_chat/` ingests the official claude.ai data export to mine corrections from
chat, which is cloud-side with no local transcript store. `adapters/opencode/` collects
sessions and turn-level corrections from an opencode install's `opencode.db` SQLite store,
plus its config surface, also evidence only. All three declare their gaps in
`parse.collector_limits` rather than emitting zeros silently. Turn-level Codex parsing is
#19, and merging every adapter into one run is #20.

## Install

    /plugin marketplace add https://github.com/acertainKnight/self-optimize     (or a local clone path)
    /plugin install self-optimize@self-optimize

## Use

    /self-optimize --stats-only     # first run: deterministic numbers, zero LLM tokens
    /self-optimize                  # full run: collect -> verify -> analysts -> report
    /self-optimize --max-budget 10000   # cap analyst input tokens; refuses below a 2000-token floor
    /self-optimize apply <id>       # apply a tier-A finding (snapshot + smoke check)
    /self-optimize reject <id> "reason"   # suppress it, with memory of why
    /self-optimize rollback <id>    # restore the snapshot
    /self-optimize decide [path]    # apply/reject the decisions.json from the dashboard

Reports land in `<config-dir>/self-optimize/reports/` (override `report_dir` in
`<config-dir>/self-optimize/config.json` — reports contain redacted transcript excerpts,
so think before pointing this at a synced folder).

### Reviewing in the dashboard

Every full run also writes `<report_dir>/latest.html` and a per-run `<run-id>.html` — a
self-contained page you open straight from disk. For each tier-A finding pick Apply,
Reject, Amend, or Skip (Reject and Amend both need a reason); check any tier-B item you
want help with as "Select for assisted work". Amend rejects the finding in favor of a
different action: pick one of the suggested alternatives or paste your own replacement
action JSON. Either way it is re-validated and guarded exactly like any other action
before it is ever applied. Your choices persist locally as you go. When ready, click
**Download decisions.json** (or **Copy command** for a quick apply/reject one-liner —
amendments require Download, they cannot be expressed as a command), then run:

    /self-optimize decide

With no argument it picks up the newest `self-optimize-decisions-*.json` in `~/Downloads`;
pass a path to use a specific file. It applies and rejects your tier-A picks and lists the
tier-B items you selected for assisted work — those are never run automatically, you do
them together with the agent afterward. Each decide run logs what happened to
`<config-dir>/self-optimize/state/decisions/`.

### Eval gym

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

### Scoring a candidate

    python3 scripts/gym.py score --artifact skill:<name> --candidate <file> --state <config-dir>/self-optimize

A judge reads the candidate text plus one case and answers a fixed JSON verdict, and
the result is always two numbers: how many failure cases the candidate would have
prevented, and how many working cases it preserves. Below the floor it short-circuits
to `unscorable` without spending a single judge call, and cases that do not fit
`--max-budget` are skipped rather than truncated.

Both numbers, every time, and no auto-apply on either of them. A loop that optimizes
one number will find ways to game that number: Prime Intellect's
[prime-agent](https://github.com/PrimeIntellect-ai/prime-agent) rewrites its own
harness mid-session with no held-out validation, and in their Factorio case study the
refine loop climbed the production metric partly by discovering reward hacks — spawning
resources outright instead of building production. The two-sided score is the check on
a candidate that "fixes" complaints by breaking what already worked, and you ratifying
every apply is the check on the score itself.

### Judge backend

There is no default judge. `gym.judge.command` is empty until you set it, and scoring
refuses (with instructions) rather than falling back to anyone's API:

    "gym": {"judge": {"command": ["<your-cli>", "run", "--model", "{model}"],
                      "model": "<your-model-id>", "timeout_s": 120}}

The contract is deliberately small: your command reads the prompt on stdin and writes
`{"prevented": true|false}` (or `{"preserved": ...}`) on stdout. Anything CLI-invokable
fits — a local server wrapper, an open-weights model, an agent CLI. Swapping backends
is a config edit, never a code change. Prompts are built deterministically (fixed
sections, no timestamps), so identical inputs are reproducible and cacheable.

### Instances

The config dir is resolved per running instance: `$CLAUDE_CONFIG_DIR` if set, else
`~/.claude`. Separate work and personal instances are fully independent: install and run
`/self-optimize` inside each, and every instance keeps its own state, evidence, ledger, and
reports under `<config-dir>/self-optimize/`.

Exception: if a second config dir symlinks `projects/` and `self-optimize/` back to the
first, that is one corpus and one ledger. Run the loop once, from either instance.

### Config (`<config-dir>/self-optimize/config.json`, auto-created)

| key | default | meaning |
|---|---|---|
| report_dir | `<state>/reports` | where reports are written |
| project_include / project_exclude | `["*"]` / `[]` | fnmatch globs over project dir names |
| since_days | 30 | analysis window |
| sample_caps | 40 excerpts / 1500 tok / 60k total | bounds analyst input, and cost |
| max_budget_tokens | 400000 | default `--max-budget`; sample caps shrink to fit, and collect.py refuses (exit 2) runs too small to sample usefully |
| retain_runs | 10 | evidence dirs kept |
| verify.min_sessions / min_rel_change | 10 / 0.10 | verification floors |
| gym.min_cases_per_side | 3 | fewer cases than this on either side and the artifact is reported unscorable |
| gym.max_cases_per_side | 20 | per-artifact corpus cap; older cases age out FIFO |
| gym.retire_after_absent_runs | 3 | consecutive runs an artifact can be missing from the inventory before it is retired |
| gym.judge | `{"command": [], "model": "", "timeout_s": 120}` | the CLI that judges gym cases: reads a prompt on stdin, writes a JSON verdict on stdout, `{model}` substituted in any argument. Empty by default — no vendor, no fallback; scoring refuses until you set it |

### Cost

`--stats-only` uses zero LLM tokens. A full run's cost is bounded by the sample caps
(around 60k tokens of analyst input by default) plus the analyst outputs; the report
footer records actual analyst tokens per run alongside the verified savings they bought.

### Running it on a schedule

Not enabled by default. Schedule it locally with a plain crontab line, adjusting the time
and config dir for the instance you want optimized:

    0 9 * * MON  CLAUDE_CONFIG_DIR=$HOME/.claude claude -p "/self-optimize" >> $HOME/.claude/self-optimize/cron.log 2>&1

Claude Code's `/schedule` cloud routines are not suitable here — they execute in the cloud,
without access to this machine's transcripts and state. It is the same full run either way,
so review the report and apply or reject by hand. Nothing auto-applies.

## What it reads and writes

Reads `<config-dir>/projects/**/*.jsonl` locally, through a secret scrubber, plus
settings, plugin, skill, and agent metadata (settings `env` blocks are never read).
Writes only under `<config-dir>/self-optimize/` — plus, on apply, exactly the config files
a finding names, snapshotted first.
