"""
bench_correlation.py — Span-to-Code Correlation Accuracy Benchmark

Tests the 8-stage correlator against labeled span→code pairs
to measure precision and recall.

Usage:
    python -m benchmarks.bench_correlation
"""

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# Labeled test cases: (span_name, span_attrs, expected_function_name_substring)
# These simulate real OTel spans and what code they should map to
LABELED_PAIRS = [
    # Stage 1: code.function attribute
    {
        "name": "db.query",
        "attrs": {"code.function": "fetch_user"},
        "expected": "fetch_user",
    },
    # Stage 2: Exact route match
    {
        "name": "GET /api/orders",
        "attrs": {},
        "expected": "orders",
    },
    # Stage 3: HTTP semantic conventions
    {
        "name": "HTTP GET",
        "attrs": {"http.method": "GET", "http.route": "/api/products"},
        "expected": "products",
    },
    # Stage 5: Exact function name
    {
        "name": "process_payment",
        "attrs": {},
        "expected": "process_payment",
    },
    # Stage 6: Keyword decomposition
    {
        "name": "mongodb.products.aggregate",
        "attrs": {},
        "expected": "product",
    },
    # Stage 7: DB statement
    {
        "name": "db.query",
        "attrs": {"db.statement": "SELECT * FROM users WHERE id = ?"},
        "expected": "user",
    },
    # Stage 8: Fuzzy fallback
    {
        "name": "validate_checkout_session",
        "attrs": {},
        "expected": "checkout",
    },
]


def bench_correlation(project_path: str):
    """Benchmark correlation accuracy against labeled pairs."""
    from inferra.indexer import CodeIndexer

    print(f"\n{'='*60}")
    print(f"  Correlation Accuracy Benchmark")
    print(f"{'='*60}\n")

    # Index the project
    indexer = CodeIndexer()
    indexer.index_directory(project_path)
    stats = indexer.stats()
    print(f"  Indexed: {stats['total_units']} units from {stats['files_indexed']} files\n")

    # Build synthetic spans
    spans = []
    for pair in LABELED_PAIRS:
        spans.append({
            "trace_id": "bench-trace-001",
            "span_id": f"span-{len(spans)}",
            "parent_span_id": "",
            "name": pair["name"],
            "service": "bench-service",
            "library": "bench",
            "kind": "INTERNAL",
            "start_time_ms": 1000,
            "end_time_ms": 1050,
            "duration_ms": 50,
            "status": "OK",
            "error": None,
            "attributes": pair["attrs"],
            "events": [],
        })

    # Run correlation
    # We need to simulate what otlp_receiver does
    import inferra.otlp_receiver as recv
    recv._indexer = indexer

    start = time.perf_counter()
    trace_events = recv.spans_to_tracer_events(spans)
    source_map = recv._correlate_spans_to_code(spans, trace_events)
    elapsed = time.perf_counter() - start

    # Score
    hits = 0
    misses = 0
    for pair in LABELED_PAIRS:
        name = pair["name"]
        expected = pair["expected"].lower()
        mapped = source_map.get(name)
        if mapped and expected in mapped.get("function", "").lower():
            hits += 1
            print(f"  ✅ {name:40s} → {mapped['function']}")
        elif mapped:
            # Partial match — mapped to something, but not what we expected
            print(f"  ⚠️  {name:40s} → {mapped['function']} (expected: *{expected}*)")
            misses += 1
        else:
            print(f"  ❌ {name:40s} → (no match)")
            misses += 1

    total = len(LABELED_PAIRS)
    accuracy = hits / total if total > 0 else 0
    print(f"\n  Results:")
    print(f"    Accuracy:    {accuracy:.0%} ({hits}/{total})")
    print(f"    Correlations: {len(source_map)}/{len(spans)} spans mapped")
    print(f"    Time:        {elapsed*1000:.1f}ms")
    print()

    # Clean up
    recv._indexer = None


if __name__ == "__main__":
    path = sys.argv[1] if len(sys.argv) > 1 else os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    bench_correlation(path)
