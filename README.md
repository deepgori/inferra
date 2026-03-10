# Inferra - Autonomous Debugging via Trace-to-Code Correlation

[![Tests](https://github.com/deepgori/inferra/actions/workflows/tests.yml/badge.svg)](https://github.com/deepgori/inferra/actions/workflows/tests.yml)
[![PyPI](https://img.shields.io/pypi/v/inferra)](https://pypi.org/project/inferra/)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue)](https://pypi.org/project/inferra/)

**Turn production traces into code-level diagnoses. Automatically.**

Inferra bridges the gap between observability tools (which show _what_ happened) and source code (which shows _why_ it happened). It ingests standard [OpenTelemetry](https://opentelemetry.io/) traces, maps each span to the exact function and line in your codebase via AST analysis, and produces a structured root cause analysis, powered by your choice of LLM.

```
Your App --> OTLP Traces --> Inferra --> Code-Aware Diagnosis  (routes.py:195, cve_extractor.py:43)
```

## Quick Start

### Install

```bash
pip install inferra
```

### Option A: OTLP Mode (Production Debugging)

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

Analyze any project without running it:

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
> "`POST /api/analyze` at `routes.py:195` calls `graph.invoke()` which runs 3 agents sequentially: `cve_extractor.py:43` (1.2s), `attack_classifier.py:110` (1.1s), `playbook_generator.py:39` (1.2s). Use `asyncio.gather()` to parallelize for ~3x speedup."

## Multi-Language Code Indexing

Inferra indexes codebases across 4 languages:

| Language | Parser | Features |
|----------|--------|----------|
| **Python** | AST (`ast.parse`) | Functions, classes, decorators, routes, docstrings, call graphs |
| **JavaScript/TypeScript** | Regex + heuristics | Functions, classes, arrow functions, generics (`<T>`), interfaces, type aliases, generators |
| **Go** | Regex + heuristics | Functions, methods, structs, interfaces, Go 1.18+ generics (`[T any]`), routes (Gin/Echo/Chi) |
| **Java** | Regex + heuristics | Classes, methods, records, sealed classes, generic return types (`ResponseEntity<UserDTO>`), Spring/JAX-RS routes |

The Python parser is production-grade (full AST). The JS/TS, Go, and Java parsers use regex with generics support and handle real-world codebases (tested on 270+ file MERN projects).

## Multi-LLM Support

Inferra supports 3 backends. Swap with one environment variable:

| Backend | Models | Speed | Quality | Cost |
|---------|--------|-------|---------|------|
| **Groq** | Kimi-K2, GPT-OSS-120b, Llama-3.3-70b | ~2-4s | 85-92% | Free tier |
| **Claude** | Sonnet 4 | ~8s | 93% | API key |
| **Ollama** | Qwen 3:8b (local) | ~15s | 70% | **Free, fully local** |

```bash
# Groq (fastest)
export GROQ_API_KEY=your-key
inferra serve --project ./my-app --llm groq --model moonshotai/kimi-k2-instruct

# Claude (highest quality)
export ANTHROPIC_API_KEY=your-key
inferra serve --project ./my-app --llm claude

# Ollama (free, runs locally)
ollama pull qwen3:8b
inferra serve --project ./my-app --llm ollama
```

## Architecture

```
+----------------------------------------------------------------------+
|                              Inferra                                  |
|                                                                       |
|  +--------------+   +---------------+   +----------------------+     |
|  | OTLP Receiver|   | Code Indexer  |   | Multi-Language       |     |
|  |              |   |               |   | Parsers              |     |
|  | Protobuf     |   | AST Parser    |   |                      |     |
|  | HTTP/JSON    |   | TF-IDF Index  |   | Python (AST)         |     |
|  | Span Buffer  |   | Route Extract |   | JS/TS, Go, Java      |     |
|  +------+-------+   +-------+-------+   +----------+-----------+     |
|         |                   |                       |                 |
|         v                   v                       v                 |
|  +---------------------------------------------------------------+   |
|  |                    Span <-> Code Correlator                    |   |
|  |  1. code.filepath attribute match                              |   |
|  |  2. Route match (GET /articles/{slug} -> articles.get)         |   |
|  |  3. Function name match                                        |   |
|  |  4. TF-IDF fuzzy search fallback                               |   |
|  +------------------------------+--------------------------------+   |
|                                 |                                     |
|        +------------------------+------------------------+            |
|        v                        v                        v            |
|  +-------------+  +-------------------+  +--------------------+      |
|  | Multi-Agent |  | LLM Synthesis     |  | Persistence &      |      |
|  | Analyzers   |  |                   |  | Streaming          |      |
|  |             |  | Claude / Groq     |  |                    |      |
|  | Metrics     |  | / Ollama          |  | SQLite history     |      |
|  | Dependency  |  |                   |  | Anomaly detection  |      |
|  | Security    |  | Agentic RAG       |  | Regression alerts  |      |
|  +-------------+  +-------------------+  +--------------------+      |
|                                                                       |
|  +---------------------------------------------------------------+   |
|  |                    Output Layer                                |   |
|  |  HTML Reports | Service Topology | Follow-up API | PR Suggest |   |
|  +---------------------------------------------------------------+   |
+----------------------------------------------------------------------+
```

## Multi-Agent Analysis

Inferra uses 5 specialized agents that run before the LLM:

| Agent | Purpose | Example Finding |
|-------|---------|----------------|
| **MetricsCorrelation** | Latency anomalies, critical path, P95 stats | "8 slow spans detected (>100ms)" |
| **DeepReasoning** | LLM-powered root cause analysis with code context | "Sequential pipeline bottleneck in graph.invoke()" |
| **Dependency** | Call graph analysis, circular deps, dead code | "High fan-out: api.routes calls 12 functions" |
| **Security** | SQL injection, hardcoded secrets, SSRF patterns | "Potential SQL injection in query builder" |
| **Coordinator** | Synthesizes all findings into a final RCA report | Severity, confidence, ranked recommendations |

## API Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/v1/traces` | POST | Accept OTLP spans |
| `/v1/traces` | GET | View buffered spans |
| `/v1/analyze` | POST | Trigger RCA on buffered spans |
| `/v1/ask` | POST | Follow-up questions on last analysis |
| `/v1/topology` | POST | Generate service topology (Mermaid + D3.js) |
| `/v1/history` | GET | View past analysis results |
| `/v1/regressions` | GET | Detect performance regressions |
| `/healthz` | GET | Health check |

## Tested On Real Projects

| Project | Language | Files | Code Units | Correlations | Finding |
|---------|----------|-------|------------|--------------|---------|
| **Cybersec Threat Analyst** | Python | 35 | 284 | 29 | Sequential pipeline bottleneck |
| **MERN Ecommerce** | JavaScript | 130 | 338 | 10 | MongoDB aggregation bottleneck (2190ms) |
| **Goldman Sachs gs-quant** | Python | 52 | 1,460+ | 45+ | Synchronous computation chains |
| **RealWorld Conduit** | Python | 37 | 104 | 6/22 | JWT auth + bcrypt bottleneck |
| **PharmaSight** | Python | 25+ | 80+ | route-based | Batch processing latency |

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

## Project Structure

```
inferra/
├── __init__.py           # Package exports + inferra.analyze() API
├── __main__.py           # CLI entry point (inferra analyze / inferra serve)
├── api.py                # Public Python API
├── indexer.py            # AST-based code indexer + TF-IDF search
├── otlp_receiver.py      # OTLP/HTTP trace receiver + correlator
├── rca_engine.py         # Root cause analysis orchestration
├── agents.py             # Rule-based analyzers (latency, errors, patterns)
├── llm_agent.py          # Multi-LLM backend (Claude, Groq, Ollama)
├── rag.py                # Context-aware code retrieval
├── embeddings.py         # TF-IDF code search index
├── parsers/
│   ├── base.py           # Parser registry + abstract base
│   ├── javascript_parser.py  # JS/TS parser (generics, interfaces, types)
│   ├── go_parser.py      # Go parser (1.18+ generics)
│   └── java_parser.py    # Java parser (records, sealed, generics)
├── dependency_agent.py   # Call graph + dependency analysis
├── security_agent.py     # Security vulnerability scanner
├── storage.py            # SQLite persistence + regression detection
├── streaming.py          # Real-time anomaly detection
├── topology.py           # Service topology (Mermaid + D3.js)
├── pr_generator.py       # PR fix suggestions
├── auto_instrument.py    # OTel auto-instrumentation generator
├── config_indexer.py     # YAML/TOML/.env config parser
├── sql_indexer.py        # SQL file indexer
└── aws_integration.py    # S3 report upload + CloudWatch ingestion
```

## How It Compares

| Tool | Trace Collection | Code Correlation | Multi-Language | LLM Root Cause | Persistence | Cost |
|---|---|---|---|---|---|---|
| **Jaeger** | Yes | No | N/A | No | Yes | Free |
| **Datadog** | Yes | Partial (APM) | Yes | Partial (Watchdog) | Yes | $15-35/host/mo |
| **Sentry** | Yes | Yes (requires SDK) | Yes | No | Yes | $26+/mo |
| **Inferra** | Yes (standard OTLP) | Yes (AST, zero SDK) | 4 languages | Yes (multi-LLM) | Yes (SQLite) | **Free** |

The key difference: Inferra uses **standard OTLP** (no vendor SDK), correlates via **AST analysis** (no runtime agent), and runs **fully local** with Ollama.

## Limitations

- **Regex-based parsers for JS/TS, Go, Java**: The Python indexer uses full AST parsing. Other languages use regex with generics support, which covers most real-world code but can miss deeply nested constructs. Tree-sitter migration is on the roadmap.
- **Not continuous monitoring**: Analysis is triggered manually via `/v1/analyze`. The streaming module provides anomaly detection on ingested spans, but Inferra is not a 24/7 monitoring agent.
- **LLM-dependent analysis quality**: Root cause analysis quality scales with the LLM backend. Claude scores 93% accuracy while Ollama/Qwen scores around 70%.
- **Simulated traces for testing**: The OTLP receiver requires externally instrumented apps or simulated spans. Auto-instrumentation generates scripts but requires manual setup.
