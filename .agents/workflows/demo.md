---
description: Interview demo script for Inferra — run the full OTLP pipeline
---

# Inferra Demo Script

> **Best project to demo:** `agentic_cybersec_threat_analyst` (multi-agent pipeline, real CVEs, rich architecture)

## Prerequisites (do ONCE before the interview)

```bash
cd ~/Desktop/async_content_tracer
pip install -e .          # install inferra
ollama serve              # start in separate terminal (for Qwen comparison)
```

---

## Terminal 1 — Start the OTLP Receiver + Code Indexer

Pick ONE backend to demo (Groq is fastest for live demo):

```bash
# Option A: Groq (fastest — ~3s analysis)
GROQ_API_KEY=$GROQ_API_KEY \
INFERRA_LLM_BACKEND=groq \
INFERRA_LLM_MODEL=moonshotai/kimi-k2-instruct \
python3 -m inferra serve \
  --project ./test_projects/agentic_cybersec_threat_analyst/backend \
  --port 4318

# Option B: Claude (highest quality — ~8s analysis)
ANTHROPIC_API_KEY=$ANTHROPIC_API_KEY \
INFERRA_LLM_BACKEND=claude \
python3 -m inferra serve \
  --project ./test_projects/agentic_cybersec_threat_analyst/backend \
  --port 4318

# Option C: Ollama/Qwen (local, no API key — ~15s analysis)
INFERRA_LLM_BACKEND=ollama \
python3 -m inferra serve \
  --project ./test_projects/agentic_cybersec_threat_analyst/backend \
  --port 4318
```

**What to say:** _"Inferra just indexed 284 code units across 35 files — functions, classes, SQL models, config entries. It extracted 1,460 search tokens for RAG retrieval."_

---

## Terminal 2 — Start the Instrumented Application

```bash
cd ~/Desktop/async_content_tracer
python3 run_cybersec_instrumented.py
```

**What to say:** _"This is the cybersecurity threat analyst API with OpenTelemetry instrumentation. Every function call emits spans with code.filepath attributes that Inferra will correlate back to source."_

---

## Terminal 3 — Send Traffic

```bash
cd ~/Desktop/async_content_tracer

# Quick: analyze Log4Shell (CVE-2021-44228) + feeds
curl -s -X POST http://localhost:8002/api/analyze \
  -H 'Content-Type: application/json' \
  -d '{"cve_id":"CVE-2021-44228"}' | python3 -m json.tool

# Or send full diverse traffic (5 CVEs, feeds, health checks)
python3 -c "
import urllib.request, json, time
BASE = 'http://localhost:8002'
ok=0
for cve in ['CVE-2021-44228','CVE-2023-44487','CVE-2024-3094','CVE-2024-21762','CVE-2023-23397']:
    urllib.request.urlopen(urllib.request.Request(f'{BASE}/api/analyze', json.dumps({'cve_id':cve}).encode(), {'Content-Type':'application/json'}), timeout=30)
    ok+=1; time.sleep(0.3)
for p in ['/api/health','/api/stats','/api/feed/recent','/api/feed/otx','/api/feed/threatfox']:
    urllib.request.urlopen(f'{BASE}{p}', timeout=10); ok+=1
time.sleep(2)
print(f'✅ {ok} requests sent')
"
```

**What to say:** _"I'm hitting the API with real CVE analysis requests. Each request triggers a 3-agent pipeline — CVE Extractor, ATT&CK Classifier, Playbook Generator — generating hundreds of spans."_

---

## Terminal 3 — Trigger RCA Analysis

```bash
curl -s -X POST http://localhost:4318/v1/analyze | python3 -m json.tool
```

**What to say:** _"Now Inferra correlates the spans to source code, builds an execution graph, and sends it to the LLM for deep reasoning. Watch — it found 29 code correlations, identified the sequential pipeline bottleneck, and recommends asyncio.gather for parallelization."_

---

## Open the HTML Report

```bash
open reports/otlp_cybersec_threat_analyst_report.html
```

**What to say:** _"The report shows the full call tree, agent findings, and the AI Deep Reasoning section with source-level code references."_

---

## Bonus: Switch Backends Live (show multi-LLM support)

Kill Terminal 1 (Ctrl+C) and restart with a different backend — the report will show a different LLM label:

```bash
# Switch to Claude for comparison
ANTHROPIC_API_KEY=sk-ant-api03-... INFERRA_LLM_BACKEND=claude \
python3 -m inferra serve --project ./test_projects/agentic_cybersec_threat_analyst/backend --port 4318
```

Re-send traffic → re-trigger analysis → compare reports side-by-side.

---

## Pre-Generated Reports (if demo breaks)

If anything goes wrong during the live demo, open pre-generated reports:

```bash
open reports/otlp_cybersec_claude_report.html
open reports/otlp_cybersec_groq_kimik2_report.html
open reports/otlp_cybersec_groq_gptoss120b_report.html
open reports/otlp_cybersec_groq_llama70b_report.html
open reports/otlp_cybersec_qwen_report.html
```

---

## Talking Points

1. **"Inferra bridges telemetry to source code"** — OTLP spans with `code.filepath` → AST-indexed codebase → RAG retrieval
2. **"Multi-agent RCA"** — MetricsCorrelation + DeepReasoning agents collaborate
3. **"Backend-agnostic"** — Claude, Groq (3 models), Ollama — swap with one env var
4. **"5-way comparison"** — Claude 93%, Kimi-K2 92%, GPT-OSS 90%, Llama 85%, Qwen 70%
5. **"Zero vendor lock-in"** — runs locally with Ollama, or in cloud with any API
