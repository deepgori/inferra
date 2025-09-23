"""
graph.py — Execution Graph Reconstruction

Takes raw TraceEvents and builds a DAG (Directed Acyclic Graph) that shows
the causal chain of everything that happened in response to a request.

This is the "execution graph reconstruction" from the interview prep:
- Nodes = function executions (spans)
- Edges = causal relationships (parent called child)
- Properties = timing, context ID, thread info

The graph reveals:
- Branching patterns (one function spawns multiple async tasks)
- Convergence patterns (multiple results feed into one handler)
- Cross-thread execution (work moved to thread pool)
- Context propagation gaps (where context was lost)

Built with NetworkX for graph operations and analysis.
"""

import json
from collections import defaultdict
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Set, Tuple

import networkx as nx

from async_content_tracer.tracer import EventType, TraceEvent


@dataclass
class SpanNode:
    """A node in the execution graph — represents one function execution."""

    span_id: str
    function_name: str
    module: str
    source_file: str
    source_line: int
    context_id: Optional[str]
    parent_span_id: Optional[str]
    depth: int
    thread_id: int
    thread_name: str
    start_time: float
    end_time: Optional[float] = None
    duration: Optional[float] = None
    error: Optional[str] = None
    has_context: bool = True  # False = context was lost at this point

    @property
    def label(self) -> str:
        err = " ❌" if self.error else ""
        ctx = "" if self.has_context else " ⚠️ NO_CTX"
        return f"{self.function_name}{err}{ctx}"


class ExecutionGraph:
    """
    Builds and analyzes execution DAGs from raw trace events.

    The graph reveals the causal structure of a request's execution:
    which functions called which, how work branched across async tasks
    and thread pools, and critically — where context was lost.

    Usage:
        from async_content_tracer import Tracer, ExecutionGraph

        tracer = Tracer()
        # ... run instrumented code ...

        graph = ExecutionGraph()
        graph.build_from_events(tracer.events)

        # Analyze
        print(graph.summary())
        roots = graph.find_roots()
        lost = graph.find_context_gaps()

        # Export
        graph.to_json("execution_graph.json")
        graph.to_dot("execution_graph.dot")
    """

    def __init__(self):
        self._graph = nx.DiGraph()
        self._nodes: Dict[str, SpanNode] = {}
        self._context_groups: Dict[str, List[str]] = defaultdict(list)

    @property
    def graph(self) -> nx.DiGraph:
        """The underlying NetworkX DiGraph."""
        return self._graph

    @property
    def nodes(self) -> Dict[str, SpanNode]:
        """All span nodes, keyed by span_id."""
        return self._nodes

    def build_from_events(self, events: List[TraceEvent]) -> "ExecutionGraph":
        """
        Reconstruct the execution graph from raw trace events.

        Algorithm:
        1. Group events by span_id to merge entry/exit into single nodes
        2. Create nodes for each unique span
        3. Add edges from parent_span_id -> span_id (causal link)
        4. Detect context gaps (spans with no context_id)
        """
        # Group events by span_id
        span_events: Dict[str, List[TraceEvent]] = defaultdict(list)
        for event in events:
            span_events[event.span_id].append(event)

        # Build nodes from grouped events
        for span_id, span_evts in span_events.items():
            entry_evt = next(
                (e for e in span_evts if e.event_type == EventType.ENTRY),
                None,
            )
            exit_evt = next(
                (e for e in span_evts if e.event_type == EventType.EXIT),
                None,
            )
            error_evt = next(
                (e for e in span_evts if e.event_type == EventType.ERROR),
                None,
            )

            # Use the first available event for base info
            base = entry_evt or exit_evt or error_evt or span_evts[0]

            node = SpanNode(
                span_id=span_id,
                function_name=base.function_name,
                module=base.module,
                source_file=base.source_file,
                source_line=base.source_line,
                context_id=base.context_id,
                parent_span_id=base.parent_span_id,
                depth=base.depth,
                thread_id=base.thread_id,
                thread_name=base.thread_name,
                start_time=entry_evt.timestamp if entry_evt else base.timestamp,
                end_time=exit_evt.timestamp if exit_evt else None,
                duration=exit_evt.duration if exit_evt else None,
                error=error_evt.error if error_evt else None,
                has_context=base.context_id is not None,
            )

            self._nodes[span_id] = node
            self._graph.add_node(
                span_id,
                label=node.label,
                function_name=node.function_name,
                module=node.module,
                source=f"{node.source_file}:{node.source_line}",
                context_id=node.context_id or "NONE",
                thread=node.thread_name,
                duration_ms=round(node.duration * 1000, 2) if node.duration else None,
                has_error=node.error is not None,
                has_context=node.has_context,
            )

            # Group by context
            if node.context_id:
                self._context_groups[node.context_id].append(span_id)

        # Build edges (parent -> child)
        for span_id, node in self._nodes.items():
            if node.parent_span_id and node.parent_span_id in self._nodes:
                parent = self._nodes[node.parent_span_id]
                # Detect cross-thread edges
                cross_thread = parent.thread_id != node.thread_id
                edge_type = "cross_thread" if cross_thread else "call"

                self._graph.add_edge(
                    node.parent_span_id,
                    span_id,
                    edge_type=edge_type,
                    cross_thread=cross_thread,
                )

        return self

    def find_roots(self) -> List[SpanNode]:
        """Find root spans (no parent) — these are request entry points."""
        return [
            self._nodes[n]
            for n in self._graph.nodes
            if self._graph.in_degree(n) == 0 and n in self._nodes
        ]

    def find_leaves(self) -> List[SpanNode]:
        """Find leaf spans (no children) — these are the terminal operations."""
        return [
            self._nodes[n]
            for n in self._graph.nodes
            if self._graph.out_degree(n) == 0 and n in self._nodes
        ]

    def find_context_gaps(self) -> List[SpanNode]:
        """
        Find spans where context was lost — THE critical diagnostic feature.

        These are the exact points where async context propagation failed:
        - asyncio.create_task() that didn't propagate
        - Thread pool submissions without context wrapping
        - Fire-and-forget calls that lost the trace
        """
        return [
            node for node in self._nodes.values() if not node.has_context
        ]

    def find_cross_thread_edges(self) -> List[Tuple[SpanNode, SpanNode]]:
        """Find edges where execution crossed thread boundaries."""
        results = []
        for u, v, data in self._graph.edges(data=True):
            if data.get("cross_thread"):
                if u in self._nodes and v in self._nodes:
                    results.append((self._nodes[u], self._nodes[v]))
        return results

    def find_errors(self) -> List[SpanNode]:
        """Find all spans that resulted in errors."""
        return [node for node in self._nodes.values() if node.error]

    def get_causal_chain(self, span_id: str) -> List[SpanNode]:
        """
        Trace the causal chain from root to a specific span.
        This answers: "What sequence of calls led to this event?"
        """
        if span_id not in self._graph:
            return []

        # Find all ancestors (path from any root to this node)
        ancestors = nx.ancestors(self._graph, span_id)
        ancestors.add(span_id)

        # Build the subgraph and topological sort for order
        subgraph = self._graph.subgraph(ancestors)
        try:
            ordered = list(nx.topological_sort(subgraph))
            return [self._nodes[n] for n in ordered if n in self._nodes]
        except nx.NetworkXUnfeasible:
            # Cycle detected — shouldn't happen in a DAG, but handle gracefully
            return [self._nodes[n] for n in ancestors if n in self._nodes]

    def get_context_subgraph(self, context_id: str) -> "ExecutionGraph":
        """Extract the subgraph for a single request context."""
        sub = ExecutionGraph()
        span_ids = self._context_groups.get(context_id, [])
        subgraph = self._graph.subgraph(span_ids)

        sub._graph = subgraph.copy()
        sub._nodes = {sid: self._nodes[sid] for sid in span_ids if sid in self._nodes}
        sub._context_groups = {context_id: span_ids}

        return sub

    def get_branching_points(self) -> List[SpanNode]:
        """Find spans that branch into multiple children (fan-out points)."""
        return [
            self._nodes[n]
            for n in self._graph.nodes
            if self._graph.out_degree(n) > 1 and n in self._nodes
        ]

    def get_convergence_points(self) -> List[SpanNode]:
        """Find spans that receive from multiple parents (fan-in points)."""
        return [
            self._nodes[n]
            for n in self._graph.nodes
            if self._graph.in_degree(n) > 1 and n in self._nodes
        ]

    def summary(self) -> str:
        """Human-readable summary of the execution graph."""
        roots = self.find_roots()
        gaps = self.find_context_gaps()
        errors = self.find_errors()
        cross_thread = self.find_cross_thread_edges()
        branches = self.get_branching_points()

        lines = [
            "═" * 60,
            "  EXECUTION GRAPH SUMMARY",
            "═" * 60,
            f"  Total spans:           {len(self._nodes)}",
            f"  Unique contexts:       {len(self._context_groups)}",
            f"  Root spans:            {len(roots)}",
            f"  Branching points:      {len(branches)}",
            f"  Cross-thread edges:    {len(cross_thread)}",
            f"  Context gaps:          {len(gaps)}",
            f"  Errors:                {len(errors)}",
            f"  Is DAG:                {nx.is_directed_acyclic_graph(self._graph)}",
        ]

        if gaps:
            lines.append("")
            lines.append("  ⚠️  CONTEXT GAPS DETECTED:")
            for gap in gaps:
                lines.append(f"    → {gap.function_name} (thread={gap.thread_name})")

        if errors:
            lines.append("")
            lines.append("  ❌ ERRORS:")
            for err in errors:
                lines.append(f"    → {err.function_name}: {err.error}")

        if cross_thread:
            lines.append("")
            lines.append("  🔀 CROSS-THREAD TRANSITIONS:")
            for parent, child in cross_thread:
                lines.append(
                    f"    → {parent.function_name} ({parent.thread_name}) "
                    f"→ {child.function_name} ({child.thread_name})"
                )

        lines.append("═" * 60)
        return "\n".join(lines)

    def to_json(self, filepath: Optional[str] = None) -> str:
        """Export the graph as JSON (nodes + edges)."""
        data = {
            "nodes": [
                {
                    "id": node.span_id,
                    "function": node.function_name,
                    "module": node.module,
                    "source": f"{node.source_file}:{node.source_line}",
                    "context_id": node.context_id,
                    "parent_span_id": node.parent_span_id,
                    "thread": node.thread_name,
                    "duration_ms": (
                        round(node.duration * 1000, 2) if node.duration else None
                    ),
                    "error": node.error,
                    "has_context": node.has_context,
                    "depth": node.depth,
                }
                for node in self._nodes.values()
            ],
            "edges": [
                {
                    "source": u,
                    "target": v,
                    "type": data.get("edge_type", "call"),
                    "cross_thread": data.get("cross_thread", False),
                }
                for u, v, data in self._graph.edges(data=True)
            ],
            "summary": {
                "total_spans": len(self._nodes),
                "total_contexts": len(self._context_groups),
                "context_gaps": len(self.find_context_gaps()),
                "errors": len(self.find_errors()),
                "cross_thread_transitions": len(self.find_cross_thread_edges()),
            },
        }

        json_str = json.dumps(data, indent=2)

        if filepath:
            with open(filepath, "w") as f:
                f.write(json_str)

        return json_str

    def to_dot(self, filepath: Optional[str] = None) -> str:
        """Export as Graphviz DOT format for visualization."""
        lines = ["digraph ExecutionGraph {", '  rankdir=TB;', '  node [shape=box, style="rounded,filled"];']

        for span_id, node in self._nodes.items():
            # Color by status
            if node.error:
                color = "#ff6b6b"  # red for errors
            elif not node.has_context:
                color = "#ffd93d"  # yellow for context gaps
            else:
                color = "#6bcb77"  # green for normal

            dur = f"\\n{node.duration*1000:.1f}ms" if node.duration else ""
            thread = f"\\n[{node.thread_name}]"
            label = f"{node.function_name}{dur}{thread}"

            lines.append(
                f'  "{span_id[:8]}" '
                f'[label="{label}", fillcolor="{color}"];'
            )

        for u, v, data in self._graph.edges(data=True):
            style = "dashed" if data.get("cross_thread") else "solid"
            color = "#e55039" if data.get("cross_thread") else "#333333"
            lines.append(
                f'  "{u[:8]}" -> "{v[:8]}" '
                f'[style={style}, color="{color}"];'
            )

        lines.append("}")
        dot_str = "\n".join(lines)

        if filepath:
            with open(filepath, "w") as f:
                f.write(dot_str)

        return dot_str

    def print_tree(self, max_depth: Optional[int] = None) -> str:
        """Print a tree-like view of the execution graph."""
        roots = self.find_roots()
        lines = []

        def _walk(node: SpanNode, depth: int = 0, prefix: str = ""):
            if max_depth is not None and depth > max_depth:
                return

            # Build display line
            dur = f" ({node.duration*1000:.1f}ms)" if node.duration else ""
            err = f" ❌ {node.error}" if node.error else ""
            ctx_warn = " ⚠️  CONTEXT LOST" if not node.has_context else ""
            thread = f" [{node.thread_name}]" if depth > 0 else f" [{node.thread_name}]"

            connector = "├── " if prefix else ""
            lines.append(
                f"{prefix}{connector}{node.function_name}{dur}{thread}{err}{ctx_warn}"
            )

            # Find children
            children_ids = list(self._graph.successors(node.span_id))
            children = [self._nodes[cid] for cid in children_ids if cid in self._nodes]

            for i, child in enumerate(children):
                is_last = i == len(children) - 1
                child_prefix = prefix + ("    " if is_last or not prefix else "│   ")
                _walk(child, depth + 1, child_prefix)

        for root in roots:
            _walk(root)
            lines.append("")

        result = "\n".join(lines)
        return result
