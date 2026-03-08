"""
inferra — AI-Powered Observability & RCA Engine

Usage:
    python -m inferra serve [--port 4318] [--llm claude|ollama|auto]
    python -m inferra analyze <path>
"""

import sys


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    cmd = sys.argv[1]

    if cmd == "serve":
        from inferra.otlp_receiver import serve
        import argparse
        parser = argparse.ArgumentParser(description="Inferra OTLP Receiver")
        parser.add_argument("--host", default="0.0.0.0")
        parser.add_argument("--port", type=int, default=4318)
        parser.add_argument("--project", type=str, default=None,
                            help="Path to project codebase for source correlation")
        parser.add_argument("--llm", type=str, default="auto",
                            choices=["claude", "ollama", "local", "auto"],
                            help="LLM backend: claude (cloud), ollama/local (Qwen), auto (detect)")
        # Skip the 'serve' arg
        args = parser.parse_args(sys.argv[2:])

        # Initialize LLM backend
        llm_choice = None if args.llm == "auto" else args.llm
        from inferra.llm_agent import get_llm_backend
        backend = get_llm_backend(llm_choice)
        if backend:
            print(f"   🧠 LLM backend: {backend.display_name}")
        else:
            print("   ⚠️  No LLM backend available (analysis will use rule-based agents only)")

        serve(host=args.host, port=args.port, project=args.project)

    elif cmd == "analyze":
        # Delegate to analyze_project.py
        import os
        script = os.path.join(os.path.dirname(__file__), "..", "analyze_project.py")
        sys.argv = [script] + sys.argv[2:]
        exec(open(script).read())

    else:
        print(f"Unknown command: {cmd}")
        print(__doc__)
        sys.exit(1)


if __name__ == "__main__":
    main()
