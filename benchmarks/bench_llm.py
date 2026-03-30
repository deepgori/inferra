"""
bench_llm.py — LLM Backend Latency Benchmark

Compares response times across available LLM backends:
- Claude (Anthropic)
- Groq (OpenAI-compatible)
- Ollama (local)

Usage:
    python -m benchmarks.bench_llm
"""

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


BENCHMARK_PROMPT = """
Analyze the following trace data and identify the root cause:

Trace: 15 spans across 2 services (order-service, payment-service)
- GET /api/orders: 250ms (OK)
- fetch_orders: 200ms (OK)
  - db.query SELECT * FROM orders: 50ms (OK)
  - fetch_user (x5, N+1 pattern): 30ms each (OK)
- POST /api/checkout: 3500ms (ERROR: TimeoutError)
  - validate_cart: 10ms (OK)
  - process_payment: 3400ms (ERROR: upstream timeout)
    - stripe.charges.create: 3350ms (TIMEOUT)

Code context:
def process_payment(order_id, amount):
    response = requests.post(STRIPE_API, json={"amount": amount}, timeout=5)
    return response.json()

What is the root cause and recommended fix?
"""


def bench_backend(backend, name: str, runs: int = 3):
    """Benchmark a single LLM backend."""
    times = []
    for i in range(runs):
        start = time.perf_counter()
        try:
            result = backend.call(
                BENCHMARK_PROMPT,
                system="You are a debugging assistant. Be concise.",
                max_tokens=500,
            )
            elapsed = time.perf_counter() - start
            if result:
                times.append(elapsed)
                tokens_approx = len(result.split())
                print(f"    Run {i+1}: {elapsed:.2f}s (~{tokens_approx} words)")
            else:
                print(f"    Run {i+1}: EMPTY RESPONSE")
        except Exception as e:
            elapsed = time.perf_counter() - start
            print(f"    Run {i+1}: FAILED ({elapsed:.2f}s) — {e}")

    if times:
        avg = sum(times) / len(times)
        print(f"    Average: {avg:.2f}s ({len(times)}/{runs} successful)")
    else:
        print(f"    All {runs} runs failed")
    return times


def bench_llm():
    """Benchmark all available LLM backends."""
    print(f"\n{'='*60}")
    print(f"  LLM Backend Latency Benchmark")
    print(f"{'='*60}\n")

    from inferra.llm_agent import get_llm_backend

    # Try each backend
    results = {}

    # Claude
    if os.environ.get("ANTHROPIC_API_KEY"):
        print("  📡 Claude (Anthropic):")
        try:
            from inferra.llm_agent import ClaudeBackend
            backend = ClaudeBackend()
            results["Claude"] = bench_backend(backend, "Claude")
        except Exception as e:
            print(f"    Skipped: {e}")
    else:
        print("  ⏭️  Claude: ANTHROPIC_API_KEY not set")

    print()

    # Groq
    if os.environ.get("GROQ_API_KEY"):
        print("  ⚡ Groq:")
        try:
            from inferra.llm_agent import GroqBackend
            backend = GroqBackend()
            results["Groq"] = bench_backend(backend, "Groq")
        except Exception as e:
            print(f"    Skipped: {e}")
    else:
        print("  ⏭️  Groq: GROQ_API_KEY not set")

    print()

    # Ollama
    print("  🦙 Ollama (local):")
    try:
        from inferra.llm_agent import OllamaBackend
        backend = OllamaBackend()
        results["Ollama"] = bench_backend(backend, "Ollama")
    except Exception as e:
        print(f"    Skipped: {e}")

    # Summary
    print(f"\n  {'─'*50}")
    print(f"  Summary:")
    for name, times in results.items():
        if times:
            avg = sum(times) / len(times)
            print(f"    {name:15s}: {avg:.2f}s avg")
        else:
            print(f"    {name:15s}: failed")
    print()


if __name__ == "__main__":
    bench_llm()
