#!/usr/bin/env python3
"""
analyze_project.py — Universal Python Project Analyzer

Point this at any Python project to:
1. Index the entire codebase (AST + SQL + YAML + config parsing)
2. Show codebase stats and structure
3. Search for code related to any query
4. Discover and run real functions with tracing to catch real bugs
5. Save structured reports to JSON/Markdown files

Usage:
    python3 analyze_project.py /path/to/project
    python3 analyze_project.py /path/to/project --trace
    python3 analyze_project.py /path/to/project --search
    python3 analyze_project.py /path/to/project --trace --output report.html
    python3 analyze_project.py /path/to/project --trace --output report.json

Environment:
    ANTHROPIC_API_KEY     — Enable AI-powered deep reasoning (Claude)
    AWS_ACCESS_KEY_ID     — Enable S3 report upload
    AWS_SECRET_ACCESS_KEY — AWS credentials
    INFERRA_S3_BUCKET     — Default S3 bucket for reports
"""

import sys
import os
import json
import time
import logging
import importlib
import importlib.util
import traceback
from datetime import datetime

# Ensure async_content_tracer is importable
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from async_content_tracer.context import (
    ContextManager, TracedThreadPoolExecutor,
    _context_id, _parent_span_id, _trace_depth,
)
from async_content_tracer.tracer import Tracer
from async_content_tracer.graph import ExecutionGraph
from inferra.rca_engine import RCAEngine
from report_html import generate_html_report

# --- Logging setup ---------------------------------------------------------

log = logging.getLogger("inferra")


class _Fmt(logging.Formatter):
    """Compact, structured formatter — no emojis, just clear labels."""
    FORMATS = {
        logging.DEBUG:    "\033[90m  [DEBUG]  %(message)s\033[0m",
        logging.INFO:     "  [INFO]   %(message)s",
        logging.WARNING:  "\033[33m  [WARN]   %(message)s\033[0m",
        logging.ERROR:    "\033[31m  [ERROR]  %(message)s\033[0m",
        logging.CRITICAL: "\033[1;31m  [CRIT]   %(message)s\033[0m",
    }

    def format(self, record):
        fmt = self.FORMATS.get(record.levelno, self.FORMATS[logging.INFO])
        return logging.Formatter(fmt).format(record)


def _setup_logging():
    handler = logging.StreamHandler()
    handler.setFormatter(_Fmt())
    log.addHandler(handler)
    log.setLevel(logging.INFO)


# --- UI helpers -----------------------------------------------------------

_SEP = "─" * 70


def _section(title):
    """Print a clean section header."""
    print(f"\n{_SEP}")
    print(f"  {title}")
    print(_SEP)


# --- Core pipeline --------------------------------------------------------

def analyze_codebase(project_path):
    """Index and analyze a codebase (Python + SQL + configs)."""
    _section(f"Indexing: {project_path}")

    engine = RCAEngine()
    engine.index_codebase(
        project_path,
        exclude_patterns=[
            "__pycache__", ".git", "venv", ".venv", "node_modules",
            "htmlcov", ".egg-info", "dist", "build", ".tox",
            ".pytest_cache", ".mypy_cache", "migrations",
        ]
    )

    stats = engine.stats()
    log.info(
        "Indexed %d code units across %d files  (%d functions, %d classes)",
        stats['total_units'], stats['files_indexed'],
        stats['functions'], stats['classes'],
    )
    if stats.get('sql_models', 0) > 0:
        log.info("SQL models: %d", stats['sql_models'])
    if stats.get('config_entries', 0) > 0:
        log.info("Config entries: %d", stats['config_entries'])
    log.info("Log patterns: %d  |  Search tokens: %d",
             stats['log_patterns'], stats['unique_tokens'])

    if stats['total_units'] == 0:
        log.warning("No code found — is the path correct?")
        return None, None, stats

    return engine, engine._indexer, stats


def interactive_search(indexer):
    """Let the user search the codebase interactively."""
    _section("Code Search  (type 'quit' to stop)")
    print()

    while True:
        try:
            query = input("  search> ").strip()
        except (EOFError, KeyboardInterrupt):
            break

        if query.lower() in ('quit', 'exit', 'q', ''):
            break

        results = indexer.search(query, top_k=5)
        if results:
            print(f"\n  {len(results)} results:\n")
            for i, r in enumerate(results, 1):
                print(f"  {i}. {r.code_unit.name} ({r.code_unit.unit_type})")
                print(f"     {os.path.basename(r.code_unit.source_file)}:{r.code_unit.start_line}")
                if r.code_unit.docstring:
                    doc = r.code_unit.docstring[:80].replace('\n', ' ')
                    print(f"     {doc}")
                print(f"     score: {r.score:.3f}")
                print()
        else:
            unit = indexer.search_by_function_name(query)
            if unit:
                print(f"\n  Found: {unit.qualified_name}")
                print(f"  {unit.source_file}:{unit.start_line}-{unit.end_line}")
                print(f"  Signature: {unit.signature}\n")
            else:
                print(f"\n  No results for '{query}'.\n")


def discover_entry_points(indexer, project_path):
    """
    Discover callable entry points in the target project.
    Looks for: main(), run(), app factories, CLI functions, pipeline functions.
    """
    entry_patterns = [
        "main", "run", "start", "execute", "pipeline",
        "run_pipeline", "run_full_pipeline", "process",
        "handle", "serve", "app", "create_app",
    ]

    discovered = []

    for pattern in entry_patterns:
        unit = indexer.search_by_function_name(pattern)
        if unit and unit.unit_type in ("function", "async_function"):
            discovered.append(unit)

    results = indexer.search("main run pipeline process execute", top_k=10)
    for r in results:
        u = r.code_unit
        if u.unit_type in ("function", "async_function") and u not in discovered:
            discovered.append(u)

    return discovered[:10]


def try_import_and_trace(unit, tracer, project_path):
    """
    Try to import a function from the target project and call it with tracing.
    Returns (success, result_or_error).
    """
    if project_path not in sys.path:
        sys.path.insert(0, project_path)

    parent = os.path.dirname(project_path)
    if parent not in sys.path:
        sys.path.insert(0, parent)

    filepath = unit.source_file
    func_name = unit.name

    try:
        spec = importlib.util.spec_from_file_location(
            f"target_module_{func_name}", filepath,
            submodule_search_locations=[]
        )
        if spec is None or spec.loader is None:
            return False, f"Could not load module from {filepath}"

        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        func = getattr(module, func_name, None)
        if func is None or not callable(func):
            return False, f"Function '{func_name}' not found or not callable"

        traced_func = tracer.trace(func)
        result = traced_func()
        return True, result

    except SystemExit:
        return False, "SystemExit — function tried to exit"
    except KeyboardInterrupt:
        return False, "KeyboardInterrupt"
    except BaseException as e:
        return False, e


def run_real_trace(engine, indexer, project_path):
    """
    Discover real functions in the target project, import and trace them.
    """
    _section("Discovering Entry Points")

    entry_points = discover_entry_points(indexer, project_path)

    if not entry_points:
        log.warning("No callable entry points found.")
        return None

    log.info("Found %d potential entry points:", len(entry_points))
    for i, ep in enumerate(entry_points, 1):
        doc = ""
        if ep.docstring:
            doc = f"  — {ep.docstring[:60].replace(chr(10), ' ')}"
        log.info("  %d. %s()  at %s:%d%s",
                 i, ep.name, os.path.basename(ep.source_file), ep.start_line, doc)

    _section("Tracing Functions")

    ctx = ContextManager()
    tracer = Tracer(context_manager=ctx)
    _context_id.set(None)
    _parent_span_id.set(None)
    _trace_depth.set(0)
    ctx.new_context()

    successes = []
    failures = []

    for ep in entry_points[:5]:
        label = f"{ep.name}() from {os.path.basename(ep.source_file)}"
        print(f"\n  > {label} ...", end=" ", flush=True)

        success, result = try_import_and_trace(ep, tracer, project_path)

        if success:
            print("OK")
            successes.append((ep, result))
        else:
            if isinstance(result, Exception):
                err_type = type(result).__name__
                err_msg = str(result)[:100]
                print(f"FAIL  {err_type}: {err_msg}")
                failures.append((ep, result))
            else:
                print(f"SKIP  {result}")

    log.info("Results: %d succeeded, %d failed", len(successes), len(failures))

    # Build graph from collected trace events
    if tracer.events:
        _section("Execution Graph")
        graph = ExecutionGraph()
        graph.build_from_events(tracer.events)
        print(graph.summary())
        print()
        print(graph.print_tree())

        if failures:
            _section("Root Cause Analysis")
            report = engine.investigate(tracer.events)
            print(f"\n{report.to_string()}")

            _section("Diagnosis")
            diagnosis = engine.quick_diagnosis(tracer.events)
            print(f"\n{diagnosis}")

            return {
                "graph_summary": graph.summary(),
                "graph_tree": graph.print_tree(),
                "report": report,
                "report_str": report.to_string(),
                "diagnosis": diagnosis,
                "events": tracer.events,
                "successes": [(ep.name, str(r)) for ep, r in successes],
                "failures": [(ep.name, f"{type(e).__name__}: {str(e)[:200]}") for ep, e in failures],
                "entry_points": [ep.name for ep in entry_points],
            }
        else:
            _section("Results")
            log.info("All traced functions completed without errors.")
            log.info("  %d trace events captured", len(tracer.events))
            log.info("  %d functions completed", len(successes))

            return {
                "graph_summary": graph.summary(),
                "graph_tree": graph.print_tree(),
                "report": None,
                "report_str": "No errors detected — all traced functions ran successfully.",
                "diagnosis": "Clean — no errors detected in traced execution.",
                "events": tracer.events,
                "successes": [(ep.name, str(r)) for ep, r in successes],
                "failures": [],
                "entry_points": [ep.name for ep in entry_points],
            }
    else:
        log.warning("No trace events captured — functions may require arguments or environment setup.")

        if failures:
            _section("Import / Execution Errors")
            for ep, err in failures:
                log.error("%s(): %s: %s", ep.name, type(err).__name__, str(err)[:200])

        return {
            "graph_summary": "No trace events captured",
            "graph_tree": "",
            "report": None,
            "report_str": "Functions could not be traced — likely require arguments or environment setup.",
            "diagnosis": f"Found {len(failures)} import/execution error(s) across {len(entry_points)} entry points.",
            "events": [],
            "successes": [(ep.name, str(r)) for ep, r in successes],
            "failures": [(ep.name, f"{type(e).__name__}: {str(e)[:200]}") for ep, e in failures],
            "entry_points": [ep.name for ep in entry_points],
        }


# --- Report helpers -------------------------------------------------------

def save_report_json(project_path, stats, trace_data, output_path):
    """Save a structured JSON report."""
    data = {
        "tool": "inferra",
        "version": "0.3.0",
        "timestamp": datetime.now().isoformat(),
        "project": {
            "path": os.path.abspath(project_path),
            "name": os.path.basename(os.path.abspath(project_path)),
        },
        "indexing": stats,
    }

    if trace_data:
        data["entry_points"] = trace_data.get("entry_points", [])
        data["successes"] = trace_data.get("successes", [])
        data["failures"] = trace_data.get("failures", [])
        data["execution_graph"] = {
            "summary": trace_data["graph_summary"],
            "tree": trace_data["graph_tree"],
        }
        data["diagnosis"] = trace_data["diagnosis"]

        report_obj = trace_data.get("report")
        if report_obj:
            data["rca_report"] = {
                "severity": report_obj.severity.value,
                "confidence": f"{report_obj.confidence:.0%}",
                "root_cause": report_obj.root_cause,
                "summary": report_obj.summary,
                "source_locations": report_obj.source_locations,
                "recommendations": report_obj.recommendations,
                "findings": [
                    {
                        "agent": f.agent_name,
                        "summary": f.summary,
                        "finding_type": f.finding_type.value,
                        "confidence": f"{f.confidence:.0%}",
                        "evidence": f.evidence,
                        "details": f.details,
                    }
                    for f in report_obj.findings
                ],
            }

    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
    with open(output_path, "w") as f:
        json.dump(data, f, indent=2, default=str)

    log.info("JSON report saved: %s", output_path)


def save_report_markdown(project_path, stats, trace_data, output_path):
    """Save a formatted Markdown report."""
    project_name = os.path.basename(os.path.abspath(project_path))

    lines = [
        f"# Analysis Report: {project_name}",
        f"",
        f"**Generated:** {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}  ",
        f"**Tool:** inferra v0.3.0  ",
        f"**Project:** `{os.path.abspath(project_path)}`",
        f"",
        f"---",
        f"",
        f"## Codebase Index",
        f"",
        f"| Metric | Value |",
        f"|---|---|",
        f"| Code units indexed | **{stats['total_units']}** |",
        f"| Files | {stats['files_indexed']} |",
        f"| Functions | {stats['functions']} |",
        f"| Classes | {stats['classes']} |",
        f"| SQL models | {stats.get('sql_models', 0)} |",
        f"| Config entries | {stats.get('config_entries', 0)} |",
        f"| Log patterns | {stats['log_patterns']} |",
        f"| Search tokens | {stats['unique_tokens']} |",
    ]

    if trace_data:
        if trace_data.get("entry_points"):
            lines.extend([f"", f"---", f"", f"## Discovered Entry Points", f""])
            for ep in trace_data["entry_points"]:
                lines.append(f"- `{ep}()`")

        successes = trace_data.get("successes", [])
        failures = trace_data.get("failures", [])

        if successes:
            lines.extend([f"", f"### Successful Traces", f""])
            for name, result in successes:
                lines.append(f"- `{name}()` — {str(result)[:80]}")

        if failures:
            lines.extend([f"", f"### Errors Detected", f""])
            for name, err in failures:
                lines.append(f"- `{name}()` — `{err[:100]}`")

        if trace_data.get("graph_tree"):
            lines.extend([
                f"", f"---", f"", f"## Execution Graph", f"",
                f"```", trace_data["graph_summary"], f"```", f"",
                f"### Call Tree", f"",
                f"```", trace_data["graph_tree"], f"```",
            ])

        report_obj = trace_data.get("report")
        if report_obj:
            lines.extend([
                f"", f"---", f"", f"## Root Cause Analysis", f"",
                f"| Field | Value |", f"|---|---|",
                f"| **Severity** | {report_obj.severity.value.upper()} |",
                f"| **Confidence** | {report_obj.confidence:.0%} |",
                f"| **Root Cause** | {report_obj.root_cause[:100]}... |",
                f"", f"### Summary", f"", f"{report_obj.summary}", f"",
                f"### Source Locations", f"",
            ])
            for loc in report_obj.source_locations:
                lines.append(f"- `{loc}`")

            lines.extend([f"", f"### Agent Findings", f""])
            for finding in report_obj.findings:
                lines.append(f"#### [{finding.agent_name}] {finding.summary[:80]}")
                lines.append(f"- **Type:** {finding.finding_type.value}")
                lines.append(f"- **Confidence:** {finding.confidence:.0%}")
                for ev in finding.evidence:
                    lines.append(f"- {ev}")
                lines.append(f"")

            lines.extend([f"### Recommendations", f""])
            for i, rec in enumerate(report_obj.recommendations, 1):
                lines.append(f"{i}. {rec}")

        lines.extend([
            f"", f"---", f"", f"## Diagnosis", f"",
            f"{trace_data['diagnosis']}",
        ])

    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
    with open(output_path, "w") as f:
        f.write("\n".join(lines))

    log.info("Markdown report saved: %s", output_path)


# --- Main -----------------------------------------------------------------

def main():
    _setup_logging()

    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    project_path = os.path.expanduser(sys.argv[1])
    if not os.path.isdir(project_path):
        log.error("Not a directory: %s", project_path)
        sys.exit(1)

    # Parse args
    args = sys.argv[2:]
    mode = "--all"
    output_path = None
    s3_bucket = None

    for i, arg in enumerate(args):
        if arg in ("--search", "--trace", "--all"):
            mode = arg
        elif arg == "--output" and i + 1 < len(args):
            output_path = args[i + 1]
        elif arg == "--s3-bucket" and i + 1 < len(args):
            s3_bucket = args[i + 1]

    project_name = os.path.basename(os.path.abspath(project_path))
    _section(f"inferra — Analyzing: {project_name}")

    # Step 1: Index
    engine, indexer, stats = analyze_codebase(project_path)
    if engine is None:
        sys.exit(1)

    # Step 2: Search (if requested)
    if mode in ("--search", "--all"):
        interactive_search(indexer)

    # Step 3: Real trace (if requested)
    trace_data = None
    if mode in ("--trace", "--all"):
        trace_data = run_real_trace(engine, indexer, project_path)

    # Step 4: Save report
    if not output_path:
        # Auto-generate HTML report when no --output specified
        project_name = os.path.basename(os.path.abspath(project_path))
        safe_name = project_name.lower().replace(" ", "_").replace("-", "_")
        reports_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "reports")
        os.makedirs(reports_dir, exist_ok=True)
        output_path = os.path.join(reports_dir, f"{safe_name}_report.html")

    if output_path.endswith(".json"):
        save_report_json(project_path, stats, trace_data, output_path)
    elif output_path.endswith(".html"):
        generate_html_report(project_path, stats, trace_data, output_path)
        log.info("HTML report saved: %s", output_path)
        import subprocess
        subprocess.Popen(["open", output_path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    else:
        save_report_markdown(project_path, stats, trace_data, output_path)

    _section("Analysis complete")
    log.info("Report: %s", os.path.abspath(output_path))

    # Step 5: Upload to S3 (if --s3-bucket specified)
    if output_path and s3_bucket:
        try:
            from inferra.aws_integration import upload_report_to_s3
            upload_report_to_s3(output_path, bucket=s3_bucket)
        except ImportError:
            log.warning("boto3 not installed — run: pip install boto3")

    # Display LLM synthesis if available
    if trace_data and trace_data.get("report"):
        llm_text = trace_data["report"].metadata.get("llm_synthesis")
        if llm_text:
            _section("AI Deep Reasoning (Claude)")
            print(f"\n{llm_text}\n")


if __name__ == "__main__":
    main()
