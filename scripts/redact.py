"""Deterministic secret/PII scrubber. Every transcript excerpt passes through here
before being written to the evidence pack. Regex + entropy; imperfect by design —
residual risk documented in SECURITY.md."""
import math
import re

PATTERNS = [
    ("private_key", re.compile(
        r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----")),
    ("anthropic_key", re.compile(r"sk-ant-[A-Za-z0-9_\-]{8,}")),
    ("openai_key", re.compile(r"sk-[A-Za-z0-9]{20,}")),
    ("github_token", re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}")),
    ("slack_token", re.compile(r"xox[baprs]-[A-Za-z0-9\-]{8,}")),
    ("aws_key", re.compile(r"AKIA[0-9A-Z]{16}")),
    ("bearer", re.compile(r"(?i)bearer\s+[A-Za-z0-9._\-]{16,}")),
    ("email", re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")),
]
_CANDIDATE = re.compile(r"[A-Za-z0-9+/_\-]{32,}")
_ENTROPY_THRESHOLD = 4.5


def _entropy(s: str) -> float:
    freq = {c: s.count(c) for c in set(s)}
    return -sum(n / len(s) * math.log2(n / len(s)) for n in freq.values())


def scrub(text: str) -> tuple[str, int]:
    count = 0
    for name, pat in PATTERNS:
        text, n = pat.subn(f"[REDACTED:{name}]", text)
        count += n

    def _maybe(m: re.Match) -> str:
        nonlocal count
        if _entropy(m.group(0)) > _ENTROPY_THRESHOLD:
            count += 1
            return "[REDACTED:entropy]"
        return m.group(0)

    text = _CANDIDATE.sub(_maybe, text)
    return text, count
