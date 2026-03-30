# Inferra — Autonomous Debugging via Trace-to-Code Correlation

[![Tests](https://github.com/deepgori/inferra/actions/workflows/tests.yml/badge.svg)](https://github.com/deepgori/inferra/actions/workflows/tests.yml)
[![PyPI](https://img.shields.io/pypi/v/inferra)](https://pypi.org/project/inferra/)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue)](https://pypi.org/project/inferra/)

**Turn production traces into code-level diagnoses. Automatically.**

Inferra bridges the gap between observability tools (which show _what_ happened) and source code (which shows _why_ it happened). It ingests standard [OpenTelemetry](https://opentelemetry.io/) traces, maps each span to the exact function and line in your codebase via AST analysis, and produces a structured root cause analysis — powered by your choice of LLM.

```
Your App --> OTLP Traces --> Inferra --> Code-Aware Diagnosis  (routes.py:195, cve_extractor.py:43)
```

## Quick Start

### Install

```bash
pip install inferra

# Optional: tree-sitter for production-grade JS/TS/Go/Java parsing
pip install inferra[treesitter]
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

### Option C: Docker

```bash
docker compose up
# Then: curl -X POST http://localhost:4318/v1/analyze
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

## v0.5.0 Highlights

### 8-Stage Span-to-Code Correlator

Maps every OTLP span to the exact function in your codebase through an 8-stage matching cascade:

| Stage | Strategy | Example |
|-------|----------|---------|
| 1 | `code.function` attribute | Explicit span tagging |
| 2 | Exact route match | `GET /api/products` → `@app.get("/products")` |
| 3 | HTTP semantic conventions | `http.route` + `http.method` attrs |
| 4 | Fuzzy route (ID stripping) | `/products/123` → `/products/{id}` |
| 5 | Exact function name | `aggregate` → function named `aggregate` |
| 6 | Keyword decomposition | `mongodb.products.aggregate` → search keywords |
| 7 | DB statement / body pattern | `SELECT * FROM users` → function body grep |
| 8 | TF-IDF fuzzy (fallback) | Score > 0.15 threshold |

### Smarter Agents (v0.5.0)

- **Security Agent**: Basic taint tracking (param → sink flow), cross-function call graph analysis, false-positive filtering (skips tests/comments). Detects: SQL injection, XSS, hardcoded secrets, SSRF, path traversal, open redirects, unsafe deserialization, weak cryptography.
- **Metrics Agent**: Statistical outlier detection (>2σ per span name), self-time bottleneck analysis, bimodal latency distribution detection.
- **LLM Error Handling**: 30s timeout + retry, heuristic fallback when LLM is unreachable, partial results instead of 500 errors.

### Tree-sitter Parsers

Production-grade AST parsing via [tree-sitter](https://tree-sitter.github.io/) (optional, with regex fallback):

| Language | Constructs | Backend |
|----------|-----------|---------|
| **Python** | Functions, classes, decorators, routes, docstrings, call graphs | AST (`ast.parse`) |
| **JavaScript** | Functions, arrow fns, classes, methods, Express/Fastify routes | Tree-sitter + regex |
| **TypeScript** | Enums, interfaces, decorators (`@Get`), abstract classes, namespaces, type guards | Tree-sitter + regex |
| **Go** | Functions, methods with receivers, structs, interfaces, generics | Tree-sitter + regex |
| **Java** | Classes, records, enums, methods, Spring/JAX-RS routes | Tree-sitter + regex |

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
inferra serve --project ./my-app --llm groq

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
|  | HTTP/JSON    |   | TF-IDF Index  |   | Python / JS / TS     |     |
|  | Span Buffer  |   | Route Extract |   | Go / Java            |     |
|  +------+-------+   +-------+-------+   +----------+-----------+     |
|         |                   |                       |                 |
|         v                   v                       v                 |
|  +---------------------------------------------------------------+   |
|  |              8-Stage Span ↔ Code Correlator                    |   |
|  |  route match • HTTP attrs • function name • keyword decomp    |   |
|  |  DB statement • TF-IDF fuzzy • code.function attr              |   |
|  +------------------------------+--------------------------------+   |
|                                 |                                     |
|        +------------------------+------------------------+            |
|        v                        v                        v            |
|  +-------------+  +-------------------+  +--------------------+      |
|  | Multi-Agent |  | LLM Synthesis     |  | Persistence &      |      |
|  | Analyzers   |  |                   |  | Streaming          |      |
|  |             |  | Claude / Groq     |  |                    |      |
|  | Metrics     |  | / Ollama          |  | SQLite history     |      |
|  | Security    |  | Timeout + Retry   |  | Session restore    |      |
|  | Dependency  |  | Heuristic fallbk  |  | Regression alerts  |      |
|  +-------------+  +-------------------+  +--------------------+      |
|                                                                       |
|  +---------------------------------------------------------------+   |
|  |                    Output Layer                                |   |
|  |  HTML Reports | SSE Streaming | Fix Diffs | Follow-up API     |   |
|  +---------------------------------------------------------------+   |
+----------------------------------------------------------------------+
```

## Multi-Agent Analysis

Inferra uses 5 specialized agents that run before the LLM:

| Agent | Purpose | Example Finding |
|-------|---------|----------------|
| **MetricsCorrelation** | Statistical outliers (>2σ), self-time bottlenecks, bimodal detection | "3 outliers detected, 2 self-time bottlenecks" |
| **DeepReasoning** | LLM-powered root cause analysis with code context | "Sequential pipeline bottleneck in graph.invoke()" |
| **Dependency** | Call graph analysis, circular deps, dead code | "High fan-out: api.routes calls 12 functions" |
| **Security** | SQL injection, XSS, secrets, SSRF, open redirects, weak crypto | "Tainted data flows from login() to sink in query()" |
| **Coordinator** | Synthesizes all findings into a final RCA report | Severity, confidence, ranked recommendations |

## API Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/v1/traces` | POST | Accept OTLP spans |
| `/v1/traces` | GET | View buffered spans |
| `/v1/analyze` | POST | Trigger RCA on buffered spans |
| `/v1/analyze/stream` | POST | **SSE streaming** — real-time analysis progress |
| `/v1/ask` | POST | Follow-up questions (persists across restarts) |
| `/v1/fix` | POST | **Generate code fix diffs** from last analysis |
| `/v1/topology` | POST | Generate service topology (Mermaid + D3.js) |
| `/v1/history` | GET | View past analysis results |
| `/v1/regressions` | GET | Detect performance regressions |
| `/healthz` | GET | Health check |

### Streaming Example

```bash
curl -N -X POST http://localhost:4318/v1/analyze/stream
```

```
event: progress
data: {"step": "start", "message": "Analyzing 47 spans..."}

event: progress
data: {"step": "correlated", "message": "Mapped 12/23 spans to code"}

event: finding
data: {"agent": "SecurityAgent", "summary": "SQL injection in query_builder", "confidence": "95%"}

event: finding
data: {"agent": "MetricsCorrelationAgent", "summary": "3 outliers detected (>2σ)", "confidence": "90%"}

event: complete
data: {"severity": "high", "root_cause": "...", "confidence": "92%"}
```

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
| `INFERRA_LLM_TIMEOUT` | Optional | LLM timeout in seconds (default: 30) |
| `INFERRA_LLM_RETRIES` | Optional | LLM retry count (default: 1) |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | For your app | Set to `http://localhost:4318` |

## Benchmarks

Run the benchmark suite:

```bash
python -m benchmarks.bench_indexing [project_path]    # Indexing throughput
python -m benchmarks.bench_correlation [project_path]  # Correlation accuracy
python -m benchmarks.bench_llm                         # LLM latency comparison
```

## Project Structure

```
inferra/
├── __init__.py           # Package exports + inferra.analyze() API
├── __main__.py           # CLI entry point (inferra analyze / inferra serve)
├── api.py                # Public Python API
├── indexer.py            # AST-based code indexer + TF-IDF search
├── otlp_receiver.py      # OTLP/HTTP trace receiver + 8-stage correlator
├── rca_engine.py         # Root cause analysis orchestration
├── agents.py             # Multi-agent analyzers (metrics, log, deep reasoning)
├── llm_agent.py          # Multi-LLM backend (Claude, Groq, Ollama)
├── rag.py                # Context-aware code retrieval
├── embeddings.py         # TF-IDF code search index
├── parsers/
│   ├── base.py               # Parser registry + abstract base
│   ├── javascript_parser.py  # JS regex parser (fallback)
│   ├── go_parser.py          # Go regex parser (fallback)
│   ├── java_parser.py        # Java regex parser (fallback)
│   ├── ts_javascript_parser.py   # JS tree-sitter parser
│   ├── ts_typescript_parser.py   # TS tree-sitter parser (enums, interfaces, decorators)
│   ├── ts_go_parser.py           # Go tree-sitter parser
│   └── ts_java_parser.py         # Java tree-sitter parser
├── security_agent.py     # Taint tracking + vulnerability scanner
├── dependency_agent.py   # Call graph + dependency analysis
├── storage.py            # SQLite persistence + session restore + regression detection
├── streaming.py          # Real-time anomaly detection
├── topology.py           # Service topology (Mermaid + D3.js)
├── pr_generator.py       # Code fix generation
├── auto_instrument.py    # OTel auto-instrumentation generator
├── config_indexer.py     # YAML/TOML/.env config parser
├── sql_indexer.py        # SQL file indexer
└── aws_integration.py    # S3 report upload + CloudWatch ingestion
```

## How It Compares

| Tool | Trace Collection | Code Correlation | Multi-Language | LLM Root Cause | Auto-Fix | Cost |
|---|---|---|---|---|---|---|
| **Jaeger** | Yes | No | N/A | No | No | Free |
| **Datadog** | Yes | Partial (APM) | Yes | Partial (Watchdog) | No | $15-35/host/mo |
| **Sentry** | Yes | Yes (requires SDK) | Yes | No | No | $26+/mo |
| **Inferra** | Yes (standard OTLP) | Yes (AST, zero SDK) | 5 languages | Yes (multi-LLM) | Yes (`/v1/fix`) | **Free** |

The key difference: Inferra uses **standard OTLP** (no vendor SDK), correlates via **AST analysis** (no runtime agent), and runs **fully local** with Ollama.

## Limitations

- **Tree-sitter optional**: The `pip install inferra[treesitter]` extra installs tree-sitter grammars for JS/TS/Go/Java. Without it, regex-based parsers are used as fallback — they handle most real-world code but can miss deeply nested constructs.
- **Not continuous monitoring**: Analysis is triggered manually via `/v1/analyze`. The streaming module provides anomaly detection on ingested spans, but Inferra is not a 24/7 monitoring agent.
- **LLM-dependent analysis quality**: Root cause analysis quality scales with the LLM backend. Claude scores 93% accuracy while Ollama/Qwen scores around 70%. When no LLM is available, heuristic-only results are returned.
- **Fix generation is best-effort**: `/v1/fix` uses the LLM to generate diffs — always review before applying.
