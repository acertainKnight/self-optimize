#!/usr/bin/env python3
"""PreToolUse check installed by hand from a self-optimize enforcement proposal.

Reads the hook payload on stdin, applies ONE named rule with the parameters
given in argv, and exits 2 — the code that blocks the call, with the reason on
stderr — when the call violates it. The rule set here is the implementation of
the table in `scripts/enforcement.py`: a proposal names a rule and its
parameters, and this file is the only place the check's logic lives, so no
analyst text ever reaches a shell.

Nothing installs this. `self-optimize` proposes the settings.json block; you
paste it yourself, exactly like the capture hook (see hooks/README.md).

    python3 hooks/enforce.py --rule forbid_bash_substring --arg 'value=rm -rf /'

Exit codes: 0 allow, 2 block, 1 misconfigured (surfaced to you, never blocking —
a check with a typo in it must not wedge every tool call in the session).
"""
import argparse
import json
import os
import re
import sys

SEGMENTS = re.compile(r"[;&|\n]+")


def _norm(p: str) -> str:
    return os.path.normpath(os.path.expanduser(p))


def _bash_segments(command: str) -> list:
    """Each `;`/`&&`/`|`-separated piece of a compound command, so a required
    flag is checked on the invocation that actually runs the program rather than
    on the whole line."""
    return [s.strip() for s in SEGMENTS.split(command) if s.strip()]


def forbid_bash_substring(params: dict, tool_name: str, tool_input: dict):
    if tool_name != "Bash":
        return None
    value = params["value"]
    if value in str(tool_input.get("command") or ""):
        return f"this command contains {value!r}, which you have been corrected on before"
    return None


def require_bash_flag(params: dict, tool_name: str, tool_input: dict):
    if tool_name != "Bash":
        return None
    program, flag = params["program"], params["flag"]
    for segment in _bash_segments(str(tool_input.get("command") or "")):
        tokens = segment.split()
        if tokens and tokens[0] == program and flag not in tokens:
            return f"{program} must be run with {flag}"
    return None


def forbid_write_path(params: dict, tool_name: str, tool_input: dict):
    if tool_name not in ("Write", "Edit", "NotebookEdit"):
        return None
    target = tool_input.get("file_path") or tool_input.get("notebook_path") or ""
    if not target:
        return None
    base, path = _norm(params["prefix"]), _norm(str(target))
    if path == base or path.startswith(base + os.sep):
        return f"writes under {params['prefix']} are not allowed"
    return None


CHECKS = {"forbid_bash_substring": forbid_bash_substring,
          "require_bash_flag": require_bash_flag,
          "forbid_write_path": forbid_write_path}


def check(rule: str, params: dict, tool_name: str, tool_input: dict):
    """The violation reason, or None when the call is fine."""
    return CHECKS[rule](params, tool_name, tool_input)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rule", required=True)
    ap.add_argument("--arg", action="append", default=[])
    a = ap.parse_args(argv)
    if a.rule not in CHECKS:
        print(f"enforce.py: unknown rule {a.rule!r}", file=sys.stderr)
        return 1
    params = dict(kv.split("=", 1) for kv in a.arg if "=" in kv)
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except json.JSONDecodeError:
        return 0
    tool_input = payload.get("tool_input")
    try:
        reason = check(a.rule, params, str(payload.get("tool_name") or ""),
                       tool_input if isinstance(tool_input, dict) else {})
    except KeyError as err:
        print(f"enforce.py: rule {a.rule} is missing parameter {err}", file=sys.stderr)
        return 1
    if reason:
        print(f"Blocked by a self-optimize enforcement check: {reason}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
