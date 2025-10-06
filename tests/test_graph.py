"""
test_graph.py — Tests for execution graph reconstruction

Covers:
- DAG construction from trace events
- Root and leaf node detection
- Context gap detection (the key feature)
- Cross-thread edge detection
- Error node detection
- Causal chain tracing
- Branching and convergence point detection
- JSON and DOT export
- Tree printing
"""

import time
import threading
from typing import List

import pytest

from async_content_tracer.tracer import EventType, TraceEvent
from async_content_tracer.graph import ExecutionGraph, SpanNode


def _make_event(
    event_type: EventType,
    function_name: str,
    span_id: str,
    context_id: str = "ctx-001",
    parent_span_id: str = None,
    depth: int = 0,
    thread_name: str = "MainThread",
    thread_id: int = 1,
    duration: float = None,
    error: str = None,
    timestamp: float = None,
) -> TraceEvent:
    """Helper to create test TraceEvents."""
    return TraceEvent(
        event_type=event_type,
        function_name=function_name,
        module="test",
        source_file="test.py",
        source_line=1,
        timestamp=timestamp or time.monotonic(),
        duration=duration,
        context_id=context_id,
        span_id=span_id,
        parent_span_id=parent_span_id,
        depth=depth,
        thread_id=thread_id,
        thread_name=thread_name,
        error=error,
    )


def _build_linear_chain() -> List[TraceEvent]:
    """Create events for: A -> B -> C (linear call chain)."""
    t = time.monotonic()
    return [
        _make_event(EventType.ENTRY, "A", "span-a", depth=0, timestamp=t),
        _make_event(EventType.ENTRY, "B", "span-b", parent_span_id="span-a", depth=1, timestamp=t + 0.01),
        _make_event(EventType.ENTRY, "C", "span-c", parent_span_id="span-b", depth=2, timestamp=t + 0.02),
        _make_event(EventType.EXIT, "C", "span-c", parent_span_id="span-b", depth=2, duration=0.01, timestamp=t + 0.03),
        _make_event(EventType.EXIT, "B", "span-b", parent_span_id="span-a", depth=1, duration=0.03, timestamp=t + 0.04),
        _make_event(EventType.EXIT, "A", "span-a", depth=0, duration=0.05, timestamp=t + 0.05),
    ]


def _build_branching() -> List[TraceEvent]:
    """Create events for: A -> [B, C] (A fans out to B and C)."""
    t = time.monotonic()
    return [
        _make_event(EventType.ENTRY, "A", "span-a", depth=0, timestamp=t),
        _make_event(EventType.ENTRY, "B", "span-b", parent_span_id="span-a", depth=1, timestamp=t + 0.01),
        _make_event(EventType.EXIT, "B", "span-b", parent_span_id="span-a", depth=1, duration=0.02, timestamp=t + 0.03),
        _make_event(EventType.ENTRY, "C", "span-c", parent_span_id="span-a", depth=1, timestamp=t + 0.01),
        _make_event(EventType.EXIT, "C", "span-c", parent_span_id="span-a", depth=1, duration=0.02, timestamp=t + 0.03),
        _make_event(EventType.EXIT, "A", "span-a", depth=0, duration=0.04, timestamp=t + 0.04),
    ]


def _build_cross_thread() -> List[TraceEvent]:
    """Create events for: A (MainThread) -> B (Worker-1) — cross-thread."""
    t = time.monotonic()
    return [
        _make_event(EventType.ENTRY, "A", "span-a", depth=0, thread_name="MainThread", thread_id=1, timestamp=t),
        _make_event(EventType.ENTRY, "B", "span-b", parent_span_id="span-a", depth=1, thread_name="Worker-1", thread_id=2, timestamp=t + 0.01),
        _make_event(EventType.EXIT, "B", "span-b", parent_span_id="span-a", depth=1, thread_name="Worker-1", thread_id=2, duration=0.02, timestamp=t + 0.03),
        _make_event(EventType.EXIT, "A", "span-a", depth=0, thread_name="MainThread", thread_id=1, duration=0.04, timestamp=t + 0.04),
    ]


class TestGraphConstruction:
    """Tests for building the execution DAG from events."""

    def test_builds_from_linear_chain(self):
        """Linear chain A -> B -> C should produce 3 nodes and 2 edges."""
        graph = ExecutionGraph()
        graph.build_from_events(_build_linear_chain())

        assert len(graph.nodes) == 3
        assert graph.graph.number_of_edges() == 2

    def test_builds_from_branching(self):
        """Branching A -> [B, C] should produce 3 nodes and 2 edges."""
        graph = ExecutionGraph()
        graph.build_from_events(_build_branching())

        assert len(graph.nodes) == 3
        assert graph.graph.number_of_edges() == 2

    def test_graph_is_dag(self):
        """The reconstructed graph should be a valid DAG."""
        graph = ExecutionGraph()
        graph.build_from_events(_build_linear_chain())

        import networkx as nx
        assert nx.is_directed_acyclic_graph(graph.graph)

    def test_node_has_duration(self):
        """Nodes should have duration from matched entry/exit events."""
        graph = ExecutionGraph()
        graph.build_from_events(_build_linear_chain())

        node_c = graph.nodes["span-c"]
        assert node_c.duration is not None
        assert node_c.duration == pytest.approx(0.01, abs=0.001)


class TestGraphAnalysis:
    """Tests for graph analysis methods."""

    def test_find_roots(self):
        """Roots are nodes with no incoming edges."""
        graph = ExecutionGraph()
        graph.build_from_events(_build_linear_chain())

        roots = graph.find_roots()
        assert len(roots) == 1
        assert roots[0].function_name == "A"

    def test_find_leaves(self):
        """Leaves are nodes with no outgoing edges."""
        graph = ExecutionGraph()
        graph.build_from_events(_build_linear_chain())

        leaves = graph.find_leaves()
        assert len(leaves) == 1
        assert leaves[0].function_name == "C"

    def test_find_branching_points(self):
        """Branching points have out_degree > 1."""
        graph = ExecutionGraph()
        graph.build_from_events(_build_branching())

        branches = graph.get_branching_points()
        assert len(branches) == 1
        assert branches[0].function_name == "A"

    def test_find_cross_thread_edges(self):
        """Should detect edges that cross thread boundaries."""
        graph = ExecutionGraph()
        graph.build_from_events(_build_cross_thread())

        cross = graph.find_cross_thread_edges()
        assert len(cross) == 1
        parent, child = cross[0]
        assert parent.thread_name == "MainThread"
        assert child.thread_name == "Worker-1"

    def test_find_errors(self):
        """Should detect spans with errors."""
        events = [
            _make_event(EventType.ENTRY, "A", "span-a"),
            _make_event(EventType.ERROR, "A", "span-a", error="ValueError: oops", duration=0.01),
        ]

        graph = ExecutionGraph()
        graph.build_from_events(events)

        errors = graph.find_errors()
        assert len(errors) == 1
        assert "ValueError" in errors[0].error

    def test_find_context_gaps(self):
        """Should detect spans where context_id is None."""
        t = time.monotonic()
        events = [
            _make_event(EventType.ENTRY, "A", "span-a", context_id="ctx-001", timestamp=t),
            _make_event(EventType.ENTRY, "B", "span-b", context_id=None, parent_span_id="span-a", depth=1, timestamp=t + 0.01),
            _make_event(EventType.EXIT, "B", "span-b", context_id=None, parent_span_id="span-a", depth=1, duration=0.01, timestamp=t + 0.02),
            _make_event(EventType.EXIT, "A", "span-a", context_id="ctx-001", duration=0.03, timestamp=t + 0.03),
        ]

        graph = ExecutionGraph()
        graph.build_from_events(events)

        gaps = graph.find_context_gaps()
        assert len(gaps) == 1
        assert gaps[0].function_name == "B"
        assert gaps[0].has_context is False

    def test_get_causal_chain(self):
        """Causal chain should trace from root to target span."""
        graph = ExecutionGraph()
        graph.build_from_events(_build_linear_chain())

        chain = graph.get_causal_chain("span-c")
        names = [n.function_name for n in chain]
        assert names == ["A", "B", "C"]

    def test_get_causal_chain_nonexistent_span(self):
        """Should return empty list for nonexistent span."""
        graph = ExecutionGraph()
        graph.build_from_events(_build_linear_chain())

        chain = graph.get_causal_chain("nonexistent")
        assert chain == []


class TestGraphExport:
    """Tests for export functionality."""

    def test_to_json_returns_valid_json(self):
        """to_json should return parseable JSON."""
        import json

        graph = ExecutionGraph()
        graph.build_from_events(_build_linear_chain())

        json_str = graph.to_json()
        data = json.loads(json_str)

        assert "nodes" in data
        assert "edges" in data
        assert "summary" in data
        assert len(data["nodes"]) == 3
        assert len(data["edges"]) == 2

    def test_to_json_writes_file(self, tmp_path):
        """to_json should write to file when path is given."""
        graph = ExecutionGraph()
        graph.build_from_events(_build_linear_chain())

        filepath = str(tmp_path / "test_graph.json")
        graph.to_json(filepath)

        import json
        with open(filepath) as f:
            data = json.loads(f.read())
        assert len(data["nodes"]) == 3

    def test_to_dot_returns_valid_dot(self):
        """to_dot should return valid DOT format."""
        graph = ExecutionGraph()
        graph.build_from_events(_build_linear_chain())

        dot = graph.to_dot()
        assert dot.startswith("digraph ExecutionGraph")
        assert "}" in dot

    def test_summary_includes_key_info(self):
        """summary() should include key metrics."""
        graph = ExecutionGraph()
        graph.build_from_events(_build_cross_thread())

        summary = graph.summary()
        assert "Total spans" in summary
        assert "Cross-thread" in summary
        assert "DAG" in summary

    def test_print_tree_returns_string(self):
        """print_tree() should return a non-empty string."""
        graph = ExecutionGraph()
        graph.build_from_events(_build_linear_chain())

        tree = graph.print_tree()
        assert "A" in tree
        assert "B" in tree
        assert "C" in tree


class TestContextSubgraph:
    """Tests for extracting per-context subgraphs."""

    def test_get_context_subgraph(self):
        """Should extract only nodes belonging to a specific context."""
        t = time.monotonic()
        events = [
            # Context 1
            _make_event(EventType.ENTRY, "A", "span-a", context_id="ctx-1", timestamp=t),
            _make_event(EventType.EXIT, "A", "span-a", context_id="ctx-1", duration=0.01, timestamp=t + 0.01),
            # Context 2
            _make_event(EventType.ENTRY, "B", "span-b", context_id="ctx-2", timestamp=t + 0.02),
            _make_event(EventType.EXIT, "B", "span-b", context_id="ctx-2", duration=0.01, timestamp=t + 0.03),
        ]

        graph = ExecutionGraph()
        graph.build_from_events(events)

        sub = graph.get_context_subgraph("ctx-1")
        assert len(sub.nodes) == 1
        assert "span-a" in sub.nodes
