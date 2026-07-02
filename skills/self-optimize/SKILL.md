---
name: self-optimize
description: Run the self-optimize loop — mine Claude Code transcripts + config, verify previously applied changes, and produce an evidence-cited optimization report. Also handles apply/reject/rollback subcommands.
disable-model-invocation: true
argument-hint: "[--stats-only] [--since YYYY-MM-DD] [--max-budget N] | apply <id[,id...]> | reject <id> [reason] | rollback <id>"
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

If the arguments begin with `apply`, `reject`, or `rollback`, run the matching command,
echo its output verbatim to the user, and stop:

- `apply <ids>` → `python3 SCRIPTS/apply.py apply --ids <ids> --state STATE --data-root DATA_ROOT --evidence <newest dir under STATE/evidence>`
- `reject <id> [reason]` → `python3 SCRIPTS/apply.py reject --id <id> --reason "<reason>" --state STATE`
- `rollback <id>` → `python3 SCRIPTS/apply.py rollback --id <id> --state STATE`

## Full run

1. **Collect (deterministic, no LLM):**
   `python3 SCRIPTS/collect.py --data-root DATA_ROOT --state STATE --out EV --run-id RUN_ID` (add `--since <ISO>` if the user passed `--since`, and `--max-budget <N>` if the user passed `--max-budget`; if it exits 2, the budget is too small to sample anything useful — show the refusal message and stop rather than retrying), then
   `python3 SCRIPTS/inventory.py --data-root DATA_ROOT --state STATE --out EV`.
   Print both summary lines. If `STATE/config.json` did not exist before this run, tell
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
   standing constraints instruction:
   - `subagent_type: "self-optimize:transcript-miner"`, prompt: "Config dir: DATA_ROOT. Evidence files: <absolute paths of EV/usage.json, EV/samples.json, EV/sessions.json, EV/constraints.json>. Standing constraints — EV/constraints.json lists ideas already rejected by this user; do not re-propose them absent new evidence. Return the JSON array."
   - `subagent_type: "self-optimize:config-auditor"`, prompt: "Config dir: DATA_ROOT. Evidence files: <absolute paths of EV/inventory.json, EV/activation.json, EV/usage.json, EV/constraints.json>. Rules file: ${CLAUDE_PLUGIN_ROOT}/adapters/claude_code/rules.json. Standing constraints — EV/constraints.json lists ideas already rejected by this user; do not re-propose them absent new evidence. Return the JSON array."
   - `subagent_type: "self-optimize:evolver"`, prompt: "Config dir: DATA_ROOT. Evidence files: <absolute paths of EV/artifacts.json, EV/samples.json, EV/constraints.json>. Rules file: ${CLAUDE_PLUGIN_ROOT}/adapters/claude_code/rules.json. Standing constraints — EV/constraints.json lists ideas already rejected by this user; do not re-propose them absent new evidence. Return the JSON array."
   Save each agent's final output verbatim with the Write tool to `EV/miner.json`,
   `EV/auditor.json`, and (if evolver ran) `EV/evolver.json`, then run `chmod 600` on
   each file written. Note the total tokens the analysts used if visible.
5. **Synthesize:**
   `python3 SCRIPTS/synth.py --evidence EV --data-root DATA_ROOT --state STATE --rules ${CLAUDE_PLUGIN_ROOT}/adapters/claude_code/rules.json --analyst EV/miner.json --analyst EV/auditor.json [--analyst EV/evolver.json] --out EV/findings.json`
   (include `--analyst EV/evolver.json` only if evolver ran in step 4.)
6. **Report:**
   `python3 SCRIPTS/report.py --evidence EV --state STATE --run-id RUN_ID [--analyst-tokens miner=<n>,auditor=<m>]`
   (build the value from the tokens noted in step 4, e.g. `miner=8000,auditor=4300`)
   Show the user: the report path, the outcomes section verdicts if any, and the
   printed top-5 with their `/self-optimize apply <id>` / `reject <id>` hints.

Rules for the runner: never read raw transcripts yourself — everything flows through
the evidence pack; do not exceed the two analyst agents; if any script exits non-zero,
show the error and stop rather than improvising.
