#!/usr/bin/env python3
"""Synthetic demo judge -- NOT a real evaluator, NOT calling any API or model.

Reads one gym scoring prompt on stdin (see scripts/gym.py:build_prompt) and
writes a JSON verdict on stdout. The rule: does the candidate artifact text in
the prompt still contain "kebab-case"? That is the one fix the planted flaw in
demo/skill/SKILL.md needs, so a candidate that keeps the rule scores well and
a candidate that drops it scores badly -- deterministically, offline, with no
network call and no credentials. Same shape as the STUB_JUDGE used by
tests/test_gym.py.
"""
import json
import sys

prompt = sys.stdin.read()
side = [ln for ln in prompt.splitlines() if ln.startswith("CASE SIDE: ")][0].split(": ", 1)[1]
_, _, rest = prompt.partition("--- CANDIDATE ARTIFACT TEXT ---")
candidate, _, _ = rest.partition("--- END CANDIDATE ARTIFACT TEXT ---")
verdict = "kebab-case" in candidate
key = "prevented" if side == "failure" else "preserved"
sys.stdout.write(json.dumps({key: verdict}))
