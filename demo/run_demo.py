#!/usr/bin/env python3
"""Synthetic, offline demo of the self-optimize eval gym.

SYNTHETIC DEMO CONTENT. Every file this script reads under demo/ is fabricated
from scratch: no real sessions, users, machine paths, or projects appear
anywhere below. See demo/README.md.

What it does, using only the real scripts/gym.py and scripts/report.py code:
  1. loads the fabricated evidence pack in demo/evidence/ into a throwaway gym
     state dir -- the same shape scripts/collect.py would have produced.
  2. scores demo/candidates/known-good.md (the fix) and
     demo/candidates/known-bad.md (a deliberately degraded rewrite) against
     that corpus, using demo/judge_stub.py as the judge backend.
  3. runs the same bounded-edit + gate path a real evolver finding goes
     through, and renders the same markdown report a real run would produce.

No API key, no network call, no credentials: the judge is a stub script that
reads the prompt and answers deterministically. Exits non-zero if the known-
bad candidate does not score strictly worse than the known-good one on both
sides -- that is the property CI checks on every push (see .github/workflows).
"""
import json
import os
import sys
import tempfile
from pathlib import Path

DEMO_DIR = Path(__file__).resolve().parent
REPO_ROOT = DEMO_DIR.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))
import gym          # noqa: E402
import report        # noqa: E402
import schema as so_schema  # noqa: E402
import so_config      # noqa: E402

ARTIFACT_ID = "skill:widget-namer"
EVIDENCE_DIR = DEMO_DIR / "evidence"
SKILL_PATH = DEMO_DIR / "skill" / "SKILL.md"
GOOD_CANDIDATE = DEMO_DIR / "candidates" / "known-good.md"
BAD_CANDIDATE = DEMO_DIR / "candidates" / "known-bad.md"
JUDGE_SCRIPT = DEMO_DIR / "judge_stub.py"

# The bounded edit an evolver would propose for the planted flaw: applied to
# demo/skill/SKILL.md this produces demo/candidates/known-good.md exactly
# (asserted in tests/test_demo.py) -- the same "current text + ops" path
# gym.py's gate command scores a real finding on.
FIX_OPS = [{"op": "replace", "anchor": "- Join the words together into a key.",
            "text": "- Convert it to kebab-case: lowercase words joined with hyphens, "
                    "never snake_case or CamelCase.",
            "motivated_by": ["sample:demo-f1", "sample:demo-f2"]}]


def judge_cfg(state) -> dict:
    cfg = so_config.load_config(state)
    cfg["gym"]["judge"] = {"command": [sys.executable, str(JUDGE_SCRIPT)],
                           "model": "demo-stub-judge", "timeout_s": 30}
    return cfg


def build_findings() -> dict:
    """One accepted candidate (the fix, scored via the bounded-edit path) and
    one rejected candidate (a deliberately degraded full rewrite) -- the same
    finding shape scripts/synth.py emits for a real run."""
    fix = {"id": "demo-fix", "title": "Make widget-namer enforce kebab-case",
           "category": "skill-improve",
           "evidence_refs": [f"artifact:{ARTIFACT_ID}", "sample:demo-f1"],
           "impact": {"ordinal": "med"}, "risk": "low",
           "metric": {"key": "correction_rate", "direction": "down", "scope": "global"},
           "action": {"harness": "demo", "tier": "A", "type": "file_ops",
                      "payload": {"path": str(SKILL_PATH), "ops": FIX_OPS}}}
    degrade = {"id": "demo-degrade", "title": "(demo only) a deliberately worse rewrite",
               "category": "skill-improve",
               "evidence_refs": [f"artifact:{ARTIFACT_ID}", "sample:demo-f1"],
               "impact": {"ordinal": "med"}, "risk": "low",
               "metric": {"key": "correction_rate", "direction": "down", "scope": "global"},
               "action": {"harness": "demo", "tier": "B", "type": "file_replace",
                          "payload": {"op": "rewrite", "content": BAD_CANDIDATE.read_text()}}}
    for rec in (fix, degrade):
        errs = so_schema.validate_rec(rec)
        assert not errs, f"demo finding {rec['id']} fails schema.validate_rec: {errs}"
    return {"findings": [fix, degrade],
            "dropped": {"invalid": 0, "citations": 0, "guard": 0, "suppressed": []}}


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="so-demo-") as tmp:
        tmp = Path(tmp)
        state = tmp / "state"
        cfg = judge_cfg(state)

        print("== collect-equivalent load ==")
        summary = gym.accrue(state, [EVIDENCE_DIR], "demo-run", cfg)
        print(f"gym: registered={summary['registered']} artifact(s), "
              f"+{summary['added_failure']} failure case(s), "
              f"+{summary['added_working']} working case(s) "
              f"(fabricated, from demo/evidence/)\n")

        print("== gym scoring: known-good vs known-bad ==")
        good = gym.score_artifact(state, ARTIFACT_ID, GOOD_CANDIDATE.read_text(), cfg)
        bad = gym.score_artifact(state, ARTIFACT_ID, BAD_CANDIDATE.read_text(), cfg)
        print(f"known-good candidate (the fix):        {report._gym_line(good)}")
        print(f"known-bad candidate  (deliberately bad): {report._gym_line(bad)}\n")

        ordering_holds = (not good["unscorable"] and not bad["unscorable"]
                          and good["prevented"]["n"] > bad["prevented"]["n"]
                          and good["preserved"]["n"] > bad["preserved"]["n"])
        if ordering_holds:
            print("[ACCEPT] known-good would be accepted -- it prevents more failure "
                  "cases and preserves more working cases than known-bad.")
            print("[REJECT] known-bad would be rejected -- it scores strictly worse on "
                  "both sides.\n")
        else:
            print("[FAIL] the expected score ordering did not hold "
                  "(known-good should score strictly higher on both sides than known-bad)\n")

        print("== report generation (same render path a real run uses) ==")
        findings_doc = build_findings()
        findings_path = tmp / "findings.json"
        findings_path.write_text(json.dumps(findings_doc))
        gate_summary = gym.gate(state, findings_path, cfg, run_id="demo-run")
        usage = json.loads((EVIDENCE_DIR / "usage.json").read_text())
        md = report.render("demo-run", findings_doc["findings"], findings_doc["dropped"],
                           verify_rows=[], trend_rows=[], usage=usage,
                           footer={"analyst_tokens": None}, gym=gate_summary["scores"])
        print(md)

        summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
        if summary_path:
            with open(summary_path, "a") as f:
                f.write("## self-optimize demo run\n\n")
                f.write("| candidate | prevented | preserved | verdict |\n")
                f.write("|---|---|---|---|\n")
                f.write(f"| known-good (the fix) | {good['prevented']['n']}/{good['prevented']['of']} "
                        f"| {good['preserved']['n']}/{good['preserved']['of']} | ACCEPT |\n")
                f.write(f"| known-bad (deliberately degraded) | {bad['prevented']['n']}/{bad['prevented']['of']} "
                        f"| {bad['preserved']['n']}/{bad['preserved']['of']} | REJECT |\n\n")
                f.write(f"Score ordering holds (known-good beats known-bad on both sides): "
                        f"**{ordering_holds}**\n\n")
                f.write(md)

        return 0 if ordering_holds else 1


if __name__ == "__main__":
    raise SystemExit(main())
