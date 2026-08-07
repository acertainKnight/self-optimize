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
a papercut, has no producer yet — there is no file or transcript signal to
key off until that channel exists, so it isn't implemented here.

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
