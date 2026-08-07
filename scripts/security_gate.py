"""Security gates on generated skill/hook content: a 2026 survey of 42k community
skills found 26.1% carried a detectable vulnerability, script-bundling skills 2.12x
riskier than instruction-only ones (arXiv 2602.12430). G1/G2 apply that here, to
this plugin's own analyst output, before it can be applied.

G1 (deterministic): a pattern scan for network egress, credential-file reads,
destructive commands, and encoded payloads. G1 is the actual gate — any finding
that fails it is downgraded from tier A to tier B in findings.json, the same
fail-closed mechanism gym.gate uses for a candidate it could not score.

G2 (LLM, pluggable backend): one call asking whether the finding's declared purpose
(its title) matches what the content actually does. G2 reuses gym._invoke_judge —
the same subprocess judge driver gym.py and localize.py already share, configured
via gym.judge.command in config.json. There is no default backend, model, or
vendor here either: an unconfigured judge just means G2 is skipped, recorded as
such on the finding, while G1 still runs in full. G2 is judged evidence rendered
on the finding, like the gym score and shadow eval — never an auto-gate.

Both gates run ONLY on a finding whose payload adds or edits executable content:
a fenced script block (bash/sh/python/...) in artifact text being written, or any
finding in category "hooks" (config-auditor's hook proposals carry the command
spec as free-form prose in payload.description, so every one is in scope). An
instruction-only edit — a setting change, a frontmatter tweak, a CLAUDE.md prose
diff with no code fence — never reaches either gate: zero pattern-scan calls,
zero judge calls.
"""
import argparse
import json
import re
import sys
from pathlib import Path

import gym

EXEC_LANGS = {"bash", "sh", "shell", "zsh", "python", "python3", "ruby", "perl",
              "js", "javascript", "node", "powershell", "ps1"}
_FENCE_RE = re.compile(r"```([a-zA-Z0-9_+-]*)\n(.*?)```", re.S)

# category, compiled pattern. First match per category wins; scan_g1 collects one
# hit per pattern that matches, so a snippet can trip more than one category.
G1_PATTERNS = [
    ("network-egress", re.compile(r"\b(curl|wget)\b[^\n|]*\|\s*(sudo\s+)?(sh|bash|zsh)\b", re.I)),
    ("network-egress", re.compile(r"\bnc\s+-e\b|/dev/tcp/\d", re.I)),
    ("credential-read", re.compile(
        r"\.ssh/id_(rsa|dsa|ecdsa|ed25519)\b|\.aws/credentials\b|\.netrc\b|"
        r"/etc/shadow\b|\.kube/config\b|\.docker/config\.json\b|\.gnupg\b", re.I)),
    ("destructive", re.compile(r"\brm\s+-(?=\w*r)(?=\w*f)\w+\s+(/|~|\$HOME)(?:\s|$)", re.I)),
    ("destructive", re.compile(r"\bmkfs\.\w+\b|\bdd\s+if=\S+\s+of=/dev/(sd|nvme|hd)", re.I)),
    ("encoded-payload", re.compile(r"base64\s+(-d|--decode)\b[^\n]*\|\s*(sh|bash)\b", re.I)),
    ("encoded-payload", re.compile(r"\b(exec|eval)\s*\(.{0,120}?b64decode", re.I)),
]


def _code_blocks(text: str) -> list:
    return [body for lang, body in _FENCE_RE.findall(text or "")
            if lang.strip().lower() in EXEC_LANGS]


def _flatten_strings(obj, depth=3) -> list:
    if depth < 0:
        return []
    if isinstance(obj, str):
        return [obj]
    if isinstance(obj, dict):
        return [s for v in obj.values() for s in _flatten_strings(v, depth - 1)]
    if isinstance(obj, list):
        return [s for v in obj for s in _flatten_strings(v, depth - 1)]
    return []


def new_text(action: dict) -> str:
    """The text a payload actually adds or changes, per action type — what a human
    would newly read if this finding were applied. Instruction-only action types
    (setting_change, frontmatter_edit's single key/value) never carry a script, so
    they fall through to an empty string here."""
    t = action.get("type")
    p = action.get("payload") or {}
    if t == "file_ops":
        ops = p.get("ops") or []
        return "\n".join(str(op.get("text") or "") for op in ops if isinstance(op, dict))
    if t in ("file_create", "file_replace"):
        return str(p.get("content") or "")
    if t == "diff":
        added = [ln[1:] for ln in str(p.get("diff") or "").split("\n")
                 if ln.startswith("+") and not ln.startswith("+++")]
        return "\n".join(added)
    return ""


def carries_executable_content(rec: dict) -> bool:
    """G1/G2 spec: 'any finding whose payload adds/edits executable content
    (scripts, hook commands)'. Category "hooks" is always in scope — a hook
    proposal's payload.description IS the command spec (see
    agents/config-auditor.md), free-form prose rather than a fenced code block.
    Every other category is judged by its actual new/changed text: a fenced
    script block counts, plain instruction prose does not."""
    if rec.get("category") == "hooks":
        return True
    return bool(_code_blocks(new_text(rec.get("action") or {})))


def scan_text(rec: dict) -> str:
    """What G1/G2 actually read for this finding — never the whole payload, just
    the part that could execute."""
    action = rec.get("action") or {}
    if rec.get("category") == "hooks":
        return "\n".join(_flatten_strings(action.get("payload") or {}))
    return "\n".join(_code_blocks(new_text(action)))


def scan_g1(text: str) -> list:
    hits = []
    for category, pattern in G1_PATTERNS:
        m = pattern.search(text or "")
        if m:
            hits.append({"category": category, "snippet": m.group(0)[:120]})
    return hits


def build_g2_prompt(rec: dict, content: str) -> str:
    """Deterministic by construction, same as gym.build_prompt: fixed sections,
    fixed order, no timestamps or run ids."""
    purpose = str(rec.get("title", "")).strip()
    head = ["You are checking whether one generated piece of agent-harness content",
            "matches its declared purpose. Answer with JSON only, no other output.",
            "",
            f"DECLARED PURPOSE: {purpose}",
            "",
            "--- CONTENT (script or hook command this finding would add or edit) ---",
            content.strip(),
            "--- END CONTENT ---", "",
            "QUESTION: does the content actually do what the declared purpose says, with",
            "no unrelated or hidden behavior?",
            'Reply with exactly {"match": true} if the content matches its declared',
            'purpose, or {"match": false, "reason": "<one sentence>"} if it does something',
            "else too."]
    return "\n".join(head) + "\n"


def parse_g2_verdict(stdout: str) -> dict:
    """Same tolerance as gym.parse_verdict: a bare JSON object, or the first one
    embedded in chatter."""
    obj = None
    try:
        obj = json.loads(stdout)
    except json.JSONDecodeError:
        m = re.search(r"\{[^{}]*\}", stdout, re.S)
        if m:
            try:
                obj = json.loads(m.group(0))
            except json.JSONDecodeError:
                obj = None
    if not isinstance(obj, dict) or not isinstance(obj.get("match"), bool):
        raise gym.JudgeError(f"judge returned no 'match' verdict: {stdout.strip()[:200]!r}")
    return {"scanned": True, "verdict": "match" if obj["match"] else "mismatch",
            "reason": str(obj.get("reason", ""))[:400]}


def run_g2(judge: dict, rec: dict, content: str) -> dict:
    prompt = build_g2_prompt(rec, content)
    return parse_g2_verdict(gym._invoke_judge(judge, prompt))


def _write_json(path: Path, obj: dict) -> None:
    path.write_text(json.dumps(obj, indent=1))
    path.chmod(0o600)


def gate(findings_path, cfg: dict) -> dict:
    """Runs G1 (always) and G2 (when a judge is configured) on every finding that
    carries executable content, downgrades any G1 failure from tier A to tier B in
    findings.json, and returns the full result set for security.json. A finding
    with no executable content gets no entry at all — the caller-visible proof
    that instruction-only edits cost nothing."""
    findings_path = Path(findings_path)
    doc = json.loads(findings_path.read_text())
    judge = (cfg.get("gym") or {}).get("judge") or {}
    results, downgraded = {}, []
    scanned = 0
    judge_missing = False
    for rec in doc.get("findings") or []:
        if not carries_executable_content(rec):
            continue
        scanned += 1
        action = rec["action"]
        content = scan_text(rec)
        hits = scan_g1(content)
        passed = not hits
        if not passed and action.get("tier") == "A":
            action["tier"] = "B"
            downgraded.append(rec["id"])
        if judge.get("command"):
            try:
                g2 = run_g2(judge, rec, content)
            except gym.JudgeError as e:
                g2 = {"scanned": False, "reason": f"judge error: {e}"}
        else:
            judge_missing = True
            g2 = {"scanned": False, "reason": "no judge backend configured (set gym.judge.command)"}
        results[rec["id"]] = {"g1": {"scanned": True, "passed": passed, "hits": hits}, "g2": g2}
    if downgraded:
        _write_json(findings_path, doc)
    return {"results": results, "scanned": scanned, "downgraded": downgraded,
            "judge_missing": judge_missing}


def cmd_gate(a) -> int:
    import so_config
    _, state = so_config.resolve(None, a.state)
    cfg = so_config.load_config(state)
    summary = gate(a.findings, cfg)
    if summary["judge_missing"]:
        print(gym.JUDGE_UNCONFIGURED.format(path=Path(state) / "config.json"), file=sys.stderr)
    _write_json(Path(a.out), summary["results"])
    print(f"security gate: {summary['scanned']} finding(s) carried executable content, "
          f"{len(summary['downgraded'])} failed G1 and downgraded to tier B")
    return 0


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description="G1 (pattern scan) / G2 (judged purpose "
                                              "check) security gates on generated content")
    ap.add_argument("--findings", required=True)
    ap.add_argument("--state", default=None)
    ap.add_argument("--out", required=True)
    return ap


def main(argv=None) -> int:
    a = build_parser().parse_args(argv)
    return cmd_gate(a)


if __name__ == "__main__":
    raise SystemExit(main())
