"""
rag.py — Context-Aware RAG Pipeline

Retrieval-Augmented Generation pipeline that connects telemetry signals
to source code context. Given a trace event, error, or log pattern, the
pipeline:

1. Retrieves the most relevant code units from the indexed codebase
2. Builds a context window with source code + telemetry
3. Formats a structured prompt for LLM reasoning
4. Returns the augmented context for the agents to reason over

This is the "RAG-based observability" described in the CV:
telemetry signal → vector search → relevant source code → AI reasoning.
"""

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from inferra.indexer import CodeIndexer, CodeUnit, SearchResult
from async_content_tracer.tracer import TraceEvent, EventType
from async_content_tracer.graph import ExecutionGraph, SpanNode


@dataclass
class RetrievedContext:
    """A package of retrieved code context for a telemetry signal."""

    query: str
    query_source: str  # "trace_event", "error", "log_pattern", "manual"
    code_results: List[SearchResult]
    related_events: List[TraceEvent]
    causal_chain: List[SpanNode]
    context_window: str  # formatted text for LLM consumption

    def __repr__(self) -> str:
        return (
            f"RetrievedContext(query='{self.query[:50]}..', "
            f"code_results={len(self.code_results)}, "
            f"related_events={len(self.related_events)})"
        )


class RAGPipeline:
    """
    Context-aware RAG pipeline for telemetry-to-code mapping.

    Connects the CodeIndexer (codebase search) with the ExecutionGraph
    (telemetry context) to build rich context windows for AI reasoning.

    Usage:
        indexer = CodeIndexer()
        indexer.index_directory("/path/to/project")

        rag = RAGPipeline(indexer)

        # From a trace event
        context = rag.retrieve_for_event(error_event, graph)

        # From a log pattern
        context = rag.retrieve_for_log("Connection timeout on port 5432")

        # From a natural language query
        context = rag.retrieve_for_query("database connection failures")
    """

    def __init__(
        self,
        indexer: CodeIndexer,
        max_code_results: int = 5,
        max_context_lines: int = 50,
    ):
        self._indexer = indexer
        self._max_code_results = max_code_results
        self._max_context_lines = max_context_lines

    def retrieve_for_event(
        self,
        event: TraceEvent,
        graph: Optional[ExecutionGraph] = None,
    ) -> RetrievedContext:
        """
        Retrieve code context for a specific trace event.

        Combines:
        - Function name search → find the traced function's source
        - Error message search → find related error handling code
        - Causal chain → upstream functions that led to this event
        """
        # Build search query from event
        query_parts = [event.function_name]
        if event.error:
            query_parts.append(event.error)
        if event.module:
            query_parts.append(event.module)
        query = " ".join(query_parts)

        # Search codebase
        code_results = self._indexer.search(query, top_k=self._max_code_results)

        # Also try exact function name match
        exact_match = self._indexer.search_by_function_name(event.function_name)
        if exact_match and not any(r.code_unit.name == exact_match.name for r in code_results):
            code_results.insert(0, SearchResult(
                code_unit=exact_match,
                score=1.0,
                matched_terms=[event.function_name],
            ))

        # Get causal chain from graph
        causal_chain = []
        related_events = []
        if graph:
            causal_chain = graph.get_causal_chain(event.span_id)
            # Get events from related spans
            for node in causal_chain:
                for ev in graph.graph.nodes:
                    if ev in graph.nodes:
                        n = graph.nodes[ev]
                        if n.context_id == event.context_id:
                            related_events.append(ev)

        # Build context window
        context_window = self._build_context_window(
            query=query,
            event=event,
            code_results=code_results,
            causal_chain=causal_chain,
        )

        return RetrievedContext(
            query=query,
            query_source="trace_event",
            code_results=code_results,
            related_events=[],
            causal_chain=causal_chain,
            context_window=context_window,
        )

    def retrieve_for_error(
        self,
        error_node: SpanNode,
        graph: ExecutionGraph,
    ) -> RetrievedContext:
        """
        Retrieve context specifically for an error — includes the full
        causal chain leading to the error.
        """
        query = f"{error_node.function_name} {error_node.error or ''}"

        # Search for error-related code
        code_results = self._indexer.search(query, top_k=self._max_code_results)

        # If it's an error, also search for exception handling patterns
        if error_node.error:
            error_type = error_node.error.split(":")[0].strip()
            error_results = self._indexer.search(
                error_type, top_k=3
            )
            # Merge without duplicates
            seen = {r.code_unit.qualified_name for r in code_results}
            for r in error_results:
                if r.code_unit.qualified_name not in seen:
                    code_results.append(r)
                    seen.add(r.code_unit.qualified_name)

        # Get full causal chain
        causal_chain = graph.get_causal_chain(error_node.span_id)

        context_window = self._build_error_context_window(
            error_node=error_node,
            code_results=code_results,
            causal_chain=causal_chain,
        )

        return RetrievedContext(
            query=query,
            query_source="error",
            code_results=code_results,
            related_events=[],
            causal_chain=causal_chain,
            context_window=context_window,
        )

    def retrieve_for_log(self, log_pattern: str) -> RetrievedContext:
        """
        Retrieve code context for a log pattern — the code-origin mapping
        feature. Given a log line, finds the source code that produced it.
        """
        # Search by log pattern first (most precise)
        code_results = self._indexer.search_by_log_pattern(
            log_pattern, top_k=self._max_code_results
        )

        # Fall back to keyword search
        if not code_results:
            code_results = self._indexer.search(
                log_pattern, top_k=self._max_code_results
            )

        context_window = self._build_log_context_window(
            log_pattern=log_pattern,
            code_results=code_results,
        )

        return RetrievedContext(
            query=log_pattern,
            query_source="log_pattern",
            code_results=code_results,
            related_events=[],
            causal_chain=[],
            context_window=context_window,
        )

    def retrieve_for_query(self, query: str) -> RetrievedContext:
        """Retrieve code context for a free-form natural language query."""
        code_results = self._indexer.search(query, top_k=self._max_code_results)

        context_window = self._build_query_context_window(
            query=query,
            code_results=code_results,
        )

        return RetrievedContext(
            query=query,
            query_source="manual",
            code_results=code_results,
            related_events=[],
            causal_chain=[],
            context_window=context_window,
        )

    def _build_context_window(
        self,
        query: str,
        event: TraceEvent,
        code_results: List[SearchResult],
        causal_chain: List[SpanNode],
    ) -> str:
        """Build a formatted context window for LLM consumption."""
        sections = []

        # Telemetry event
        sections.append("## Telemetry Event")
        sections.append(f"Function: {event.function_name}")
        sections.append(f"Module: {event.module}")
        sections.append(f"Type: {event.event_type.value}")
        sections.append(f"Thread: {event.thread_name}")
        if event.duration:
            sections.append(f"Duration: {event.duration * 1000:.1f}ms")
        if event.error:
            sections.append(f"Error: {event.error}")
        sections.append(f"Context ID: {event.context_id or 'NONE'}")

        # Causal chain
        if causal_chain:
            sections.append("\n## Causal Chain (root → error)")
            for i, node in enumerate(causal_chain):
                prefix = "└→" if i == len(causal_chain) - 1 else "├→"
                err = f" ❌ {node.error}" if node.error else ""
                sections.append(f"  {prefix} {node.function_name}{err}")

        # Retrieved code
        sections.append("\n## Relevant Source Code")
        for i, result in enumerate(code_results[:self._max_code_results]):
            unit = result.code_unit
            sections.append(f"\n### [{i+1}] {unit.qualified_name} (score: {result.score:.3f})")
            sections.append(f"File: {unit.source_file}:{unit.start_line}")
            sections.append(f"Type: {unit.unit_type}")
            if unit.docstring:
                sections.append(f"Docstring: {unit.docstring[:200]}")
            # Truncate body
            body_lines = unit.body_text.split("\n")[:self._max_context_lines]
            sections.append("```python")
            sections.append("\n".join(body_lines))
            sections.append("```")

        return "\n".join(sections)

    def _build_error_context_window(
        self,
        error_node: SpanNode,
        code_results: List[SearchResult],
        causal_chain: List[SpanNode],
    ) -> str:
        """Build context window specifically for error investigation."""
        sections = []

        sections.append("## Error Under Investigation")
        sections.append(f"Function: {error_node.function_name}")
        sections.append(f"Error: {error_node.error}")
        sections.append(f"Thread: {error_node.thread_name}")
        if error_node.duration:
            sections.append(f"Duration before error: {error_node.duration * 1000:.1f}ms")

        if causal_chain:
            sections.append("\n## Causal Chain")
            for i, node in enumerate(causal_chain):
                prefix = "└→" if i == len(causal_chain) - 1 else "├→"
                err = f" ❌ {node.error}" if node.error else ""
                dur = f" ({node.duration * 1000:.1f}ms)" if node.duration else ""
                sections.append(f"  {prefix} {node.function_name}{dur}{err}")

        sections.append("\n## Retrieved Source Code")
        for i, result in enumerate(code_results[:self._max_code_results]):
            unit = result.code_unit
            sections.append(f"\n### [{i+1}] {unit.qualified_name}")
            sections.append(f"File: {unit.source_file}:{unit.start_line}")
            body_lines = unit.body_text.split("\n")[:self._max_context_lines]
            sections.append("```python")
            sections.append("\n".join(body_lines))
            sections.append("```")

        return "\n".join(sections)

    def _build_log_context_window(
        self,
        log_pattern: str,
        code_results: List[SearchResult],
    ) -> str:
        """Build context for log-to-code mapping."""
        sections = [
            "## Log Pattern Investigation",
            f"Pattern: \"{log_pattern}\"",
            "",
            "## Source Code Locations That Produce This Pattern",
        ]

        for i, result in enumerate(code_results):
            unit = result.code_unit
            sections.append(f"\n### [{i+1}] {unit.qualified_name}")
            sections.append(f"File: {unit.source_file}:{unit.start_line}")
            sections.append(f"Matched: {', '.join(result.matched_terms)}")
            body_lines = unit.body_text.split("\n")[:self._max_context_lines]
            sections.append("```python")
            sections.append("\n".join(body_lines))
            sections.append("```")

        return "\n".join(sections)

    def _build_query_context_window(
        self,
        query: str,
        code_results: List[SearchResult],
    ) -> str:
        """Build context for a free-form query."""
        sections = [
            "## Query",
            f"\"{query}\"",
            "",
            "## Relevant Code",
        ]

        for i, result in enumerate(code_results):
            unit = result.code_unit
            sections.append(f"\n### [{i+1}] {unit.qualified_name} (score: {result.score:.3f})")
            sections.append(f"File: {unit.source_file}:{unit.start_line}")
            if unit.docstring:
                sections.append(f"Docstring: {unit.docstring[:200]}")
            body_lines = unit.body_text.split("\n")[:self._max_context_lines]
            sections.append("```python")
            sections.append("\n".join(body_lines))
            sections.append("```")

        return "\n".join(sections)
