"""
rca_engine.py — Root Cause Analysis Orchestration Engine

The top-level entry point that ties everything together:
    async_content_tracer (telemetry) → inferra (AI reasoning) → RCA

Pipeline:
    1. Accept trace events from async_content_tracer
    2. Build execution graph (DAG)
    3. Index the associated codebase
    4. Run RAG pipeline to retrieve relevant code context
    5. Dispatch multi-agent investigation (Coordinator → Specialists)
    6. Produce a structured RCA report

This is the "autonomous debugging workspace" described in the CV.
"""

from typing import List, Optional

from async_content_tracer.tracer import Tracer, TraceEvent
from async_content_tracer.graph import ExecutionGraph

from inferra.indexer import CodeIndexer
from inferra.rag import RAGPipeline
from inferra.agents import (
    CoordinatorAgent,
    LogAnalysisAgent,
    MetricsCorrelationAgent,
    RCAReport,
)


class RCAEngine:
    """
    Autonomous Root Cause Analysis engine.

    Integrates:
    - async_content_tracer for telemetry collection
    - CodeIndexer for codebase understanding
    - RAGPipeline for telemetry-to-code mapping
    - Multi-agent system for automated investigation

    Usage:
        from async_content_tracer import Tracer, ContextManager
        from inferra import RCAEngine

        # Setup
        tracer = Tracer()
        engine = RCAEngine()
        engine.index_codebase("/path/to/project")

        # ... run instrumented code with @tracer.trace ...

        # Investigate
        report = engine.investigate(tracer.events)
        print(report.to_string())
    """

    def __init__(
        self,
        slow_threshold_ms: float = 100.0,
    ):
        self._indexer = CodeIndexer()
        self._rag: Optional[RAGPipeline] = None
        self._slow_threshold_ms = slow_threshold_ms
        self._coordinator = CoordinatorAgent(indexer=self._indexer)
        self._codebase_indexed = False

    def index_codebase(
        self,
        directory: str,
        exclude_patterns: Optional[List[str]] = None,
    ) -> "RCAEngine":
        """
        Index a codebase for RAG-based code retrieval.

        This enables the engine to map telemetry signals back to source code.

        Args:
            directory: Path to the codebase root
            exclude_patterns: Glob patterns to exclude
        """
        self._indexer.index_directory(directory, exclude_patterns)
        self._rag = RAGPipeline(self._indexer)
        self._coordinator = CoordinatorAgent(
            rag_pipeline=self._rag,
        )
        self._codebase_indexed = True
        return self

    def investigate(
        self,
        events: List[TraceEvent],
        code_context: str = "",
    ) -> RCAReport:
        """
        Run a full autonomous investigation on trace events.

        Pipeline:
        1. Build execution graph from events
        2. Retrieve code context (if codebase indexed)
        3. Run multi-agent investigation
        4. Return structured RCA report

        Args:
            events: List of TraceEvents from async_content_tracer
            code_context: Full source code of correlated functions (from indexer)

        Returns:
            RCAReport with root cause, findings, and recommendations
        """
        # Step 1: Build execution graph
        graph = ExecutionGraph()
        graph.build_from_events(events)

        # Step 2: Run investigation with code context
        report = self._coordinator.investigate(graph, code_context=code_context)

        return report

    def investigate_from_tracer(self, tracer: Tracer) -> RCAReport:
        """
        Convenience method — investigate directly from a Tracer instance.
        """
        return self.investigate(tracer.events)

    def quick_diagnosis(self, events: List[TraceEvent]) -> str:
        """
        Quick one-line diagnosis — useful for Slack notifications.

        Returns a concise human-readable summary.
        """
        report = self.investigate(events)

        if report.confidence > 0.8:
            return (
                f"🔴 {report.severity.value.upper()}: {report.root_cause} "
                f"(confidence: {report.confidence:.0%})"
            )
        else:
            return (
                f"🟡 Possible: {report.root_cause} "
                f"(confidence: {report.confidence:.0%} — needs review)"
            )

    @property
    def indexer(self) -> CodeIndexer:
        return self._indexer

    @property
    def rag(self) -> Optional[RAGPipeline]:
        return self._rag

    def stats(self) -> dict:
        """Get engine statistics."""
        stats = {
            "codebase_indexed": self._codebase_indexed,
        }
        if self._codebase_indexed:
            stats.update(self._indexer.stats())
        return stats
