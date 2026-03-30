"""
report_html.py — Styled HTML Report Generator (v0.5.0)

Generates a detailed HTML report with:
  - Codebase index stats
  - File breakdown with function/class counts
  - Route / endpoint map
  - Security findings
  - Execution graph + call tree
  - Root cause analysis with severity & confidence
  - Agent findings with evidence
  - AI deep reasoning section
  - Code snippets for source locations

To save as PDF: open the HTML in a browser → Cmd+P → Save as PDF.
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


CSS = """
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&family=JetBrains+Mono:wght@400;500&display=swap');

:root {
  --bg: #0f172a; --bg2: #1e293b; --card: #1e293b; --border: #334155;
  --text: #e2e8f0; --muted: #94a3b8; --accent: #818cf8; --accent-bg: rgba(129,140,248,.12);
  --success: #34d399; --danger: #f87171; --warning: #fbbf24;
  --code-bg: #0f172a; --card-hover: #273548;
}
* { margin: 0; padding: 0; box-sizing: border-box; }
body {
  font-family: 'Inter', -apple-system, sans-serif;
  background: var(--bg); color: var(--text); line-height: 1.6; font-size: 14px;
}
.container { max-width: 960px; margin: 0 auto; padding: 2rem 1.5rem; }

/* Header */
.header {
  background: linear-gradient(135deg, #312e81 0%, #4f46e5 40%, #7c3aed 70%, #a78bfa 100%);
  color: #fff; padding: 2.5rem 2rem; border-radius: 16px; margin-bottom: 2rem;
  box-shadow: 0 20px 60px rgba(79,70,229,.3);
  position: relative; overflow: hidden;
}
.header::before {
  content: ''; position: absolute; top: -50%; right: -20%; width: 60%; height: 200%;
  background: radial-gradient(circle, rgba(255,255,255,.06) 0%, transparent 60%);
}
.header h1 { font-size: 1.8rem; font-weight: 700; margin-bottom: .25rem; position: relative; }
.header .sub { opacity: .85; font-size: 1rem; font-weight: 300; position: relative; }
.header .meta {
  margin-top: 1.2rem; display: flex; flex-wrap: wrap; gap: 1.5rem;
  font-size: .82rem; opacity: .7; position: relative;
}

/* Cards */
.card {
  background: var(--card); border: 1px solid var(--border); border-radius: 14px;
  padding: 1.5rem; margin-bottom: 1.2rem;
  box-shadow: 0 2px 8px rgba(0,0,0,.2);
  transition: transform .15s, box-shadow .15s;
}
.card:hover { transform: translateY(-1px); box-shadow: 0 4px 16px rgba(0,0,0,.3); }
.card h2 {
  font-size: 1.05rem; font-weight: 600; color: var(--accent);
  margin-bottom: 1rem; display: flex; align-items: center; gap: .5rem;
}

/* Stats grid */
.stats { display: grid; grid-template-columns: repeat(4,1fr); gap: .6rem; }
.stat {
  text-align: center; padding: .75rem .5rem;
  background: var(--accent-bg); border-radius: 10px;
  border: 1px solid rgba(129,140,248,.15);
}
.stat .v { font-size: 1.4rem; font-weight: 700; color: var(--accent); }
.stat .l { font-size: .72rem; color: var(--muted); margin-top: .15rem; text-transform: uppercase; letter-spacing: .5px; }

/* Tables */
table { width: 100%; border-collapse: collapse; font-size: .88rem; }
th {
  text-align: left; padding: .55rem .7rem;
  background: rgba(129,140,248,.08); font-weight: 600;
  border-bottom: 2px solid var(--border); color: var(--accent);
  font-size: .8rem; text-transform: uppercase; letter-spacing: .4px;
}
td { padding: .5rem .7rem; border-bottom: 1px solid rgba(51,65,85,.5); }
tr:hover td { background: rgba(129,140,248,.04); }

/* Badges */
.badge {
  display: inline-block; padding: .15rem .55rem; border-radius: 6px;
  font-size: .72rem; font-weight: 600; letter-spacing: .3px;
}
.b-ok { background: rgba(52,211,153,.15); color: var(--success); }
.b-err { background: rgba(248,113,113,.15); color: var(--danger); }
.b-info { background: var(--accent-bg); color: var(--accent); }
.b-warn { background: rgba(251,191,36,.15); color: var(--warning); }
.b-sec { background: rgba(248,113,113,.1); color: var(--danger); border: 1px solid rgba(248,113,113,.2); }

/* Code */
pre {
  background: var(--code-bg); color: #e2e8f0; padding: 1rem 1.2rem; border-radius: 10px;
  overflow-x: auto; font-family: 'JetBrains Mono', monospace; font-size: .8rem;
  line-height: 1.6; margin: .5rem 0; white-space: pre-wrap; word-break: break-word;
  border: 1px solid var(--border);
}
code {
  font-family: 'JetBrains Mono', monospace; font-size: .82rem;
  background: rgba(129,140,248,.1); padding: .1rem .4rem; border-radius: 4px; color: var(--accent);
}

/* Findings */
.finding {
  border-left: 3px solid var(--accent); padding: .7rem 1rem;
  margin: .5rem 0; background: rgba(129,140,248,.04);
  border-radius: 0 10px 10px 0;
}
.finding .ag { font-weight: 600; color: var(--accent); font-size: .85rem; }
.finding .ev { color: var(--muted); font-size: .82rem; margin-top: .3rem; }

.finding-sec {
  border-left: 3px solid var(--danger); padding: .7rem 1rem;
  margin: .5rem 0; background: rgba(248,113,113,.05);
  border-radius: 0 10px 10px 0;
}
.finding-sec .ag { font-weight: 600; color: var(--danger); font-size: .85rem; }
.finding-sec .ev { color: var(--muted); font-size: .82rem; margin-top: .3rem; }

/* Severity banner */
.sev-banner {
  display: flex; align-items: center; gap: 1.5rem; padding: 1rem 1.5rem;
  border-radius: 12px; margin-bottom: 1rem;
}

/* File bar chart */
.file-bar { display: flex; align-items: center; gap: .5rem; margin: .3rem 0; }
.file-bar .name { font-size: .82rem; min-width: 200px; font-family: 'JetBrains Mono', monospace; color: var(--muted); }
.file-bar .bar {
  height: 20px; border-radius: 4px; min-width: 3px;
  background: linear-gradient(90deg, var(--accent), #a78bfa);
  transition: width .3s;
}
.file-bar .count { font-size: .75rem; color: var(--muted); min-width: 40px; }

/* Route pill */
.route-pill {
  display: inline-block; padding: .2rem .5rem; border-radius: 5px;
  font-size: .72rem; font-weight: 700; font-family: 'JetBrains Mono', monospace;
  margin-right: .3rem;
}
.route-get { background: rgba(52,211,153,.15); color: var(--success); }
.route-post { background: rgba(129,140,248,.15); color: var(--accent); }
.route-put { background: rgba(251,191,36,.15); color: var(--warning); }
.route-delete { background: rgba(248,113,113,.15); color: var(--danger); }

/* Footer */
.footer {
  text-align: center; color: var(--muted); font-size: .78rem;
  padding: 1.5rem 0; border-top: 1px solid var(--border); margin-top: 1.5rem;
}

/* Collapsible */
details { margin: .3rem 0; }
details summary {
  cursor: pointer; font-size: .85rem; color: var(--accent);
  font-weight: 500; padding: .3rem 0;
}
details summary:hover { color: #a78bfa; }

@media print {
  body { background: #fff; color: #1e293b; font-size: 12px; }
  .container { max-width: 100%; padding: .5rem; }
  .card { break-inside: avoid; box-shadow: none; border: 1px solid #ddd; background: #fff; }
  .header,.stats,.stat,.badge,.sev-banner,.finding,.finding-sec,pre {
    -webkit-print-color-adjust: exact; print-color-adjust: exact;
  }
  .card:hover { transform: none; }
}

@media (max-width: 640px) {
  .stats { grid-template-columns: repeat(2,1fr); }
  .file-bar .name { min-width: 120px; font-size: .75rem; }
  .header .meta { flex-direction: column; gap: .4rem; }
}
"""


def generate_html_report(project_path, stats, trace_data, output_path,
                         indexer=None, security_findings=None):
    """Generate a detailed, styled HTML report.

    Args:
        project_path: Path to the analyzed project
        stats: dict from indexer.stats()
        trace_data: dict with graph_tree, report, entry_points, etc.
        output_path: where to write the HTML
        indexer: (optional) CodeIndexer instance for file breakdown + routes
        security_findings: (optional) list of security Finding objects
    """
    project_name = os.path.basename(os.path.abspath(project_path))
    ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    report_obj = trace_data.get("report") if trace_data else None

    sev_color = "#f87171"
    sev_label = "CRITICAL"
    sev_bg = "rgba(248,113,113,.1)"
    if report_obj:
        sv = report_obj.severity.value.upper()
        sev_label = sv
        colors = {
            "WARNING": ("#fbbf24", "rgba(251,191,36,.1)"),
            "INFO": ("#60a5fa", "rgba(96,165,250,.1)"),
            "LOW": ("#34d399", "rgba(52,211,153,.1)"),
        }
        sev_color, sev_bg = colors.get(sv, ("#f87171", "rgba(248,113,113,.1)"))

    conf = f"{report_obj.confidence:.0%}" if report_obj else "N/A"

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
  <h1>🔬 Inferra Analysis Report</h1>
  <div class="sub">{_esc(project_name)}</div>
  <div class="meta">
    <span>📅 {ts}</span>
    <span>🛠️ inferra v0.5.0</span>
    <span>📁 {stats['files_indexed']} files · {stats['total_units']} code units</span>
  </div>
</div>

"""

    # ── Section: Codebase Index Stats ──
    h += f"""<div class="card">
  <h2>📦 Codebase Index</h2>
  <div class="stats">
    <div class="stat"><div class="v">{stats['total_units']}</div><div class="l">Code Units</div></div>
    <div class="stat"><div class="v">{stats['files_indexed']}</div><div class="l">Files</div></div>
    <div class="stat"><div class="v">{stats['functions']}</div><div class="l">Functions</div></div>
    <div class="stat"><div class="v">{stats['classes']}</div><div class="l">Classes</div></div>
  </div>
  <div class="stats" style="margin-top:.5rem">
    <div class="stat"><div class="v">{stats.get('sql_models',0)}</div><div class="l">SQL Models</div></div>
    <div class="stat"><div class="v">{stats.get('config_entries',0)}</div><div class="l">Config</div></div>
    <div class="stat"><div class="v">{stats.get('log_patterns',0)}</div><div class="l">Log Patterns</div></div>
    <div class="stat"><div class="v">{stats.get('unique_tokens',0)}</div><div class="l">Search Tokens</div></div>
  </div>
</div>
"""

    # ── Section: File Breakdown (if indexer provided) ──
    if indexer and hasattr(indexer, '_units'):
        by_file = {}
        for u in indexer._units:
            short = _rel(u.source_file or '', project_path)
            by_file.setdefault(short, []).append(u)

        sorted_files = sorted(by_file.items(), key=lambda x: -len(x[1]))
        max_count = max(len(units) for _, units in sorted_files) if sorted_files else 1

        h += '<div class="card"><h2>📊 File Breakdown</h2>\n'
        for fpath, units in sorted_files[:20]:
            funcs = sum(1 for u in units if 'function' in u.unit_type)
            classes = sum(1 for u in units if u.unit_type == 'class')
            total = len(units)
            bar_pct = int((total / max_count) * 100)
            label_parts = []
            if funcs: label_parts.append(f"{funcs}F")
            if classes: label_parts.append(f"{classes}C")
            h += f'<div class="file-bar">'
            h += f'<span class="name">{_esc(fpath)}</span>'
            h += f'<span class="bar" style="width:{bar_pct}%"></span>'
            h += f'<span class="count">{" ".join(label_parts)}</span>'
            h += '</div>\n'
        h += '</div>\n'

        # ── Section: Routes ──
        routes = []
        for u in indexer._units:
            if hasattr(u, 'route_path') and u.route_path:
                routes.append(u)

        if routes:
            h += '<div class="card"><h2>🌐 API Routes / Endpoints</h2>\n'
            h += '<table><tr><th>Method</th><th>Path</th><th>Handler</th><th>File</th></tr>\n'
            for u in routes:
                rp = u.route_path
                method = "GET"
                if rp.startswith(("POST ", "PUT ", "DELETE ", "PATCH ", "GET ")):
                    parts = rp.split(" ", 1)
                    method = parts[0]
                    rp = parts[1] if len(parts) > 1 else rp

                mclass = f"route-{method.lower()}"
                short = _rel(u.source_file or '', project_path)
                h += f'<tr><td><span class="route-pill {mclass}">{_esc(method)}</span></td>'
                h += f'<td><code>{_esc(rp)}</code></td>'
                h += f'<td><code>{_esc(u.name)}()</code></td>'
                h += f'<td style="color:var(--muted)">{_esc(short)}:{u.start_line}</td></tr>\n'
            h += '</table></div>\n'

    # ── Section: Security Findings ──
    if security_findings:
        h += f'<div class="card" style="border:1px solid rgba(248,113,113,.3)">'
        h += f'<h2>🔒 Security Findings <span class="badge b-sec">{len(security_findings)} issue(s)</span></h2>\n'
        for sf in security_findings:
            sev = sf.severity.value if hasattr(sf.severity, 'value') else str(sf.severity)
            sev_cls = "b-err" if sev in ("critical", "high") else "b-warn"
            h += f'<div class="finding-sec">'
            h += f'<div class="ag"><span class="badge {sev_cls}">{_esc(sev.upper())}</span> {_esc(sf.summary[:150])}</div>\n'
            h += f'<div class="ev">'
            for ev in (sf.evidence or [])[:3]:
                h += f'<br>• {_esc(str(ev)[:120])}'
            if sf.source_locations:
                for loc in sf.source_locations[:2]:
                    h += f'<br>📍 <code>{_esc(_rel(loc, project_path))}</code>'
            h += '</div></div>\n'
        h += '</div>\n'
    elif security_findings is not None:
        h += '<div class="card"><h2>🔒 Security</h2>'
        h += '<p style="color:var(--success)">✅ No security vulnerabilities detected.</p></div>\n'

    # ── Section: Entry Points & Trace Results ──
    if trace_data:
        successes = trace_data.get("successes", [])
        failures = trace_data.get("failures", [])
        entry_points = trace_data.get("entry_points", [])

        if entry_points:
            success_names = {s[0] for s in successes}
            failure_dict = {f[0]: f[1] for f in failures}
            h += '<div class="card"><h2>🎯 Discovered Entry Points</h2>\n'
            h += '<table><tr><th>Function</th><th>Status</th><th>Details</th></tr>\n'
            for ep in entry_points:
                if ep in success_names:
                    badge = '<span class="badge b-ok">✅ Success</span>'
                    detail = ""
                elif ep in failure_dict:
                    err = failure_dict[ep][:80]
                    badge = '<span class="badge b-err">❌ Error</span>'
                    detail = f'<span style="font-size:.8rem;color:var(--muted)">{_esc(err)}</span>'
                else:
                    badge = '<span class="badge b-info">⏸ Not traced</span>'
                    detail = ""
                h += f'<tr><td><code>{_esc(ep)}()</code></td><td>{badge}</td><td>{detail}</td></tr>\n'
            h += '</table></div>\n'

        # Execution graph
        if trace_data.get("graph_tree"):
            h += '<div class="card"><h2>📊 Execution Graph</h2>\n'
            h += '<details open><summary>Graph Summary</summary>\n'
            h += f'<pre>{_esc(trace_data["graph_summary"])}</pre>\n'
            h += '</details>\n'
            h += '<details open><summary>Call Tree</summary>\n'
            h += f'<pre>{_esc(trace_data["graph_tree"])}</pre>\n'
            h += '</details></div>\n'

        # ── Section: Root Cause Analysis ──
        if report_obj:
            h += '<div class="card"><h2>🔬 Root Cause Analysis</h2>\n'
            h += f'<div class="sev-banner" style="background:{sev_bg};border:1px solid {sev_color}30">'
            h += f'<div style="font-size:1.4rem;font-weight:700;color:{sev_color}">{sev_label}</div>'
            h += f'<div style="font-size:.9rem">Confidence: <strong style="color:{sev_color}">{conf}</strong></div>'
            h += '</div>\n'

            h += '<h3 style="font-size:.95rem;margin:.8rem 0 .4rem;color:var(--accent)">Root Cause</h3>\n'
            h += f'<p style="color:var(--text);line-height:1.7">{_esc(report_obj.root_cause[:500])}</p>\n'

            if report_obj.summary and report_obj.summary != report_obj.root_cause:
                h += '<h3 style="font-size:.95rem;margin:.8rem 0 .4rem;color:var(--accent)">Summary</h3>\n'
                h += f'<p style="color:var(--muted)">{_esc(report_obj.summary[:500])}</p>\n'

            # Source locations with code snippets
            if report_obj.source_locations:
                h += '<h3 style="font-size:.95rem;margin:.8rem 0 .4rem;color:var(--accent)">Source Locations</h3>\n'
                for loc in report_obj.source_locations:
                    rel_loc = _rel(loc, project_path)
                    h += f'<div style="margin:.3rem 0">📍 <code>{_esc(rel_loc)}</code></div>\n'

                    # Try to read the actual source code snippet
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
                                snippet_lines = []
                                for i in range(start, end):
                                    marker = " ►" if i == line_no - 1 else "  "
                                    snippet_lines.append(f"{i+1:4d}{marker} {lines[i].rstrip()}")
                                if snippet_lines:
                                    h += f'<pre>{_esc(chr(10).join(snippet_lines))}</pre>\n'
                        except (ValueError, IOError):
                            pass

            # Agent findings
            h += '<h3 style="font-size:.95rem;margin:1rem 0 .4rem;color:var(--accent)">Agent Findings</h3>\n'
            for f in report_obj.findings:
                h += f'<div class="finding"><div class="ag">[{_esc(f.agent_name)}]</div>\n'
                h += f'<div style="margin:.2rem 0">{_esc(f.summary[:250])}</div>\n'
                h += f'<div class="ev"><span class="badge b-info">{_esc(f.finding_type.value)}</span> '
                h += f'<span class="badge b-warn">{f.confidence:.0%}</span>'
                for ev in f.evidence[:4]:
                    h += f'<br>• {_esc(ev[:150])}'
                h += '</div></div>\n'

            if report_obj.recommendations:
                h += '<h3 style="font-size:.95rem;margin:1rem 0 .4rem;color:var(--accent)">Recommendations</h3>\n'
                h += '<ol style="padding-left:1.5rem;color:var(--muted)">\n'
                for rec in report_obj.recommendations:
                    h += f'<li style="margin:.3rem 0">{_esc(rec)}</li>\n'
                h += '</ol>\n'

            h += '</div>\n'

        # ── Section: AI Deep Reasoning ──
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

            def _md_inline(text):
                t = re.sub(r'\*\*(.+?)\*\*', r'⟨B⟩\1⟨/B⟩', text)
                t = re.sub(r'`([^`]+)`', r'⟨C⟩\1⟨/C⟩', t)
                t = _esc(t)
                t = t.replace('⟨B⟩', '<strong>').replace('⟨/B⟩', '</strong>')
                t = t.replace('⟨C⟩', '<code>').replace('⟨/C⟩', '</code>')
                return t

            llm_html = ""
            in_code_block = False
            for line in llm_text.split("\n"):
                stripped = line.strip()

                if stripped.startswith("```"):
                    if in_code_block:
                        llm_html += '</pre>\n'
                        in_code_block = False
                    else:
                        llm_html += '<pre>\n'
                        in_code_block = True
                    continue
                if in_code_block:
                    llm_html += _esc(line) + "\n"
                    continue

                if stripped.startswith("## "):
                    llm_html += f'<h3 style="font-size:1rem;margin:1rem 0 .4rem;color:var(--accent)">{_md_inline(stripped[3:])}</h3>\n'
                elif stripped.startswith("**") and stripped.endswith("**") and stripped.count("**") == 2:
                    llm_html += f'<p style="font-weight:600;margin:.6rem 0 .2rem">{_md_inline(stripped)}</p>\n'
                elif stripped.startswith("- "):
                    llm_html += f'<div style="padding-left:1rem;margin:.2rem 0">• {_md_inline(stripped[2:])}</div>\n'
                elif re.match(r'^\d+\.', stripped):
                    llm_html += f'<div style="padding-left:1rem;margin:.2rem 0">{_md_inline(stripped)}</div>\n'
                elif stripped:
                    llm_html += f'<p style="margin:.3rem 0">{_md_inline(stripped)}</p>\n'

            _llm_label = "Claude"
            if report_obj:
                for f in report_obj.findings:
                    if f.agent_name == "DeepReasoningAgent" and f.metadata.get("llm_model"):
                        _llm_label = f.metadata["llm_model"]
                        break

            h += '<div class="card" style="border-left:3px solid var(--accent);background:linear-gradient(135deg,rgba(129,140,248,.04),rgba(167,139,250,.06))">\n'
            h += f'<h2>🧠 AI Deep Reasoning <span style="font-size:.75rem;font-weight:400;color:var(--muted)">({_esc(_llm_label)})</span></h2>\n'
            h += llm_html
            h += '</div>\n'

        # Diagnosis
        if trace_data.get("diagnosis"):
            h += '<div class="card"><h2>⚡ Diagnosis</h2>\n'
            h += f'<p style="font-size:.95rem">{_esc(trace_data["diagnosis"])}</p></div>\n'

    # ── Footer ──
    h += f'<div class="footer">Generated by <strong>Inferra</strong> v0.5.0 · {ts}</div>\n'
    h += '</div></body></html>'

    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
    with open(output_path, "w") as f:
        f.write(h)

    return output_path
