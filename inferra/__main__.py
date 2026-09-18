"""
inferra — AI-Powered Autonomous Debugging Engine

CLI Usage:
    inferra analyze <path>                        # Analyze any Python project
    inferra analyze <path> --llm groq             # Use Groq backend
    inferra analyze <path> --llm claude           # Use Claude backend
    inferra analyze <path> --llm ollama           # Use local Ollama
    inferra serve --project <path> --port 4318    # Start OTLP receiver

Python API:
    import inferra
    report = inferra.analyze("./my_project")
    report = inferra.analyze("./my_project", llm="groq")
"""

import sys
import os
import argparse


def main():
    parser = argparse.ArgumentParser(
        prog="inferra",
        description="Autonomous debugging engine — trace-to-code correlation via AST analysis and LLM synthesis",
    )
    sub = parser.add_subparsers(dest="command")

    # ── inferra analyze ──────────────────────────────────────────────
    analyze_p = sub.add_parser("analyze", help="Analyze a Python project")
    analyze_p.add_argument("path", help="Path to project directory")
    analyze_p.add_argument("--llm", default="auto",
                           choices=["claude", "groq", "ollama", "local", "auto"],
                           help="LLM backend (default: auto-detect)")
    analyze_p.add_argument("--model", default=None,
                           help="Specific model name (e.g., moonshotai/kimi-k2-instruct)")
    analyze_p.add_argument("--output", "-o", default=None,
                           help="Output path (.html, .json, or .md)")
    analyze_p.add_argument("--no-search", action="store_true",
                           help="Skip interactive search prompt")
    analyze_p.add_argument("--file-issue", metavar="OWNER/REPO", default=None,
                           help="File the diagnosis as a GitHub issue via Composio")
    analyze_p.add_argument("--issue-label", action="append", default=None,
                           help="Label to add to the filed issue (repeatable)")

    # ── inferra serve ────────────────────────────────────────────────
    serve_p = sub.add_parser("serve", help="Start OTLP receiver for live tracing")
    serve_p.add_argument("--host", default="0.0.0.0")
    serve_p.add_argument("--port", type=int, default=4318)
    serve_p.add_argument("--project", type=str, default=None,
                         help="Path to project codebase for source correlation")
    serve_p.add_argument("--llm", type=str, default="auto",
                         choices=["claude", "groq", "ollama", "local", "auto"],
                         help="LLM backend")
    serve_p.add_argument("--model", default=None,
                         help="Specific model name")

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(1)

    # Set model env var if specified
    if getattr(args, "model", None):
        os.environ["INFERRA_LLM_MODEL"] = args.model

    # Initialize LLM backend
    llm_choice = None if args.llm == "auto" else args.llm
    from inferra.llm_agent import get_llm_backend
    backend = get_llm_backend(llm_choice)
    if backend:
        print(f"   🧠 LLM backend: {backend.display_name}")
    else:
        print("   ⚠️  No LLM backend available (analysis will use rule-based agents only)")

    if args.command == "serve":
        from inferra.otlp_receiver import serve
        serve(host=args.host, port=args.port, project=args.project)

    elif args.command == "analyze":
        from inferra.api import analyze
        result = analyze(
            project_path=args.path,
            output_path=args.output,
            skip_search=args.no_search,
        )
        if args.file_issue:
            _file_issue(result, args.file_issue, args.issue_label)


def _file_issue(result, repo, labels):
    if result.report is None:
        print("   ℹ️  No diagnosis to file.")
        return

    from inferra.github_issues import GitHubIssueFiler, GitHubNotConnected, IssueFilingError
    try:
        filer = GitHubIssueFiler()
        url = filer.connect_url()
        if url:
            print(f"   🔗 Connect GitHub so Inferra can file issues: {url}")
            print("   Waiting for authorization (up to 3 minutes)...")
            filer.wait_for_connection()
        issue = filer.file(result.report, repo, result.project_name, labels=labels)
    except (ImportError, RuntimeError, ValueError, GitHubNotConnected, IssueFilingError) as exc:
        print(f"   ❌ Could not file issue: {exc}")
        sys.exit(1)

    verb = "Updated existing" if issue.duplicate else "Filed"
    print(f"   🐛 {verb} issue #{issue.number}: {issue.url}")


if __name__ == "__main__":
    main()
