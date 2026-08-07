# Demo: the eval gym, end to end, on fabricated data

SYNTHETIC DEMO CONTENT. Everything under `demo/` is fabricated from scratch for
this demo: an invented product ("Widgetforge"), an invented skill
(`widget-namer`), and invented corrections. Nothing here is a real session,
user, or project. The real gym corpus holds real transcript excerpts and never
enters this repo -- see `docs/evidence-schema.md`.

## Run it

    ./demo/run.sh

Offline, no API key, no network call, no credentials. It prints the accept/
reject contrast and exits non-zero if the expected score ordering does not
hold (that is also what CI checks on every push).

## What it demonstrates

- `demo/evidence/` is a fabricated evidence pack in the exact shape
  `scripts/collect.py` would produce (`docs/evidence-schema.md`): an inventory
  naming one skill, four sessions where a correction followed
  (`demo/evidence/samples.json`), and four where nothing needed correcting
  (`demo/evidence/working.json`).
- `demo/skill/SKILL.md` is the current, flawed version of that skill: it never
  says which casing convention to use for a generated config key, so the
  demo corpus is full of corrections about casing.
- `demo/candidates/known-good.md` is the fix: it states the casing rule
  (`kebab-case`) explicitly. It is also exactly what applying the bounded edit
  in `demo/run_demo.py`'s `FIX_OPS` to `demo/skill/SKILL.md` produces --
  `tests/test_demo.py` asserts the two are byte-identical.
- `demo/candidates/known-bad.md` is a deliberately degraded rewrite: it drops
  even the casing guidance the original had.
- `demo/judge_stub.py` is the judge backend for the demo: it reads a scoring
  prompt on stdin and answers `{"prevented": true/false}` (or `"preserved"`)
  based on whether the candidate text still contains `kebab-case` -- the same
  shape as the `STUB_JUDGE` used in `tests/test_gym.py`, but standalone so
  `scripts/gym.py` can invoke it as a real subprocess.
- `demo/run_demo.py` loads the evidence pack, scores both candidates,
  builds one accepted finding (the fix, via the bounded-edit/gate path) and
  one rejected finding (the degraded rewrite), and renders the same markdown
  report `scripts/report.py` produces for a real run.

## Dual purpose

`tests/test_demo.py` scores the same `demo/evidence/`, `demo/skill/SKILL.md`,
and `demo/candidates/*.md` fixtures as part of the regular test suite
(`python3 -m unittest discover -s tests`), asserting the known-good candidate
scores strictly higher than known-bad on both sides. That is the planted-
regression property the CI benchmark also checks (see the repo root README).
