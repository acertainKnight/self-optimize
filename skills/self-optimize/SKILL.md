---
name: self-optimize
description: Run the self-optimize loop — mine Claude Code transcripts + config, verify previously applied changes, and produce an evidence-cited optimization report. Also handles apply/reject/rollback subcommands.
disable-model-invocation: true
argument-hint: "[--stats-only] [--since YYYY-MM-DD] [--max-budget N] | apply <id[,id...]> | reject <id> [reason] | rollback <id> | decide [path]"
allowed-tools: Bash, Read, Write, Agent
---

# self-optimize runner

Definitions used below:
- `SCRIPTS` = `${CLAUDE_PLUGIN_ROOT}/scripts`
- `DATA_ROOT` = this instance's config dir: `${CLAUDE_CONFIG_DIR:-$HOME/.claude}` —
  NEVER assume `~/.claude`; separate work/personal instances have separate config dirs
  and are optimized independently.
- `STATE` = `DATA_ROOT/self-optimize`
- `RUN_ID` = today as YYYY-MM-DD; `EV` = `STATE/evidence/RUN_ID`

## Subcommands (run and stop)

If the arguments begin with `apply`, `reject`, `rollback`, or `decide`, run the matching
command, echo its output verbatim to the user, then regenerate the dashboard (see the
last bullet below), and stop:

- `apply <ids>` → `python3 SCRIPTS/apply.py apply --ids <ids> --state STATE --data-root DATA_ROOT --evidence <newest dir under STATE/evidence>`
- `reject <id> [reason]` → `python3 SCRIPTS/apply.py reject --id <id> --reason "<reason>" --state STATE`
- `rollback <id>` → `python3 SCRIPTS/apply.py rollback --id <id> --state STATE`
- `decide [path]` → `python3 SCRIPTS/apply.py decide [path] --state STATE --data-root DATA_ROOT --evidence <newest dir under STATE/evidence>`
  (with no path, it picks up the newest `self-optimize-decisions-*.json` from
  `~/Downloads`). Then present each listed assisted-work item to the user and
  implement together upon their approval (tier B stays human-supervised).
- After any of the four commands above: regenerate the dashboard so it reflects the
  new ledger state — `python3 SCRIPTS/dashboard.py --evidence <newest dir under
  STATE/evidence> --state STATE --run-id <that dir's name>` — and print the
  `DASHBOARD:` line plus an `open <path>` hint.

## Full run

1. **Collect (deterministic, no LLM):**
   `python3 SCRIPTS/collect.py --data-root DATA_ROOT --state STATE --out EV --run-id RUN_ID` (add `--since <ISO>` if the user passed `--since`, and `--max-budget <N>` if the user passed `--max-budget`; if it exits 2, the budget is too small to sample anything useful — show the refusal message and stop rather than retrying), then
   `python3 SCRIPTS/inventory.py --data-root DATA_ROOT --state STATE --out EV --rules ${CLAUDE_PLUGIN_ROOT}/adapters/claude_code/rules.json`, then
   `python3 SCRIPTS/gym.py update --evidence EV --state STATE --run-id RUN_ID` (derives
   the eval-gym registry from this run's inventory and accrues its graded cases; add a
   second `--evidence <dir>` for each additional harness pack collected by an adapter).
   Print all three summary lines. If `STATE/config.json` did not exist before this run, tell
   the user it was created with defaults, and summarize what was read (session count,
   window) before continuing.
2. **`--stats-only`?** Print the summaries and the EV path, remind the user this cost
   zero LLM tokens, and STOP.
3. **Verify previous applies:**
   `python3 SCRIPTS/verify.py --evidence EV --state STATE --out EV/verify.json` —
   skip silently if the script does not exist yet or the ledger has no applied entries.
4. **Analysts (the only LLM step):** data floors first — if collect reported fewer
   than 20 sessions, skip transcript-miner and tell the user why ("not enough data
   yet"); if inventory shows zero plugins and zero skills, skip config-auditor
   likewise; if `EV/artifacts.json` is missing or its `artifacts` array is empty, skip
   evolver ("no artifact activation data yet"). Then launch the surviving analysts in
   parallel with the Agent tool. They are plugin agents restricted to the Read tool and
   carry their own instructions — the invocation prompt is just the file list plus a
   standing constraints instruction. Each analyst reads ONLY its own copies under
   `EV/analysts/<name>/` (written by inventory.py) — never the canonical `EV/*.json`
   paths, so parallel analysts cannot interfere through shared-path session state:
   - `subagent_type: "self-optimize:transcript-miner"`, prompt: "Config dir: DATA_ROOT. Evidence files: <absolute paths of EV/analysts/miner/usage.json, EV/analysts/miner/samples.json, EV/analysts/miner/sessions.json, EV/analysts/miner/constraints.json>. Standing constraints — constraints.json lists ideas already rejected by this user; do not re-propose them absent new evidence. Return the JSON array."
   - `subagent_type: "self-optimize:config-auditor"`, prompt: "Config dir: DATA_ROOT. Evidence files: <absolute paths of EV/analysts/auditor/inventory.json, EV/analysts/auditor/activation.json, EV/analysts/auditor/usage.json, EV/analysts/auditor/constraints.json>. Rules file: EV/analysts/auditor/rules.json. Standing constraints — constraints.json lists ideas already rejected by this user; do not re-propose them absent new evidence. Return the JSON array."
   - `subagent_type: "self-optimize:evolver"`, prompt: "Config dir: DATA_ROOT. Evidence files: <absolute paths of EV/analysts/evolver/artifacts.json, EV/analysts/evolver/samples.json, EV/analysts/evolver/constraints.json>. Rules file: EV/analysts/evolver/rules.json. Standing constraints — constraints.json lists ideas already rejected by this user; do not re-propose them absent new evidence. Return the JSON array."
   - `subagent_type: "self-optimize:sample-labeler"` (skip when samples are empty), prompt: "Samples file: <absolute path of EV/analysts/labeler/samples.json>. Label every sample. Return the JSON array."
   Save each agent's final output verbatim with the Write tool to `EV/miner.json`,
   `EV/auditor.json`, (if evolver ran) `EV/evolver.json`, and (if the labeler ran)
   `EV/labels.json`, then run `chmod 600` on each file written. After saving
   labels.json, run `python3 SCRIPTS/labels.py --evidence EV --state STATE` and print
   its summary line (it validates the labels and records per-category correction
   counts in the metrics trend). Write the analysts' token counts (from the completion
   notifications, summed across retries per analyst) as a JSON object to
   `EV/analyst_tokens.json` (e.g. `{"miner": 8000, "auditor": 4300}`) — report.py
   reads it automatically.
5. **Synthesize:**
   **Deep localize (opt-in):** if `deep_localize.enabled` is true in config, run `python3 SCRIPTS/localize.py --evidence EV --data-root DATA_ROOT --state STATE --out EV/localize.json` (add `--max-budget <N>` if the user passed one) BEFORE the synth command below — it bisects the top-N friction sessions with the gym's judge backend and writes bracketed turn ranges + rationale that synth.py then attaches to any finding whose evidence touches that session, as supporting evidence never its own finding category; skip quietly (no file written) if it exits 2 for an unconfigured judge.
   `python3 SCRIPTS/synth.py --evidence EV --data-root DATA_ROOT --state STATE --rules ${CLAUDE_PLUGIN_ROOT}/adapters/claude_code/rules.json --analyst EV/miner.json --analyst EV/auditor.json [--analyst EV/evolver.json] --out EV/findings.json`
   (include `--analyst EV/evolver.json` only if evolver ran in step 4.)
   **Citation retry (at most ONE round):** if the synth summary shows
   `dropped_citations > 0`, read `EV/findings.json`'s `dropped.citation_detail` — each
   entry names the analyst, the finding title, and exactly which refs failed. For each
   affected analyst, message that same agent (do not spawn a new one): include its
   dropped findings verbatim, the failed refs, and this instruction: "These findings
   were dropped because the marked evidence_refs do not resolve. Re-read your evidence
   files' actual ids and fix ONLY the evidence_refs — do not add findings, change
   payloads, or re-litigate content. If no valid ref exists for a claim, drop that
   finding. Return the corrected JSON array of just these findings." Save each reply to
   `EV/<analyst>-retry.json` (chmod 600) and re-run the same synth command with the
   retry files as additional `--analyst` args. If citations still drop after the retry,
   proceed — never loop again.
   **Shadow eval (evolver rewrites only):** for each finding in `EV/findings.json`
   with category "skill-improve" whose action carries a full rewrite or diff, and
   whose evidence_refs include `sample:` refs: for each such sample, spawn one judge
   agent (`model: haiku`, general-purpose, no tools needed) with the proposed
   artifact text (from the finding payload) and that sample's `user_text` +
   `prior_assistant_text` from EV/samples.json, asking exactly: "If the assistant
   had been operating under this artifact text, would this correction still have
   been needed? Answer with JSON {\"prevented\": true|false} only — prevented=true
   means the rewrite addresses what the user corrected." Tally per finding and Write
   `EV/shadow.json` as {"<finding-id>": {"prevented": <n>, "total": <m>}, ...},
   chmod 600. The score renders on the finding in the report — it is judged
   evidence for the human decision, not an auto-gate. Skip the whole step when no
   finding qualifies.
   **Gym score (evolver rewrites only, judged):** for each finding whose action carries
   a full rewrite of a skill or agent, resolve its gym id — the `artifacts.json` id
   without the `artifact:` prefix, so `artifact:skill:foo` is `skill:foo` — and check
   `python3 SCRIPTS/gym.py status --state STATE --json` for that id. Skip any artifact
   whose row is not `scorable` (say which reason it gave). For the rest, Write the
   proposed artifact text to a temp file and run
   `python3 SCRIPTS/gym.py score --artifact <gym id> --candidate <temp file> --state STATE --out EV/score-<finding-id>.json`
   (add `--max-budget <N>` if the user passed one). If it exits 2 because no judge
   backend is configured, skip the whole gym step and tell the user once that
   `gym.judge.command` is unset in `STATE/config.json` — this plugin ships no default
   judge, so it will not pick a backend or model on their behalf. Merge the score files
   into `EV/gym.json` as `{"<finding-id>": <score object>, ...}` and chmod 600. Both
   numbers render on the finding: prevented failure cases AND preserved working cases,
   evidence for the human decision, never an auto-gate.
6. **Report:**
   `python3 SCRIPTS/report.py --evidence EV --state STATE --run-id RUN_ID`
   (analyst tokens come from `EV/analyst_tokens.json` written in step 4; the
   `--analyst-tokens miner=<n>,auditor=<m>` flag still works as an override)
   Show the user: the report path, the outcomes section verdicts if any, and the
   printed top-5 with their `/self-optimize apply <id>` / `reject <id>` hints. Then run
   `python3 SCRIPTS/dashboard.py --evidence EV --state STATE --run-id RUN_ID` and print
   the `DASHBOARD:` line plus an `open <path>` hint.

Rules for the runner: never read raw transcripts yourself — everything flows through
the evidence pack; do not exceed the four analyst agents (miner, auditor, evolver,
sample-labeler) plus their citation-retry and shadow-eval judge calls; if any script
exits non-zero, show the error and stop rather than improvising.
