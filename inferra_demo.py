"""
inferra_demo.py — Inferra Autonomous Debugging Demo

Demonstrates the full pipeline:
    async_content_tracer (telemetry capture)
        → inferra (AI reasoning)
            → RCA report

Shows:
1. Codebase indexing (indexes this project's own source code)
2. Running instrumented code that produces errors
3. RAG retrieval (mapping errors back to source code)
4. Multi-agent investigation (LogAnalysis + MetricsCorrelation + Coordinator)
5. Final RCA report with root cause, causal chain, and recommendations
"""

import asyncio
import time
from concurrent.futures import ThreadPoolExecutor

from async_content_tracer.context import ContextManager, TracedThreadPoolExecutor
from async_content_tracer.tracer import Tracer
from async_content_tracer.graph import ExecutionGraph

from inferra.indexer import CodeIndexer
from inferra.rag import RAGPipeline
from inferra.rca_engine import RCAEngine


# ── Setup ─────────────────────────────────────────────────────────────────────

ctx = ContextManager()
tracer = Tracer(context_manager=ctx)


# ── Simulated Production Service ──────────────────────────────────────────────
# A mini e-commerce order pipeline that has a real bug


@tracer.trace
async def handle_order(order_id: str) -> dict:
    """Main order handler — entry point for a customer order."""
    print(f"\n📦 Processing order: {order_id}")

    # Validate the order
    valid = await validate_order(order_id)
    if not valid:
        raise ValueError(f"Order {order_id} failed validation")

    # Process payment and fetch inventory concurrently
    payment_task = asyncio.create_task(process_payment(order_id, amount=99.99))
    inventory_task = asyncio.create_task(check_inventory(order_id, sku="WIDGET-42"))

    payment_result = await payment_task
    inventory_result = await inventory_task

    # Ship the order (this offloads to thread pool)
    shipment = await create_shipment(order_id, inventory_result)

    return {
        "order_id": order_id,
        "payment": payment_result,
        "shipment": shipment,
        "status": "complete",
    }


@tracer.trace
async def validate_order(order_id: str) -> bool:
    """Validates order data against business rules."""
    await asyncio.sleep(0.02)
    print(f"  ✓ Order {order_id} validated")
    return True


@tracer.trace
async def process_payment(order_id: str, amount: float) -> dict:
    """Calls the payment gateway — this is where things go wrong."""
    await asyncio.sleep(0.05)

    # Simulate the payment gateway being down
    await call_payment_gateway(order_id, amount)

    return {"status": "charged", "amount": amount}


@tracer.trace
async def call_payment_gateway(order_id: str, amount: float) -> dict:
    """
    External payment service call.
    BUG: This service has been timing out since the last deploy.
    """
    await asyncio.sleep(0.1)  # Simulate slow response

    # The actual bug — gateway returns an error
    raise ConnectionError(
        f"Payment gateway timeout after 3000ms for order {order_id} "
        f"(amount=${amount:.2f}) — upstream service at payments.internal:443 "
        f"is not responding. Last successful call was 4 minutes ago."
    )


@tracer.trace
async def check_inventory(order_id: str, sku: str) -> dict:
    """Checks warehouse inventory levels."""
    await asyncio.sleep(0.03)
    print(f"  ✓ Inventory available for {sku}")
    return {"sku": sku, "available": True, "warehouse": "US-WEST-1"}


@tracer.trace
async def create_shipment(order_id: str, inventory: dict) -> dict:
    """Creates a shipment by offloading label generation to thread pool."""
    loop = asyncio.get_running_loop()

    with TracedThreadPoolExecutor(max_workers=2) as pool:
        label = await loop.run_in_executor(
            pool, generate_shipping_label, order_id, inventory
        )

    return {"tracking_id": f"TRACK-{order_id}", "label": label}


@tracer.trace
def generate_shipping_label(order_id: str, inventory: dict) -> str:
    """CPU-intensive label generation on thread pool."""
    time.sleep(0.04)
    print(f"  ✓ Shipping label generated for {order_id}")
    return f"LABEL-{order_id}-{inventory.get('warehouse', 'UNKNOWN')}"


# ── Demo Runner ───────────────────────────────────────────────────────────────


async def main():
    print("=" * 70)
    print("  inferra — Autonomous Debugging Demo")
    print("  Telemetry → CodeIndex → RAG → Multi-Agent RCA")
    print("=" * 70)

    # ── Step 1: Index the codebase ──
    print("\n" + "─" * 70)
    print("  STEP 1: Index the Codebase")
    print("─" * 70)

    engine = RCAEngine(slow_threshold_ms=80.0)
    engine.index_codebase(".", exclude_patterns=[
        "__pycache__", ".git", "venv", "interview_prep", ".pytest_cache",
    ])

    stats = engine.stats()
    print(f"  Indexed: {stats['total_units']} code units across {stats['files_indexed']} files")
    print(f"  Functions: {stats['functions']}, Classes: {stats['classes']}")
    print(f"  Log patterns: {stats['log_patterns']}")
    print(f"  Unique tokens: {stats['unique_tokens']}")

    # ── Step 2: Run instrumented code (with a bug) ──
    print("\n" + "─" * 70)
    print("  STEP 2: Run Instrumented Code (simulated production request)")
    print("─" * 70)

    root = ctx.new_context()
    print(f"  Context ID: {root.context_id[:16]}...")

    try:
        result = await handle_order("ORD-2024-001")
        print(f"  Result: {result}")
    except (ConnectionError, Exception) as e:
        print(f"\n  ❌ Order failed: {e}")

    # ── Step 3: RAG Code Retrieval ──
    print("\n" + "─" * 70)
    print("  STEP 3: RAG — Map Telemetry to Source Code")
    print("─" * 70)

    if engine.rag:
        # Search for payment-related code
        results = engine.indexer.search("payment gateway timeout connection")
        print(f"\n  Query: 'payment gateway timeout connection'")
        print(f"  Found {len(results)} relevant code locations:")
        for i, r in enumerate(results[:5]):
            print(f"    [{i+1}] {r.code_unit.qualified_name} (score: {r.score:.3f})")
            print(f"        File: {r.code_unit.source_file}:{r.code_unit.start_line}")
            if r.code_unit.docstring:
                print(f"        Doc: {r.code_unit.docstring[:100]}")

        # Log pattern search — code-origin mapping
        print(f"\n  Log Pattern Search: 'Shipping label generated'")
        log_results = engine.indexer.search_by_log_pattern("Shipping label generated")
        for r in log_results[:3]:
            print(f"    📍 {r.code_unit.qualified_name}")
            print(f"       File: {r.code_unit.source_file}:{r.code_unit.start_line}")

    # ── Step 4: Multi-Agent Investigation ──
    print("\n" + "─" * 70)
    print("  STEP 4: Multi-Agent Investigation → RCA Report")
    print("─" * 70)

    report = engine.investigate(tracer.events)

    # Print the full RCA report
    print(f"\n{report.to_string()}")

    # Quick diagnosis (Slack-style)
    print(f"\n  📱 Slack notification:")
    print(f"    {engine.quick_diagnosis(tracer.events)}")

    # ── Step 5: Execution Graph ──
    print("\n" + "─" * 70)
    print("  STEP 5: Execution Graph Visualization")
    print("─" * 70)

    graph = ExecutionGraph()
    graph.build_from_events(tracer.events)
    print(f"\n{graph.summary()}")
    print(f"\n📊 Execution Tree:")
    print(graph.print_tree())

    print("=" * 70)
    print("  Demo complete.")
    print("  async_content_tracer captured telemetry → inferra produced RCA")
    print("=" * 70)


if __name__ == "__main__":
    asyncio.run(main())
