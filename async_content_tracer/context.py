"""
context.py — Context ID Propagation Engine

Handles the core problem: carrying a unique context ID across async boundaries
where it would normally get lost.

Key mechanisms:
- contextvars.ContextVar for async-safe storage
- Manual context snapshot/restore for thread pools (fixing context bleed)
- Wrapper around ThreadPoolExecutor.submit() that propagates caller context

The critical insight: asyncio.create_task() DOES copy context (shallow copy),
but thread pools DON'T — threads get reused, so stale context from a previous
task bleeds into the next one. We fix this by snapshotting the caller's context
before submission and restoring it on the worker thread.
"""

import asyncio
import contextvars
import uuid
import time
import threading
from concurrent.futures import ThreadPoolExecutor, Future
from dataclasses import dataclass, field
from typing import Any, Callable, Optional


# ── Context Variables ──────────────────────────────────────────────────────────
# These are the "thread-local but async-aware" storage mechanism.
# In a normal asyncio flow, context copies on `await`. But create_task() does a
# shallow copy — mutations in child don't propagate back to parent. And thread
# pools don't propagate at all.

_context_id: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "context_id", default=None
)
_parent_span_id: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "parent_span_id", default=None
)
_trace_depth: contextvars.ContextVar[int] = contextvars.ContextVar(
    "trace_depth", default=0
)


@dataclass
class SpanContext:
    """Immutable snapshot of the current execution context at a point in time."""

    context_id: str
    span_id: str
    parent_span_id: Optional[str]
    depth: int
    thread_id: int
    thread_name: str
    timestamp: float

    def __repr__(self) -> str:
        return (
            f"SpanContext(ctx={self.context_id[:8]}.. "
            f"span={self.span_id[:8]}.. "
            f"parent={self.parent_span_id[:8] + '..' if self.parent_span_id else 'None'} "
            f"depth={self.depth} "
            f"thread={self.thread_name})"
        )


class ContextManager:
    """
    Manages context propagation across async and thread boundaries.

    Usage:
        ctx_mgr = ContextManager()

        # Start a new request context
        root = ctx_mgr.new_context()

        # Create child spans
        child = ctx_mgr.new_span()

        # Get current context
        current = ctx_mgr.current()

        # Wrap a function for thread pool execution (prevents context bleed)
        wrapped = ctx_mgr.wrap_for_thread(some_function)
    """

    def __init__(self):
        self._lock = threading.Lock()

    def new_context(self) -> SpanContext:
        """
        Start a brand new request context. This is the root — every span
        created after this (until the next new_context) will be a descendant.
        """
        context_id = str(uuid.uuid4())
        span_id = str(uuid.uuid4())

        _context_id.set(context_id)
        _parent_span_id.set(None)
        _trace_depth.set(0)

        return SpanContext(
            context_id=context_id,
            span_id=span_id,
            parent_span_id=None,
            depth=0,
            thread_id=threading.get_ident(),
            thread_name=threading.current_thread().name,
            timestamp=time.monotonic(),
        )

    def new_span(self, parent_span_id: Optional[str] = None) -> SpanContext:
        """
        Create a child span within the current context. If parent_span_id
        is not provided, uses the current span as parent.
        """
        context_id = _context_id.get()
        if context_id is None:
            # No active context — create a new root context
            return self.new_context()

        span_id = str(uuid.uuid4())
        current_parent = parent_span_id or _parent_span_id.get()
        current_depth = _trace_depth.get()

        # Update context vars for the new span
        _parent_span_id.set(span_id)
        _trace_depth.set(current_depth + 1)

        return SpanContext(
            context_id=context_id,
            span_id=span_id,
            parent_span_id=current_parent,
            depth=current_depth + 1,
            thread_id=threading.get_ident(),
            thread_name=threading.current_thread().name,
            timestamp=time.monotonic(),
        )

    def current(self) -> Optional[SpanContext]:
        """Get the current context, or None if no context is active."""
        context_id = _context_id.get()
        if context_id is None:
            return None

        return SpanContext(
            context_id=context_id,
            span_id=str(uuid.uuid4()),  # snapshot ID
            parent_span_id=_parent_span_id.get(),
            depth=_trace_depth.get(),
            thread_id=threading.get_ident(),
            thread_name=threading.current_thread().name,
            timestamp=time.monotonic(),
        )

    def snapshot(self) -> contextvars.Context:
        """
        Take a snapshot of the current context. This is the key mechanism
        for thread pool propagation — you snapshot BEFORE submitting to the
        pool, then restore on the worker thread.
        """
        return contextvars.copy_context()

    def wrap_for_thread(self, fn: Callable, *args, **kwargs) -> Callable:
        """
        Wrap a function so it carries the caller's context into a thread.

        This is the fix for context bleed: thread pools reuse threads, so
        if Task A sets context on Thread 1, and Task B later runs on Thread 1,
        it sees Task A's stale context. By wrapping, we:
        1. Snapshot the caller's context at submit time
        2. Restore it on the worker thread before execution
        3. Clean up after execution to prevent bleed
        """
        ctx_snapshot = self.snapshot()

        def _wrapped():
            return ctx_snapshot.run(fn, *args, **kwargs)

        return _wrapped


class TracedThreadPoolExecutor(ThreadPoolExecutor):
    """
    A ThreadPoolExecutor that automatically propagates context to worker threads.

    The standard ThreadPoolExecutor has a context bleed problem: threads are
    reused, so context from a previous task can leak into the next one.

    This wrapper fixes it by:
    1. Capturing the caller's contextvars.Context at submit() time
    2. Running the submitted function inside that captured context
    3. Ensuring clean context isolation between tasks on the same thread

    This is essentially what OpenTelemetry does with their executor wrapper,
    but built from scratch to understand the mechanics.
    """

    def __init__(self, *args, **kwargs):
        self._context_manager = ContextManager()
        super().__init__(*args, **kwargs)

    def submit(self, fn: Callable, *args, **kwargs) -> Future:
        """
        Submit a function for execution, propagating the caller's context.

        Instead of just submitting `fn`, we:
        1. Copy the current context (snapshot)
        2. Create a wrapper that runs `fn` inside that snapshot
        3. Submit the wrapper to the thread pool

        This ensures each task sees the context from when it was submitted,
        not whatever stale context was left on the worker thread.
        """
        # Capture context from the CALLER's scope (this is the key moment)
        ctx = contextvars.copy_context()

        def _context_aware_fn():
            # Run inside the captured context — this is what prevents bleed
            return ctx.run(fn, *args, **kwargs)

        return super().submit(_context_aware_fn)


def propagate_context(coro):
    """
    Decorator that ensures an async coroutine properly propagates context
    when used with asyncio.create_task().

    asyncio.create_task() copies context (shallow), which means:
    - The child task SEES the parent's context at creation time ✓
    - Mutations in the child DON'T propagate back to the parent ✗
    - This is usually what you want for independent child tasks

    This decorator makes the propagation explicit and adds tracking.
    """

    async def wrapper(*args, **kwargs):
        # Ensure we have a valid context
        ctx_id = _context_id.get()
        if ctx_id is None:
            # If somehow we lost context, log it — this is the exact bug
            # that this whole project exists to catch
            import warnings
            warnings.warn(
                f"Context lost before entering {coro.__name__}! "
                "This is the async boundary propagation problem."
            )

        return await coro(*args, **kwargs)

    wrapper.__name__ = coro.__name__
    wrapper.__qualname__ = coro.__qualname__
    return wrapper
