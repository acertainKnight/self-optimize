"""HTML decision dashboard: one self-contained file rendering a run's findings so a
human can decide apply/reject/assist without touching the CLI, then hand the decision
back to `self-optimize decide`.

decisions.json schema (downloaded from the dashboard, consumed by `apply.py decide`):
    {
      "run_id": str,
      "apply": [id, ...],                       # applicable ids to apply (incl. diff)
      "reject": [{"id": str, "reason": str}],   # applicable ids to reject, reason required
      "assist": [id, ...],                      # manual ids selected for human-supervised work
      "amend": [{"id": str, "reason": str, "action": {...}}]   # reject id, apply this instead
    }
Unknown top-level keys are ignored by the consumer; ids must be strings; run_id must
match the evidence run being decided against or the file is refused.

Security: every analyst-derived string (title, category, risk, evidence refs, payload
text) is html.escape()'d before interpolation into the document. The one piece of data
the page's JS needs (run id + per-finding id/tier/status/amend-alternatives) is embedded
as inert JSON via a `<script type="application/json" id="data">` island, never
string-concatenated into executable JS; `<` is escaped to `\\u003c` in that JSON text so
a title or payload value can never close the surrounding <script> tag. The per-finding
`alts` (canonical amend alternatives) are built server-side from typed id/enum values
only — never from analyst free text — and the replacement action a user picks (a canned
alt, or hand-edited JSON) is re-validated by `schema.validate_rec` + `synth.guard` in
`apply.py::cmd_decide` before anything touches disk; the dashboard/JS side is UI only.
"""
import argparse
import html
import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import ledger as ledger_mod
import schema as so_schema
from report import _cumulative_savings, _impact, _num

_BADGE_CLASS = {
    "applied": "good", "verified": "good",
    "rejected": "warn", "regressed": "warn", "apply_failed": "warn",
    "rolled_back": "neutral", "inconclusive": "neutral",
}
_OPEN_STATUSES = (None, "proposed", "approved")


def _esc(x) -> str:
    return html.escape(str(x))


def _json_island(data: dict) -> str:
    return json.dumps(data, ensure_ascii=False).replace("<", "\\u003c")


def _payload_body(payload: dict) -> str:
    if not isinstance(payload, dict):
        return json.dumps(payload, indent=2)
    body = payload.get("diff") or payload.get("description") or payload.get("content")
    return body if body else json.dumps(payload, indent=2)


def _evidence_chips(refs: list) -> str:
    return " ".join(f'<code class="chip">{_esc(r)}</code>' for r in refs)


_SKILL_OVERRIDE_VALUES = ("off", "name-only", "user-invocable-only")


def _setting_alt(key_path: list, value) -> dict:
    return {"harness": "claude-code", "tier": "A", "type": "setting_change",
            "payload": {"file": "settings.json", "key_path": key_path, "value": value}}


def _alts_for(rec: dict) -> list:
    """Canonical amend alternatives, computed ONLY from the typed rec (action fields,
    evidence-ref ids, fixed enum values) — never from analyst free text. This is the
    security property the dashboard offers as a menu; apply.py::cmd_decide re-validates
    whatever gets picked (a canned alt or hand-edited JSON) regardless."""
    a = rec.get("action") or {}
    p = a.get("payload") or {}
    kp = p.get("key_path") or []
    refs = rec.get("evidence_refs") or []
    alts = []
    if a.get("type") == "setting_change" and len(kp) >= 2 and kp[0] == "enabledPlugins" and p.get("value") is False:
        for ref in refs:
            if isinstance(ref, str) and ref.startswith("inventory:skill:"):
                name = ref[len("inventory:skill:"):]
                alts.append({"label": f"name-only skill {name}",
                             "action": _setting_alt(["skillOverrides", name], "name-only")})
                alts.append({"label": f"off skill {name}",
                             "action": _setting_alt(["skillOverrides", name], "off")})
    elif a.get("type") == "setting_change" and len(kp) >= 2 and kp[0] == "skillOverrides":
        skill, current = kp[1], p.get("value")
        for v in _SKILL_OVERRIDE_VALUES:
            if v != current:
                alts.append({"label": f"skill {skill} → {v}",
                             "action": _setting_alt(["skillOverrides", skill], v)})
        for ref in refs:
            if isinstance(ref, str) and ref.startswith("inventory:plugin:"):
                pid = ref[len("inventory:plugin:"):]
                alts.append({"label": f"disable plugin {pid}",
                             "action": _setting_alt(["enabledPlugins", pid], False)})
    return alts


def _security_html(sec: dict | None) -> str:
    """G1/G2 results from security_gate.gate, when this finding carried executable
    content. G1 failing is why an otherwise tier-A finding is already tier B by the
    time this renders; G2 is judged evidence shown either way, never itself a reason
    the tier changed."""
    if not sec:
        return ""
    g1, g2 = sec.get("g1") or {}, sec.get("g2") or {}
    parts = []
    if not g1.get("passed"):
        hits = "; ".join(f'{_esc(h.get("category"))}: <code>{_esc(h.get("snippet"))}</code>'
                         for h in g1.get("hits") or [])
        parts.append(f'<p class="note">SECURITY: G1 blocked &mdash; {hits} '
                     f'&mdash; ineligible for tier A</p>')
    if g2.get("scanned") and g2.get("verdict") == "mismatch":
        parts.append(f'<p class="note">SECURITY: G2 purpose mismatch &mdash; '
                     f'{_esc(g2.get("reason", ""))}</p>')
    return "".join(parts)


def _card_html(rec: dict, entry: dict | None, sec: dict | None = None) -> str:
    rid = rec["id"]
    tier = rec["action"]["tier"]
    applicable = so_schema.is_applicable(rec["action"])
    status = (entry or {}).get("status")
    interactive = status in _OPEN_STATUSES
    payload = rec["action"].get("payload", {})
    parts = [
        f'<div class="card tier-{tier.lower()}{"" if interactive else " inert"}" data-id="{_esc(rid)}">',
        '<div class="card-head">',
        f'<span class="badge cat">{_esc(rec["category"])}</span>',
        f'<span class="impact">{_esc(_impact(rec))}</span>',
    ]
    if not interactive:
        cls = _BADGE_CLASS.get(status, "neutral")
        parts.append(f'<span class="badge {cls}">{_esc(status)}</span>')
    parts.append("</div>")
    parts.append(f'<h3>{_esc(rec["title"])} <code class="chip id">{_esc(rid)}</code></h3>')
    if rec.get("prior_rejection"):
        parts.append(f'<p class="note">previously rejected: {_esc(rec["prior_rejection"])} '
                      f'&mdash; evidence has changed since</p>')
    parts.append(f'<p class="evidence">{_evidence_chips(rec["evidence_refs"])}</p>')
    parts.append(f'<p class="risk">{_esc(rec["risk"])}</p>')
    parts.append(_security_html(sec))
    parts.append(f'<details><summary>payload</summary><pre>{_esc(_payload_body(payload))}</pre></details>')
    if interactive and applicable:
        # applicable = every tier-A type PLUS diff; amend guided-alts only ever
        # populate for setting_change (see _alts_for) — a diff/file_create/etc.
        # card still offers Apply/Reject/Skip and a custom-JSON amend, just with
        # an empty canned-alternatives list.
        parts.append(
            '<div class="toggle" role="group">'
            f'<label><input type="radio" name="choice-{_esc(rid)}" value="apply"> Apply</label>'
            f'<label><input type="radio" name="choice-{_esc(rid)}" value="reject"> Reject</label>'
            f'<label><input type="radio" name="choice-{_esc(rid)}" value="amend"> Amend</label>'
            f'<label><input type="radio" name="choice-{_esc(rid)}" value="skip" checked> Skip</label>'
            '</div>'
            f'<input type="text" class="reason-input" id="reason-{_esc(rid)}" '
            'style="display:none" placeholder="Reason (required for reject/amend)">'
            f'<div class="amend-controls" id="amend-controls-{_esc(rid)}" style="display:none">'
            f'<select class="amend-select" id="amend-select-{_esc(rid)}"></select>'
            f'<textarea class="amend-custom" id="amend-custom-{_esc(rid)}" style="display:none" '
            'placeholder="Replacement action JSON"></textarea>'
            f'<p class="amend-warn" id="amend-warn-{_esc(rid)}" hidden>Invalid action JSON.</p>'
            '</div>'
        )
    elif interactive:  # manual: the only remaining non-applicable type
        parts.append(
            f'<label class="assist-label"><input type="checkbox" id="assist-{_esc(rid)}"> '
            'Select for assisted work</label>'
        )
    parts.append("</div>")
    return "\n".join(parts)


def _outcomes_table(verify_rows: list) -> str:
    if not verify_rows:
        return ""
    rows = []
    for v in verify_rows:
        cls = _BADGE_CLASS.get(v.get("verdict"), "neutral")
        verdict_cell = f'<span class="badge {cls}">{_esc(v.get("verdict"))}</span>'
        if v.get("verdict") == "regressed":
            verdict_cell += f' <code class="chip">/self-optimize rollback {_esc(v["id"])}</code>'
        rows.append(
            "<tr>"
            f'<td><code class="chip id">{_esc(v.get("id"))}</code></td>'
            f'<td>{_esc(v.get("title"))}</td>'
            f'<td>{_esc(v.get("metric"))}</td>'
            f'<td>{verdict_cell}</td>'
            f'<td>{_esc(_num(v.get("baseline")))}</td>'
            f'<td>{_esc(_num(v.get("value")))}</td>'
            f'<td>{_esc(v.get("n"))}</td>'
            "</tr>"
        )
    return (
        '<section class="outcomes"><h2>Applied changes: outcomes</h2>'
        '<div class="table-wrap"><table><thead><tr>'
        "<th>id</th><th>title</th><th>metric</th><th>verdict</th>"
        "<th>baseline</th><th>now</th><th>n</th></tr></thead><tbody>"
        + "".join(rows) + "</tbody></table></div></section>"
    )


def _stat_tiles(findings: list, verify_rows: list, cumulative: list, usage: dict) -> str:
    tier_a = [f for f in findings if f.get("action", {}).get("tier") == "A"]
    tier_b = [f for f in findings if f.get("action", {}).get("tier") == "B"]
    base_ctx = usage.get("base_context_est")
    base_ctx_text = f"{base_ctx:,}" if isinstance(base_ctx, (int, float)) else "-"
    changes = sum(c for _, (_, c) in cumulative)
    cum_detail = " · ".join(f"{_esc(m)}: {_esc(_num(d))}" for m, (d, c) in cumulative) or "no verified changes yet"
    counts = Counter(v.get("verdict") for v in verify_rows)
    verdict_detail = " · ".join(f"{n} {_esc(k)}" for k, n in counts.items() if n) or "no applied changes yet"
    return (
        '<section class="stats">'
        f'<div class="tile"><div class="tile-n">{base_ctx_text}</div>'
        '<div class="tile-l">base context (est.)</div></div>'
        f'<div class="tile"><div class="tile-n">{changes}</div>'
        f'<div class="tile-l">verified changes (cumulative)</div><div class="tile-d">{cum_detail}</div></div>'
        f'<div class="tile"><div class="tile-n">{len(findings)}</div>'
        f'<div class="tile-l">findings</div><div class="tile-d">{len(tier_a)} tier A &middot; {len(tier_b)} tier B</div></div>'
        f'<div class="tile"><div class="tile-n">{len(verify_rows)}</div>'
        f'<div class="tile-l">applied-outcome verdicts</div><div class="tile-d">{verdict_detail}</div></div>'
        "</section>"
    )


_CSS = """
:root {
  color-scheme: light dark;
  --bg: #f7f7f8; --panel: #ffffff; --text: #1a1a1e; --muted: #6b6b76;
  --border: #e2e2e6; --accent: #2563eb; --accent-ink: #ffffff;
  --warn: #b45309; --warn-bg: #fef3c7; --good: #15803d; --good-bg: #dcfce7;
  --neutral-bg: #ececef;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #16161a; --panel: #1f1f24; --text: #ececef; --muted: #9a9aa4;
    --border: #2e2e35; --accent: #60a5fa; --accent-ink: #0b1220;
    --warn: #fbbf24; --warn-bg: #3f2d0a; --good: #4ade80; --good-bg: #0f2e1a;
    --neutral-bg: #2a2a31;
  }
}
* { box-sizing: border-box; }
body {
  margin: 0; padding: 0 0 6rem 0; background: var(--bg); color: var(--text);
  font: 15px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif;
}
.wrap { max-width: 1100px; margin: 0 auto; padding: 2rem 1.25rem; }
header h1 { font-size: 1.4rem; margin: 0 0 0.25rem 0; }
header .meta { color: var(--muted); font-size: 0.85rem; margin: 0 0 1.5rem 0; }
.stats { display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 0.75rem; margin-bottom: 1.75rem; }
.tile { background: var(--panel); border: 1px solid var(--border); border-radius: 10px; padding: 0.9rem 1rem; }
.tile-n { font-size: 1.6rem; font-weight: 600; }
.tile-l { color: var(--muted); font-size: 0.8rem; margin-top: 0.15rem; }
.tile-d { color: var(--muted); font-size: 0.75rem; margin-top: 0.35rem; word-break: break-word; }
.table-wrap { overflow-x: auto; }
table { width: 100%; border-collapse: collapse; font-size: 0.85rem; }
th, td { text-align: left; padding: 0.5rem 0.6rem; border-bottom: 1px solid var(--border); vertical-align: top; }
section.outcomes, section.findings { background: var(--panel); border: 1px solid var(--border);
  border-radius: 10px; padding: 1rem 1.25rem; margin-bottom: 1.25rem; }
h2 { font-size: 1.05rem; margin-top: 0; }
.cards { display: grid; gap: 0.85rem; }
.card { border: 1px solid var(--border); border-radius: 10px; padding: 0.9rem 1rem; background: var(--bg); }
.card.tier-a { border-left: 4px solid var(--accent); }
.card.tier-b { border-left: 4px solid var(--muted); }
.card.inert { opacity: 0.72; }
.card-head { display: flex; gap: 0.5rem; align-items: center; margin-bottom: 0.3rem; flex-wrap: wrap; }
.card h3 { font-size: 0.98rem; margin: 0.1rem 0 0.5rem 0; display: flex; align-items: center; gap: 0.5rem; flex-wrap: wrap; }
.badge { font-size: 0.72rem; padding: 0.12rem 0.5rem; border-radius: 999px; background: var(--neutral-bg); }
.badge.cat { text-transform: uppercase; letter-spacing: 0.02em; }
.badge.good { background: var(--good-bg); color: var(--good); }
.badge.warn { background: var(--warn-bg); color: var(--warn); }
.badge.neutral { background: var(--neutral-bg); color: var(--muted); }
.impact { color: var(--muted); font-size: 0.8rem; }
.chip { background: var(--neutral-bg); border-radius: 6px; padding: 0.05rem 0.4rem; font-size: 0.78rem; }
.chip.id { font-size: 0.72rem; }
p.evidence { margin: 0.3rem 0; }
p.risk { color: var(--muted); font-size: 0.85rem; margin: 0.3rem 0; }
p.note { color: var(--warn); font-size: 0.8rem; margin: 0.3rem 0; }
details { margin: 0.4rem 0; }
details pre { background: var(--neutral-bg); border-radius: 8px; padding: 0.6rem 0.75rem;
  overflow-x: auto; font-size: 0.78rem; white-space: pre-wrap; word-break: break-word; }
.toggle { display: flex; gap: 0.9rem; margin-top: 0.6rem; font-size: 0.85rem; }
.reason-input { width: 100%; margin-top: 0.5rem; padding: 0.35rem 0.5rem; border-radius: 6px;
  border: 1px solid var(--border); background: var(--panel); color: var(--text); }
.assist-label { display: inline-flex; gap: 0.4rem; margin-top: 0.5rem; font-size: 0.85rem; }
.amend-controls { margin-top: 0.5rem; display: flex; flex-direction: column; gap: 0.4rem; }
.amend-select { padding: 0.3rem 0.5rem; border-radius: 6px; border: 1px solid var(--border);
  background: var(--panel); color: var(--text); }
.amend-custom { width: 100%; min-height: 4.5rem; padding: 0.4rem 0.5rem; border-radius: 6px;
  border: 1px solid var(--border); background: var(--panel); color: var(--text);
  font: 0.8rem/1.4 ui-monospace, monospace; }
.amend-warn { color: var(--warn); font-size: 0.78rem; margin: 0; }
footer.dock {
  position: fixed; left: 0; right: 0; bottom: 0; background: var(--panel);
  border-top: 1px solid var(--border); padding: 0.7rem 1.25rem;
  display: flex; align-items: center; gap: 1rem; flex-wrap: wrap; font-size: 0.85rem;
}
footer.dock .counts { display: flex; gap: 0.75rem; color: var(--muted); }
footer.dock .preview { flex: 1 1 260px; min-width: 0; }
footer.dock code#cmd-preview { display: block; overflow-x: auto; white-space: nowrap;
  background: var(--neutral-bg); border-radius: 6px; padding: 0.3rem 0.5rem; }
footer.dock .warning { color: var(--warn); }
footer.dock button {
  border: 1px solid var(--border); background: var(--bg); color: var(--text);
  border-radius: 8px; padding: 0.45rem 0.85rem; cursor: pointer; font: inherit;
}
footer.dock button#download-btn { background: var(--accent); color: var(--accent-ink); border-color: var(--accent); }
footer.dock button:disabled { opacity: 0.5; cursor: not-allowed; }
"""

_JS = """
(function () {
  var DATA = JSON.parse(document.getElementById('data').textContent);
  var RUN_ID = DATA.run_id;
  var STORAGE_KEY = 'so-dash-' + RUN_ID;
  var findingById = {};
  DATA.findings.forEach(function (f) { findingById[f.id] = f; });
  // routing is by TYPE (applicable), not literal tier: diff is tier B but applies
  // like any tier-A action; only manual (applicable === false) is a hand-off.
  var applicableIds = DATA.findings.filter(function (f) { return f.applicable && f.interactive; })
    .map(function (f) { return f.id; });
  var manualIds = DATA.findings.filter(function (f) { return !f.applicable && f.interactive; })
    .map(function (f) { return f.id; });

  function loadState() {
    try {
      var raw = localStorage.getItem(STORAGE_KEY);
      if (!raw) return { decisions: {}, assist: [] };
      var parsed = JSON.parse(raw);
      return { decisions: parsed.decisions || {}, assist: parsed.assist || [] };
    } catch (e) { return { decisions: {}, assist: [] }; }
  }

  var state = loadState();

  // amend <select> options are DOM-built (createElement + textContent) from the
  // JSON island's per-finding alts — never innerHTML, never free text.
  function populateAmendSelect(id) {
    var sel = document.getElementById('amend-select-' + id);
    if (!sel || sel.options.length) return;
    var alts = (findingById[id] && findingById[id].alts) || [];
    alts.forEach(function (alt, idx) {
      var opt = document.createElement('option');
      opt.value = String(idx);
      opt.textContent = alt.label;
      sel.appendChild(opt);
    });
    var custom = document.createElement('option');
    custom.value = 'custom';
    custom.textContent = 'custom\\u2026';
    sel.appendChild(custom);
  }

  function resolveAmend(id, sel, customVal) {
    var alts = (findingById[id] && findingById[id].alts) || [];
    if (sel !== 'custom') {
      var alt = alts[parseInt(sel, 10)];
      return alt ? { action: alt.action, valid: true } : { action: null, valid: false };
    }
    try {
      var parsed = JSON.parse(customVal);
      // must be a plain object (a real action shape), not a bare number/string/
      // array/null — those parse fine but can never be a valid action and must
      // not be silently treated as "valid" here (decide would refuse them anyway,
      // but that refusal should never be masked as a client-side success)
      var isPlainObject = parsed !== null && typeof parsed === 'object' && !Array.isArray(parsed);
      return isPlainObject ? { action: parsed, valid: true } : { action: null, valid: false };
    } catch (e) {
      return { action: null, valid: false };
    }
  }

  applicableIds.forEach(function (id) {
    populateAmendSelect(id);
    var saved = state.decisions[id] || {};
    var choice = saved.choice || 'skip';
    var radio = document.querySelector('input[name="choice-' + id + '"][value="' + choice + '"]');
    if (radio) radio.checked = true;
    var reasonInput = document.getElementById('reason-' + id);
    if (reasonInput) {
      reasonInput.value = saved.reason || '';
      reasonInput.style.display = (choice === 'reject' || choice === 'amend') ? '' : 'none';
    }
    var controls = document.getElementById('amend-controls-' + id);
    if (controls) controls.style.display = choice === 'amend' ? '' : 'none';
    var sel = document.getElementById('amend-select-' + id);
    if (sel && saved.amendSel) sel.value = saved.amendSel;
    var customArea = document.getElementById('amend-custom-' + id);
    if (customArea) {
      customArea.value = saved.amendCustom || '';
      customArea.style.display = (sel && sel.value === 'custom') ? '' : 'none';
    }
  });
  manualIds.forEach(function (id) {
    var cb = document.getElementById('assist-' + id);
    if (cb) cb.checked = state.assist.indexOf(id) !== -1;
  });

  function recompute() {
    var decisions = {};
    applicableIds.forEach(function (id) {
      var checked = document.querySelector('input[name="choice-' + id + '"]:checked');
      var choice = checked ? checked.value : 'skip';
      var reasonInput = document.getElementById('reason-' + id);
      if (reasonInput) reasonInput.style.display = (choice === 'reject' || choice === 'amend') ? '' : 'none';
      var controls = document.getElementById('amend-controls-' + id);
      if (controls) controls.style.display = choice === 'amend' ? '' : 'none';
      var sel = document.getElementById('amend-select-' + id);
      var amendSel = sel ? sel.value : '';
      var customArea = document.getElementById('amend-custom-' + id);
      if (customArea) customArea.style.display = (choice === 'amend' && amendSel === 'custom') ? '' : 'none';
      var warn = document.getElementById('amend-warn-' + id);
      var amendValid = true;
      if (choice === 'amend') {
        amendValid = resolveAmend(id, amendSel, customArea ? customArea.value : '').valid;
      }
      if (warn) warn.hidden = choice !== 'amend' || amendValid;
      decisions[id] = {
        choice: choice, reason: reasonInput ? reasonInput.value.trim() : '',
        amendSel: amendSel, amendCustom: customArea ? customArea.value : ''
      };
    });
    var assist = manualIds.filter(function (id) {
      var cb = document.getElementById('assist-' + id);
      return cb && cb.checked;
    });
    state = { decisions: decisions, assist: assist };
    localStorage.setItem(STORAGE_KEY, JSON.stringify(state));
    renderFooter();
  }

  function amendEntries() {
    return applicableIds.filter(function (id) {
      return state.decisions[id] && state.decisions[id].choice === 'amend';
    }).map(function (id) {
      var d = state.decisions[id];
      var resolved = resolveAmend(id, d.amendSel, d.amendCustom);
      return { id: id, reason: d.reason, action: resolved.action, valid: resolved.valid && !!d.reason };
    });
  }

  function renderFooter() {
    var applyIds = applicableIds.filter(function (id) {
      return state.decisions[id] && state.decisions[id].choice === 'apply';
    });
    var rejectEntries = applicableIds.filter(function (id) {
      return state.decisions[id] && state.decisions[id].choice === 'reject';
    }).map(function (id) { return { id: id, reason: state.decisions[id].reason }; });
    var amends = amendEntries();
    var skipCount = applicableIds.length - applyIds.length - rejectEntries.length - amends.length;
    var missingReason = rejectEntries.some(function (r) { return !r.reason; });
    var badAmend = amends.some(function (a) { return !a.valid; });
    var blocked = missingReason || badAmend;

    document.getElementById('count-apply').textContent = applyIds.length;
    document.getElementById('count-reject').textContent = rejectEntries.length;
    document.getElementById('count-amend').textContent = amends.length;
    document.getElementById('count-skip').textContent = skipCount;
    document.getElementById('count-assist').textContent = state.assist.length;
    document.getElementById('cmd-preview').textContent =
      '/self-optimize decide  (' + applyIds.length + ' apply, ' + rejectEntries.length +
      ' reject, ' + amends.length + ' amend, ' + state.assist.length + ' assist, ' + skipCount + ' skip)';
    var warn = document.getElementById('reject-warning');
    warn.hidden = !blocked;
    var amendNote = document.getElementById('amend-note');
    if (amendNote) amendNote.hidden = amends.length === 0;
    document.getElementById('download-btn').disabled = blocked;
    document.getElementById('copy-btn').disabled = blocked;
  }

  document.querySelectorAll('input[type=radio], input[type=checkbox]').forEach(function (el) {
    el.addEventListener('change', recompute);
  });
  document.querySelectorAll('.reason-input').forEach(function (el) {
    el.addEventListener('input', recompute);
  });
  document.querySelectorAll('.amend-select').forEach(function (el) {
    el.addEventListener('change', recompute);
  });
  document.querySelectorAll('.amend-custom').forEach(function (el) {
    el.addEventListener('input', recompute);
  });

  document.getElementById('download-btn').addEventListener('click', function () {
    var applyIds = applicableIds.filter(function (id) {
      return state.decisions[id] && state.decisions[id].choice === 'apply';
    });
    var reject = applicableIds.filter(function (id) {
      return state.decisions[id] && state.decisions[id].choice === 'reject';
    }).map(function (id) { return { id: id, reason: state.decisions[id].reason }; });
    if (reject.some(function (r) { return !r.reason; })) return;
    var amends = amendEntries();
    if (amends.some(function (a) { return !a.valid; })) return;
    var amend = amends.map(function (a) { return { id: a.id, reason: a.reason, action: a.action }; });
    var payload = { run_id: RUN_ID, apply: applyIds, reject: reject, assist: state.assist, amend: amend };
    var blob = new Blob([JSON.stringify(payload, null, 1)], { type: 'application/json' });
    var a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = 'self-optimize-decisions-' + RUN_ID + '.json';
    document.body.appendChild(a);
    a.click();
    a.remove();
  });

  document.getElementById('copy-btn').addEventListener('click', function () {
    var applyIds = applicableIds.filter(function (id) {
      return state.decisions[id] && state.decisions[id].choice === 'apply';
    });
    var rejectEntries = applicableIds.filter(function (id) {
      return state.decisions[id] && state.decisions[id].choice === 'reject';
    }).map(function (id) { return { id: id, reason: state.decisions[id].reason }; });
    if (rejectEntries.some(function (r) { return !r.reason; })) return;
    // amend has no CLI flag: never encoded here, only via Download + /self-optimize decide
    var lines = [];
    if (applyIds.length) lines.push('/self-optimize apply ' + applyIds.join(','));
    var sanitized = false;
    rejectEntries.forEach(function (r) {
      // reason is free text going into a shell-shaped --reason "..."; neutralize
      // backticks / $ / quotes / backslash so a reason can't inject shell. The
      // downloaded decisions.json keeps the full reason; only this copy path strips.
      var safe = r.reason.replace(/[`$"\\\\]/g, ' ').trim();
      if (safe !== r.reason.trim()) sanitized = true;
      lines.push('/self-optimize reject ' + r.id + ' "' + safe + '"');
    });
    if (sanitized) lines.push('# note: reject reason(s) sanitized for shell safety; decisions.json keeps the full text');
    navigator.clipboard.writeText(lines.join('\\n'));
  });

  renderFooter();
})();
"""


def render_dashboard(run_id, findings, dropped, verify_rows, cumulative, usage,
                      ledger_entries, generated_at=None, security=None) -> str:
    generated_at = generated_at or datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    tier_a = [f for f in findings if f.get("action", {}).get("tier") == "A"]
    tier_b = [f for f in findings if f.get("action", {}).get("tier") == "B"]
    dropped = dropped or {}
    suppressed = dropped.get("suppressed") or []
    dropped_line = (f"{dropped.get('invalid', 0)} invalid &middot; "
                    f"{dropped.get('citations', 0)} failed citations &middot; "
                    f"{dropped.get('guard', 0)} guard &middot; "
                    f"{len(suppressed)} suppressed by ledger")

    data_island = {
        "run_id": run_id,
        "findings": [
            {"id": f["id"], "tier": f.get("action", {}).get("tier"),
             "applicable": so_schema.is_applicable(f.get("action", {})),
             "interactive": (ledger_entries.get(f["id"]) or {}).get("status") in _OPEN_STATUSES,
             "alts": _alts_for(f)}
            for f in findings
        ],
    }

    parts = [
        "<div class=\"wrap\">",
        "<header>",
        f'<h1>self-optimize &mdash; {_esc(run_id)}</h1>',
        f'<p class="meta">Generated {_esc(generated_at)} &middot; '
        f'findings dropped: {dropped_line}</p>',
        "</header>",
        _stat_tiles(findings, verify_rows, cumulative, usage),
        _outcomes_table(verify_rows),
        '<section class="findings">',
        "<h2>Tier A &mdash; apply now</h2>",
        '<div class="cards">',
    ]
    security = security or {}
    parts += [_card_html(rec, ledger_entries.get(rec["id"]), security.get(rec["id"]))
              for rec in tier_a] or ["<p>None.</p>"]
    parts += ["</div>", "<h2>Tier B &mdash; assisted work</h2>", '<div class="cards">']
    parts += [_card_html(rec, ledger_entries.get(rec["id"]), security.get(rec["id"]))
              for rec in tier_b] or ["<p>None.</p>"]
    parts += ["</div>", "</section>", "</div>"]

    body = "\n".join(parts)
    data_json = _json_island(data_island)

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>self-optimize dashboard &mdash; {_esc(run_id)}</title>
<style>{_CSS}</style>
</head>
<body>
{body}
<script type="application/json" id="data">{data_json}</script>
<footer class="dock">
<div class="counts">
<span>apply <b id="count-apply">0</b></span>
<span>reject <b id="count-reject">0</b></span>
<span>amend <b id="count-amend">0</b></span>
<span>skip <b id="count-skip">0</b></span>
<span>assist <b id="count-assist">0</b></span>
</div>
<div class="preview"><code id="cmd-preview">/self-optimize decide</code></div>
<div class="warning" id="reject-warning" hidden>Give every rejection and amendment a reason before downloading or copying, and fix any invalid amendment JSON.</div>
<div class="warning" id="amend-note" hidden>Amendments aren't expressible as a copyable command &mdash; download decisions.json and run /self-optimize decide (it picks up your newest download automatically).</div>
<div class="actions">
<button id="download-btn">Download decisions.json</button>
<button id="copy-btn">Copy command</button>
</div>
</footer>
<script>{_JS}</script>
</body>
</html>
"""


def main(argv=None):
    import so_config
    ap = argparse.ArgumentParser()
    ap.add_argument("--evidence", required=True)
    ap.add_argument("--state", default=None)
    ap.add_argument("--run-id", required=True)
    ap.add_argument("--out", default=None)
    a = ap.parse_args(argv)
    _, state = so_config.resolve(None, a.state)
    cfg = so_config.load_config(state)
    evdir = Path(a.evidence)
    fdata = json.loads((evdir / "findings.json").read_text())
    usage = json.loads((evdir / "usage.json").read_text())
    verify_rows = []
    vf = evdir / "verify.json"
    if vf.exists():
        verify_rows = json.loads(vf.read_text())["rows"]
    inv_f = evdir / "inventory.json"
    if inv_f.exists():
        usage = dict(usage)
        usage["base_context_est"] = json.loads(inv_f.read_text()).get("base_context_est")
    ledger_entries = ledger_mod.load(state / "state" / "ledger.jsonl")
    cumulative = _cumulative_savings(ledger_entries)
    generated_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    sec_f = evdir / "security.json"
    security = None
    if sec_f.exists():
        try:
            security = json.loads(sec_f.read_text())
        except json.JSONDecodeError:
            security = None
    doc = render_dashboard(a.run_id, fdata["findings"], fdata["dropped"], verify_rows,
                           cumulative, usage, ledger_entries, generated_at, security)
    rdir = Path(a.out) if a.out else Path(cfg["report_dir"])
    rdir.mkdir(parents=True, exist_ok=True)
    out = rdir / f"{a.run_id}.html"
    out.write_text(doc)
    out.chmod(0o600)
    latest = rdir / "latest.html"
    latest.write_text(doc)
    latest.chmod(0o600)
    print(f"DASHBOARD: {out}")
    print(f"open {out}")


if __name__ == "__main__":
    main()
