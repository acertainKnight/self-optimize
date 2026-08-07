# Public-repo history and content audit — 2026-08-07

Scope: full git history (all 124 commits across every local ref, including
the 80 commits reachable from `origin/main`), the current working tree, all
44 GitHub issues (open and closed, titles + bodies), and non-code surfaces
(docs, test fixtures, `.superpowers/`, workflow files, plugin manifests).

## Method

1. **gitleaks 8.30.1** (official upstream binary, installed via `brew install
   gitleaks`): `gitleaks detect --source . --log-opts="--all"` — walks every
   commit on every ref and matches against gitleaks' built-in secret-pattern
   rule set (API keys, tokens, private key blocks, cloud credentials, etc.).
   Result: **124 commits scanned, no leaks found.**
2. **Manual grep pass** over `git log --all -p` (the full patch text of every
   commit), because a rule-based scanner can miss things that aren't
   shaped like a known credential:
   - Home-directory paths (`/Users/<name>`)
   - Email addresses
   - Machine hostnames (`*.local` and similar)
   - The word "transcript" (this tool's own subject matter, so it needed a
     read of every hit rather than a pattern match) and evidence-file
     content (`sessions.json`, `usage.json`, etc. — checking whether any
     real evidence pack was ever committed)
   - Credential-shaped tokens (`sk-`, `ghp_`, `xox`, `AKIA`, PEM key
     headers) beyond gitleaks' own rule set
3. **Current-tree sweep**: the same patterns re-run against every file
   `git ls-files` reports, to separate "fixed in a later commit" from "still
   present today."
4. **Non-code surfaces**: every file ever added in history was diffed
   against the current tree's file list (no evidence dumps, no deleted
   `.superpowers/` artifacts, no forgotten fixtures) and all 44 issue
   bodies were pulled via `gh issue list --state all --json number,title,body`
   and scanned with the same patterns, plus a separate pass for
   private-project/colleague names.

## Findings

### 1. Personal email in commit authorship metadata — history-only, informational

Every one of the 124 commits carries the author's personal Gmail address in
its author and committer fields (deliberately not reproduced in this
document, which would only widen the exposure it describes). This is
ordinary git authorship metadata,
not a credential — it grants no access to anything — but it is a real
personal email address, and it is visible to anyone who runs `git log` or
opens a commit on GitHub for this repo. The GitHub account `acertainKnight`
itself does not publish an email or display name on its public profile (checked
via `gh api users/acertainKnight`), so the commit metadata is currently the
only place this address is exposed through the repo.

**Verdict: no history rewrite.** Scrubbing the author email from all 124
commits means rewriting every commit's SHA, which breaks any existing clone,
fork, or SHA-based reference (including this repo's own issue history, which
cites no SHAs today but might later). An email address is PII, not a secret
— it grants no access — and this is standard, expected practice for a
personal open-source repo committed under one's own account. The
proportionate fix is forward-looking, not retroactive: new commits can use
GitHub's provided no-reply address (`<id>+acertainKnight@users.noreply.github.com`,
via `git config user.email`) if the exposure is unwanted going forward. That
is a local git-config change, not a repo change, so it is not made as part
of this PR.

### 2. Local dev paths in early commit diffs — history-only, fixed in current tree

Two early commits show the author's literal home-directory paths (the
`/Users/<name>/...` checkout path as the install line in `README.md`, and a
similar path as a literal value in what is now `tests/test_inventory.py`;
the concrete paths are deliberately not repeated here). Both were replaced
in later commits — the README install line now points at the GitHub URL, and
the test literal now uses `/Users/someone/.claude/memory`. A full-tree grep
of every file `git ls-files` reports today confirms **zero** occurrences of
the author's home-directory prefix or personal email address in the current
tree.

**Verdict: no history rewrite.** These are non-secret local file-system
paths on the author's own machine, already corrected going forward, and
rewriting history to remove a personal dev path from two old diffs is not
proportionate to the exposure (a folder name, not a credential).

### 3. Fake credential strings in test fixtures — reviewed, not a leak

`tests/test_redact.py` and `tests/fixtures/basic_session.jsonl` contain
strings shaped like API keys and tokens (`sk-ant-api03-...`,
`ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ012345`, `xoxb-1234567890-abcdefghijk`,
`AKIAIOSFODNN7EXAMPLE`, a PEM private-key block, `nick@example.com`). These
are intentional fixtures exercising `scripts/redact.py`'s own scrubbing
logic — every one uses an obviously-placeholder body (`SECRETSECRETSECRET`,
a sequential alphabet, AWS's own documented `EXAMPLE` suffix, the RFC 2606
reserved `example.com` domain) and none of them is a live credential.
Confirmed clean; no action needed.

### 4. Dangling path reference in issue #40 — flagged for the owner, not edited here

Issue #40's body reads "Design decisions of record:
`docs/decisions-2026-08-06-v2-grill.md`." That file does not exist anywhere
in this repo's git history — it exists only as an untracked file in one
local checkout, deliberately never committed because it references private,
non-public project context. The issue currently points a public reader at a
path that will 404. This is flagged here for the repo owner to resolve
(strip the reference, or replace it with a public-safe pointer); it is not
edited as part of this audit, per this PR's scope.

### 5. Issue bodies (44 total, open + closed) — clean

No home-directory paths, email addresses, or credential-shaped strings in
any issue title or body. A separate pass for private company/colleague name
fragments returned only substring false-positives; no actual references to
non-public projects or people.

### 6. `.superpowers/` and stray evidence artifacts — none found

`git log --all --diff-filter=A --name-only` (every file ever added, across
all history) lists exactly the ~35 files present in the current tree —
`README.md`, `SECURITY.md`, `adapters/`, `agents/`, `docs/evidence-schema.md`,
`scripts/`, `skills/`, `tests/`. No `.superpowers/` directory, no committed
evidence pack (`sessions.json`, `usage.json`, etc. — this tool's own output
format, which is meant to stay local), and nothing was ever added and later
deleted.

## Actions taken

- No current-tree leaks found, so nothing to remove or rotate.
- No history-only leak rose to "genuine secret shipped," so no history
  rewrite was performed.
- Added a `gitleaks` CI gate (`.github/workflows/secret-scan.yml`) so future
  pushes and PRs are scanned automatically going forward.

## Explicit verdict on history rewrite

**No history rewrite is warranted.** Every finding above is either (a) not a
secret — a personal email address and a local file path, both PII-adjacent
but granting no access to anything — or (b) already corrected in the current
tree. Per the issue's stated fix policy ("rewrite history only if a genuine
secret shipped"), that condition was not met: gitleaks' full-history scan
returned zero credential matches, and the manual pass found no API keys,
tokens, private keys, or other live secrets anywhere in the 124-commit
history.
