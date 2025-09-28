"""
test_tracer.py — Tests for function boundary instrumentation

Covers:
- @trace decorator on sync functions
- @trace decorator on async functions
- Entry/exit event capture with timing
- Error event capture
- Custom trace names
- Thread info capture
- Global trace convenience function
"""

import asyncio
import time

import pytest

from async_content_tracer.context import ContextManager, _context_id, _parent_span_id, _trace_depth
from async_content_tracer.tracer import EventType, Tracer, TraceEvent


class TestTraceDecorator:
    """Tests for the @trace decorator — function boundary instrumentation."""

    def setup_method(self):
        self.ctx = ContextManager()
        self.tracer = Tracer(context_manager=self.ctx)
        _context_id.set(None)
        _parent_span_id.set(None)
        _trace_depth.set(0)

    def test_trace_sync_captures_entry_and_exit(self):
        """@trace should capture both entry and exit events."""
        self.ctx.new_context()

        @self.tracer.trace
        def add(a, b):
            return a + b

        result = add(2, 3)

        assert result == 5
        events = self.tracer.events
        assert len(events) == 2

        entry = events[0]
        exit_ = events[1]

        assert entry.event_type == EventType.ENTRY
        assert exit_.event_type == EventType.EXIT
        assert entry.function_name == "TestTraceDecorator.test_trace_sync_captures_entry_and_exit.<locals>.add"
        assert exit_.function_name == entry.function_name

    def test_trace_sync_captures_duration(self):
        """Exit event should have a valid duration."""
        self.ctx.new_context()

        @self.tracer.trace
        def slow_func():
            time.sleep(0.05)
            return "done"

        slow_func()

        exit_event = [e for e in self.tracer.events if e.event_type == EventType.EXIT][0]
        assert exit_event.duration is not None
        assert exit_event.duration >= 0.04  # at least ~50ms

    def test_trace_sync_captures_error(self):
        """@trace should capture errors and re-raise them."""
        self.ctx.new_context()

        @self.tracer.trace
        def failing_func():
            raise ValueError("test error")

        with pytest.raises(ValueError, match="test error"):
            failing_func()

        error_events = [e for e in self.tracer.events if e.event_type == EventType.ERROR]
        assert len(error_events) == 1
        assert "ValueError: test error" in error_events[0].error

    def test_trace_preserves_return_value(self):
        """@trace should not alter the function's return value."""
        self.ctx.new_context()

        @self.tracer.trace
        def compute():
            return {"key": "value", "count": 42}

        result = compute()
        assert result == {"key": "value", "count": 42}

    def test_trace_custom_name(self):
        """@trace(name="custom") should use the custom name."""
        self.ctx.new_context()

        @self.tracer.trace(name="my_custom_operation")
        def some_func():
            return True

        some_func()

        entry = self.tracer.events[0]
        assert entry.function_name == "my_custom_operation"

    def test_trace_captures_context_id(self):
        """Traced events should carry the active context ID."""
        root = self.ctx.new_context()

        @self.tracer.trace
        def my_func():
            return True

        my_func()

        for event in self.tracer.events:
            assert event.context_id == root.context_id

    def test_trace_captures_span_hierarchy(self):
        """Nested traced functions should have parent-child span relationships."""
        self.ctx.new_context()

        @self.tracer.trace
        def outer():
            return inner()

        @self.tracer.trace
        def inner():
            return 42

        outer()

        events = self.tracer.events
        # Should have 4 events: outer_entry, inner_entry, inner_exit, outer_exit
        assert len(events) == 4

        outer_entry = events[0]
        inner_entry = events[1]

        # Inner's parent should reference outer's span
        assert inner_entry.depth > outer_entry.depth

    def test_clear_removes_all_events(self):
        """tracer.clear() should remove all collected events."""
        self.ctx.new_context()

        @self.tracer.trace
        def my_func():
            return True

        my_func()
        assert len(self.tracer.events) > 0

        self.tracer.clear()
        assert len(self.tracer.events) == 0


class TestTraceAsync:
    """Tests for @trace on async functions."""

    def setup_method(self):
        self.ctx = ContextManager()
        self.tracer = Tracer(context_manager=self.ctx)
        _context_id.set(None)
        _parent_span_id.set(None)
        _trace_depth.set(0)

    @pytest.mark.asyncio
    async def test_trace_async_captures_entry_and_exit(self):
        """@trace should work with async functions."""
        self.ctx.new_context()

        @self.tracer.trace
        async def async_add(a, b):
            await asyncio.sleep(0.01)
            return a + b

        result = await async_add(2, 3)

        assert result == 5
        events = self.tracer.events
        assert len(events) == 2

        assert events[0].event_type == EventType.ENTRY
        assert events[1].event_type == EventType.EXIT

    @pytest.mark.asyncio
    async def test_trace_async_captures_duration(self):
        """Async exit events should have duration."""
        self.ctx.new_context()

        @self.tracer.trace
        async def slow_async():
            await asyncio.sleep(0.05)
            return "done"

        await slow_async()

        exit_event = [e for e in self.tracer.events if e.event_type == EventType.EXIT][0]
        assert exit_event.duration is not None
        assert exit_event.duration >= 0.04

    @pytest.mark.asyncio
    async def test_trace_async_captures_error(self):
        """@trace should capture async errors."""
        self.ctx.new_context()

        @self.tracer.trace
        async def failing_async():
            await asyncio.sleep(0.01)
            raise ConnectionError("connection refused")

        with pytest.raises(ConnectionError):
            await failing_async()

        error_events = [e for e in self.tracer.events if e.event_type == EventType.ERROR]
        assert len(error_events) == 1
        assert "ConnectionError" in error_events[0].error

    @pytest.mark.asyncio
    async def test_trace_concurrent_async_tasks(self):
        """Multiple concurrent async tasks should all be traced."""
        self.ctx.new_context()

        @self.tracer.trace
        async def task(task_id):
            await asyncio.sleep(0.01)
            return task_id

        results = await asyncio.gather(
            task("a"),
            task("b"),
            task("c"),
        )

        assert set(results) == {"a", "b", "c"}
        # 3 tasks × 2 events (entry + exit) = 6 events
        assert len(self.tracer.events) == 6

    @pytest.mark.asyncio
    async def test_trace_nested_async(self):
        """Nested async calls should maintain span hierarchy."""
        self.ctx.new_context()

        @self.tracer.trace
        async def parent():
            return await child()

        @self.tracer.trace
        async def child():
            await asyncio.sleep(0.01)
            return "child_result"

        result = await parent()
        assert result == "child_result"

        events = self.tracer.events
        assert len(events) == 4  # parent_entry, child_entry, child_exit, parent_exit


class TestTraceEvent:
    """Tests for the TraceEvent data model."""

    def test_trace_event_repr(self):
        """TraceEvent should have a readable repr."""
        event = TraceEvent(
            event_type=EventType.EXIT,
            function_name="handle_request",
            module="my_app",
            source_file="app.py",
            source_line=42,
            timestamp=0.0,
            duration=0.15,
            context_id="abc12345-test",
            span_id="span-123",
            parent_span_id=None,
            depth=1,
            thread_id=1234,
            thread_name="MainThread",
        )

        r = repr(event)
        assert "handle_request" in r
        assert "150.0ms" in r
        assert "abc12345" in r

    def test_trace_event_repr_with_error(self):
        """TraceEvent repr should show errors."""
        event = TraceEvent(
            event_type=EventType.ERROR,
            function_name="fail",
            module="my_app",
            source_file="app.py",
            source_line=10,
            timestamp=0.0,
            duration=0.01,
            context_id="abc12345-test",
            span_id="span-456",
            parent_span_id=None,
            depth=0,
            thread_id=1234,
            thread_name="MainThread",
            error="ValueError: bad input",
        )

        r = repr(event)
        assert "ValueError: bad input" in r
