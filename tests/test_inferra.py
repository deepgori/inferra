"""
test_inferra.py — Dedicated test suite for the inferra module

Covers:
1. CodeIndexer — AST parsing, indexing, search, log-pattern matching
2. Embeddings — LocalEmbedding, VectorStore, backend auto-detection
3. RAGPipeline — telemetry-to-code retrieval, context window building
4. Multi-Agent System — LogAnalysis, MetricsCorrelation, Coordinator
5. RCAEngine — full pipeline integration
"""

import os
import time
import tempfile
import textwrap

import pytest
import numpy as np

from async_content_tracer.context import ContextManager, _context_id, _parent_span_id, _trace_depth
from async_content_tracer.tracer import Tracer, TraceEvent, EventType
from async_content_tracer.graph import ExecutionGraph, SpanNode

from inferra.indexer import CodeIndexer, CodeUnit, SearchResult
from inferra.embeddings import LocalEmbedding, VectorStore, get_best_backend
from inferra.rag import RAGPipeline, RetrievedContext
from inferra.agents import (
    LogAnalysisAgent,
    MetricsCorrelationAgent,
    CoordinatorAgent,
    Finding,
    FindingType,
    Severity,
    RCAReport,
)
from inferra.rca_engine import RCAEngine


# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture
def sample_source():
    """Create a temporary Python file for indexing."""
    code = textwrap.dedent('''\
        import logging

        logger = logging.getLogger(__name__)

        class DatabaseClient:
            """Handles database connections and queries."""

            def connect(self, host: str, port: int) -> bool:
                """Connect to the database server."""
                logger.info("Connecting to database at {host}:{port}")
                return True

            def execute_query(self, query: str) -> list:
                """Execute a SQL query."""
                logger.debug("Executing query: {query}")
                if not query:
                    raise ValueError("Empty query")
                return [{"id": 1}]

            def close(self):
                """Close the database connection."""
                logger.info("Connection closed")

        async def process_request(request_id: str) -> dict:
            """Process an incoming API request."""
            logger.info("Processing request {request_id}")
            db = DatabaseClient()
            db.connect("localhost", 5432)
            result = db.execute_query("SELECT * FROM users")
            db.close()
            return {"status": "ok", "data": result}

        def handle_timeout(service_name: str, timeout_ms: int):
            """Handle a service timeout."""
            logger.error("Service {service_name} timed out after {timeout_ms}ms")
            raise TimeoutError(f"{service_name} timeout after {timeout_ms}ms")
    ''')

    with tempfile.NamedTemporaryFile(mode='w', suffix='.py', delete=False, dir='/tmp') as f:
        f.write(code)
        f.flush()
        yield f.name

    os.unlink(f.name)


@pytest.fixture
def indexed_codebase(sample_source):
    """An indexer with the sample source indexed."""
    indexer = CodeIndexer(embedding_backend=None)
    indexer._embedding_backend = None  # force no embeddings for basic tests
    indexer.index_file(sample_source, root='/tmp')
    return indexer


@pytest.fixture
def sample_graph():
    """An ExecutionGraph with a realistic error scenario."""
    t = time.monotonic()
    events = [
        TraceEvent(
            event_type=EventType.ENTRY, function_name="handle_request",
            module="api", source_file="api.py", source_line=10,
            timestamp=t, duration=None, context_id="ctx-1",
            span_id="span-root", parent_span_id=None, depth=0,
            thread_id=1, thread_name="MainThread",
        ),
        TraceEvent(
            event_type=EventType.ENTRY, function_name="query_database",
            module="db", source_file="db.py", source_line=20,
            timestamp=t + 0.01, duration=None, context_id="ctx-1",
            span_id="span-db", parent_span_id="span-root", depth=1,
            thread_id=1, thread_name="MainThread",
        ),
        TraceEvent(
            event_type=EventType.ERROR, function_name="query_database",
            module="db", source_file="db.py", source_line=25,
            timestamp=t + 0.15, duration=0.14, context_id="ctx-1",
            span_id="span-db", parent_span_id="span-root", depth=1,
            thread_id=1, thread_name="MainThread",
            error="ConnectionError: database connection refused on port 5432",
        ),
        TraceEvent(
            event_type=EventType.ENTRY, function_name="send_notification",
            module="notify", source_file="notify.py", source_line=5,
            timestamp=t + 0.02, duration=None, context_id="ctx-1",
            span_id="span-notify", parent_span_id="span-root", depth=1,
            thread_id=2, thread_name="ThreadPool-1",
        ),
        TraceEvent(
            event_type=EventType.EXIT, function_name="send_notification",
            module="notify", source_file="notify.py", source_line=5,
            timestamp=t + 0.05, duration=0.03, context_id="ctx-1",
            span_id="span-notify", parent_span_id="span-root", depth=1,
            thread_id=2, thread_name="ThreadPool-1",
        ),
        TraceEvent(
            event_type=EventType.ERROR, function_name="handle_request",
            module="api", source_file="api.py", source_line=15,
            timestamp=t + 0.16, duration=0.16, context_id="ctx-1",
            span_id="span-root", parent_span_id=None, depth=0,
            thread_id=1, thread_name="MainThread",
            error="ConnectionError: database connection refused on port 5432",
        ),
    ]

    graph = ExecutionGraph()
    graph.build_from_events(events)
    return graph


@pytest.fixture
def graph_with_gaps():
    """A graph with context propagation gaps."""
    t = time.monotonic()
    events = [
        TraceEvent(
            event_type=EventType.ENTRY, function_name="root_fn",
            module="app", source_file="app.py", source_line=1,
            timestamp=t, duration=None, context_id="ctx-1",
            span_id="span-a", parent_span_id=None, depth=0,
            thread_id=1, thread_name="MainThread",
        ),
        TraceEvent(
            event_type=EventType.EXIT, function_name="root_fn",
            module="app", source_file="app.py", source_line=1,
            timestamp=t + 0.1, duration=0.1, context_id="ctx-1",
            span_id="span-a", parent_span_id=None, depth=0,
            thread_id=1, thread_name="MainThread",
        ),
        # This span has NO context (context_id=None) — represents context loss
        TraceEvent(
            event_type=EventType.ENTRY, function_name="lost_fn",
            module="app", source_file="app.py", source_line=10,
            timestamp=t + 0.05, duration=None, context_id=None,
            span_id="span-lost", parent_span_id=None, depth=0,
            thread_id=3, thread_name="ThreadPool-2",
        ),
        TraceEvent(
            event_type=EventType.EXIT, function_name="lost_fn",
            module="app", source_file="app.py", source_line=10,
            timestamp=t + 0.08, duration=0.03, context_id=None,
            span_id="span-lost", parent_span_id=None, depth=0,
            thread_id=3, thread_name="ThreadPool-2",
        ),
    ]

    graph = ExecutionGraph()
    graph.build_from_events(events)
    return graph


# ══════════════════════════════════════════════════════════════════════════════
#  1. INDEXER TESTS
# ══════════════════════════════════════════════════════════════════════════════


class TestCodeIndexer:
    """Tests for the CodeIndexer."""

    def test_indexes_functions_and_classes(self, indexed_codebase):
        stats = indexed_codebase.stats()
        assert stats["total_units"] > 0
        assert stats["functions"] > 0
        assert stats["classes"] > 0

    def test_extracts_function_signatures(self, indexed_codebase):
        unit = indexed_codebase.search_by_function_name("connect")
        assert unit is not None
        assert "host: str" in unit.signature
        assert "port: int" in unit.signature

    def test_extracts_docstrings(self, indexed_codebase):
        unit = indexed_codebase.search_by_function_name("execute_query")
        assert unit is not None
        assert unit.docstring is not None
        assert "SQL query" in unit.docstring

    def test_extracts_log_patterns(self, indexed_codebase):
        stats = indexed_codebase.stats()
        assert stats["log_patterns"] > 0

    def test_search_by_keywords(self, indexed_codebase):
        results = indexed_codebase.search("database connection timeout")
        assert len(results) > 0
        # Should find database-related code
        names = [r.code_unit.name for r in results]
        assert any("connect" in n.lower() or "database" in n.lower() or "timeout" in n.lower() for n in names)

    def test_search_by_function_name(self, indexed_codebase):
        unit = indexed_codebase.search_by_function_name("process_request")
        assert unit is not None
        assert unit.unit_type == "async_function"

    def test_search_by_log_pattern(self, indexed_codebase):
        results = indexed_codebase.search_by_log_pattern("Connection closed")
        assert len(results) > 0

    def test_search_returns_empty_for_no_match(self, indexed_codebase):
        results = indexed_codebase.search("quantum entanglement hyperspace")
        assert len(results) == 0

    def test_empty_indexer_returns_empty(self):
        indexer = CodeIndexer(embedding_backend=None)
        indexer._embedding_backend = None
        assert indexer.search("anything") == []
        assert indexer.stats()["total_units"] == 0

    def test_indexes_own_codebase(self):
        indexer = CodeIndexer(embedding_backend=None)
        indexer._embedding_backend = None
        indexer.index_directory(".", exclude_patterns=[
            "__pycache__", ".git", "venv", "interview_prep", ".pytest_cache",
        ])
        indexer._build_tfidf_index()
        stats = indexer.stats()
        assert stats["total_units"] > 50  # our project has 200+ units
        assert stats["files_indexed"] > 5

    def test_extracts_function_calls(self, indexed_codebase):
        unit = indexed_codebase.search_by_function_name("process_request")
        assert unit is not None
        assert "connect" in unit.calls or "execute_query" in unit.calls


# ══════════════════════════════════════════════════════════════════════════════
#  2. EMBEDDING TESTS
# ══════════════════════════════════════════════════════════════════════════════


class TestLocalEmbedding:
    """Tests for the SVD-based local embedding backend."""

    def test_fit_and_encode(self):
        emb = LocalEmbedding(n_components=8)
        documents = [
            ["database", "connection", "query", "sql"],
            ["http", "request", "response", "api"],
            ["thread", "pool", "context", "async"],
            ["error", "timeout", "connection", "retry"],
        ]
        emb.fit(documents)

        vectors = emb.encode(["database connection error", "http api request"])
        assert vectors.shape[0] == 2
        assert vectors.shape[1] <= 8  # SVD reduces to min(n_docs-1, n_components)

    def test_similar_texts_have_higher_similarity(self):
        emb = LocalEmbedding(n_components=8)
        documents = [
            ["database", "sql", "query", "connection", "postgres"],
            ["http", "request", "response", "api", "endpoint"],
            ["database", "connection", "timeout", "error", "retry"],
            ["http", "server", "handler", "route", "middleware"],
        ]
        emb.fit(documents)

        db_vec = emb.encode_query("database connection error")
        http_vec = emb.encode_query("http api server")

        # Compute cosine similarity with db-related texts
        doc_vecs = emb.encode([
            "database sql connection",
            "http api endpoint",
        ])

        sim_db_to_db = np.dot(db_vec, doc_vecs[0])
        sim_db_to_http = np.dot(db_vec, doc_vecs[1])

        # db query should be more similar to db document than http document
        assert sim_db_to_db > sim_db_to_http

    def test_dimension_property(self):
        emb = LocalEmbedding(n_components=32)
        assert emb.dimension == 32

    def test_encode_before_fit_raises(self):
        emb = LocalEmbedding(n_components=8)
        with pytest.raises(RuntimeError, match="fit"):
            emb.encode(["test"])


class TestVectorStore:
    """Tests for the in-memory vector store."""

    def test_add_and_search(self):
        store = VectorStore()
        vectors = np.array([
            [1.0, 0.0, 0.0],
            [0.0, 1.0, 0.0],
            [0.0, 0.0, 1.0],
        ], dtype=np.float32)
        store.add(vectors, [0, 1, 2])

        # Query close to first vector
        query = np.array([0.9, 0.1, 0.0], dtype=np.float32)
        results = store.search(query, top_k=2)

        assert len(results) == 2
        assert results[0][0] == 0  # first vector is closest
        assert results[0][1] > results[1][1]  # higher similarity

    def test_empty_store_returns_empty(self):
        store = VectorStore()
        query = np.array([1.0, 0.0], dtype=np.float32)
        assert store.search(query) == []

    def test_size_property(self):
        store = VectorStore()
        assert store.size == 0
        vectors = np.array([[1.0, 0.0]], dtype=np.float32)
        store.add(vectors, [0])
        assert store.size == 1

    def test_top_k_limit(self):
        store = VectorStore()
        n = 20
        vectors = np.eye(n, dtype=np.float32)
        store.add(vectors, list(range(n)))

        results = store.search(np.ones(n, dtype=np.float32), top_k=5)
        assert len(results) <= 5


class TestEmbeddingIntegration:
    """Integration test: indexer + embeddings working together."""

    def test_indexer_with_local_embedding(self, sample_source):
        backend = LocalEmbedding(n_components=8)
        indexer = CodeIndexer(embedding_backend=backend)
        indexer.index_file(sample_source, root='/tmp')

        assert indexer._embeddings_built is True

        # Semantic search should return results
        results = indexer.search_semantic("database connection", top_k=3)
        assert len(results) > 0

    def test_hybrid_search_returns_results(self, sample_source):
        backend = LocalEmbedding(n_components=8)
        indexer = CodeIndexer(embedding_backend=backend)
        indexer.index_file(sample_source, root='/tmp')

        # Hybrid search (RRF fusion)
        results = indexer.search("timeout error handling", top_k=3)
        assert len(results) > 0

    def test_auto_backend_detection(self):
        backend = get_best_backend()
        # Should return LocalEmbedding since numpy is always available
        assert backend is not None
        assert isinstance(backend, LocalEmbedding)


# ══════════════════════════════════════════════════════════════════════════════
#  3. RAG PIPELINE TESTS
# ══════════════════════════════════════════════════════════════════════════════


class TestRAGPipeline:
    """Tests for the telemetry-to-code RAG pipeline."""

    @pytest.fixture
    def rag(self, indexed_codebase):
        return RAGPipeline(indexed_codebase)

    def test_retrieve_for_query(self, rag):
        ctx = rag.retrieve_for_query("database connection timeout")
        assert isinstance(ctx, RetrievedContext)
        assert len(ctx.code_results) > 0
        assert ctx.query_source == "manual"
        assert len(ctx.context_window) > 0

    def test_retrieve_for_log_pattern(self, rag):
        ctx = rag.retrieve_for_log("Connection closed")
        assert isinstance(ctx, RetrievedContext)
        assert ctx.query_source == "log_pattern"

    def test_retrieve_for_event(self, rag):
        event = TraceEvent(
            event_type=EventType.ERROR,
            function_name="execute_query",
            module="db",
            source_file="db.py",
            source_line=20,
            timestamp=time.monotonic(),
            duration=0.1,
            context_id="ctx-test",
            span_id="span-test",
            parent_span_id=None,
            depth=0,
            thread_id=1,
            thread_name="MainThread",
            error="ValueError: Empty query",
        )

        ctx = rag.retrieve_for_event(event)
        assert isinstance(ctx, RetrievedContext)
        assert ctx.query_source == "trace_event"
        assert "execute_query" in ctx.context_window

    def test_context_window_contains_source_code(self, rag):
        ctx = rag.retrieve_for_query("database")
        # Context window should include Python code blocks
        assert "```python" in ctx.context_window or "def " in ctx.context_window

    def test_retrieve_for_error_node(self, rag, sample_graph):
        errors = sample_graph.find_errors()
        if errors:
            ctx = rag.retrieve_for_error(errors[0], sample_graph)
            assert ctx.query_source == "error"
            assert len(ctx.context_window) > 0


# ══════════════════════════════════════════════════════════════════════════════
#  4. AGENT TESTS
# ══════════════════════════════════════════════════════════════════════════════


class TestLogAnalysisAgent:
    """Tests for the LogAnalysisAgent."""

    def test_detects_errors(self, sample_graph):
        agent = LogAnalysisAgent()
        findings = agent.analyze(sample_graph)
        assert len(findings) > 0

        error_findings = [f for f in findings if f.finding_type in (
            FindingType.ERROR_TRACE, FindingType.CONNECTION_ERROR, FindingType.TIMEOUT
        )]
        assert len(error_findings) > 0

    def test_classifies_connection_errors(self, sample_graph):
        agent = LogAnalysisAgent()
        findings = agent.analyze(sample_graph)
        connection_findings = [f for f in findings if f.finding_type == FindingType.CONNECTION_ERROR]
        assert len(connection_findings) > 0

    def test_detects_cascading_failures(self, sample_graph):
        agent = LogAnalysisAgent()
        findings = agent.analyze(sample_graph)
        cascades = [f for f in findings if f.finding_type == FindingType.CASCADING_FAILURE]
        assert len(cascades) > 0

    def test_detects_context_gaps(self, graph_with_gaps):
        agent = LogAnalysisAgent()
        findings = agent.analyze(graph_with_gaps)
        gap_findings = [f for f in findings if f.finding_type == FindingType.CONTEXT_LOSS]
        assert len(gap_findings) > 0

    def test_finding_has_recommendations(self, sample_graph):
        agent = LogAnalysisAgent()
        findings = agent.analyze(sample_graph)
        for f in findings:
            assert len(f.recommendations) > 0

    def test_finding_has_evidence(self, sample_graph):
        agent = LogAnalysisAgent()
        findings = agent.analyze(sample_graph)
        for f in findings:
            assert len(f.evidence) > 0

    def test_no_errors_means_no_findings(self):
        """Agent on a clean graph with no errors should find nothing."""
        t = time.monotonic()
        events = [
            TraceEvent(
                event_type=EventType.ENTRY, function_name="happy_fn",
                module="app", source_file="app.py", source_line=1,
                timestamp=t, duration=None, context_id="ctx-1",
                span_id="happy-span", parent_span_id=None, depth=0,
                thread_id=1, thread_name="MainThread",
            ),
            TraceEvent(
                event_type=EventType.EXIT, function_name="happy_fn",
                module="app", source_file="app.py", source_line=1,
                timestamp=t + 0.01, duration=0.01, context_id="ctx-1",
                span_id="happy-span", parent_span_id=None, depth=0,
                thread_id=1, thread_name="MainThread",
            ),
        ]
        graph = ExecutionGraph()
        graph.build_from_events(events)

        agent = LogAnalysisAgent()
        findings = agent.analyze(graph)
        assert len(findings) == 0


class TestMetricsCorrelationAgent:
    """Tests for the MetricsCorrelationAgent."""

    def test_detects_slow_spans(self):
        t = time.monotonic()
        events = [
            TraceEvent(
                event_type=EventType.ENTRY, function_name="slow_fn",
                module="app", source_file="app.py", source_line=1,
                timestamp=t, duration=None, context_id="ctx-1",
                span_id="slow-span", parent_span_id=None, depth=0,
                thread_id=1, thread_name="MainThread",
            ),
            TraceEvent(
                event_type=EventType.EXIT, function_name="slow_fn",
                module="app", source_file="app.py", source_line=1,
                timestamp=t + 0.5, duration=0.5, context_id="ctx-1",
                span_id="slow-span", parent_span_id=None, depth=0,
                thread_id=1, thread_name="MainThread",
            ),
        ]
        graph = ExecutionGraph()
        graph.build_from_events(events)

        agent = MetricsCorrelationAgent(slow_threshold_ms=100.0)
        findings = agent.analyze(graph)
        perf_findings = [f for f in findings if f.finding_type == FindingType.PERFORMANCE_ANOMALY]
        assert len(perf_findings) > 0

    def test_detects_cross_thread_transitions(self, sample_graph):
        agent = MetricsCorrelationAgent()
        findings = agent.analyze(sample_graph)
        thread_findings = [f for f in findings if f.finding_type == FindingType.THREAD_CONTENTION]
        assert len(thread_findings) > 0  # sample_graph has cross-thread edges

    def test_no_slow_spans_below_threshold(self):
        t = time.monotonic()
        events = [
            TraceEvent(
                event_type=EventType.ENTRY, function_name="fast_fn",
                module="app", source_file="app.py", source_line=1,
                timestamp=t, duration=None, context_id="ctx-1",
                span_id="fast-span", parent_span_id=None, depth=0,
                thread_id=1, thread_name="MainThread",
            ),
            TraceEvent(
                event_type=EventType.EXIT, function_name="fast_fn",
                module="app", source_file="app.py", source_line=1,
                timestamp=t + 0.001, duration=0.001, context_id="ctx-1",
                span_id="fast-span", parent_span_id=None, depth=0,
                thread_id=1, thread_name="MainThread",
            ),
        ]
        graph = ExecutionGraph()
        graph.build_from_events(events)

        agent = MetricsCorrelationAgent(slow_threshold_ms=100.0)
        findings = agent.analyze(graph)
        slow = [f for f in findings if "slow" in f.summary.lower()]
        assert len(slow) == 0


class TestCoordinatorAgent:
    """Tests for the CoordinatorAgent."""

    def test_produces_rca_report(self, sample_graph):
        coordinator = CoordinatorAgent()
        report = coordinator.investigate(sample_graph)
        assert isinstance(report, RCAReport)
        assert report.root_cause is not None
        assert len(report.findings) > 0

    def test_report_severity_matches_worst_finding(self, sample_graph):
        coordinator = CoordinatorAgent()
        report = coordinator.investigate(sample_graph)
        # Report severity should be at least as bad as the worst finding
        severity_order = {"low": 1, "medium": 2, "high": 3, "critical": 4}
        worst = max((f.severity for f in report.findings),
                    key=lambda s: severity_order[s.value])
        assert severity_order[report.severity.value] >= severity_order[worst.value]

    def test_report_to_string(self, sample_graph):
        coordinator = CoordinatorAgent()
        report = coordinator.investigate(sample_graph)
        text = report.to_string()
        assert "ROOT CAUSE ANALYSIS" in text
        assert "Recommendations" in text

    def test_clean_graph_produces_no_issues_report(self):
        t = time.monotonic()
        events = [
            TraceEvent(
                event_type=EventType.ENTRY, function_name="ok_fn",
                module="app", source_file="app.py", source_line=1,
                timestamp=t, duration=None, context_id="ctx-1",
                span_id="ok", parent_span_id=None, depth=0,
                thread_id=1, thread_name="MainThread",
            ),
            TraceEvent(
                event_type=EventType.EXIT, function_name="ok_fn",
                module="app", source_file="app.py", source_line=1,
                timestamp=t + 0.01, duration=0.01, context_id="ctx-1",
                span_id="ok", parent_span_id=None, depth=0,
                thread_id=1, thread_name="MainThread",
            ),
        ]
        graph = ExecutionGraph()
        graph.build_from_events(events)

        coordinator = CoordinatorAgent()
        report = coordinator.investigate(graph)
        # MetricsCorrelationAgent always reports critical path, so the
        # report may have low-severity informational findings.
        # The key test: no errors, no high-severity findings.
        high_severity = [f for f in report.findings if f.severity.value in ("high", "critical")]
        assert len(high_severity) == 0

    def test_conflict_detection(self, sample_graph):
        coordinator = CoordinatorAgent()
        report = coordinator.investigate(sample_graph)
        # With mixed error + performance findings on same spans,
        # there may be conflicts
        # Just verify it doesn't crash
        assert isinstance(report.conflicting_findings, list)


# ══════════════════════════════════════════════════════════════════════════════
#  5. RCA ENGINE TESTS
# ══════════════════════════════════════════════════════════════════════════════


class TestRCAEngine:
    """Tests for the top-level RCA engine."""

    def test_investigate_produces_report(self):
        ctx = ContextManager()
        tracer = Tracer(context_manager=ctx)
        _context_id.set(None)
        _parent_span_id.set(None)
        _trace_depth.set(0)
        ctx.new_context()

        @tracer.trace
        def failing_service():
            raise TimeoutError("service X timed out after 5000ms")

        try:
            failing_service()
        except TimeoutError:
            pass

        engine = RCAEngine()
        report = engine.investigate(tracer.events)

        assert report is not None
        assert "timeout" in report.root_cause.lower()
        assert report.severity.value in ("high", "critical")

    def test_quick_diagnosis(self):
        ctx = ContextManager()
        tracer = Tracer(context_manager=ctx)
        _context_id.set(None)
        _parent_span_id.set(None)
        _trace_depth.set(0)
        ctx.new_context()

        @tracer.trace
        def crash():
            raise ConnectionError("db refused")

        try:
            crash()
        except ConnectionError:
            pass

        engine = RCAEngine()
        msg = engine.quick_diagnosis(tracer.events)

        assert isinstance(msg, str)
        assert "🔴" in msg or "🟡" in msg

    def test_engine_with_indexed_codebase(self, sample_source):
        engine = RCAEngine()
        engine.index_codebase('/tmp', exclude_patterns=["__pycache__"])

        stats = engine.stats()
        assert stats["codebase_indexed"] is True
        assert stats["total_units"] > 0

    def test_stats_without_indexing(self):
        engine = RCAEngine()
        stats = engine.stats()
        assert stats["codebase_indexed"] is False

    def test_investigate_from_tracer(self):
        ctx = ContextManager()
        tracer = Tracer(context_manager=ctx)
        _context_id.set(None)
        _parent_span_id.set(None)
        _trace_depth.set(0)
        ctx.new_context()

        @tracer.trace
        def normal_fn():
            return 42

        normal_fn()

        engine = RCAEngine()
        report = engine.investigate_from_tracer(tracer)
        assert report is not None
        # Should find no issues
        # MetricsCorrelationAgent always finds a critical path, which
        # is a low-severity informational finding — that's fine.
        high_severity = [f for f in report.findings if f.severity.value in ("high", "critical")]
        assert len(high_severity) == 0
