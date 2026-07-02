# Threat model

Single-user, per-machine, no telemetry. Trust boundaries and mitigations:

1. **Transcripts → collectors.** Transcripts contain secrets pasted in past sessions
   and untrusted third-party content (fetched web pages, tool output). Mitigation:
   deterministic redaction (`scripts/redact.py`, regex + entropy) on every excerpt
   before it is stored; settings are read through an allowlist (`env` never read);
   evidence files are written into the state dir only.
2. **Evidence → LLM analysts.** Excerpts may contain prompt injection. Mitigations:
   analysts get no tools beyond reading the named evidence files; prompts frame all
   content as data; output must conform to a strict JSON schema.
3. **Findings → your config.** A poisoned finding is an attack vector. Mitigations:
   `synth.py` mechanically verifies every citation and drops findings whose evidence
   does not resolve; typed-payload guards reject actions outside
   `<config-dir>/{skills,agents}` + the settings allowlist; hooks and permission changes
   are never machine-applied (tier B, human applies); every apply is per-item human
   approval with a file snapshot, post-apply syntax smoke check (auto-restore on
   failure), and one-command rollback.
4. **Artifacts at rest.** Evidence packs are chmod 600 and pruned by `retain_runs`.
   Reports contain redacted excerpts — the default report_dir is local; pointing it
   at a synced folder is your choice.

Residual risks, stated honestly: regex+entropy redaction is not perfect; injection
framing is mitigation, not guarantee; path guards normalize lexically and deliberately
do not resolve symlinks (so your legitimately symlinked skills keep working) — a
symlink already placed inside skills/ or agents/ by other means could point outside
them; `enabledPlugins` disable semantics are only partially documented by Anthropic
(verified against local behavior). The transcript format is explicitly not
stability-guaranteed — collectors skip what they cannot parse and report the skip
count.
