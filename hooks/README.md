This directory holds two hooks. Both ship inert — nothing in the plugin registers
either one, and installing self-optimize never turns them on.

- `capture_trigger.py` — a **Stop** hook that queues skill-worthy moments for the
  next run (below).
- `enforce.py` — a **PreToolUse** check proposed by the loop and installed by you
  ([Enforcement checks](#enforcement-checks-opt-in-off-by-default)).

# Inline trigger capture (opt-in, off by default)

`capture_trigger.py` is a Claude Code **Stop** hook. It looks at the turn that
just finished and, if it matches one of a small set of cheap patterns, appends
one line to `<config-dir>/self-optimize/capture-queue.jsonl`. The next
`/self-optimize` run reads that queue and samples those sessions first, so
skill-worthy moments get picked up instead of relying on the batch miner to
rediscover them later purely by chance.

Nothing in this plugin registers the hook. Installing or enabling
self-optimize never turns it on — you opt in by hand, in your own
`settings.json`, exactly like any other hook you'd add yourself.

## What it detects

All detection is pattern/state-based over the transcript file Claude Code
already writes — no LLM calls, and it only runs the same lightweight
correction-regex match `collect.py` already uses:

- **explicit user correction** — the next user message matches the same
  correction pattern `collect.py` uses for its correction samples (`"no"`,
  `"actually"`, `"that's wrong"`, and so on).
- **dead-end-then-working-path** — a tool call fails, and the turn goes on to
  make more tool calls anyway (as opposed to a failure the agent just gives
  up on).
- **task completed after 5+ tool calls** — the turn used five or more tool
  calls before stopping, a decent proxy for "this was a real multi-step
  workflow."

A fourth trigger from the original spec, an agent noticing and self-reporting
a papercut, is deliberately not a Stop-hook trigger here: it is its own direct
channel (a standing instruction, no hook required) documented in README.md's
Papercut channel section, and `collect.py` reads it independently of this
queue.

## Why Stop, not SessionEnd

- **Timing matches the point of this feature.** The whole idea is to capture
  a moment when it happens, not to re-derive it from a full transcript after
  the fact — that's what the existing batch miner already does. `SessionEnd`
  fires once, at the very end, which is really just a smaller batch job.
  `Stop` fires after every completed turn, so a correction or a 5-tool-call
  turn gets queued right after it happens.
- **Reliability.** `Stop` is part of the normal agentic loop — it fires for
  every turn that finishes. `SessionEnd` is documented as not firing on an
  abrupt exit (a killed terminal, a crashed process, a dropped connection),
  and since it only fires once, missing it loses the *entire* session's
  signal. With `Stop`, an abrupt exit only loses whatever hadn't happened
  yet — everything already queued from earlier turns in that session is
  safe.
- **Cost stays bounded even though it fires often.** Each invocation only
  reads the bytes appended to the transcript since the previous `Stop` (a
  tiny per-session cursor file records the byte offset), so a hook that fires
  on every turn still does O(one turn) of work per call, not O(the whole
  session so far).

## Privacy

A queue line is `{ts, harness, session_id, trigger, artifact_hint?}` — never
transcript text, tool arguments, file contents, or secrets. `artifact_hint`,
when present, is either a tool-call count (`"7_tool_calls"`) or the short,
fixed keyword the correction regex matched (`"no"`, `"actually"`, ...) — it
never contains anything the user or the model wrote freely.

Each session gets at most 5 queued lines total, then the hook stops doing any
work for that session (no more transcript reads, not just no more writes).

## Opting in

Add this to your own `settings.json` (merge into an existing `"hooks"` key if
you have one):

```json
{
  "hooks": {
    "Stop": [
      {
        "matcher": "*",
        "hooks": [
          {
            "type": "command",
            "command": "python3 ${CLAUDE_PLUGIN_ROOT}/hooks/capture_trigger.py",
            "timeout": 5
          }
        ]
      }
    ]
  }
}
```

`${CLAUDE_PLUGIN_ROOT}` resolves automatically when a hook is registered
through a plugin's own hook manifest. Pasted directly into your personal
`settings.json` like this, it isn't — replace it with the absolute path to
wherever this plugin is checked out on your machine, e.g.
`python3 /path/to/self-optimize/hooks/capture_trigger.py`.

Restart Claude Code for the change to take effect (hooks load at session
start).

## Uninstalling

Remove the block above from `settings.json` and restart. That's the whole
uninstall — the hook only ever appends to `capture-queue.jsonl` and writes
its own small per-session cursor files under
`<config-dir>/self-optimize/state/capture-cursor/`; nothing else reads or
depends on it running. `collect.py` treats a missing or empty queue exactly
like it always has, so removing the hook is a clean, total no-op on the rest
of the pipeline.

# Enforcement checks (opt-in, off by default)

`enforce.py` is a Claude Code **PreToolUse** hook. It reads the tool call about
to run, applies one named rule with the parameters given on its command line,
and exits 2 — the code that blocks the call and hands the reason back to the
model — when the call breaks that rule.

The rules are a fixed table (`scripts/enforcement.py`), and the logic is this
file. That split is the point: when the loop proposes a check, the analyst
supplies a rule name and its parameters, never a command, so no text mined out
of a transcript can end up in a shell.

- `forbid_bash_substring` (`value`) — block any Bash command containing that
  literal text.
- `require_bash_flag` (`program`, `flag`) — every invocation of that program
  must carry that flag.
- `forbid_write_path` (`prefix`) — no `Write`/`Edit`/`NotebookEdit` under that
  path.

## Where a proposal comes from

A correction you have had to give in two or more separate sessions is a
preference the model keeps failing to read, and writing it down again just
produces more prose to ignore. When the corrected behavior has a mechanically
checkable shape, the miner proposes it as a check instead, and the report prints
the settings.json block to paste. Every proposal also states which correction
category it expects to fall, and `verify.py` scores that prediction against the
labeled correction counts on later runs — so a check that changes nothing is
visible rather than quietly permanent.

These proposals are permanently tier B. `apply.py` refuses to install one no
matter what its evidence looks like: a loop that can install its own checks can
install a check that silences its own evidence. You paste the block yourself,
then record it so the prediction can be scored:

    /self-optimize adopt <finding-id>

## Uninstalling

Remove the block from `settings.json` and restart. Nothing else depends on the
check running; the ledger entry stays, and its prediction simply stops moving.
