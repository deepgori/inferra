"""
tracer.py — Function Boundary Instrumentation

Two approaches to capturing function entry/exit:
1. @trace decorator — explicit, manual instrumentation (recommended)
2. auto_trace() via sys.settrace — automatic, captures everything (higher overhead)

Each traced function emits a TraceEvent with:
- Function name, module, source location
- Entry/exit timestamps (for duration calculation)
- The active SpanContext (context_id, span_id, parent, depth)
- Thread info (to see cross-thread propagation)
"""

import sys
import time
import asyncio
import inspect
import functools
import threading
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Set

from async_content_tracer.context import ContextManager, SpanContext


class EventType(Enum):
    ENTRY = "entry"
    EXIT = "exit"
    ERROR = "error"
    TASK_CREATE = "task_create"
    TASK_COMPLETE = "task_complete"
    THREAD_SUBMIT = "thread_submit"
    THREAD_COMPLETE = "thread_complete"


@dataclass
class TraceEvent:
    """A single instrumentation event — the atomic unit of the trace."""

    event_type: EventType
    function_name: str
    module: str
    source_file: str
    source_line: int
    timestamp: float
    duration: Optional[float]  # None for entry events
    context_id: Optional[str]
    span_id: str
    parent_span_id: Optional[str]
    depth: int
    thread_id: int
    thread_name: str
    return_value: Optional[Any] = None
    error: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    def __repr__(self) -> str:
        ctx_short = self.context_id[:8] + ".." if self.context_id else "NO_CTX"
        dur = f" {self.duration*1000:.1f}ms" if self.duration else ""
        err = f" ❌ {self.error}" if self.error else ""
        indent = "  " * self.depth
        arrow = "→" if self.event_type == EventType.ENTRY else "←"
        return (
            f"{indent}{arrow} [{ctx_short}] "
            f"{self.function_name}{dur}{err} "
            f"(thread={self.thread_name})"
        )


class Tracer:
    """
    Central trace collector. All instrumented functions report events here.

    Usage:
        tracer = Tracer()

        @tracer.trace
        async def handle_request(data):
            result = await process(data)
            return result

        # Or use the global instance:
        from async_content_tracer import trace

        @trace
        async def handle_request(data):
            ...

        # View collected events:
        for event in tracer.events:
            print(event)
    """

    def __init__(self, context_manager: Optional[ContextManager] = None):
        self._context_manager = context_manager or ContextManager()
        self._events: List[TraceEvent] = []
        self._lock = threading.Lock()
        self._active_spans: Dict[str, float] = {}  # span_id -> start_time

    @property
    def events(self) -> List[TraceEvent]:
        """Get all collected trace events, ordered by timestamp."""
        with self._lock:
            return sorted(self._events, key=lambda e: e.timestamp)

    def clear(self):
        """Clear all collected events."""
        with self._lock:
            self._events.clear()
            self._active_spans.clear()

    def _record(self, event: TraceEvent):
        """Thread-safe event recording."""
        with self._lock:
            self._events.append(event)

    def _get_source_info(self, fn: Callable) -> tuple:
        """Extract source file and line from a function."""
        try:
            source_file = inspect.getfile(fn)
            source_lines, start_line = inspect.getsourcelines(fn)
            return source_file, start_line
        except (TypeError, OSError):
            return "<unknown>", 0

    def trace(self, fn: Optional[Callable] = None, *, name: Optional[str] = None):
        """
        Decorator for function boundary instrumentation.

        Works with both sync and async functions. Captures:
        - Entry timestamp + context
        - Exit timestamp + duration
        - Errors (re-raised after recording)
        - Cross-thread/async context propagation

        Usage:
            @tracer.trace
            async def my_function():
                ...

            @tracer.trace(name="custom_name")
            def another_function():
                ...
        """
        # Handle both @trace and @trace(name="...")
        if fn is None:
            return lambda f: self.trace(f, name=name)

        func_name = name or fn.__qualname__
        module = fn.__module__ or "<unknown>"
        source_file, source_line = self._get_source_info(fn)

        if asyncio.iscoroutinefunction(fn):
            return self._trace_async(fn, func_name, module, source_file, source_line)
        else:
            return self._trace_sync(fn, func_name, module, source_file, source_line)

    def _trace_sync(
        self,
        fn: Callable,
        func_name: str,
        module: str,
        source_file: str,
        source_line: int,
    ) -> Callable:
        """Wrap a synchronous function with entry/exit tracing."""
        from async_content_tracer.context import _parent_span_id, _trace_depth

        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            # Save context state BEFORE new_span modifies it
            saved_parent = _parent_span_id.get(None)
            saved_depth = _trace_depth.get(0)

            span = self._context_manager.new_span()
            start_time = time.monotonic()

            # Record ENTRY
            entry_event = TraceEvent(
                event_type=EventType.ENTRY,
                function_name=func_name,
                module=module,
                source_file=source_file,
                source_line=source_line,
                timestamp=start_time,
                duration=None,
                context_id=span.context_id,
                span_id=span.span_id,
                parent_span_id=span.parent_span_id,
                depth=span.depth,
                thread_id=threading.get_ident(),
                thread_name=threading.current_thread().name,
            )
            self._record(entry_event)

            try:
                result = fn(*args, **kwargs)
                end_time = time.monotonic()

                # Record EXIT
                exit_event = TraceEvent(
                    event_type=EventType.EXIT,
                    function_name=func_name,
                    module=module,
                    source_file=source_file,
                    source_line=source_line,
                    timestamp=end_time,
                    duration=end_time - start_time,
                    context_id=span.context_id,
                    span_id=span.span_id,
                    parent_span_id=span.parent_span_id,
                    depth=span.depth,
                    thread_id=threading.get_ident(),
                    thread_name=threading.current_thread().name,
                    return_value=repr(result)[:200],
                )
                self._record(exit_event)
                return result

            except Exception as e:
                end_time = time.monotonic()

                # Record ERROR
                error_event = TraceEvent(
                    event_type=EventType.ERROR,
                    function_name=func_name,
                    module=module,
                    source_file=source_file,
                    source_line=source_line,
                    timestamp=end_time,
                    duration=end_time - start_time,
                    context_id=span.context_id,
                    span_id=span.span_id,
                    parent_span_id=span.parent_span_id,
                    depth=span.depth,
                    thread_id=threading.get_ident(),
                    thread_name=threading.current_thread().name,
                    error=f"{type(e).__name__}: {str(e)}",
                )
                self._record(error_event)
                raise

            finally:
                # CRITICAL: Restore context state so siblings get correct depth
                _parent_span_id.set(saved_parent)
                _trace_depth.set(saved_depth)

        return wrapper

    def _trace_async(
        self,
        fn: Callable,
        func_name: str,
        module: str,
        source_file: str,
        source_line: int,
    ) -> Callable:
        """Wrap an async function with entry/exit tracing."""
        from async_content_tracer.context import _parent_span_id, _trace_depth

        @functools.wraps(fn)
        async def wrapper(*args, **kwargs):
            # Save context state BEFORE new_span modifies it
            saved_parent = _parent_span_id.get(None)
            saved_depth = _trace_depth.get(0)

            span = self._context_manager.new_span()
            start_time = time.monotonic()

            # Record ENTRY
            entry_event = TraceEvent(
                event_type=EventType.ENTRY,
                function_name=func_name,
                module=module,
                source_file=source_file,
                source_line=source_line,
                timestamp=start_time,
                duration=None,
                context_id=span.context_id,
                span_id=span.span_id,
                parent_span_id=span.parent_span_id,
                depth=span.depth,
                thread_id=threading.get_ident(),
                thread_name=threading.current_thread().name,
            )
            self._record(entry_event)

            try:
                result = await fn(*args, **kwargs)
                end_time = time.monotonic()

                # Record EXIT
                exit_event = TraceEvent(
                    event_type=EventType.EXIT,
                    function_name=func_name,
                    module=module,
                    source_file=source_file,
                    source_line=source_line,
                    timestamp=end_time,
                    duration=end_time - start_time,
                    context_id=span.context_id,
                    span_id=span.span_id,
                    parent_span_id=span.parent_span_id,
                    depth=span.depth,
                    thread_id=threading.get_ident(),
                    thread_name=threading.current_thread().name,
                    return_value=repr(result)[:200],
                )
                self._record(exit_event)
                return result

            except Exception as e:
                end_time = time.monotonic()

                # Record ERROR
                error_event = TraceEvent(
                    event_type=EventType.ERROR,
                    function_name=func_name,
                    module=module,
                    source_file=source_file,
                    source_line=source_line,
                    timestamp=end_time,
                    duration=end_time - start_time,
                    context_id=span.context_id,
                    span_id=span.span_id,
                    parent_span_id=span.parent_span_id,
                    depth=span.depth,
                    thread_id=threading.get_ident(),
                    thread_name=threading.current_thread().name,
                    error=f"{type(e).__name__}: {str(e)}",
                )
                self._record(error_event)
                raise

            finally:
                # CRITICAL: Restore context state so siblings get correct depth
                _parent_span_id.set(saved_parent)
                _trace_depth.set(saved_depth)

        return wrapper

    def trace_task_creation(self, task_name: str, span: SpanContext):
        """Record when asyncio.create_task() is called — marks a context fork."""
        self._record(
            TraceEvent(
                event_type=EventType.TASK_CREATE,
                function_name=task_name,
                module="asyncio",
                source_file="<task>",
                source_line=0,
                timestamp=time.monotonic(),
                duration=None,
                context_id=span.context_id,
                span_id=span.span_id,
                parent_span_id=span.parent_span_id,
                depth=span.depth,
                thread_id=threading.get_ident(),
                thread_name=threading.current_thread().name,
                metadata={"event": "context_fork"},
            )
        )

    def trace_thread_submit(self, func_name: str, span: SpanContext):
        """Record when a function is submitted to a thread pool."""
        self._record(
            TraceEvent(
                event_type=EventType.THREAD_SUBMIT,
                function_name=func_name,
                module="threading",
                source_file="<thread_pool>",
                source_line=0,
                timestamp=time.monotonic(),
                duration=None,
                context_id=span.context_id,
                span_id=span.span_id,
                parent_span_id=span.parent_span_id,
                depth=span.depth,
                thread_id=threading.get_ident(),
                thread_name=threading.current_thread().name,
                metadata={"event": "thread_submit"},
            )
        )


class _AutoTracer:
    """
    Automatic function-level tracing via sys.settrace.

    This hooks into Python's trace mechanism to capture ALL function calls
    without manual decoration. Higher overhead, but complete coverage.

    This is the sys.settrace approach mentioned in the interview prep.
    """

    def __init__(self, tracer: Tracer, include_modules: Optional[Set[str]] = None):
        self._tracer = tracer
        self._include_modules = include_modules
        self._active = False
        self._call_stack: Dict[int, List[float]] = {}  # thread_id -> [start_times]

    def start(self):
        """Enable automatic tracing for all function calls."""
        self._active = True
        sys.settrace(self._trace_callback)
        threading.settrace(self._trace_callback)

    def stop(self):
        """Disable automatic tracing."""
        self._active = False
        sys.settrace(None)
        threading.settrace(None)

    def _trace_callback(self, frame, event, arg):
        """
        The sys.settrace callback. Called on every function call/return.

        frame: the current stack frame
        event: 'call', 'return', 'exception', etc.
        arg: depends on event type
        """
        if not self._active:
            return None

        # Filter to relevant modules only
        module = frame.f_globals.get("__name__", "")
        if self._include_modules and module not in self._include_modules:
            return self._trace_callback  # keep tracing but skip this frame

        func_name = frame.f_code.co_name
        source_file = frame.f_code.co_filename
        source_line = frame.f_lineno
        thread_id = threading.get_ident()

        # Skip internal/dunder functions
        if func_name.startswith("_") and not func_name.startswith("__init__"):
            return self._trace_callback

        if event == "call":
            start_time = time.monotonic()
            if thread_id not in self._call_stack:
                self._call_stack[thread_id] = []
            self._call_stack[thread_id].append(start_time)

            span = self._tracer._context_manager.new_span()
            self._tracer._record(
                TraceEvent(
                    event_type=EventType.ENTRY,
                    function_name=func_name,
                    module=module,
                    source_file=source_file,
                    source_line=source_line,
                    timestamp=start_time,
                    duration=None,
                    context_id=span.context_id,
                    span_id=span.span_id,
                    parent_span_id=span.parent_span_id,
                    depth=span.depth,
                    thread_id=thread_id,
                    thread_name=threading.current_thread().name,
                )
            )
            return self._trace_callback

        elif event == "return":
            end_time = time.monotonic()
            start_time = (
                self._call_stack.get(thread_id, [None]).pop()
                if self._call_stack.get(thread_id)
                else None
            )
            duration = (end_time - start_time) if start_time else None

            span = self._tracer._context_manager.current()
            if span:
                self._tracer._record(
                    TraceEvent(
                        event_type=EventType.EXIT,
                        function_name=func_name,
                        module=module,
                        source_file=source_file,
                        source_line=source_line,
                        timestamp=end_time,
                        duration=duration,
                        context_id=span.context_id,
                        span_id=span.span_id,
                        parent_span_id=span.parent_span_id,
                        depth=span.depth,
                        thread_id=thread_id,
                        thread_name=threading.current_thread().name,
                        return_value=repr(arg)[:200] if arg is not None else None,
                    )
                )
            return None

        elif event == "exception":
            end_time = time.monotonic()
            start_time = (
                self._call_stack.get(thread_id, [None]).pop()
                if self._call_stack.get(thread_id)
                else None
            )
            duration = (end_time - start_time) if start_time else None
            exc_type, exc_value, _ = arg

            span = self._tracer._context_manager.current()
            if span:
                self._tracer._record(
                    TraceEvent(
                        event_type=EventType.ERROR,
                        function_name=func_name,
                        module=module,
                        source_file=source_file,
                        source_line=source_line,
                        timestamp=end_time,
                        duration=duration,
                        context_id=span.context_id,
                        span_id=span.span_id,
                        parent_span_id=span.parent_span_id,
                        depth=span.depth,
                        thread_id=thread_id,
                        thread_name=threading.current_thread().name,
                        error=f"{exc_type.__name__}: {exc_value}",
                    )
                )
            return self._trace_callback

        return self._trace_callback


# ── Module-level convenience ──────────────────────────────────────────────────
# Global tracer instance for simple usage

_global_tracer = Tracer()


def trace(fn=None, *, name=None):
    """Module-level @trace decorator using the global tracer."""
    return _global_tracer.trace(fn, name=name)


def auto_trace(include_modules: Optional[Set[str]] = None) -> _AutoTracer:
    """
    Start automatic tracing via sys.settrace.

    Args:
        include_modules: Set of module names to trace. If None, traces everything
                        (high overhead — use for debugging only).

    Returns:
        An _AutoTracer instance. Call .stop() to disable.

    Usage:
        at = auto_trace(include_modules={"my_app", "my_app.handlers"})
        # ... run your code ...
        at.stop()
    """
    at = _AutoTracer(_global_tracer, include_modules)
    at.start()
    return at
