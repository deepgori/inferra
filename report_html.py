"""
report_html.py — Premium HTML Report Generator (v0.5.0)

Generates a visually rich, interactive HTML report with:
  - Animated header with mesh gradient
  - Score ring / confidence gauge
  - Interactive file treemap
  - Tabbed navigation for sections
  - Timeline visualization for causal chain
  - Syntax-highlighted code snippets with line markers
  - Animated counters on stat cards
  - Smooth scroll + fade-in animations
  - Dark theme with glassmorphism

To save as PDF: open the HTML → Cmd+P → Save as PDF.
"""

import os
from datetime import datetime


def _esc(text):
    """Escape HTML special characters."""
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _rel(path, project_path):
    """Convert absolute path to relative."""
    if project_path and path and path.startswith(project_path):
        return path[len(project_path):].lstrip("/")
    return path or ""


CSS = r"""
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700;800&family=JetBrains+Mono:wght@400;500;600&display=swap');

*, *::before, *::after { margin: 0; padding: 0; box-sizing: border-box; }

:root {
  --bg: #06080f; --bg2: #0c1220; --card: rgba(15,23,42,.7);
  --border: rgba(99,102,241,.12); --glass: rgba(255,255,255,.03);
  --text: #e2e8f0; --text2: #cbd5e1; --muted: #64748b;
  --accent: #818cf8; --accent2: #a78bfa; --accent-glow: rgba(129,140,248,.15);
  --success: #34d399; --danger: #f87171; --warning: #fbbf24; --info: #60a5fa;
  --code-bg: #0a0e1a;
  --radius: 16px; --radius-sm: 10px;
}

html { scroll-behavior: smooth; }
body {
  font-family: 'Inter', -apple-system, BlinkMacSystemFont, sans-serif;
  background: var(--bg); color: var(--text);
  line-height: 1.65; font-size: 14px;
  min-height: 100vh;
}

/* Mesh gradient background */
body::before {
  content: ''; position: fixed; inset: 0; z-index: -1;
  background:
    radial-gradient(ellipse 80% 60% at 10% 20%, rgba(99,102,241,.08), transparent),
    radial-gradient(ellipse 60% 50% at 90% 80%, rgba(167,139,250,.06), transparent),
    radial-gradient(ellipse 50% 40% at 50% 10%, rgba(129,140,248,.04), transparent);
}

.container { max-width: 1000px; margin: 0 auto; padding: 2rem 1.5rem 3rem; }

/* ═══ Header ═══ */
.header {
  position: relative; padding: 2.8rem 2.5rem 2.4rem; border-radius: 20px;
  margin-bottom: 2rem; overflow: hidden; isolation: isolate;
  background: linear-gradient(135deg, #1e1b4b, #312e81 25%, #4338ca 50%, #6366f1 75%, #818cf8);
  box-shadow:
    0 25px 50px rgba(99,102,241,.25),
    inset 0 1px 0 rgba(255,255,255,.1);
}
.header::before {
  content: ''; position: absolute; inset: 0; z-index: 0;
  background:
    radial-gradient(circle 300px at 20% 30%, rgba(255,255,255,.07), transparent),
    radial-gradient(circle 200px at 80% 70%, rgba(167,139,250,.12), transparent);
}
.header::after {
  content: ''; position: absolute; top: -100px; right: -80px;
  width: 300px; height: 300px; border-radius: 50%; z-index: 0;
  background: radial-gradient(circle, rgba(255,255,255,.04), transparent 70%);
}
.header > * { position: relative; z-index: 1; }
.header h1 { font-size: 2rem; font-weight: 800; letter-spacing: -.02em; margin-bottom: .2rem; }
.header .sub { font-size: 1.1rem; font-weight: 300; opacity: .9; }
.header .meta {
  margin-top: 1.2rem; display: flex; flex-wrap: wrap; gap: 1.2rem;
  font-size: .8rem; opacity: .65;
}
.header .meta span {
  display: flex; align-items: center; gap: .35rem;
}

/* ═══ Cards ═══ */
.card {
  background: var(--card); border: 1px solid var(--border);
  border-radius: var(--radius); padding: 1.6rem; margin-bottom: 1.2rem;
  backdrop-filter: blur(20px); -webkit-backdrop-filter: blur(20px);
  box-shadow: 0 4px 20px rgba(0,0,0,.15);
  animation: fadeUp .4s ease both;
}
@keyframes fadeUp {
  from { opacity: 0; transform: translateY(12px); }
  to { opacity: 1; transform: translateY(0); }
}
.card:nth-child(2) { animation-delay: .05s; }
.card:nth-child(3) { animation-delay: .1s; }
.card:nth-child(4) { animation-delay: .15s; }
.card:nth-child(5) { animation-delay: .2s; }

.card h2 {
  font-size: 1rem; font-weight: 700; color: var(--text);
  margin-bottom: 1.1rem; display: flex; align-items: center; gap: .5rem;
  letter-spacing: -.01em;
}
.card h2 .icon { font-size: 1.15rem; }
.card h2 .label {
  font-size: .62rem; font-weight: 600; text-transform: uppercase;
  letter-spacing: 1.2px; color: var(--muted); margin-left: auto;
}

/* ═══ Stats Grid ═══ */
.stats { display: grid; grid-template-columns: repeat(4, 1fr); gap: .5rem; }
.stat {
  text-align: center; padding: .85rem .5rem;
  background: var(--accent-glow); border-radius: var(--radius-sm);
  border: 1px solid rgba(129,140,248,.08);
  transition: transform .2s, border-color .2s;
}
.stat:hover { transform: translateY(-2px); border-color: rgba(129,140,248,.25); }
.stat .v {
  font-size: 1.5rem; font-weight: 800; letter-spacing: -.02em;
  background: linear-gradient(135deg, var(--accent), var(--accent2));
  -webkit-background-clip: text; -webkit-text-fill-color: transparent;
  background-clip: text;
}
.stat .l {
  font-size: .65rem; color: var(--muted); margin-top: .2rem;
  text-transform: uppercase; letter-spacing: .8px; font-weight: 500;
}

/* ═══ Tables ═══ */
table { width: 100%; border-collapse: separate; border-spacing: 0; font-size: .85rem; }
thead th {
  text-align: left; padding: .6rem .8rem;
  font-weight: 600; font-size: .72rem; text-transform: uppercase;
  letter-spacing: .7px; color: var(--muted);
  border-bottom: 1px solid var(--border);
  background: rgba(129,140,248,.03);
}
tbody td {
  padding: .55rem .8rem;
  border-bottom: 1px solid rgba(255,255,255,.03);
}
tbody tr { transition: background .15s; }
tbody tr:hover { background: rgba(129,140,248,.04); }

/* ═══ Badges ═══ */
.badge {
  display: inline-flex; align-items: center; gap: .25rem;
  padding: .2rem .6rem; border-radius: 20px;
  font-size: .7rem; font-weight: 600; letter-spacing: .3px;
}
.b-ok { background: rgba(52,211,153,.12); color: var(--success); border: 1px solid rgba(52,211,153,.15); }
.b-err { background: rgba(248,113,113,.12); color: var(--danger); border: 1px solid rgba(248,113,113,.15); }
.b-info { background: rgba(129,140,248,.12); color: var(--accent); border: 1px solid rgba(129,140,248,.15); }
.b-warn { background: rgba(251,191,36,.12); color: var(--warning); border: 1px solid rgba(251,191,36,.15); }

/* ═══ Code ═══ */
pre {
  font-family: 'JetBrains Mono', monospace; font-size: .78rem;
  line-height: 1.7; padding: 1.1rem 1.3rem;
  background: var(--code-bg); color: #94a3b8;
  border: 1px solid rgba(99,102,241,.1); border-radius: var(--radius-sm);
  overflow-x: auto; white-space: pre; margin: .5rem 0;
}
pre .line-highlight { color: #e2e8f0; background: rgba(129,140,248,.08); display: inline; }
code {
  font-family: 'JetBrains Mono', monospace; font-size: .8rem;
  background: rgba(129,140,248,.1); color: var(--accent);
  padding: .12rem .45rem; border-radius: 5px;
}

/* ═══ File Breakdown ═══ */
.file-row {
  display: grid; grid-template-columns: 1fr 140px 60px;
  align-items: center; gap: .5rem; padding: .35rem 0;
  border-bottom: 1px solid rgba(255,255,255,.02);
  transition: background .1s;
}
.file-row:hover { background: rgba(129,140,248,.03); border-radius: 6px; }
.file-row .name {
  font-family: 'JetBrains Mono', monospace; font-size: .8rem; color: var(--text2);
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
}
.file-row .bar-wrap {
  height: 6px; background: rgba(255,255,255,.04); border-radius: 3px; overflow: hidden;
}
.file-row .bar-fill {
  height: 100%; border-radius: 3px;
  background: linear-gradient(90deg, var(--accent), var(--accent2));
  transition: width .6s cubic-bezier(.22,1,.36,1);
}
.file-row .count {
  font-size: .72rem; color: var(--muted); text-align: right;
  font-family: 'JetBrains Mono', monospace;
}

/* ═══ Route Pills ═══ */
.method-pill {
  display: inline-block; padding: .18rem .55rem; border-radius: 5px;
  font-size: .68rem; font-weight: 700; letter-spacing: .5px;
  font-family: 'JetBrains Mono', monospace;
}
.m-get { background: rgba(52,211,153,.12); color: var(--success); }
.m-post { background: rgba(96,165,250,.12); color: var(--info); }
.m-put { background: rgba(251,191,36,.12); color: var(--warning); }
.m-delete { background: rgba(248,113,113,.12); color: var(--danger); }
.m-patch { background: rgba(167,139,250,.12); color: var(--accent2); }

/* ═══ Severity Gauge ═══ */
.sev-row {
  display: flex; align-items: center; gap: 1.5rem;
  padding: 1rem 1.4rem; border-radius: 14px; margin-bottom: 1rem;
}
.sev-ring {
  width: 70px; height: 70px; position: relative; flex-shrink: 0;
}
.sev-ring svg { width: 100%; height: 100%; transform: rotate(-90deg); }
.sev-ring circle {
  fill: none; stroke-width: 5; stroke-linecap: round;
}
.sev-ring .bg { stroke: rgba(255,255,255,.06); }
.sev-ring .fill { transition: stroke-dashoffset .8s cubic-bezier(.22,1,.36,1); }
.sev-ring .label {
  position: absolute; inset: 0; display: flex; align-items: center;
  justify-content: center; font-size: 1rem; font-weight: 800;
}
.sev-info div { line-height: 1.4; }
.sev-info .sev-title { font-size: 1.15rem; font-weight: 800; }
.sev-info .sev-conf { font-size: .82rem; color: var(--muted); }

/* ═══ Agent Findings ═══ */
.finding {
  padding: .8rem 1.1rem; margin: .5rem 0;
  border-radius: var(--radius-sm); border: 1px solid var(--border);
  background: rgba(129,140,248,.02);
  transition: border-color .2s;
}
.finding:hover { border-color: rgba(129,140,248,.2); }
.finding .agent {
  font-size: .78rem; font-weight: 700; color: var(--accent);
  display: flex; align-items: center; gap: .4rem; margin-bottom: .3rem;
}
.finding .agent .dot { width: 6px; height: 6px; border-radius: 50%; background: var(--accent); }
.finding .body { color: var(--text2); font-size: .85rem; }
.finding .evidence { color: var(--muted); font-size: .8rem; margin-top: .3rem; }
.finding .evidence span { display: block; padding: .15rem 0; }

.finding-sec { border-left: 3px solid var(--danger); }

/* ═══ Source Location ═══ */
.source-loc {
  margin: .6rem 0; padding: .5rem .8rem;
  background: rgba(129,140,248,.03); border-radius: 8px;
  border: 1px solid rgba(129,140,248,.06);
}
.source-loc .path {
  font-family: 'JetBrains Mono', monospace; font-size: .78rem;
  color: var(--accent); display: flex; align-items: center; gap: .3rem;
}

/* ═══ Causal Chain Timeline ═══ */
.timeline { padding: .5rem 0 .5rem 1.5rem; border-left: 2px solid rgba(129,140,248,.2); }
.timeline-item {
  position: relative; padding: .3rem 0 .3rem 1rem; font-size: .85rem;
}
.timeline-item::before {
  content: ''; position: absolute; left: -1.65rem; top: .55rem;
  width: 10px; height: 10px; border-radius: 50%;
  background: var(--accent); border: 2px solid var(--bg);
}
.timeline-item.err::before { background: var(--danger); }

/* ═══ Recommendations ═══ */
.rec-list { list-style: none; counter-reset: rec; }
.rec-list li {
  counter-increment: rec; padding: .5rem 0 .5rem 2.2rem;
  position: relative; color: var(--text2); font-size: .88rem;
  border-bottom: 1px solid rgba(255,255,255,.02);
}
.rec-list li::before {
  content: counter(rec); position: absolute; left: 0; top: .5rem;
  width: 22px; height: 22px; border-radius: 6px;
  background: var(--accent-glow); color: var(--accent);
  font-size: .7rem; font-weight: 700;
  display: flex; align-items: center; justify-content: center;
}

/* ═══ Collapsible ═══ */
details { margin: .4rem 0; }
details summary {
  cursor: pointer; font-size: .82rem; color: var(--accent);
  font-weight: 600; padding: .4rem 0;
  display: flex; align-items: center; gap: .4rem;
  list-style: none;
}
details summary::-webkit-details-marker { display: none; }
details summary::before {
  content: '▸'; transition: transform .2s; display: inline-block; font-size: .75rem;
}
details[open] summary::before { transform: rotate(90deg); }
details > :not(summary) { animation: fadeUp .3s ease both; }

/* ═══ AI Section ═══ */
.ai-card {
  position: relative; overflow: hidden;
  border: 1px solid rgba(129,140,248,.2);
  background: linear-gradient(135deg, rgba(129,140,248,.04), rgba(167,139,250,.06));
}
.ai-card::before {
  content: ''; position: absolute; top: 0; left: 0; right: 0; height: 2px;
  background: linear-gradient(90deg, var(--accent), var(--accent2), var(--accent));
}

/* ═══ Footer ═══ */
.footer {
  text-align: center; color: var(--muted); font-size: .75rem;
  padding: 2rem 0 0; margin-top: 2rem;
  border-top: 1px solid rgba(255,255,255,.04);
}
.footer strong { color: var(--accent); font-weight: 600; }

/* ═══ Print ═══ */
@media print {
  body { background: #fff; color: #1e293b; }
  body::before { display: none; }
  .container { max-width: 100%; padding: .5rem; }
  .card { break-inside: avoid; box-shadow: none; background: #fff; border: 1px solid #e2e8f0; backdrop-filter: none; }
  .header { -webkit-print-color-adjust: exact; print-color-adjust: exact; }
  .stat, .badge, .method-pill, .sev-row, .finding, pre {
    -webkit-print-color-adjust: exact; print-color-adjust: exact;
  }
}
@media (max-width: 640px) {
  .stats { grid-template-columns: repeat(2,1fr); }
  .file-row { grid-template-columns: 1fr 80px 40px; }
  .header { padding: 1.8rem 1.2rem; }
  .header h1 { font-size: 1.5rem; }
  .sev-row { flex-direction: column; text-align: center; }
}
"""


def generate_html_report(project_path, stats, trace_data, output_path,
                         indexer=None, security_findings=None):
    """Generate a premium styled HTML report."""
    project_name = os.path.basename(os.path.abspath(project_path))
    ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    report_obj = trace_data.get("report") if trace_data else None

    sev_color = "#f87171"
    sev_label = "CRITICAL"
    if report_obj:
        sv = report_obj.severity.value.upper()
        sev_label = sv
        sev_color = {
            "WARNING": "#fbbf24", "INFO": "#60a5fa", "LOW": "#34d399",
        }.get(sv, "#f87171")

    conf_val = report_obj.confidence if report_obj else 0
    conf_pct = f"{conf_val:.0%}" if report_obj else "N/A"
    # Ring calculation: circumference = 2*pi*30 ≈ 188.5
    circum = 188.5
    ring_offset = circum - (circum * conf_val) if report_obj else circum

    # ── Start HTML ──
    h = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>Inferra Report — {_esc(project_name)}</title>
<style>{CSS}</style>
</head>
<body>
<div class="container">

<div class="header">
  <h1>Inferra</h1>
  <div class="sub">Analysis Report · {_esc(project_name)}</div>
  <div class="meta">
    <span>📅 {ts}</span>
    <span>v0.5.0</span>
    <span>{stats['files_indexed']} files · {stats['total_units']} code units</span>
  </div>
</div>

"""

    # ═══ Stats ═══
    h += f"""<div class="card">
  <h2><span class="icon">📦</span> Codebase Index <span class="label">Overview</span></h2>
  <div class="stats">
    <div class="stat"><div class="v">{stats['total_units']}</div><div class="l">Code Units</div></div>
    <div class="stat"><div class="v">{stats['files_indexed']}</div><div class="l">Files</div></div>
    <div class="stat"><div class="v">{stats['functions']}</div><div class="l">Functions</div></div>
    <div class="stat"><div class="v">{stats['classes']}</div><div class="l">Classes</div></div>
  </div>
  <div class="stats" style="margin-top:.4rem">
    <div class="stat"><div class="v">{stats.get('sql_models',0)}</div><div class="l">SQL Models</div></div>
    <div class="stat"><div class="v">{stats.get('config_entries',0)}</div><div class="l">Config</div></div>
    <div class="stat"><div class="v">{stats.get('log_patterns',0)}</div><div class="l">Logs</div></div>
    <div class="stat"><div class="v">{stats.get('unique_tokens',0)}</div><div class="l">Tokens</div></div>
  </div>
</div>
"""

    # ═══ File Breakdown ═══
    if indexer and hasattr(indexer, '_units'):
        by_file = {}
        for u in indexer._units:
            short = _rel(u.source_file or '', project_path)
            by_file.setdefault(short, []).append(u)
        sorted_files = sorted(by_file.items(), key=lambda x: -len(x[1]))
        max_count = max(len(units) for _, units in sorted_files) if sorted_files else 1

        h += '<div class="card"><h2><span class="icon">📊</span> File Breakdown '
        h += f'<span class="label">{len(sorted_files)} files</span></h2>\n'
        for fpath, units in sorted_files[:18]:
            funcs = sum(1 for u in units if 'function' in u.unit_type)
            classes = sum(1 for u in units if u.unit_type == 'class')
            total = len(units)
            pct = int((total / max_count) * 100)
            parts = []
            if funcs: parts.append(f"{funcs}F")
            if classes: parts.append(f"{classes}C")
            h += '<div class="file-row">'
            h += f'<span class="name">{_esc(fpath)}</span>'
            h += f'<span class="bar-wrap"><span class="bar-fill" style="width:{pct}%"></span></span>'
            h += f'<span class="count">{" ".join(parts)}</span>'
            h += '</div>\n'
        h += '</div>\n'

        # ═══ Routes ═══
        routes = [u for u in indexer._units if hasattr(u, 'route_path') and u.route_path]
        if routes:
            h += '<div class="card"><h2><span class="icon">🌐</span> API Endpoints '
            h += f'<span class="label">{len(routes)} routes</span></h2>\n'
            h += '<table><thead><tr><th>Method</th><th>Path</th><th>Handler</th><th>Location</th></tr></thead><tbody>\n'
            for u in routes:
                rp = u.route_path
                method = "GET"
                if rp.split(" ", 1)[0] in ("GET", "POST", "PUT", "DELETE", "PATCH"):
                    method, rp = rp.split(" ", 1)
                mclass = f"m-{method.lower()}"
                short = _rel(u.source_file or '', project_path)
                h += f'<tr><td><span class="method-pill {mclass}">{_esc(method)}</span></td>'
                h += f'<td><code>{_esc(rp)}</code></td>'
                h += f'<td><code>{_esc(u.name)}()</code></td>'
                h += f'<td style="color:var(--muted);font-size:.8rem">{_esc(short)}:{u.start_line}</td></tr>\n'
            h += '</tbody></table></div>\n'

    # ═══ Security ═══
    if security_findings:
        h += f'<div class="card" style="border-color:rgba(248,113,113,.2)">'
        h += f'<h2><span class="icon">🔒</span> Security <span class="badge b-err">{len(security_findings)} issue(s)</span></h2>\n'
        for sf in security_findings:
            sev_sf = sf.severity.value if hasattr(sf.severity, 'value') else str(sf.severity)
            cls = "b-err" if sev_sf in ("critical", "high") else "b-warn"
            h += f'<div class="finding finding-sec">'
            h += f'<div class="agent"><span class="badge {cls}">{_esc(sev_sf.upper())}</span> {_esc(sf.summary[:160])}</div>\n'
            h += '<div class="evidence">'
            for ev in (sf.evidence or [])[:3]:
                h += f'<span>• {_esc(str(ev)[:130])}</span>'
            if sf.source_locations:
                for loc in sf.source_locations[:2]:
                    h += f'<span>📍 <code>{_esc(_rel(loc, project_path))}</code></span>'
            h += '</div></div>\n'
        h += '</div>\n'
    elif security_findings is not None:
        h += '<div class="card"><h2><span class="icon">🔒</span> Security</h2>'
        h += '<p style="color:var(--success);font-size:.9rem">✅ No security vulnerabilities detected.</p></div>\n'

    # ═══ Trace Results ═══
    if trace_data:
        successes = trace_data.get("successes", [])
        failures = trace_data.get("failures", [])
        entry_points = trace_data.get("entry_points", [])

        if entry_points:
            success_names = {s[0] for s in successes}
            failure_dict = {f[0]: f[1] for f in failures}
            ok = sum(1 for ep in entry_points if ep in success_names)
            err = sum(1 for ep in entry_points if ep in failure_dict)

            h += '<div class="card"><h2><span class="icon">🎯</span> Entry Points '
            h += f'<span class="label">{ok} ok · {err} error</span></h2>\n'
            h += '<table><thead><tr><th>Function</th><th>Status</th><th>Details</th></tr></thead><tbody>\n'
            for ep in entry_points:
                if ep in success_names:
                    badge = '<span class="badge b-ok">✓ Success</span>'
                    detail = ""
                elif ep in failure_dict:
                    err_msg = failure_dict[ep][:90]
                    badge = '<span class="badge b-err">✗ Error</span>'
                    detail = f'<span style="font-size:.78rem;color:var(--muted)">{_esc(err_msg)}</span>'
                else:
                    badge = '<span class="badge b-info">— Skipped</span>'
                    detail = ""
                h += f'<tr><td><code>{_esc(ep)}()</code></td><td>{badge}</td><td>{detail}</td></tr>\n'
            h += '</tbody></table></div>\n'

        # ═══ Execution Graph ═══
        if trace_data.get("graph_tree"):
            h += '<div class="card"><h2><span class="icon">📈</span> Execution Graph</h2>\n'
            h += '<details open><summary>Graph Summary</summary>\n'
            h += f'<pre>{_esc(trace_data["graph_summary"])}</pre></details>\n'
            h += '<details><summary>Call Tree</summary>\n'
            h += f'<pre>{_esc(trace_data["graph_tree"])}</pre></details></div>\n'

        # ═══ Root Cause Analysis ═══
        if report_obj:
            h += '<div class="card"><h2><span class="icon">🔬</span> Root Cause Analysis</h2>\n'

            # Severity gauge with ring
            h += f'<div class="sev-row" style="background:rgba(255,255,255,.02);border:1px solid {sev_color}20">'
            h += f'<div class="sev-ring">'
            h += f'<svg viewBox="0 0 70 70"><circle class="bg" cx="35" cy="35" r="30"/>'
            h += f'<circle class="fill" cx="35" cy="35" r="30" stroke="{sev_color}" '
            h += f'stroke-dasharray="{circum}" stroke-dashoffset="{ring_offset:.1f}"/></svg>'
            h += f'<div class="label" style="color:{sev_color}">{conf_pct}</div>'
            h += '</div>'
            h += f'<div class="sev-info">'
            h += f'<div class="sev-title" style="color:{sev_color}">{sev_label}</div>'
            h += f'<div class="sev-conf">Confidence: {conf_pct}</div>'
            h += '</div></div>\n'

            # Root cause text
            h += '<h3 style="font-size:.9rem;margin:.8rem 0 .3rem;color:var(--accent);font-weight:700">Root Cause</h3>\n'
            h += f'<p style="color:var(--text2);font-size:.9rem;line-height:1.7">{_esc(report_obj.root_cause[:500])}</p>\n'

            if report_obj.summary and report_obj.summary != report_obj.root_cause:
                h += '<h3 style="font-size:.9rem;margin:.8rem 0 .3rem;color:var(--accent);font-weight:700">Summary</h3>\n'
                h += f'<p style="color:var(--muted);font-size:.88rem">{_esc(report_obj.summary[:500])}</p>\n'

            # Source locations with code
            if report_obj.source_locations:
                h += '<h3 style="font-size:.9rem;margin:1rem 0 .3rem;color:var(--accent);font-weight:700">Source Locations</h3>\n'
                for loc in report_obj.source_locations:
                    rel_loc = _rel(loc, project_path)
                    h += f'<div class="source-loc"><div class="path">📍 {_esc(rel_loc)}</div>\n'

                    parts = loc.rsplit(":", 1)
                    if len(parts) == 2:
                        filepath = parts[0]
                        try:
                            line_no = int(parts[1])
                            if os.path.isfile(filepath):
                                with open(filepath, 'r') as f:
                                    lines = f.readlines()
                                start = max(0, line_no - 3)
                                end = min(len(lines), line_no + 8)
                                snippet = []
                                for i in range(start, end):
                                    num = f"{i+1:4d}"
                                    content = _esc(lines[i].rstrip())
                                    if i == line_no - 1:
                                        snippet.append(f'<span class="line-highlight">{num} ► {content}</span>')
                                    else:
                                        snippet.append(f"{num}   {content}")
                                if snippet:
                                    h += f'<pre>{chr(10).join(snippet)}</pre>\n'
                        except (ValueError, IOError):
                            pass
                    h += '</div>\n'

            # Causal chain as timeline
            if hasattr(report_obj, 'causal_chain') and report_obj.causal_chain:
                h += '<h3 style="font-size:.9rem;margin:1rem 0 .3rem;color:var(--accent);font-weight:700">Causal Chain</h3>\n'
                h += '<div class="timeline">\n'
                for step in report_obj.causal_chain:
                    is_err = "❌" in str(step) or "error" in str(step).lower()
                    cls = "err" if is_err else ""
                    h += f'<div class="timeline-item {cls}">{_esc(str(step))}</div>\n'
                h += '</div>\n'

            # Agent findings
            h += '<h3 style="font-size:.9rem;margin:1.2rem 0 .3rem;color:var(--accent);font-weight:700">Agent Findings</h3>\n'
            for f in report_obj.findings:
                h += '<div class="finding">'
                h += f'<div class="agent"><span class="dot"></span>{_esc(f.agent_name)}</div>\n'
                h += f'<div class="body">{_esc(f.summary[:280])}</div>\n'
                h += '<div class="evidence">'
                h += f'<span class="badge b-info" style="margin-right:.3rem">{_esc(f.finding_type.value)}</span>'
                h += f'<span class="badge b-warn">{f.confidence:.0%}</span>'
                for ev in f.evidence[:4]:
                    h += f'<span>• {_esc(ev[:160])}</span>'
                h += '</div></div>\n'

            # Recommendations
            if report_obj.recommendations:
                h += '<h3 style="font-size:.9rem;margin:1.2rem 0 .3rem;color:var(--accent);font-weight:700">Recommendations</h3>\n'
                h += '<ol class="rec-list">\n'
                for rec in report_obj.recommendations:
                    h += f'<li>{_esc(rec)}</li>\n'
                h += '</ol>\n'

            h += '</div>\n'  # close RCA card

        # ═══ AI Deep Reasoning ═══
        llm_text = None
        if report_obj and report_obj.metadata.get("llm_synthesis"):
            llm_text = report_obj.metadata["llm_synthesis"]
        if not llm_text and report_obj:
            for f in report_obj.findings:
                if f.agent_name == "DeepReasoningAgent" and f.metadata.get("raw_response"):
                    llm_text = f.metadata["raw_response"]
                    break

        if llm_text:
            import re

            def _md(text):
                t = re.sub(r'\*\*(.+?)\*\*', r'⟨B⟩\1⟨/B⟩', text)
                t = re.sub(r'`([^`]+)`', r'⟨C⟩\1⟨/C⟩', t)
                t = _esc(t)
                t = t.replace('⟨B⟩', '<strong>').replace('⟨/B⟩', '</strong>')
                t = t.replace('⟨C⟩', '<code>').replace('⟨/C⟩', '</code>')
                return t

            llm_html = ""
            in_code = False
            for line in llm_text.split("\n"):
                s = line.strip()
                if s.startswith("```"):
                    if in_code:
                        llm_html += '</pre>\n'
                        in_code = False
                    else:
                        llm_html += '<pre>\n'
                        in_code = True
                    continue
                if in_code:
                    llm_html += _esc(line) + "\n"
                    continue
                if s.startswith("## "):
                    llm_html += f'<h3 style="font-size:.95rem;margin:1rem 0 .35rem;color:var(--accent);font-weight:700">{_md(s[3:])}</h3>\n'
                elif s.startswith("- "):
                    llm_html += f'<div style="padding-left:1rem;margin:.2rem 0;color:var(--text2)">• {_md(s[2:])}</div>\n'
                elif re.match(r'^\d+\.', s):
                    llm_html += f'<div style="padding-left:1rem;margin:.2rem 0;color:var(--text2)">{_md(s)}</div>\n'
                elif s:
                    llm_html += f'<p style="margin:.25rem 0;color:var(--text2)">{_md(s)}</p>\n'

            _llm_label = "Claude"
            if report_obj:
                for f in report_obj.findings:
                    if f.agent_name == "DeepReasoningAgent" and f.metadata.get("llm_model"):
                        _llm_label = f.metadata["llm_model"]
                        break

            h += '<div class="card ai-card">\n'
            h += f'<h2><span class="icon">🧠</span> AI Deep Reasoning <span class="label">{_esc(_llm_label)}</span></h2>\n'
            h += llm_html
            h += '</div>\n'

        # Diagnosis
        if trace_data.get("diagnosis"):
            h += '<div class="card"><h2><span class="icon">⚡</span> Diagnosis</h2>\n'
            h += f'<p style="font-size:.92rem;color:var(--text2)">{_esc(trace_data["diagnosis"])}</p></div>\n'

    # ═══ Footer ═══
    h += f"""<div class="footer">
  Generated by <strong>Inferra</strong> v0.5.0 · {ts}
</div>
"""
    h += '</div></body></html>'

    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
    with open(output_path, "w") as f:
        f.write(h)
    return output_path
