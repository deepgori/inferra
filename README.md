# Inferra — Autonomous Debugging via Trace-to-Code Correlation

[![Tests](https://github.com/deepgori/inferra/actions/workflows/tests.yml/badge.svg)](https://github.com/deepgori/inferra/actions/workflows/tests.yml)
[![PyPI](https://img.shields.io/pypi/v/inferra)](https://pypi.org/project/inferra/)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue)](https://pypi.org/project/inferra/)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)

**Turn production traces into code-level diagnoses. Automatically.**

Inferra bridges the gap between observability tools (which show _what_ happened) and source code (which shows _why_ it happened). It ingests standard [OpenTelemetry](https://opentelemetry.io/) traces, maps each span to the exact function and line in your codebase via AST analysis, and produces a structured root cause analysis — powered by your choice of LLM.

```
Your App → OTLP Traces → Inferra → Code-Aware Diagnosis  (routes.py:195, cve_extractor.py:43)
```

## Quick Start

### Install

```bash
pip install inferra
```

### Option A: OTLP Mode (Production Debugging — Recommended)

Point Inferra at your codebase, send it traces from your running app:

```bash
# Terminal 1: Start the OTLP receiver + code indexer
inferra serve --project /path/to/your/app/src --llm groq

# Terminal 2: Your app sends OTel traces to Inferra
export OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318
python my_app.py

# Terminal 3: Trigger analysis after some traffic
curl -X POST http://localhost:4318/v1/analyze | python3 -m json.tool
```

### Option B: Static Analysis (Quick Code Review)

Analyze any Python project without running it:

```bash
inferra analyze /path/to/any/project --llm groq
```

### Python API

```python
import inferra

report = inferra.analyze("./my_project", llm="groq")
print(report.confidence)   # "92%"
print(report.root_cause)   # "Sequential pipeline with blocking I/O..."
report.open()              # Opens HTML report in browser
```

## The Problem

When a production API is slow, observability tools tell you:

> "`POST /api/analyze` took 3.5s"

But they don't tell you _which function_ handles that endpoint, _what_ the code does, or _where_ the bottleneck is. You're left grep-ing through source code, matching route decorators to trace names by hand.

### Without Inferra
> "The endpoint is slow. Consider optimizing."

### With Inferra
> "`POST /api/analyze` at `routes.py:195` calls `graph.invoke()` which runs 3 agents sequentially — `cve_extractor.py:43` (1.2s), `attack_classifier.py:110` (1.1s), `playbook_generator.py:39` (1.2s). Use `asyncio.gather()` to parallelize for ~3x speedup."

## Multi-LLM Support

Inferra supports 3 backends — swap with one environment variable:

| Backend | Models | Speed | Quality | Cost |
|---------|--------|-------|---------|------|
| **Groq** | Kimi-K2, GPT-OSS-120b, Llama-3.3-70b | ⚡ ~2-4s | 85-92% | Free tier |
| **Claude** | Sonnet 4 | ~8s | 93% | API key |
| **Ollama** | Qwen 3:8b (local) | ~15s | 70% | **Free, fully local** |

```bash
# Groq (fastest)
export GROQ_API_KEY=your-key
inferra serve --project ./my-app --llm groq --model moonshotai/kimi-k2-instruct

# Claude (highest quality)
export ANTHROPIC_API_KEY=your-key
inferra serve --project ./my-app --llm claude

# Ollama (free, runs locally — no API key needed)
ollama pull qwen3:8b
inferra serve --project ./my-app --llm ollama
```

### 5-Way LLM Comparison (Cybersec Threat Analyst Project)

All backends analyzed identical traffic (218 spans, 29 code correlations):

| Model | Confidence | Accuracy | Speed | Root Cause Identified |
|-------|-----------|----------|-------|----------------------|
| Claude Sonnet 4 | **93%** | ~92% | ~8s | Sequential 3-agent pipeline + blocking I/O |
| Groq Kimi-K2 | **92%** | ~88% | ~3s | Missing `asyncio.gather` parallelization |
| Groq GPT-OSS-120b | 90% | ~85% | ~4s | Blocking `requests.get` in async context |
| Groq Llama-70b | 85% | ~80% | ~2s | Inefficient data fetching + `run_in_executor` |
| Ollama Qwen 3:8b | 90% | ~70% | ~15s | Repeated `fetch_cve` calls without caching |

## Architecture

```
┌──────────────────────────────────────────────────────────────────────┐
│                              Inferra                                 │
│                                                                      │
│  ┌──────────────┐   ┌───────────────┐   ┌──────────────────────┐    │
│  │ OTLP Receiver │   │ Code Indexer  │   │  Route Prefix        │    │
│  │               │   │               │   │  Resolver            │    │
│  │ Protobuf      │   │ AST Parser    │   │                      │    │
│  │ HTTP/JSON     │   │ TF-IDF Index  │   │ Resolves full URL    │    │
│  │ Span Buffer   │   │ Route Extract │   │ paths across files   │    │
│  └───────┬───────┘   └───────┬───────┘   └──────────┬───────────┘   │
│          │                   │                       │               │
│          ▼                   ▼                       ▼               │
│  ┌───────────────────────────────────────────────────────────────┐   │
│  │                    Span ↔ Code Correlator                     │   │
│  │  1. code.filepath attribute match                             │   │
│  │  2. Route match (GET /articles/{slug} → articles.get)         │   │
│  │  3. Function name match                                       │   │
│  │  4. TF-IDF fuzzy search fallback                              │   │
│  └──────────────────────────┬────────────────────────────────────┘   │
│                             │                                        │
│         ┌───────────────────┼───────────────────┐                    │
│         ▼                   ▼                   ▼                    │
│  ┌─────────────┐   ┌───────────────┐   ┌───────────────┐           │
│  │ Heuristic   │   │ LLM Synthesis │   │ HTML Report   │           │
│  │ Analyzers   │   │               │   │ Generator     │           │
│  │             │   │ Claude / Groq │   │               │           │
│  │ Latency     │   │ / Ollama      │   │ Call trees    │           │
│  │ Error class │   │               │   │ Timing stats  │           │
│  │ Crit. path  │   │ Agentic RAG   │   │ Code links   │           │
│  └─────────────┘   └───────────────┘   └───────────────┘           │
└──────────────────────────────────────────────────────────────────────┘
```

## Tested On Real Projects

| Project | Type | Files | Code Units | Correlations | LLM Analysis |
|---------|------|-------|------------|--------------|-------------|
| **Cybersec Threat Analyst** | Multi-agent LangGraph pipeline | 35 | 284 | 29 | Sequential pipeline bottleneck, blocking I/O |
| **Goldman Sachs gs-quant** | Financial analytics library | 52 | 1,460+ | 45+ | Synchronous computation chains |
| **RealWorld Conduit** | FastAPI REST API | 37 | 104 | 6/22 spans | JWT auth + bcrypt bottleneck |
| **PharmaSight** | Healthcare API | 25+ | 80+ | route-based | Batch processing latency |
| **PlanIt** | Task management API | 20+ | 60+ | route-based | N+1 query patterns |

## CLI Reference

```bash
# OTLP receiver mode (production debugging)
inferra serve --project <path> [--llm groq|claude|ollama] [--model <name>] [--port 4318]

# Static analysis mode (code review)
inferra analyze <path> [--llm groq|claude|ollama] [--model <name>] [--output report.html]

# Help
inferra --help
inferra analyze --help
inferra serve --help
```

## Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `GROQ_API_KEY` | For Groq backend | Get from [console.groq.com](https://console.groq.com) |
| `ANTHROPIC_API_KEY` | For Claude backend | Get from [console.anthropic.com](https://console.anthropic.com) |
| `INFERRA_LLM_BACKEND` | Optional | Force backend: `groq`, `claude`, `ollama` |
| `INFERRA_LLM_MODEL` | Optional | Specific model name |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | For your app | Set to `http://localhost:4318` |

## Code Indexer — The Core

The code indexer is the critical component that makes Inferra more than a trace viewer. For every function it finds, it extracts:

| Field | Example | Purpose |
|---|---|---|
| `qualified_name` | `api.routes.articles.get` | Display and search |
| `source_file:line` | `articles.py:109` | Pinpoint exact location |
| `body_text` | Full function body | Fed to LLM for analysis |
| `route_path` | `GET /articles/{slug}` | Maps trace spans → functions |
| `calls` | `["articles.get_by_slug"]` | Dependency chain |
| `log_patterns` | `["error fetching article"]` | Match against log messages |

## Analysis Pipeline — Heuristics + LLM

1. **Rule-based analyzers** run first — `LogAnalysisAgent` classifies errors, `MetricsCorrelationAgent` finds slow spans and computes the critical path, `PatternAnalysisAgent` identifies antipatterns (N+1 queries, missing error handlers)
2. **Findings are structured** into typed objects with severity, confidence scores, and evidence chains
3. **LLM synthesis** with agentic code retrieval — the LLM receives structured findings and correlated source code, then can request additional code via `[NEED_CODE: function_name]` markers

## Project Structure

```
inferra/
├── __init__.py           # Package exports + inferra.analyze() API
├── __main__.py           # CLI entry point (inferra analyze / inferra serve)
├── api.py                # Public Python API (inferra.analyze())
├── indexer.py            # AST-based code indexer + TF-IDF search
├── otlp_receiver.py      # OTLP/HTTP trace receiver + correlator
├── rca_engine.py         # Root cause analysis orchestration
├── agents.py             # Rule-based analyzers (latency, errors, patterns)
├── llm_agent.py          # Multi-LLM backend (Claude, Groq, Ollama)
├── rag.py                # Context-aware code retrieval
├── embeddings.py         # TF-IDF code search index
├── config_indexer.py     # YAML/TOML/.env config parser
├── sql_indexer.py        # SQL file indexer
└── aws_integration.py    # S3 report upload + CloudWatch ingestion
```

## How It Compares

| Tool | Trace Collection | Code Correlation | LLM Root Cause | Cost |
|---|---|---|---|---|
| **Jaeger** | ✅ | ❌ | ❌ | Free |
| **Datadog** | ✅ | ⚠️ (APM, paid) | ⚠️ (Watchdog) | $15-35/host/mo |
| **Sentry** | ✅ | ✅ (requires SDK) | ❌ | $26+/mo |
| **Inferra** | ✅ (standard OTLP) | ✅ (AST, zero SDK) | ✅ (multi-LLM) | **Free** |

The key difference: Inferra uses **standard OTLP** (no vendor SDK), correlates via **AST analysis** (no runtime agent), and runs **fully local** with Ollama. Point your existing OTel instrumentation at Inferra and it maps spans to source code automatically.

## Limitations

- **Python only** — The AST indexer currently supports Python. The architecture is language-agnostic (OTLP + pluggable indexers), but only one indexer is implemented.
- **Not real-time monitoring** — Analysis is triggered manually, not streaming 24/7.
- **Manual instrumented runners** — For the OTLP mode, projects without OTel instrumentation need a custom runner. Auto-instrumentation is on the roadmap.
- **No persistence** — Restarting clears the span buffer. No database backing.

## License

MIT
