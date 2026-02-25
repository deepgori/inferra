#!/usr/bin/env python3
"""
stress_test.py — Comprehensive Stress Test for Inferra

Tests:
  1. Codebase Indexer — large project indexing speed & memory
  2. RCA Engine — complex multi-error traces
  3. OTLP Receiver — throughput under load
  4. RAG Pipeline — search accuracy under volume
  5. Execution Graph — deep/wide DAGs
  6. Concurrent OTLP requests
"""

import sys, os, time, json, threading, random, string, traceback
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

PASS = 0
FAIL = 0
RESULTS = []

SEP = "─" * 70

def header(title):
    print(f"\n{SEP}")
    print(f"  {title}")
    print(SEP)

def test(name):
    """Decorator to register and run a test."""
    def decorator(func):
        def wrapper():
            global PASS, FAIL
            print(f"\n  [{name}] ", end="", flush=True)
            t0 = time.time()
            try:
                func()
                elapsed = time.time() - t0
                print(f"PASS  ({elapsed:.2f}s)")
                PASS += 1
                RESULTS.append((name, "PASS", elapsed, None))
            except Exception as e:
                elapsed = time.time() - t0
                print(f"FAIL  ({elapsed:.2f}s)")
                print(f"    Error: {e}")
                traceback.print_exc()
                FAIL += 1
                RESULTS.append((name, "FAIL", elapsed, str(e)))
        wrapper._test_name = name
        return wrapper
    return decorator


# ═══════════════════════════════════════════════════════════════════════
# 1. CODEBASE INDEXER STRESS
# ═══════════════════════════════════════════════════════════════════════

@test("Indexer: httpx (55 files, 1261 units)")
def test_index_httpx():
    from inferra.indexer import CodeIndexer
    idx = CodeIndexer()
    idx.index_directory(
        "test_projects/httpx",
        exclude_patterns=["__pycache__", ".git", "venv", ".venv"]
    )
    stats = idx.stats()
    assert stats["total_units"] > 1000, f"Expected >1000 units, got {stats['total_units']}"
    assert stats["files_indexed"] > 40, f"Expected >40 files, got {stats['files_indexed']}"
    assert stats["functions"] > 800, f"Expected >800 functions, got {stats['functions']}"

@test("Indexer: black (184 files, 2415 units)")
def test_index_black():
    from inferra.indexer import CodeIndexer
    idx = CodeIndexer()
    idx.index_directory(
        "test_projects/black",
        exclude_patterns=["__pycache__", ".git", "venv", ".venv"]
    )
    stats = idx.stats()
    assert stats["total_units"] > 2000, f"Expected >2000 units, got {stats['total_units']}"
    assert stats["files_indexed"] > 150, f"Expected >150 files, got {stats['files_indexed']}"

@test("Indexer: self (async_content_tracer)")
def test_index_self():
    from inferra.indexer import CodeIndexer
    idx = CodeIndexer()
    idx.index_directory(
        ".",
        exclude_patterns=["__pycache__", ".git", "venv", ".venv", "test_projects", "reports"]
    )
    stats = idx.stats()
    assert stats["total_units"] > 100, f"Expected >100 units, got {stats['total_units']}"


# ═══════════════════════════════════════════════════════════════════════
# 2. RAG SEARCH STRESS
# ═══════════════════════════════════════════════════════════════════════

@test("RAG: 100 sequential searches on httpx index")
def test_rag_throughput():
    from inferra.indexer import CodeIndexer
    idx = CodeIndexer()
    idx.index_directory(
        "test_projects/httpx",
        exclude_patterns=["__pycache__", ".git"]
    )
    queries = [
        "HTTP client", "async request", "timeout error", "SSL certificate",
        "redirect", "cookie", "proxy", "authentication", "stream response",
        "connection pool", "retry logic", "header parsing", "URL encoding",
        "multipart upload", "JSON response", "status code", "websocket",
        "middleware", "transport", "base client",
    ]
    total_results = 0
    for i in range(5):  # 5 rounds × 20 queries = 100 searches
        for q in queries:
            results = idx.search(q, top_k=5)
            total_results += len(results)
    assert total_results > 0, "Expected search results"

@test("RAG: function name lookup accuracy")
def test_rag_accuracy():
    from inferra.indexer import CodeIndexer
    idx = CodeIndexer()
    idx.index_directory(
        "test_projects/httpx",
        exclude_patterns=["__pycache__", ".git"]
    )
    # These should all be findable
    for name in ["main", "request", "get", "post"]:
        unit = idx.search_by_function_name(name)
        assert unit is not None, f"Failed to find function: {name}"


# ═══════════════════════════════════════════════════════════════════════
# 3. RCA ENGINE STRESS
# ═══════════════════════════════════════════════════════════════════════

@test("RCA: engine with 50-span trace (no LLM)")
def test_rca_many_spans():
    from async_content_tracer.tracer import TraceEvent, EventType
    from inferra.rca_engine import RCAEngine

    events = []
    for i in range(50):
        evt = TraceEvent(
            event_type=EventType.EXIT,
            function_name=f"handler_{i}",
            module="stress_test",
            source_file="stress_test.py",
            source_line=i * 10,
            timestamp=time.time() + i * 0.1,
            duration=random.uniform(0.001, 0.5),
            context_id="stress-ctx",
            span_id=f"span-{i:03d}",
            parent_span_id=f"span-{i-1:03d}" if i > 0 else None,
            depth=min(i, 10),
            thread_id=0,
            thread_name="MainThread",
            error=f"ValueError: bad input {i}" if i % 10 == 0 else None,
        )
        events.append(evt)

    engine = RCAEngine()
    report = engine.investigate(events)
    assert report is not None
    assert report.confidence > 0
    assert len(report.findings) > 0

@test("RCA: engine with concurrent error types")
def test_rca_mixed_errors():
    from async_content_tracer.tracer import TraceEvent, EventType
    from inferra.rca_engine import RCAEngine

    error_types = [
        "TimeoutError: connection timed out",
        "ConnectionRefusedError: target machine refused connection",
        "ValueError: invalid JSON payload",
        "KeyError: 'user_id'",
        "PermissionError: access denied to /etc/shadow",
    ]

    events = []
    for i, err in enumerate(error_types):
        events.append(TraceEvent(
            event_type=EventType.EXIT,
            function_name=f"endpoint_{i}",
            module="api_server",
            source_file="api.py",
            source_line=100 + i * 20,
            timestamp=time.time() + i,
            duration=random.uniform(0.01, 2.0),
            context_id=f"req-{i}",
            span_id=f"span-{i}",
            parent_span_id=None,
            depth=0,
            thread_id=0,
            thread_name="worker-1",
            error=err,
        ))

    engine = RCAEngine()
    report = engine.investigate(events)
    assert report is not None
    assert len(report.source_locations) > 0


# ═══════════════════════════════════════════════════════════════════════
# 4. EXECUTION GRAPH STRESS
# ═══════════════════════════════════════════════════════════════════════

@test("Graph: deep chain (100 nested spans)")
def test_graph_deep():
    from async_content_tracer.tracer import TraceEvent, EventType
    from async_content_tracer.graph import ExecutionGraph

    events = []
    for i in range(100):
        events.append(TraceEvent(
            event_type=EventType.EXIT,
            function_name=f"level_{i}",
            module="deep_test",
            source_file="deep.py",
            source_line=i,
            timestamp=time.time() + i * 0.01,
            duration=0.01 * (100 - i),
            context_id="deep-ctx",
            span_id=f"deep-{i:03d}",
            parent_span_id=f"deep-{i-1:03d}" if i > 0 else None,
            depth=i,
            thread_id=0,
            thread_name="MainThread",
        ))

    g = ExecutionGraph()
    g.build_from_events(events)
    summary = g.summary()
    assert "100" in summary or "Total spans" in summary

@test("Graph: wide fan-out (1 parent, 50 children)")
def test_graph_wide():
    from async_content_tracer.tracer import TraceEvent, EventType
    from async_content_tracer.graph import ExecutionGraph

    events = [TraceEvent(
        event_type=EventType.EXIT,
        function_name="dispatcher",
        module="wide_test",
        source_file="wide.py",
        source_line=1,
        timestamp=time.time(),
        duration=1.0,
        context_id="wide-ctx",
        span_id="root",
        parent_span_id=None,
        depth=0,
        thread_id=0,
        thread_name="MainThread",
    )]

    for i in range(50):
        events.append(TraceEvent(
            event_type=EventType.EXIT,
            function_name=f"worker_{i}",
            module="wide_test",
            source_file="wide.py",
            source_line=10 + i,
            timestamp=time.time() + 0.01 * i,
            duration=0.02,
            context_id="wide-ctx",
            span_id=f"child-{i:03d}",
            parent_span_id="root",
            depth=1,
            thread_id=i % 4,
            thread_name=f"pool-{i % 4}",
        ))

    g = ExecutionGraph()
    g.build_from_events(events)
    tree = g.print_tree()
    assert "dispatcher" in tree


# ═══════════════════════════════════════════════════════════════════════
# 5. OTLP RECEIVER STRESS
# ═══════════════════════════════════════════════════════════════════════

@test("OTLP: span conversion (1000 spans)")
def test_otlp_conversion():
    from inferra.otlp_receiver import otlp_to_trace_events, spans_to_tracer_events

    # Build a payload with 1000 spans
    spans = []
    for i in range(1000):
        spans.append({
            "traceId": f"trace-{i // 10}",
            "spanId": f"span-{i:05d}",
            "parentSpanId": f"span-{i-1:05d}" if i % 10 != 0 else "",
            "name": f"operation_{i}",
            "kind": random.choice([1, 2, 3]),
            "startTimeUnixNano": str(1709740000000000000 + i * 1000000),
            "endTimeUnixNano": str(1709740000000000000 + i * 1000000 + random.randint(100000, 50000000)),
            "status": {"code": 2, "message": f"Error #{i}"} if i % 50 == 0 else {"code": 0},
            "attributes": [
                {"key": "http.method", "value": {"stringValue": "GET"}},
                {"key": "http.status", "value": {"intValue": 200 if i % 50 != 0 else 500}},
            ],
        })

    payload = {
        "resourceSpans": [{
            "resource": {"attributes": [{"key": "service.name", "value": {"stringValue": "stress-svc"}}]},
            "scopeSpans": [{"scope": {"name": "stress"}, "spans": spans}]
        }]
    }

    events = otlp_to_trace_events(payload)
    assert len(events) == 1000, f"Expected 1000 events, got {len(events)}"

    # Convert to TraceEvents
    trace_events = spans_to_tracer_events(events)
    assert len(trace_events) == 1000

@test("OTLP: span buffer ring-buffer behavior")
def test_otlp_buffer():
    from inferra.otlp_receiver import SpanBuffer

    buf = SpanBuffer(max_spans=100)
    for i in range(500):
        buf.add([{"name": f"span-{i}"}])
    assert len(buf) == 100, f"Expected 100 (ring buffer), got {len(buf)}"
    # Oldest should be dropped
    all_spans = buf.get_all()
    assert all_spans[0]["name"] == "span-400"
    buf.clear()
    assert len(buf) == 0

@test("OTLP: concurrent buffer writes (10 threads × 100 spans)")
def test_otlp_concurrent():
    from inferra.otlp_receiver import SpanBuffer

    buf = SpanBuffer(max_spans=10000)
    errors = []

    def writer(thread_id):
        try:
            for i in range(100):
                buf.add([{"thread": thread_id, "seq": i}])
        except Exception as e:
            errors.append(e)

    threads = [threading.Thread(target=writer, args=(t,)) for t in range(10)]
    for t in threads: t.start()
    for t in threads: t.join()

    assert len(errors) == 0, f"Thread errors: {errors}"
    assert len(buf) == 1000, f"Expected 1000, got {len(buf)}"


# ═══════════════════════════════════════════════════════════════════════
# 6. OTLP HTTP SERVER STRESS (live)
# ═══════════════════════════════════════════════════════════════════════

@test("OTLP HTTP: 50 rapid POST requests")
def test_otlp_http_burst():
    import urllib.request

    # Start server in background
    from inferra.otlp_receiver import serve, _buffer
    _buffer.clear()

    server_thread = threading.Thread(target=serve, kwargs={"port": 14318}, daemon=True)
    server_thread.start()
    time.sleep(1)  # Wait for server to start

    errors = []
    accepted = 0

    def send_batch(batch_id):
        nonlocal accepted
        try:
            payload = json.dumps({
                "resourceSpans": [{
                    "resource": {"attributes": [{"key": "service.name", "value": {"stringValue": f"svc-{batch_id}"}}]},
                    "scopeSpans": [{"scope": {"name": "test"}, "spans": [{
                        "traceId": f"trace-{batch_id}",
                        "spanId": f"span-{batch_id}",
                        "name": f"op-{batch_id}",
                        "kind": 1,
                        "startTimeUnixNano": "1709740000000000000",
                        "endTimeUnixNano": "1709740001000000000",
                        "status": {"code": 0},
                    }]}]
                }]
            }).encode()

            req = urllib.request.Request(
                "http://localhost:14318/v1/traces",
                data=payload,
                headers={"Content-Type": "application/json"},
            )
            resp = urllib.request.urlopen(req, timeout=5)
            data = json.loads(resp.read())
            accepted += data.get("accepted", 0)
        except Exception as e:
            errors.append(e)

    threads = [threading.Thread(target=send_batch, args=(i,)) for i in range(50)]
    for t in threads: t.start()
    for t in threads: t.join()

    assert len(errors) == 0, f"HTTP errors: {errors[:3]}"
    assert accepted == 50, f"Expected 50 accepted, got {accepted}"
    assert len(_buffer) == 50, f"Expected 50 in buffer, got {len(_buffer)}"


# ═══════════════════════════════════════════════════════════════════════
# 7. END-TO-END: Full pipeline
# ═══════════════════════════════════════════════════════════════════════

@test("E2E: index + trace + RCA + HTML report")
def test_e2e_pipeline():
    from inferra.rca_engine import RCAEngine
    from inferra.indexer import CodeIndexer
    from report_html import generate_html_report

    engine = RCAEngine()
    engine.index_codebase(".", exclude_patterns=[
        "__pycache__", ".git", "venv", ".venv", "test_projects", "reports"
    ])
    stats = engine.stats()
    assert stats["total_units"] > 50

    # Generate report
    output = "/tmp/stress_test_report.html"
    generate_html_report(".", stats, None, output)
    assert os.path.exists(output)
    size = os.path.getsize(output)
    assert size > 1000, f"Report too small: {size} bytes"


# ═══════════════════════════════════════════════════════════════════════
# RUNNER
# ═══════════════════════════════════════════════════════════════════════

def main():
    header("Inferra Stress Test Suite")
    print(f"  Started: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

    # Collect all test functions
    tests = [v for v in globals().values() if callable(v) and hasattr(v, '_test_name')]

    header("Running Tests")

    t0 = time.time()
    for test_fn in tests:
        test_fn()
    total_time = time.time() - t0

    header("Results")

    for name, status, elapsed, err in RESULTS:
        indicator = "PASS" if status == "PASS" else "FAIL"
        print(f"  {indicator}  {name}  ({elapsed:.2f}s)")
        if err:
            print(f"         {err[:80]}")

    header("Summary")
    print(f"  Total:   {PASS + FAIL}")
    print(f"  Passed:  {PASS}")
    print(f"  Failed:  {FAIL}")
    print(f"  Time:    {total_time:.2f}s")
    print()

    sys.exit(1 if FAIL > 0 else 0)


if __name__ == "__main__":
    main()
