"""
test_stress.py — Stress tests to expose edge cases and bugs

These tests push the project harder than the basic tests:
- Concurrent context isolation under load
- Depth tracking correctness after nested call chains
- Graph reconstruction with complex topologies
- Error propagation in deeply nested async
- Thread pool with many concurrent submissions
- Edge cases: empty events, single events, duplicate span IDs
"""

import asyncio
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
from async_content_tracer.tracer import EventType, Tracer, TraceEvent
from async_content_tracer.graph import ExecutionGraph


class TestDepthTracking:
    """Verify depth tracking is correct through nested calls."""

    def setup_method(self):
        self.ctx = ContextManager()
        self.tracer = Tracer(context_manager=self.ctx)
        _context_id.set(None)
        _parent_span_id.set(None)
        _trace_depth.set(0)

    def test_depth_restores_after_traced_function_returns(self):
        """
        BUG CHECK: After a @trace'd function returns, the depth should
        be restored to what it was before the call. Otherwise sibling
        calls get ever-increasing depths.
        """
        self.ctx.new_context()

        @self.tracer.trace
        def level_one_a():
            return "a"

        @self.tracer.trace
        def level_one_b():
            return "b"

        # These are siblings — both called at depth 0
        level_one_a()
        depth_after_a = _trace_depth.get()

        level_one_b()
        depth_after_b = _trace_depth.get()

        # Get the entry events
        entries = [e for e in self.tracer.events if e.event_type == EventType.ENTRY]
        assert len(entries) == 2

        # BOTH should have the same depth since they're siblings
        # If depth isn't restored, level_one_b would have depth = 2
        # while it should have depth = 1 (same as level_one_a)
        assert entries[0].depth == entries[1].depth, (
            f"Sibling depths differ: {entries[0].depth} vs {entries[1].depth}. "
            f"Depth may not be restoring after calls."
        )

    def test_depth_correct_for_deep_nesting(self):
        """Verify depths are monotonically increasing in a deep chain."""
        self.ctx.new_context()

        @self.tracer.trace
        def depth_0():
            return depth_1()

        @self.tracer.trace
        def depth_1():
            return depth_2()

        @self.tracer.trace
        def depth_2():
            return depth_3()

        @self.tracer.trace
        def depth_3():
            return "bottom"

        depth_0()

        entries = [e for e in self.tracer.events if e.event_type == EventType.ENTRY]
        depths = [e.depth for e in entries]

        # Should be strictly increasing
        for i in range(1, len(depths)):
            assert depths[i] > depths[i - 1], (
                f"Depth didn't increase: {depths}"
            )

    @pytest.mark.asyncio
    async def test_depth_correct_across_async_siblings(self):
        """Async siblings should have equal depth."""
        self.ctx.new_context()

        @self.tracer.trace
        async def parent():
            # These are sequential siblings
            await child_a()
            await child_b()
            await child_c()

        @self.tracer.trace
        async def child_a():
            await asyncio.sleep(0.001)

        @self.tracer.trace
        async def child_b():
            await asyncio.sleep(0.001)

        @self.tracer.trace
        async def child_c():
            await asyncio.sleep(0.001)

        await parent()

        entries = [e for e in self.tracer.events if e.event_type == EventType.ENTRY]
        child_entries = [e for e in entries if e.function_name != parent.__qualname__]

        # All children should have the same depth
        child_depths = [e.depth for e in child_entries]
        assert len(set(child_depths)) == 1, (
            f"Sibling async tasks have different depths: {child_depths}"
        )


class TestConcurrentContextIsolation:
    """Heavy concurrent tests for context isolation."""

    def setup_method(self):
        self.ctx = ContextManager()
        _context_id.set(None)
        _parent_span_id.set(None)
        _trace_depth.set(0)

    def test_many_concurrent_tasks_all_see_own_context(self):
        """50 concurrent thread pool tasks should each see their own context."""
        results = []
        errors = []
        num_tasks = 50

        with TracedThreadPoolExecutor(max_workers=4) as pool:
            futures = []
            for i in range(num_tasks):
                root = self.ctx.new_context()
                expected_ctx = root.context_id

                def worker(task_id, expected):
                    actual = _context_id.get()
                    results.append({
                        "task_id": task_id,
                        "expected": expected,
                        "actual": actual,
                        "match": actual == expected,
                    })

                futures.append(pool.submit(worker, i, expected_ctx))

            for f in futures:
                f.result()

        # Every task should have seen its own context
        mismatches = [r for r in results if not r["match"]]
        assert len(mismatches) == 0, (
            f"{len(mismatches)}/{num_tasks} tasks saw wrong context: "
            f"{mismatches[:5]}"
        )

    def test_rapid_context_switching(self):
        """Rapidly creating and switching contexts should not corrupt state."""
        contexts = []
        for _ in range(100):
            root = self.ctx.new_context()
            contexts.append(root.context_id)
            # Immediately check
            assert _context_id.get() == root.context_id

        # All should be unique
        assert len(set(contexts)) == 100


class TestGraphEdgeCases:
    """Edge cases for graph reconstruction."""

    def test_empty_events(self):
        """Graph should handle empty event list gracefully."""
        graph = ExecutionGraph()
        graph.build_from_events([])

        assert len(graph.nodes) == 0
        assert graph.find_roots() == []
        assert graph.find_errors() == []
        assert graph.find_context_gaps() == []
        assert graph.summary() is not None
        assert graph.print_tree() is not None
        assert graph.to_json() is not None
        assert graph.to_dot() is not None

    def test_single_entry_event_only(self):
        """A single entry event (no matching exit) should still create a node."""
        events = [
            TraceEvent(
                event_type=EventType.ENTRY,
                function_name="orphan",
                module="test",
                source_file="test.py",
                source_line=1,
                timestamp=time.monotonic(),
                duration=None,
                context_id="ctx-1",
                span_id="span-orphan",
                parent_span_id=None,
                depth=0,
                thread_id=1,
                thread_name="MainThread",
            )
        ]

        graph = ExecutionGraph()
        graph.build_from_events(events)

        assert len(graph.nodes) == 1
        node = graph.nodes["span-orphan"]
        assert node.duration is None  # No exit event to calculate duration

    def test_error_only_event(self):
        """An error event without a matching entry should still create a node."""
        events = [
            TraceEvent(
                event_type=EventType.ERROR,
                function_name="crashed",
                module="test",
                source_file="test.py",
                source_line=1,
                timestamp=time.monotonic(),
                duration=0.05,
                context_id="ctx-1",
                span_id="span-crash",
                parent_span_id=None,
                depth=0,
                thread_id=1,
                thread_name="MainThread",
                error="RuntimeError: kaboom",
            )
        ]

        graph = ExecutionGraph()
        graph.build_from_events(events)

        assert len(graph.nodes) == 1
        assert graph.find_errors()[0].error == "RuntimeError: kaboom"

    def test_disconnected_spans(self):
        """Spans with parent_span_id pointing to nonexistent spans should be roots."""
        t = time.monotonic()
        events = [
            TraceEvent(
                event_type=EventType.ENTRY,
                function_name="detached",
                module="test",
                source_file="test.py",
                source_line=1,
                timestamp=t,
                duration=None,
                context_id="ctx-1",
                span_id="span-detached",
                parent_span_id="nonexistent-parent",  # Points to nothing
                depth=0,
                thread_id=1,
                thread_name="MainThread",
            ),
            TraceEvent(
                event_type=EventType.EXIT,
                function_name="detached",
                module="test",
                source_file="test.py",
                source_line=1,
                timestamp=t + 0.01,
                duration=0.01,
                context_id="ctx-1",
                span_id="span-detached",
                parent_span_id="nonexistent-parent",
                depth=0,
                thread_id=1,
                thread_name="MainThread",
            ),
        ]

        graph = ExecutionGraph()
        graph.build_from_events(events)

        # Should still be a root (no valid parent in graph)
        roots = graph.find_roots()
        assert len(roots) == 1
        assert roots[0].function_name == "detached"

    def test_wide_fanout(self):
        """A parent with 20 children should reconstruct correctly."""
        t = time.monotonic()
        events = [
            TraceEvent(
                event_type=EventType.ENTRY,
                function_name="dispatcher",
                module="test",
                source_file="test.py",
                source_line=1,
                timestamp=t,
                duration=None,
                context_id="ctx-1",
                span_id="root",
                parent_span_id=None,
                depth=0,
                thread_id=1,
                thread_name="MainThread",
            ),
        ]

        for i in range(20):
            events.append(
                TraceEvent(
                    event_type=EventType.ENTRY,
                    function_name=f"worker_{i}",
                    module="test",
                    source_file="test.py",
                    source_line=10 + i,
                    timestamp=t + 0.001 * (i + 1),
                    duration=None,
                    context_id="ctx-1",
                    span_id=f"worker-{i}",
                    parent_span_id="root",
                    depth=1,
                    thread_id=1,
                    thread_name="MainThread",
                )
            )
            events.append(
                TraceEvent(
                    event_type=EventType.EXIT,
                    function_name=f"worker_{i}",
                    module="test",
                    source_file="test.py",
                    source_line=10 + i,
                    timestamp=t + 0.001 * (i + 1) + 0.0005,
                    duration=0.0005,
                    context_id="ctx-1",
                    span_id=f"worker-{i}",
                    parent_span_id="root",
                    depth=1,
                    thread_id=1,
                    thread_name="MainThread",
                )
            )

        graph = ExecutionGraph()
        graph.build_from_events(events)

        assert len(graph.nodes) == 21  # root + 20 children
        branches = graph.get_branching_points()
        assert len(branches) == 1
        assert branches[0].function_name == "dispatcher"
        assert graph.graph.out_degree("root") == 20


class TestTracerEdgeCases:
    """Edge cases for the tracer."""

    def setup_method(self):
        self.ctx = ContextManager()
        self.tracer = Tracer(context_manager=self.ctx)
        _context_id.set(None)
        _parent_span_id.set(None)
        _trace_depth.set(0)

    def test_trace_without_context_still_works(self):
        """@trace should still record events even without an active context."""
        # DON'T create a context first
        @self.tracer.trace
        def no_context_func():
            return 42

        result = no_context_func()
        assert result == 42

        # Should still have events (new_span creates a context automatically)
        assert len(self.tracer.events) >= 2

    def test_trace_preserves_function_metadata(self):
        """@trace should preserve __name__, __doc__, etc."""
        @self.tracer.trace
        def documented_func():
            """I have a docstring."""
            return True

        assert documented_func.__name__ == "documented_func"
        assert "docstring" in documented_func.__doc__

    @pytest.mark.asyncio
    async def test_trace_exception_still_raises(self):
        """Exceptions should be re-raised after recording."""
        self.ctx.new_context()

        @self.tracer.trace
        async def bomb():
            raise RuntimeError("💥")

        with pytest.raises(RuntimeError, match="💥"):
            await bomb()

        # Error should be recorded
        errors = [e for e in self.tracer.events if e.event_type == EventType.ERROR]
        assert len(errors) == 1

    @pytest.mark.asyncio
    async def test_many_concurrent_traced_functions(self):
        """100 concurrent async traced functions should all complete."""
        self.ctx.new_context()
        results = []

        @self.tracer.trace
        async def worker(i):
            await asyncio.sleep(0.001)
            results.append(i)
            return i

        tasks = [asyncio.create_task(worker(i)) for i in range(100)]
        await asyncio.gather(*tasks)

        assert len(results) == 100
        assert len(self.tracer.events) == 200  # 100 entries + 100 exits

    def test_thread_safety_of_event_recording(self):
        """Multiple threads recording events should not lose any."""
        self.ctx.new_context()
        num_threads = 10
        events_per_thread = 50

        @self.tracer.trace
        def thread_work():
            time.sleep(0.001)

        threads = []
        for _ in range(num_threads):
            t = threading.Thread(target=lambda: [thread_work() for _ in range(events_per_thread)])
            threads.append(t)

        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # Should have exactly num_threads * events_per_thread * 2 events (entry + exit)
        expected = num_threads * events_per_thread * 2
        actual = len(self.tracer.events)
        assert actual == expected, f"Lost events: expected {expected}, got {actual}"


class TestInferraIntegration:
    """Integration tests for the inferra module."""

    def test_rca_engine_on_traced_events(self):
        """Full pipeline: trace → graph → inferra RCA."""
        from inferra.rca_engine import RCAEngine

        ctx = ContextManager()
        tracer = Tracer(context_manager=ctx)
        _context_id.set(None)
        _parent_span_id.set(None)
        _trace_depth.set(0)

        ctx.new_context()

        @tracer.trace
        def parent():
            return child()

        @tracer.trace
        def child():
            raise ConnectionError("database connection refused")

        try:
            parent()
        except ConnectionError:
            pass

        engine = RCAEngine()
        report = engine.investigate(tracer.events)

        assert report is not None
        assert report.root_cause is not None
        assert len(report.findings) > 0
        assert any("connection" in f.summary.lower() for f in report.findings)

    def test_code_indexer_on_own_codebase(self):
        """The indexer should successfully index this project."""
        from inferra.indexer import CodeIndexer

        indexer = CodeIndexer()
        indexer.index_directory(".", exclude_patterns=[
            "__pycache__", ".git", "venv", "interview_prep", ".pytest_cache",
        ])

        stats = indexer.stats()
        assert stats["total_units"] > 0
        assert stats["files_indexed"] > 0

        # Should find our own classes
        result = indexer.search_by_function_name("TracedThreadPoolExecutor")
        assert result is not None
        assert result.name == "TracedThreadPoolExecutor"

    def test_rag_finds_relevant_code(self):
        """RAG should retrieve relevant code for a query."""
        from inferra.indexer import CodeIndexer
        from inferra.rag import RAGPipeline

        indexer = CodeIndexer()
        indexer.index_directory(".", exclude_patterns=[
            "__pycache__", ".git", "venv", "interview_prep", ".pytest_cache",
        ])

        rag = RAGPipeline(indexer)
        context = rag.retrieve_for_query("thread pool context propagation")

        assert len(context.code_results) > 0
        # Should find TracedThreadPoolExecutor or related code
        found_names = [r.code_unit.name for r in context.code_results]
        assert any(
            "thread" in name.lower() or "pool" in name.lower() or "context" in name.lower()
            for name in found_names
        ), f"Expected thread/pool/context-related results, got: {found_names}"
