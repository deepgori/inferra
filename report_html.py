"""
report_html.py — Styled HTML Report Generator

Generates a beautifully formatted HTML report with CSS print styles.
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


CSS = """
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&family=JetBrains+Mono:wght@400;500&display=swap');

:root {
  --bg: #f8fafc; --card: #fff; --border: #e2e8f0; --text: #1e293b;
  --muted: #64748b; --accent: #6366f1; --accent-bg: #eef2ff;
  --success: #10b981; --danger: #ef4444; --warning: #f59e0b;
  --code-bg: #1e293b;
}
* { margin: 0; padding: 0; box-sizing: border-box; }
body { font-family: 'Inter', -apple-system, sans-serif; background: var(--bg); color: var(--text); line-height: 1.6; font-size: 14px; }
.container { max-width: 900px; margin: 0 auto; padding: 2rem; }

.header {
  background: linear-gradient(135deg, #312e81, #6366f1, #818cf8);
  color: #fff; padding: 2.5rem 2rem; border-radius: 16px; margin-bottom: 2rem;
  box-shadow: 0 10px 40px rgba(99,102,241,.3);
}
.header h1 { font-size: 1.8rem; font-weight: 700; margin-bottom: .3rem; }
.header .sub { opacity: .85; font-size: .95rem; font-weight: 300; }
.header .meta { margin-top: 1rem; display: flex; gap: 1.5rem; font-size: .85rem; opacity: .75; }

.card {
  background: var(--card); border: 1px solid var(--border); border-radius: 12px;
  padding: 1.5rem; margin-bottom: 1.5rem; box-shadow: 0 1px 3px rgba(0,0,0,.04);
}
.card h2 { font-size: 1.1rem; font-weight: 600; color: var(--accent); margin-bottom: 1rem; }

.stats { display: grid; grid-template-columns: repeat(4,1fr); gap: .8rem; }
.stat { text-align: center; padding: .8rem; background: var(--accent-bg); border-radius: 10px; }
.stat .v { font-size: 1.5rem; font-weight: 700; color: var(--accent); }
.stat .l { font-size: .75rem; color: var(--muted); margin-top: .2rem; }

table { width: 100%; border-collapse: collapse; font-size: .9rem; }
th { text-align: left; padding: .5rem .7rem; background: #f1f5f9; font-weight: 600; border-bottom: 2px solid var(--border); }
td { padding: .5rem .7rem; border-bottom: 1px solid var(--border); }

.badge { display: inline-block; padding: .15rem .5rem; border-radius: 6px; font-size: .75rem; font-weight: 600; }
.b-ok { background: #ecfdf5; color: var(--success); }
.b-err { background: #fef2f2; color: var(--danger); }
.b-info { background: var(--accent-bg); color: var(--accent); }
.b-warn { background: #fffbeb; color: var(--warning); }

pre {
  background: var(--code-bg); color: #e2e8f0; padding: 1rem; border-radius: 8px;
  overflow-x: auto; font-family: 'JetBrains Mono', monospace; font-size: .82rem;
  line-height: 1.5; margin: .6rem 0; white-space: pre-wrap; word-break: break-word;
}
code { font-family: 'JetBrains Mono', monospace; font-size: .85rem; background: #f1f5f9; padding: .1rem .35rem; border-radius: 4px; color: var(--accent); }

.finding { border-left: 3px solid var(--accent); padding: .6rem 1rem; margin: .6rem 0; background: #fafbff; border-radius: 0 8px 8px 0; }
.finding .ag { font-weight: 600; color: var(--accent); font-size: .85rem; }
.finding .ev { color: var(--muted); font-size: .85rem; }

.sev { display: flex; align-items: center; gap: 1.5rem; padding: .8rem 1.2rem; border-radius: 10px; margin-bottom: .8rem; }

.footer { text-align: center; color: var(--muted); font-size: .8rem; padding: 1.5rem 0; border-top: 1px solid var(--border); margin-top: 1rem; }

@media print {
  body { background: #fff; font-size: 12px; }
  .container { max-width: 100%; padding: 0; }
  .card { break-inside: avoid; box-shadow: none; border: 1px solid #ddd; }
  .header,.stats,.stat,.badge,.sev,.finding,pre { -webkit-print-color-adjust: exact; print-color-adjust: exact; }
}
"""


def generate_html_report(project_path, stats, trace_data, output_path):
    """Generate a styled HTML report."""
    project_name = os.path.basename(os.path.abspath(project_path))
    ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    report_obj = trace_data.get("report") if trace_data else None

    sev_color = "#ef4444"
    sev_label = "CRITICAL"
    if report_obj:
        sv = report_obj.severity.value.upper()
        sev_label = sv
        sev_color = {"WARNING": "#f59e0b", "INFO": "#3b82f6"}.get(sv, "#ef4444")

    conf = f"{report_obj.confidence:.0%}" if report_obj else "N/A"

    h = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>Analysis Report — {_esc(project_name)}</title>
<style>{CSS}</style>
</head>
<body>
<div class="container">

<div class="header">
  <h1>🔬 Analysis Report</h1>
  <div class="sub">{_esc(project_name)}</div>
  <div class="meta"><span>📅 {ts}</span><span>🛠️ async_content_tracer + inferra v0.2.0</span></div>
</div>

<div class="card">
  <h2>{stats.get('_section_title', '📦 Codebase Index')}</h2>
  <div class="stats">
    <div class="stat"><div class="v">{stats['total_units']}</div><div class="l">{stats.get('_labels', {}).get('total_units', 'Code Units')}</div></div>
    <div class="stat"><div class="v">{stats['files_indexed']}</div><div class="l">{stats.get('_labels', {}).get('files_indexed', 'Files')}</div></div>
    <div class="stat"><div class="v">{stats['functions']}</div><div class="l">{stats.get('_labels', {}).get('functions', 'Functions')}</div></div>
    <div class="stat"><div class="v">{stats['classes']}</div><div class="l">{stats.get('_labels', {}).get('classes', 'Classes')}</div></div>
  </div>
  <div class="stats" style="margin-top:.6rem">
    <div class="stat"><div class="v">{stats.get('sql_models',0)}</div><div class="l">{stats.get('_labels', {}).get('sql_models', 'SQL Models')}</div></div>
    <div class="stat"><div class="v">{stats.get('config_entries',0)}</div><div class="l">{stats.get('_labels', {}).get('config_entries', 'Config Entries')}</div></div>
    <div class="stat"><div class="v">{stats['log_patterns']}</div><div class="l">{stats.get('_labels', {}).get('log_patterns', 'Log Patterns')}</div></div>
    <div class="stat"><div class="v">{stats['unique_tokens']}</div><div class="l">{stats.get('_labels', {}).get('unique_tokens', 'Search Tokens')}</div></div>
  </div>
</div>
"""

    if trace_data:
        successes = trace_data.get("successes", [])
        failures = trace_data.get("failures", [])
        entry_points = trace_data.get("entry_points", [])

        # Entry points table
        if entry_points:
            success_names = {s[0] for s in successes}
            failure_dict = {f[0]: f[1] for f in failures}
            h += '<div class="card"><h2>🎯 Discovered Entry Points</h2>\n<table><tr><th>Function</th><th>Status</th></tr>\n'
            for ep in entry_points:
                if ep in success_names:
                    badge = '<span class="badge b-ok">✅ Success</span>'
                elif ep in failure_dict:
                    badge = f'<span class="badge b-err">❌ {_esc(failure_dict[ep][:60])}</span>'
                else:
                    badge = '<span class="badge b-info">Not traced</span>'
                h += f'<tr><td><code>{_esc(ep)}()</code></td><td>{badge}</td></tr>\n'
            h += '</table></div>\n\n'

        # Execution graph
        if trace_data.get("graph_tree"):
            h += f'<div class="card"><h2>📊 Execution Graph</h2>\n'
            h += f'<pre>{_esc(trace_data["graph_summary"])}</pre>\n'
            h += f'<h3 style="font-size:.95rem;margin:.8rem 0 .4rem">Call Tree</h3>\n'
            h += f'<pre>{_esc(trace_data["graph_tree"])}</pre>\n</div>\n\n'

        # RCA
        if report_obj:
            h += f'<div class="card"><h2>🔬 Root Cause Analysis</h2>\n'
            h += f'<div class="sev" style="background:{sev_color}15;border:1px solid {sev_color}30">'
            h += f'<div style="font-size:1.3rem;font-weight:700;color:{sev_color}">{sev_label}</div>'
            h += f'<div style="font-size:.9rem;opacity:.9">Confidence: <strong>{conf}</strong></div></div>\n'

            h += f'<h3 style="font-size:.95rem;margin:.8rem 0 .4rem">Root Cause</h3>\n'
            h += f'<p style="color:var(--muted)">{_esc(report_obj.root_cause[:300])}</p>\n'

            h += '<h3 style="font-size:.95rem;margin:.8rem 0 .4rem">Source Locations</h3><ul>\n'
            for loc in report_obj.source_locations:
                h += f'<li><code>{_esc(loc)}</code></li>\n'
            h += '</ul>\n'

            h += '<h3 style="font-size:.95rem;margin:.8rem 0 .4rem">Agent Findings</h3>\n'
            for f in report_obj.findings:
                h += f'<div class="finding"><div class="ag">[{_esc(f.agent_name)}]</div>\n'
                h += f'<div>{_esc(f.summary[:200])}</div>\n'
                h += f'<div class="ev"><span class="badge b-info">{_esc(f.finding_type.value)}</span> '
                h += f'<span class="badge b-warn">{f.confidence:.0%}</span>'
                for ev in f.evidence[:3]:
                    h += f'<br>• {_esc(ev)}'
                h += '</div></div>\n'

            if report_obj.recommendations:
                h += '<h3 style="font-size:.95rem;margin:.8rem 0 .4rem">Recommendations</h3><ol>\n'
                for rec in report_obj.recommendations:
                    h += f'<li>{_esc(rec)}</li>\n'
                h += '</ol>\n'

            h += '</div>\n\n'

        # LLM Deep Reasoning (Claude)
        llm_text = None
        if report_obj and report_obj.metadata.get("llm_synthesis"):
            llm_text = report_obj.metadata["llm_synthesis"]
        if not llm_text and report_obj:
            # Check if DeepReasoningAgent produced a finding with raw_response
            for f in report_obj.findings:
                if f.agent_name == "DeepReasoningAgent" and f.metadata.get("raw_response"):
                    llm_text = f.metadata["raw_response"]
                    break

        if llm_text:
            import re

            def _md_inline(text):
                """Convert inline markdown (bold, code) to HTML."""
                # Process markdown BEFORE escaping — work on raw text
                # Replace **bold** with placeholder tags
                t = re.sub(r'\*\*(.+?)\*\*', r'⟨BOLD⟩\1⟨/BOLD⟩', text)
                # Replace `code` with placeholder tags
                t = re.sub(r'`([^`]+)`', r'⟨CODE⟩\1⟨/CODE⟩', t)
                # Now escape the whole thing
                t = _esc(t)
                # Restore the placeholder tags to real HTML
                t = t.replace('⟨BOLD⟩', '<strong>').replace('⟨/BOLD⟩', '</strong>')
                t = t.replace('⟨CODE⟩', '<code>').replace('⟨/CODE⟩', '</code>')
                return t

            llm_html = ""
            in_code_block = False
            for line in llm_text.split("\n"):
                stripped = line.strip()

                # Code blocks
                if stripped.startswith("```"):
                    if in_code_block:
                        llm_html += '</pre>\n'
                        in_code_block = False
                    else:
                        llm_html += '<pre style="background:var(--code-bg);color:#e2e8f0;padding:.8rem 1rem;border-radius:8px;font-size:.82rem;margin:.4rem 0">\n'
                        in_code_block = True
                    continue
                if in_code_block:
                    llm_html += _esc(line) + "\n"
                    continue

                # Headings
                if stripped.startswith("## "):
                    llm_html += f'<h3 style="font-size:1rem;margin:1rem 0 .4rem;color:var(--accent)">{_md_inline(stripped[3:])}</h3>\n'
                # Standalone bold line
                elif stripped.startswith("**") and stripped.endswith("**") and stripped.count("**") == 2:
                    llm_html += f'<p style="font-weight:600;margin:.6rem 0 .2rem">{_md_inline(stripped)}</p>\n'
                # Bullet points
                elif stripped.startswith("- "):
                    llm_html += f'<div style="padding-left:1rem;margin:.2rem 0">• {_md_inline(stripped[2:])}</div>\n'
                # Numbered items
                elif re.match(r'^\d+\.', stripped):
                    llm_html += f'<div style="padding-left:1rem;margin:.2rem 0">{_md_inline(stripped)}</div>\n'
                elif stripped:
                    llm_html += f'<p style="margin:.3rem 0">{_md_inline(stripped)}</p>\n'

            h += '<div class="card" style="border-left:3px solid #6366f1;background:linear-gradient(135deg,#fafbff,#eef2ff)">\n'
            # Determine which LLM backend was used
            _llm_label = "Claude"  # default
            if report_obj:
                for f in report_obj.findings:
                    if f.agent_name == "DeepReasoningAgent" and f.metadata.get("llm_model"):
                        _llm_label = f.metadata["llm_model"]
                        break
            h += f'<h2>🧠 AI Deep Reasoning <span style="font-size:.75rem;font-weight:400;color:var(--muted)">({_esc(_llm_label)})</span></h2>\n'
            h += llm_html
            h += '</div>\n\n'

        # Diagnosis
        h += f'<div class="card"><h2>⚡ Diagnosis</h2>\n'
        h += f'<p>{_esc(trace_data.get("diagnosis", "N/A"))}</p></div>\n\n'

    h += f'<div class="footer">Generated by <strong>async_content_tracer + inferra</strong> v0.2.0 · {ts}</div>\n'
    h += '</div></body></html>'

    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
    with open(output_path, "w") as f:
        f.write(h)

    return output_path
