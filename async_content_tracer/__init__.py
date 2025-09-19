"""
async_content_tracer — Async Context Propagation & Execution Graph Reconstruction

A lightweight instrumentation library that:
1. Traces function boundaries (entry/exit) with timing metadata
2. Propagates unique context IDs across async tasks and thread pools
3. Reconstructs execution DAGs (directed acyclic graphs) from collected traces

Built to understand how async context propagation works under the hood —
specifically where context gets LOST (asyncio.create_task, thread pool reuse)
and how to fix it.
"""

from async_content_tracer.context import (
    ContextManager,
    TracedThreadPoolExecutor,
    propagate_context,
)
from async_content_tracer.tracer import (
    Tracer,
    trace,
    auto_trace,
)
from async_content_tracer.graph import ExecutionGraph
from async_content_tracer.http_propagator import HTTPContextPropagator

__version__ = "0.2.0"
__all__ = [
    "ContextManager",
    "TracedThreadPoolExecutor",
    "propagate_context",
    "Tracer",
    "trace",
    "auto_trace",
    "ExecutionGraph",
    "HTTPContextPropagator",
]
