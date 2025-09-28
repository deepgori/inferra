"""
test_context.py — Tests for context propagation engine

Covers:
- Basic context creation and span nesting
- Context isolation between different requests
- Thread pool context propagation (TracedThreadPoolExecutor)
- Context bleed detection with standard ThreadPoolExecutor
- Snapshot/restore mechanics
"""

import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from async_content_tracer.context import (
    ContextManager,
    TracedThreadPoolExecutor,
    _context_id,
    _parent_span_id,
    _trace_depth,
)


class TestContextManager:
    """Tests for ContextManager — context creation and span nesting."""

    def setup_method(self):
        self.ctx = ContextManager()
        # Reset context vars between tests
        _context_id.set(None)
        _parent_span_id.set(None)
        _trace_depth.set(0)

    def test_new_context_creates_unique_id(self):
        """Each new_context() should produce a unique context ID."""
        ctx1 = self.ctx.new_context()
        ctx2 = self.ctx.new_context()

        assert ctx1.context_id != ctx2.context_id
        assert ctx1.span_id != ctx2.span_id

    def test_new_context_is_root(self):
        """Root contexts should have no parent and depth 0."""
        root = self.ctx.new_context()

        assert root.parent_span_id is None
        assert root.depth == 0

    def test_new_span_inherits_context_id(self):
        """Child spans should share the parent's context ID."""
        root = self.ctx.new_context()
        child = self.ctx.new_span()

        assert child.context_id == root.context_id
        assert child.span_id != root.span_id
        assert child.depth == 1

    def test_nested_spans_increment_depth(self):
        """Each nested span should increment the depth."""
        root = self.ctx.new_context()
        child1 = self.ctx.new_span()
        child2 = self.ctx.new_span()
        child3 = self.ctx.new_span()

        assert root.depth == 0
        assert child1.depth == 1
        assert child2.depth == 2
        assert child3.depth == 3

    def test_new_context_resets_depth(self):
        """Starting a new context should reset depth to 0."""
        self.ctx.new_context()
        self.ctx.new_span()
        self.ctx.new_span()

        new_root = self.ctx.new_context()
        assert new_root.depth == 0

    def test_current_returns_active_context(self):
        """current() should reflect the active context."""
        root = self.ctx.new_context()
        current = self.ctx.current()

        assert current is not None
        assert current.context_id == root.context_id

    def test_current_returns_none_without_context(self):
        """current() should return None when no context is active."""
        current = self.ctx.current()
        assert current is None

    def test_span_captures_thread_info(self):
        """Spans should capture the current thread's ID and name."""
        root = self.ctx.new_context()

        assert root.thread_id == threading.get_ident()
        assert root.thread_name == threading.current_thread().name

    def test_span_has_monotonic_timestamp(self):
        """Spans should have monotonically increasing timestamps."""
        root = self.ctx.new_context()
        time.sleep(0.001)
        child = self.ctx.new_span()

        assert child.timestamp > root.timestamp

    def test_snapshot_captures_context(self):
        """snapshot() should capture the current context state."""
        root = self.ctx.new_context()
        snapshot = self.ctx.snapshot()

        # Start a completely different context
        self.ctx.new_context()

        # Running inside the snapshot should restore the original
        def check():
            return _context_id.get()

        result = snapshot.run(check)
        assert result == root.context_id


class TestTracedThreadPoolExecutor:
    """
    Tests for TracedThreadPoolExecutor — the context bleed fix.

    This is the core innovation: ensuring context propagates correctly
    to worker threads instead of bleeding between tasks.
    """

    def setup_method(self):
        self.ctx = ContextManager()
        _context_id.set(None)
        _parent_span_id.set(None)
        _trace_depth.set(0)

    def test_context_propagates_to_worker(self):
        """Context should be available on the worker thread."""
        root = self.ctx.new_context()
        results = {}

        def worker():
            ctx_id = _context_id.get()
            results["context_id"] = ctx_id
            results["thread"] = threading.current_thread().name

        with TracedThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(worker)
            future.result()

        assert results["context_id"] == root.context_id
        assert results["thread"] != threading.current_thread().name

    def test_context_isolation_between_tasks(self):
        """Different tasks should see their own context, not stale context."""
        results = []

        def capture_context(label):
            ctx_id = _context_id.get()
            results.append({"label": label, "context_id": ctx_id})
            time.sleep(0.01)  # ensure thread reuse

        with TracedThreadPoolExecutor(max_workers=1) as pool:
            # Submit task with context A
            root_a = self.ctx.new_context()
            future_a = pool.submit(capture_context, "task_a")
            future_a.result()

            # Submit task with context B on the SAME worker thread
            root_b = self.ctx.new_context()
            future_b = pool.submit(capture_context, "task_b")
            future_b.result()

        # Each task should see its OWN context, not the other's
        task_a_result = next(r for r in results if r["label"] == "task_a")
        task_b_result = next(r for r in results if r["label"] == "task_b")

        assert task_a_result["context_id"] == root_a.context_id
        assert task_b_result["context_id"] == root_b.context_id
        assert task_a_result["context_id"] != task_b_result["context_id"]

    def test_standard_executor_may_not_propagate(self):
        """
        Standard ThreadPoolExecutor doesn't propagate contextvars
        in the same guaranteed way. This test documents the behavior.
        """
        # Reset to ensure no context
        _context_id.set(None)

        results = {}

        def worker():
            # On a fresh thread with no context set, this should be None
            results["context_id"] = _context_id.get()

        # Note: In CPython 3.12+, contextvars DO propagate to threads
        # by default via PEP 567. But the TracedThreadPoolExecutor
        # ensures explicit, snapshot-based propagation regardless.
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(worker)
            future.result()

        # The key point: without explicit propagation, we can't guarantee
        # the worker sees the RIGHT context (it might see stale state
        # from a previous task on the same thread)

    def test_multiple_concurrent_tasks_isolated(self):
        """Multiple concurrent tasks should each see their own context."""
        results = []
        barrier = threading.Barrier(3)  # sync 3 workers

        def worker(task_id):
            ctx_id = _context_id.get()
            barrier.wait(timeout=5)  # ensure all run concurrently
            results.append({"task_id": task_id, "context_id": ctx_id})

        # We'll submit all with the same context to test they all see it
        root = self.ctx.new_context()

        with TracedThreadPoolExecutor(max_workers=3) as pool:
            futures = [pool.submit(worker, i) for i in range(3)]
            for f in futures:
                f.result()

        # All should see the same context
        for r in results:
            assert r["context_id"] == root.context_id


class TestWrapForThread:
    """Tests for the manual context wrapping mechanism."""

    def setup_method(self):
        self.ctx = ContextManager()
        _context_id.set(None)
        _parent_span_id.set(None)
        _trace_depth.set(0)

    def test_wrap_preserves_context(self):
        """wrap_for_thread should carry context into the wrapped function."""
        root = self.ctx.new_context()
        results = {}

        def my_func():
            results["context_id"] = _context_id.get()

        wrapped = self.ctx.wrap_for_thread(my_func)

        # Run on a different thread
        t = threading.Thread(target=wrapped)
        t.start()
        t.join()

        assert results["context_id"] == root.context_id

    def test_wrap_isolates_from_thread_state(self):
        """Wrapped function should see caller's context, not thread's state."""
        root = self.ctx.new_context()
        results = {}

        def pollute_and_check():
            # Even if this thread had different context, the wrapped
            # function should see the caller's snapshot
            results["context_id"] = _context_id.get()

        wrapped = self.ctx.wrap_for_thread(pollute_and_check)

        # Change the context AFTER wrapping
        self.ctx.new_context()

        # The wrapped function should still see the ORIGINAL context
        t = threading.Thread(target=wrapped)
        t.start()
        t.join()

        assert results["context_id"] == root.context_id
