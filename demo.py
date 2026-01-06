"""
demo.py — Full Working Demonstration of async_content_tracer

Simulates a realistic async request pipeline that hits all the key scenarios:

1. An incoming request fans out to multiple async tasks (branching)
2. Some work gets offloaded to a thread pool (cross-thread propagation)
3. One path deliberately loses context (showing the problem)
4. Another path uses TracedThreadPoolExecutor (showing the fix)
5. An error occurs in one branch (error tracking)

The demo produces:
- Console output showing the traced execution
- An execution graph summary with context gap detection
- A JSON export of the full DAG
- A DOT file for Graphviz visualization
"""

import asyncio
import time
import random
from concurrent.futures import ThreadPoolExecutor

from async_content_tracer.context import ContextManager, TracedThreadPoolExecutor
from async_content_tracer.tracer import Tracer
from async_content_tracer.graph import ExecutionGraph


# ── Setup ─────────────────────────────────────────────────────────────────────

ctx = ContextManager()
tracer = Tracer(context_manager=ctx)


# ── Simulated Service Functions ───────────────────────────────────────────────
# These simulate a microservice-style request pipeline:
# handle_request -> [validate, fetch_user, process_data] -> aggregate -> respond


@tracer.trace
async def handle_request(request_id: str) -> dict:
    """Entry point — simulates an incoming HTTP request handler."""
    print(f"\n📥 Incoming request: {request_id}")

    # Fan out to multiple async tasks (this is where context forks)
    validation_task = asyncio.create_task(validate_request(request_id))
    user_task = asyncio.create_task(fetch_user_data(request_id))

    is_valid = await validation_task
    user_data = await user_task

    if not is_valid:
        raise ValueError(f"Request {request_id} failed validation")

    # Process data — this offloads to thread pool
    processed = await process_data(user_data)

    # Aggregate results
    result = await aggregate_results(request_id, user_data, processed)

    print(f"✅ Request {request_id} complete")
    return result


@tracer.trace
async def validate_request(request_id: str) -> bool:
    """Validates the incoming request (simulated)."""
    await asyncio.sleep(0.05)  # Simulate I/O
    print(f"  ✓ Validated request {request_id}")
    return True


@tracer.trace
async def fetch_user_data(request_id: str) -> dict:
    """Fetches user data from a database (simulated)."""
    await asyncio.sleep(0.08)  # Simulate DB query

    # Sub-query: fetch permissions
    permissions = await fetch_permissions(request_id)

    user = {"id": request_id, "name": "demo_user", "permissions": permissions}
    print(f"  ✓ Fetched user data for {request_id}")
    return user


@tracer.trace
async def fetch_permissions(request_id: str) -> list:
    """Nested async call — fetches user permissions."""
    await asyncio.sleep(0.03)
    return ["read", "write", "admin"]


@tracer.trace
async def process_data(user_data: dict) -> dict:
    """
    Offloads CPU-intensive work to a thread pool.
    This is where context bleed can happen with a standard ThreadPoolExecutor.
    """
    loop = asyncio.get_running_loop()

    # ── DEMO: Show the problem and the fix side by side ──

    # 1. Using TracedThreadPoolExecutor (THE FIX) — context propagates correctly
    print("\n  🔧 Thread pool submission (with context propagation)...")
    with TracedThreadPoolExecutor(max_workers=2) as pool:
        future_good = loop.run_in_executor(
            pool, cpu_intensive_work, user_data, "with_context"
        )
        result_good = await future_good

    # 2. Using standard ThreadPoolExecutor (THE PROBLEM) — context may bleed
    print("  ⚠️  Thread pool submission (standard — context may bleed)...")
    with ThreadPoolExecutor(max_workers=2) as pool:
        future_bad = loop.run_in_executor(
            pool, cpu_intensive_work, user_data, "without_context"
        )
        result_bad = await future_bad

    return {"processed_with_ctx": result_good, "processed_without_ctx": result_bad}


@tracer.trace
def cpu_intensive_work(data: dict, label: str) -> dict:
    """
    Simulates CPU-bound work running on a thread pool worker.
    This function checks whether context was properly propagated.
    """
    import threading

    current_ctx = ctx.current()
    has_context = current_ctx is not None and current_ctx.context_id is not None

    thread_name = threading.current_thread().name
    status = "✅ HAS CONTEXT" if has_context else "❌ CONTEXT LOST"
    print(f"    [{label}] Running on {thread_name}: {status}")

    # Simulate CPU work
    time.sleep(0.05)

    return {
        "label": label,
        "thread": thread_name,
        "context_preserved": has_context,
        "result": sum(range(10000)),
    }


@tracer.trace
async def aggregate_results(
    request_id: str, user_data: dict, processed: dict
) -> dict:
    """Aggregates results from all sub-tasks."""
    await asyncio.sleep(0.02)
    return {
        "request_id": request_id,
        "user": user_data["name"],
        "processing_complete": True,
        "context_comparison": {
            "with_propagation": processed["processed_with_ctx"]["context_preserved"],
            "without_propagation": processed["processed_without_ctx"][
                "context_preserved"
            ],
        },
    }


# ── Scenario 2: Error Propagation ─────────────────────────────────────────────


@tracer.trace
async def handle_failing_request(request_id: str) -> dict:
    """Simulates a request that fails partway through — shows error tracing."""
    print(f"\n📥 Incoming request (will fail): {request_id}")
    await asyncio.sleep(0.02)

    # This will raise — we want to see it in the trace
    result = await flaky_service_call(request_id)
    return result


@tracer.trace
async def flaky_service_call(request_id: str) -> dict:
    """A service that intermittently fails."""
    await asyncio.sleep(0.03)
    raise ConnectionError(
        f"Service unavailable for request {request_id} — "
        "upstream timeout after 3000ms"
    )


# ── Main Demo Runner ──────────────────────────────────────────────────────────


async def main():
    print("=" * 70)
    print("  async_content_tracer — DEMO")
    print("  Async Context Propagation & Execution Graph Reconstruction")
    print("=" * 70)

    # ── Scenario 1: Successful request with context propagation ──
    print("\n" + "─" * 70)
    print("  SCENARIO 1: Request with async fan-out + thread pool offload")
    print("─" * 70)

    # Start a new request context
    root_span = ctx.new_context()
    print(f"  Context ID: {root_span.context_id[:16]}...")

    result = await handle_request("req-001")
    print(f"\n  Result: {result}")

    # ── Scenario 2: Failing request ──
    print("\n" + "─" * 70)
    print("  SCENARIO 2: Request that fails midway (error tracing)")
    print("─" * 70)

    root_span_2 = ctx.new_context()
    print(f"  Context ID: {root_span_2.context_id[:16]}...")

    try:
        await handle_failing_request("req-002")
    except ConnectionError as e:
        print(f"\n  ❌ Caught error: {e}")

    # ── Build Execution Graph ──
    print("\n" + "─" * 70)
    print("  EXECUTION GRAPH ANALYSIS")
    print("─" * 70)

    graph = ExecutionGraph()
    graph.build_from_events(tracer.events)

    # Print summary
    print(f"\n{graph.summary()}")

    # Print tree view
    print("\n📊 Execution Tree:")
    print(graph.print_tree())

    # Show cross-thread transitions
    cross_thread = graph.find_cross_thread_edges()
    if cross_thread:
        print("🔀 Cross-Thread Transitions Detected:")
        for parent, child in cross_thread:
            print(
                f"  {parent.function_name} ({parent.thread_name}) "
                f"→ {child.function_name} ({child.thread_name})"
            )

    # Show errors
    errors = graph.find_errors()
    if errors:
        print("\n❌ Errors in Execution Graph:")
        for err_node in errors:
            chain = graph.get_causal_chain(err_node.span_id)
            print(f"  Error: {err_node.error}")
            print(f"  Causal chain ({len(chain)} spans):")
            for i, span in enumerate(chain):
                arrow = "  └→ " if i == len(chain) - 1 else "  ├→ "
                print(f"    {arrow}{span.function_name}")

    # Export
    json_path = "execution_graph.json"
    dot_path = "execution_graph.dot"
    graph.to_json(json_path)
    graph.to_dot(dot_path)
    print(f"\n💾 Exported: {json_path}, {dot_path}")

    # ── Context Propagation Comparison ──
    print("\n" + "─" * 70)
    print("  CONTEXT PROPAGATION COMPARISON")
    print("─" * 70)
    print(
        """
    TracedThreadPoolExecutor (our fix):
      → Snapshots context at submit() time
      → Restores it on the worker thread
      → ✅ Context preserved across thread boundary

    Standard ThreadPoolExecutor (the problem):
      → Thread pool reuses threads
      → Worker thread may have stale context from previous task
      → ❌ Context bleed / loss

    This is essentially what OpenTelemetry does with their executor wrapper,
    but built from scratch to understand the mechanics.
    """
    )

    print("=" * 70)
    print("  Demo complete. Check execution_graph.json and execution_graph.dot")
    print("=" * 70)


if __name__ == "__main__":
    asyncio.run(main())
