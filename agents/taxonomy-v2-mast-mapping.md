# Correction taxonomy v2: MAST mapping

The sample-labeler's category vocabulary (`scripts/labels.py::CATEGORIES`, mirrored
in `agents/sample-labeler.md`) is adapted from MAST: Cemri, Pan, Yang, Agrawal,
Chopra, Tiwari, Keutzer, Parameswaran, Klein, Ramchandran, Zaharia, Gonzalez, and
Stoica, "Why Do Multi-Agent LLM Systems Fail?", arXiv:2503.13657. MAST is a
validated taxonomy of 14 failure modes in 3 groups, built from 1600+ annotated
multi-agent traces across 7 frameworks, with Cohen's Kappa 0.88 between human
annotators on mode assignment.

## Why adapt a multi-agent taxonomy for a single agent

The self-optimize labeler categorizes corrections a user makes to a single coding
agent, not failures inside a multi-agent system. MAST's three groups still apply,
but its middle group — "inter-agent misalignment", failures in how agents in a
system coordinate with each other — needs a substitution to mean something for one
agent. Here, the agent's own stated plan, the tools and subagents it calls, and the
user it's working with take the place of MAST's "other agents": the same failure
(ignoring another party's input, silently changing course, doing something other
than what you said you'd do) still occurs, just among a different cast.

## Mode-by-mode mapping

| MAST group | MAST mode | MAST definition | v2 category | Single-agent reading |
|---|---|---|---|---|
| System design issues | FM-1.1 Disobey task specification | Failure to adhere to the specified constraints or requirements of a given task | `spec-violation` | Ignored an explicit constraint, requirement, or target the task specified |
| System design issues | FM-1.2 Disobey role specification | Failure to adhere to the defined responsibilities and constraints of an assigned role | `role-violation` | Broke a defined persona, mode, house style, or tool-access limit |
| System design issues | FM-1.3 Step repetition | Unnecessary reiteration of previously completed steps | `step-repetition` | Redid work already completed (reread a file, reran a command, restated an explanation) |
| System design issues | FM-1.4 Loss of conversation history | Unexpected context truncation, disregarding recent interaction history | `context-loss` | Dropped or contradicted earlier context or instructions from the same session |
| System design issues | FM-1.5 Unaware of termination conditions | Lack of recognition of criteria that should trigger the interaction's end | `no-stop-condition` | Kept going, or stopped, without recognizing the condition that should have decided that; includes padded or repetitive output |
| Inter-agent misalignment | FM-2.1 Conversation reset | Unexpected or unwarranted restarting of a dialogue, losing context and progress | `plan-reset` | Abandoned or restarted an in-progress approach without carrying forward prior progress |
| Inter-agent misalignment | FM-2.2 Fail to ask for clarification | Inability to request additional information when facing unclear or incomplete data | `no-clarification` | Proceeded on an ambiguous request, an invented requirement, or a plan the user expected to approve, instead of asking or waiting |
| Inter-agent misalignment | FM-2.3 Task derailment | Deviation from the intended objective or focus of a task | `scope-creep` | Did more than asked (extra files, side quests, unrequested features) |
| Inter-agent misalignment | FM-2.4 Information withholding | Failure to share data or insights that could affect another agent's decisions | `info-withholding` | Took an action or reached a conclusion without surfacing it to the user |
| Inter-agent misalignment | FM-2.5 Ignored other agent's input | Disregarding input another agent provided | `ignored-input` | Disregarded the user's stated preference, a subagent's finding, or a tool's output |
| Inter-agent misalignment | FM-2.6 Reasoning-action mismatch | A gap between the agent's stated reasoning and the action it actually took | `reasoning-action-mismatch` | Did something other than what its own stated plan or reasoning said it would do |
| Task verification | FM-3.1 Premature termination | Ending a dialogue or task before all necessary work is done | `premature-termination` | Ended the task or turn before the work was actually complete |
| Task verification | FM-3.2 No or incomplete verification | Omission of proper checking or confirmation of task outcomes | `no-verification` | Didn't check the change (no test run, no gate, no read-back) |
| Task verification | FM-3.3 Incorrect verification | Failure to adequately validate or cross-check crucial decisions | `incorrect-verification` | Checked, but the check was wrong or insufficient, and reported success anyway |
| — (not in MAST) | — | MAST's taxonomy was built to be exhaustive over its own annotated trace set | `other` | Real correction that fits none of the above; retained from v1 as a catch-all for corrections MAST's coordination/verification-oriented modes don't cover (e.g. pure taste calls) |

## v1 -> v2 migration

`scripts/labels.py::LEGACY_CATEGORY_MAP` is the static table `scripts/labels.py::migrate_categories`
applies to fold a `corrections_by_category` dict from the old 8-category vocabulary
onto the new one. `scripts/report.py` runs every trend row through it before
building the report's "Correction categories" line, so a run straddling the
migration reads as continuous history rather than the trend resetting to zero.
Several old categories collapse onto the same new one where MAST doesn't draw the
distinction the old ad hoc set implied it did.

| v1 category | v1 definition | v2 category | Rationale |
|---|---|---|---|
| `scope-creep` | Assistant did more than asked | `scope-creep` | Same concept; kept the name, now grounded in MAST FM-2.3 |
| `wrong-target` | Right kind of action, wrong file/function/object | `spec-violation` | Acting on the wrong target is failing to follow what the task specified (FM-1.1) |
| `over-engineering` | Solution more complex than the ask warranted | `spec-violation` | Exceeding the task's implicit simplicity constraint is a specification miss (FM-1.1) |
| `verbosity` | Output too long, repetitive, or padded | `no-stop-condition` | Not recognizing where output should have ended is FM-1.5 |
| `style` | Formatting, tone, naming, or convention misses | `role-violation` | House style and conventions are part of the agent's role contract (FM-1.2) |
| `wrong-assumption` | Assistant misread intent or invented a requirement | `no-clarification` | Should have asked instead of assuming (FM-2.2) |
| `premature-action` | Acted before confirming a plan the user expected to approve | `no-clarification` | Should have asked/waited before acting (FM-2.2); collapses with `wrong-assumption` above |
| `other` | Real correction that fits none of the above | `other` | Unchanged |

Categories with no v1 equivalent (`step-repetition`, `context-loss`, `plan-reset`,
`info-withholding`, `ignored-input`, `reasoning-action-mismatch`,
`premature-termination`, `no-verification`, `incorrect-verification`) start at zero
in migrated history — the old taxonomy had no way to distinguish them, most notably
the entire task-verification group, which v1 couldn't represent at all.
