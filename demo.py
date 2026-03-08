#!/usr/bin/env python3
"""
demo.py — One-command Inferra demo

Usage:
    python3 demo.py                                    # uses cybersec project (default)
    python3 demo.py ./test_projects/gs-quant/gs_quant  # any project path
    python3 demo.py <github-url>                       # clone + analyze

Starts OTLP receiver, instrumented app, sends traffic, triggers analysis,
and opens the HTML report — all in one shot.
"""

import os
import sys
import time
import json
import signal
import subprocess
import urllib.request
import urllib.error

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
GROQ_KEY = os.environ.get("GROQ_API_KEY", "")
LLM_MODEL = os.environ.get("INFERRA_LLM_MODEL", "moonshotai/kimi-k2-instruct")

# ── Resolve target project ──────────────────────────────────────────
target = sys.argv[1] if len(sys.argv) > 1 else "./test_projects/agentic_cybersec_threat_analyst/backend"

if target.startswith("http"):
    # Clone from GitHub
    repo_name = target.rstrip("/").split("/")[-1].replace(".git", "")
    dest = os.path.join(PROJECT_DIR, "test_projects", repo_name)
    if not os.path.exists(dest):
        print(f"\n📥 Cloning {target} ...")
        subprocess.run(["git", "clone", "--depth=1", target, dest], check=True)
    # Auto-detect Python source dir
    for candidate in ["src", "app", repo_name, repo_name.replace("-", "_"), "."]:
        if os.path.isdir(os.path.join(dest, candidate)):
            target = os.path.join(dest, candidate)
            break
    else:
        target = dest
    print(f"   → Using: {target}")

# ── Pick runner if available ─────────────────────────────────────────
RUNNERS = {
    "agentic_cybersec_threat_analyst": ("run_cybersec_instrumented.py", 8002),
    "gs-quant": ("run_gsquant_instrumented.py", 8001),
}

runner_file = None
api_port = None
for key, (rf, port) in RUNNERS.items():
    if key in target:
        runner_path = os.path.join(PROJECT_DIR, rf)
        if os.path.exists(runner_path):
            runner_file = runner_path
            api_port = port
        break

processes = []

def cleanup(*_):
    print("\n🧹 Cleaning up...")
    for p in processes:
        try: p.terminate()
        except: pass
    sys.exit(0)

signal.signal(signal.SIGINT, cleanup)
signal.signal(signal.SIGTERM, cleanup)

try:
    # ── Step 1: Kill existing processes on ports ─────────────────────
    print("\n" + "═" * 60)
    print("  🔬 Inferra Demo — One-Command Pipeline")
    print("═" * 60)

    for port in [4318, api_port or 0]:
        if port:
            os.system(f"lsof -ti :{port} | xargs kill -9 2>/dev/null")
    time.sleep(1)

    env = {
        **os.environ,
        "GROQ_API_KEY": GROQ_KEY,
        "INFERRA_LLM_BACKEND": "groq",
        "INFERRA_LLM_MODEL": LLM_MODEL,
    }

    # ── Step 2: Start OTLP receiver ─────────────────────────────────
    print(f"\n📡 Starting OTLP receiver (indexing {os.path.basename(target)})...")
    otlp = subprocess.Popen(
        [sys.executable, "-m", "inferra", "serve", "--project", target, "--port", "4318"],
        cwd=PROJECT_DIR, env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )
    processes.append(otlp)

    # Wait for OTLP to be ready
    for _ in range(60):
        try:
            urllib.request.urlopen("http://localhost:4318/healthz", timeout=2)
            break
        except:
            time.sleep(1)
    else:
        print("   ❌ OTLP receiver failed to start")
        cleanup()

    print("   ✅ OTLP receiver ready")

    # ── Step 3: Start instrumented app (if runner exists) ────────────
    if runner_file:
        print(f"\n🚀 Starting instrumented app on port {api_port}...")
        app = subprocess.Popen(
            [sys.executable, runner_file],
            cwd=PROJECT_DIR, env=env,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        )
        processes.append(app)
        time.sleep(3)
        print(f"   ✅ App running on http://localhost:{api_port}")

        # ── Step 4: Send traffic ────────────────────────────────────
        print("\n📨 Sending traffic...")
        BASE = f"http://localhost:{api_port}"
        ok = fail = 0

        if "cybersec" in target:
            for cve in ["CVE-2021-44228", "CVE-2023-44487", "CVE-2024-3094", "CVE-2024-21762", "CVE-2023-23397"]:
                try:
                    urllib.request.urlopen(urllib.request.Request(
                        f"{BASE}/api/analyze",
                        json.dumps({"cve_id": cve}).encode(),
                        {"Content-Type": "application/json"},
                    ), timeout=30)
                    ok += 1
                except:
                    fail += 1
                time.sleep(0.3)
            for p in ["/api/health", "/api/stats", "/api/feed/recent"]:
                try: urllib.request.urlopen(f"{BASE}{p}", timeout=10); ok += 1
                except: fail += 1

        elif "gsquant" in target or "gs-quant" in target:
            for t in ["AAPL", "GOOGL", "MSFT", "NVDA", "GS"]:
                try:
                    urllib.request.urlopen(urllib.request.Request(
                        f"{BASE}/api/v1/analytics/returns",
                        json.dumps({"ticker": t, "days": 252, "window": 22}).encode(),
                        {"Content-Type": "application/json"},
                    ), timeout=15)
                    ok += 1
                except:
                    fail += 1
                time.sleep(0.2)

        time.sleep(3)
        print(f"   ✅ {ok} ok / {fail} fail")
    else:
        print(f"\n⚠️  No instrumented runner for this project — using static analysis only")

    # ── Step 5: Trigger analysis ─────────────────────────────────────
    print("\n🧠 Triggering RCA analysis...")
    try:
        resp = urllib.request.urlopen(
            urllib.request.Request("http://localhost:4318/v1/analyze", method="POST"),
            timeout=180,
        )
        result = json.loads(resp.read().decode())
        print(f"   ✅ Analysis complete!")
        print(f"   📊 Spans: {result.get('span_count', 'N/A')}")
        print(f"   🔗 Code correlations: {result.get('code_correlations', 'N/A')}")
        print(f"   🎯 Confidence: {result.get('confidence', 'N/A')}")
        print(f"   🔍 Root cause: {result.get('root_cause', 'N/A')[:80]}")
        report = result.get("report_path", "")
    except Exception as e:
        print(f"   ❌ Analysis failed: {e}")
        report = ""

    # ── Step 6: Open report ──────────────────────────────────────────
    if report and os.path.exists(report):
        print(f"\n📄 Opening report: {os.path.basename(report)}")
        subprocess.run(["open", report])
    else:
        print("\n   ℹ️  No report generated")

    print("\n" + "═" * 60)
    print("  ✨ Demo complete! Press Ctrl+C to stop servers.")
    print("═" * 60 + "\n")

    # Keep running until Ctrl+C
    while True:
        time.sleep(1)

except KeyboardInterrupt:
    cleanup()
except Exception as e:
    print(f"\n❌ Error: {e}")
    cleanup()
