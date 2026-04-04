"""
inferra.api — Public Python API for Inferra

Usage:
    import inferra
    report = inferra.analyze("./my_project")
    report = inferra.analyze("./my_project", llm="groq", model="moonshotai/kimi-k2-instruct")

    # Access results
    print(report.confidence)       # "92%"
    print(report.root_cause)       # "Sequential pipeline..."
    print(report.report_path)      # "/path/to/report.html"
    report.open()                  # Opens HTML report in browser
"""

import os
import sys
import logging

log = logging.getLogger("inferra")


class AnalysisResult:
    """Structured result from inferra.analyze()."""

    def __init__(self, stats, trace_data, report_path):
        self.stats = stats
        self.trace_data = trace_data
        self.report_path = report_path

        # Extract key fields
        rca = trace_data.get("report") if trace_data else None
        self.root_cause = rca.root_cause if rca else "No issues detected"
        self.confidence = f"{rca.confidence:.0%}" if rca else "N/A"
        self.severity = rca.severity.value if rca else "none"
        self.diagnosis = trace_data.get("diagnosis", "") if trace_data else ""
        self.recommendations = rca.recommendations if rca else []

        # Summary stats
        self.files_indexed = stats.get("files_indexed", 0)
        self.total_units = stats.get("total_units", 0)
        self.functions = stats.get("functions", 0)
        self.classes = stats.get("classes", 0)

    def open(self):
        """Open the HTML report in the default browser."""
        if self.report_path and os.path.exists(self.report_path):
            import subprocess
            subprocess.Popen(["open", self.report_path],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        else:
            print("No report file available.")

    def __repr__(self):
        return (
            f"AnalysisResult(files={self.files_indexed}, units={self.total_units}, "
            f"confidence={self.confidence}, severity={self.severity})"
        )


def analyze(project_path, llm=None, model=None, output_path=None, skip_search=True):
    """
    Analyze a Python project — index, trace, and generate an RCA report.

    Args:
        project_path: Path to the Python project directory
        llm: LLM backend ("claude", "groq", "ollama", or None for auto)
        model: Specific model name (e.g., "moonshotai/kimi-k2-instruct")
        output_path: Output report path (.html, .json, .md). Auto-generated if None.
        skip_search: Skip the interactive search prompt (default: True)

    Returns:
        AnalysisResult with stats, trace data, and report path
    """
    # Ensure the project root is importable
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if root not in sys.path:
        sys.path.insert(0, root)

    project_path = os.path.expanduser(project_path)
    if not os.path.isdir(project_path):
        raise ValueError(f"Not a directory: {project_path}")

    # Set up LLM backend if not already done
    if model:
        os.environ["INFERRA_LLM_MODEL"] = model

    from inferra.llm_agent import get_llm_backend, get_active_backend
    if not get_active_backend():
        backend = get_llm_backend(llm)
        if backend:
            log.info("LLM backend: %s", backend.display_name)

    # Import analysis functions from analyze_project.py
    from analyze_project import (
        analyze_codebase,
        interactive_search,
        run_real_trace,
        save_report_json,
        save_report_markdown,
        _setup_logging,
        _section,
    )
    from report_html import generate_html_report

    _setup_logging()

    project_name = os.path.basename(os.path.abspath(project_path))
    _section(f"inferra — Analyzing: {project_name}")

    # Step 1: Index
    engine, indexer, stats = analyze_codebase(project_path)
    if engine is None:
        return AnalysisResult(stats or {}, None, None)

    # Step 2: Search (skip by default in API mode)
    if not skip_search:
        interactive_search(indexer)

    # Step 3: Trace
    trace_data = run_real_trace(engine, indexer, project_path)

    # Step 4: Generate report
    if not output_path:
        safe_name = project_name.lower().replace(" ", "_").replace("-", "_")
        reports_dir = os.path.join(root, "reports")
        os.makedirs(reports_dir, exist_ok=True)
        output_path = os.path.join(reports_dir, f"{safe_name}_report.html")

    if output_path.endswith(".json"):
        save_report_json(project_path, stats, trace_data, output_path)
    elif output_path.endswith(".html"):
        # Run security scan for richer report
        sec_findings = []
        try:
            from inferra.security_agent import SecurityAgent
            sec = SecurityAgent()
            for root_dir, dirs, files in os.walk(project_path):
                dirs[:] = [d for d in dirs if d not in ('.git', 'node_modules', '__pycache__', '.venv', 'venv')]
                for fname in files:
                    if fname.endswith(('.py', '.js', '.ts', '.jsx', '.tsx', '.go', '.java')):
                        fpath = os.path.join(root_dir, fname)
                        rel = os.path.relpath(fpath, project_path)
                        if '/test' in rel or rel.startswith('scripts/'):
                            continue
                        try:
                            content = open(fpath).read()
                            sec_findings.extend(sec._scan_file_patterns(fpath, content))
                        except Exception:
                            pass
        except Exception:
            pass

        generate_html_report(
            project_path, stats, trace_data, output_path,
            indexer=indexer, security_findings=sec_findings,
        )
        log.info("HTML report saved: %s", output_path)
    else:
        save_report_markdown(project_path, stats, trace_data, output_path)

    _section("Analysis complete")
    log.info("Report: %s", os.path.abspath(output_path))

    return AnalysisResult(stats, trace_data, output_path)
