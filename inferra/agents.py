"""
agents.py — Multi-Agent Reasoning System

Implements the coordinator pattern for autonomous debugging:

1. LogAnalysisAgent — Specializes in parsing trace events, identifying
   anomalies, and classifying error types
2. MetricsCorrelationAgent — Analyzes timing data, detects performance
   anomalies, and correlates cross-service patterns
3. CoordinatorAgent — Orchestrates specialists, synthesizes their findings,
   resolves conflicts, and produces the final RCA

The multi-agent flow:
    1. Coordinator receives an alert (error in execution graph)
    2. Dispatches to LogAnalysisAgent and MetricsCorrelationAgent in parallel
    3. Each specialist returns a structured Finding
    4. Coordinator synthesizes findings into a unified RCA
    5. If findings conflict, Coordinator requests targeted follow-ups

This architecture is pluggable:
- Default: heuristic-based reasoning (works without API keys)
- Optional: LLM-powered reasoning via OpenAI/Claude for production quality
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple

from async_content_tracer.tracer import TraceEvent, EventType
from async_content_tracer.graph import ExecutionGraph, SpanNode
from inferra.rag import RAGPipeline, RetrievedContext


class Severity(Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class FindingType(Enum):
    ERROR_TRACE = "error_trace"
    PERFORMANCE_ANOMALY = "performance_anomaly"
    CONTEXT_LOSS = "context_loss"
    TIMEOUT = "timeout"
    CONNECTION_ERROR = "connection_error"
    DATA_ERROR = "data_error"
    CASCADING_FAILURE = "cascading_failure"
    THREAD_CONTENTION = "thread_contention"
    UNKNOWN = "unknown"


@dataclass
class Finding:
    """A structured finding from a specialist agent."""

    agent_name: str
    finding_type: FindingType
    severity: Severity
    summary: str
    details: str
    evidence: List[str]  # specific data points supporting the finding
    affected_spans: List[str]  # span IDs involved
    confidence: float  # 0.0 to 1.0
    recommendations: List[str]
    source_locations: List[str]  # file:line references
    metadata: Dict[str, Any] = field(default_factory=dict)

    def __repr__(self) -> str:
        return (
            f"Finding({self.agent_name}: {self.finding_type.value} "
            f"[{self.severity.value}] confidence={self.confidence:.0%} "
            f"— {self.summary})"
        )


@dataclass
class RCAReport:
    """The final Root Cause Analysis report produced by the Coordinator."""

    root_cause: str
    severity: Severity
    confidence: float
    summary: str
    detailed_analysis: str
    causal_chain: List[str]  # human-readable causal chain
    findings: List[Finding]  # all specialist findings
    recommendations: List[str]
    source_locations: List[str]
    conflicting_findings: List[Tuple[Finding, Finding]]
    metadata: Dict[str, Any] = field(default_factory=dict)

    def __repr__(self) -> str:
        return (
            f"RCAReport(root_cause='{self.root_cause[:80]}..', "
            f"severity={self.severity.value}, "
            f"confidence={self.confidence:.0%}, "
            f"findings={len(self.findings)})"
        )

    def to_string(self) -> str:
        """Human-readable RCA report."""
        lines = [
            "═" * 70,
            "  ROOT CAUSE ANALYSIS REPORT",
            "═" * 70,
            f"  Severity:    {self.severity.value.upper()}",
            f"  Confidence:  {self.confidence:.0%}",
            "",
            f"  Root Cause:",
            f"    {self.root_cause}",
            "",
            f"  Summary:",
            f"    {self.summary}",
        ]

        if self.causal_chain:
            lines.append("")
            lines.append("  Causal Chain:")
            for i, step in enumerate(self.causal_chain):
                arrow = "  └→" if i == len(self.causal_chain) - 1 else "  ├→"
                lines.append(f"    {arrow} {step}")

        if self.source_locations:
            lines.append("")
            lines.append("  Source Locations:")
            for loc in self.source_locations:
                lines.append(f"    📍 {loc}")

        lines.append("")
        lines.append("  Agent Findings:")
        for finding in self.findings:
            lines.append(f"    [{finding.agent_name}] {finding.summary}")
            lines.append(f"      Type: {finding.finding_type.value} | Confidence: {finding.confidence:.0%}")
            for ev in finding.evidence[:3]:
                lines.append(f"      • {ev}")

        if self.conflicting_findings:
            lines.append("")
            lines.append("  ⚠️  Conflicting Findings:")
            for f1, f2 in self.conflicting_findings:
                lines.append(f"    {f1.agent_name}: {f1.summary}")
                lines.append(f"    vs {f2.agent_name}: {f2.summary}")

        if self.recommendations:
            lines.append("")
            lines.append("  Recommendations:")
            for i, rec in enumerate(self.recommendations, 1):
                lines.append(f"    {i}. {rec}")

        lines.append("")
        lines.append(f"  Detailed Analysis:")
        for line in self.detailed_analysis.split("\n"):
            lines.append(f"    {line}")

        lines.append("═" * 70)
        return "\n".join(lines)


# ── Specialist Agents ─────────────────────────────────────────────────────────


class BaseAgent(ABC):
    """Base class for specialist agents."""

    def __init__(self, name: str):
        self.name = name

    @abstractmethod
    def analyze(
        self,
        graph: ExecutionGraph,
        context: Optional[RetrievedContext] = None,
    ) -> List[Finding]:
        """Analyze the execution graph and return findings."""
        ...


class LogAnalysisAgent(BaseAgent):
    """
    Specializes in analyzing trace events and log patterns.

    Detects:
    - Error patterns and their classification
    - Context propagation gaps
    - Cascading failures (one error causing others)
    - Anomalous event sequences
    """

    def __init__(self):
        super().__init__("LogAnalysisAgent")

    def analyze(
        self,
        graph: ExecutionGraph,
        context: Optional[RetrievedContext] = None,
    ) -> List[Finding]:
        findings = []

        # ── Detect Errors ──
        errors = graph.find_errors()
        for error_node in errors:
            finding = self._analyze_error(error_node, graph)
            findings.append(finding)

        # ── Detect Context Gaps ──
        gaps = graph.find_context_gaps()
        if gaps:
            findings.append(self._analyze_context_gaps(gaps, graph))

        # ── Detect Cascading Failures ──
        cascade = self._detect_cascading_failures(errors, graph)
        if cascade:
            findings.append(cascade)

        return findings

    def _analyze_error(self, node: SpanNode, graph: ExecutionGraph) -> Finding:
        """Classify and analyze a single error."""
        error_str = node.error or "Unknown error"

        # Classify error type
        finding_type = self._classify_error(error_str)
        severity = self._assess_severity(error_str, node, graph)

        # Build causal chain
        chain = graph.get_causal_chain(node.span_id)
        chain_names = [n.function_name for n in chain]

        # Source location
        source_locs = [f"{node.source_file}:{node.source_line}"]

        evidence = [
            f"Error occurred in {node.function_name}",
            f"Error type: {error_str}",
            f"Thread: {node.thread_name}",
        ]
        if node.duration:
            evidence.append(f"Duration before error: {node.duration * 1000:.1f}ms")
        if len(chain) > 1:
            evidence.append(f"Call chain depth: {len(chain)} functions")

        recommendations = self._generate_error_recommendations(finding_type, error_str)

        return Finding(
            agent_name=self.name,
            finding_type=finding_type,
            severity=severity,
            summary=f"{finding_type.value.replace('_', ' ').title()} in {node.function_name}: {error_str}",
            details=(
                f"An error of type {finding_type.value} occurred in "
                f"{node.function_name}. The causal chain involves "
                f"{' → '.join(chain_names)}."
            ),
            evidence=evidence,
            affected_spans=[node.span_id],
            confidence=0.85,
            recommendations=recommendations,
            source_locations=source_locs,
        )

    def _analyze_context_gaps(
        self, gaps: List[SpanNode], graph: ExecutionGraph
    ) -> Finding:
        """Analyze context propagation gaps."""
        gap_names = [g.function_name for g in gaps]
        gap_threads = list(set(g.thread_name for g in gaps))

        return Finding(
            agent_name=self.name,
            finding_type=FindingType.CONTEXT_LOSS,
            severity=Severity.MEDIUM,
            summary=f"Context propagation lost at {len(gaps)} point(s)",
            details=(
                f"Async context was lost at the following functions: "
                f"{', '.join(gap_names)}. This typically indicates "
                f"a missing context propagation wrapper at an async boundary "
                f"(create_task, thread pool, or fire-and-forget call)."
            ),
            evidence=[
                f"Context lost at: {name} (thread: {gaps[i].thread_name})"
                for i, name in enumerate(gap_names)
            ],
            affected_spans=[g.span_id for g in gaps],
            confidence=0.95,
            recommendations=[
                "Wrap thread pool submissions with context snapshot/restore",
                "Use TracedThreadPoolExecutor instead of standard ThreadPoolExecutor",
                "Ensure asyncio.create_task() callsites propagate context",
            ],
            source_locations=[
                f"{g.source_file}:{g.source_line}" for g in gaps
            ],
        )

    def _detect_cascading_failures(
        self, errors: List[SpanNode], graph: ExecutionGraph
    ) -> Optional[Finding]:
        """Detect if errors are cascading (one causing the others)."""
        if len(errors) < 2:
            return None

        # Check if errors share a causal chain
        error_chains = {}
        for err in errors:
            chain = graph.get_causal_chain(err.span_id)
            error_chains[err.span_id] = set(n.span_id for n in chain)

        # Find common ancestors
        cascades = []
        for i, err1 in enumerate(errors):
            for err2 in errors[i + 1:]:
                chain1 = error_chains[err1.span_id]
                chain2 = error_chains[err2.span_id]
                if err1.span_id in chain2 or err2.span_id in chain1:
                    cascades.append((err1, err2))

        if not cascades:
            return None

        root_error = cascades[0][0]
        return Finding(
            agent_name=self.name,
            finding_type=FindingType.CASCADING_FAILURE,
            severity=Severity.HIGH,
            summary=f"Cascading failure detected: {len(errors)} errors from common root",
            details=(
                f"Multiple errors appear to cascade from a common root in "
                f"{root_error.function_name}. The initial failure propagated "
                f"to {len(errors) - 1} downstream function(s)."
            ),
            evidence=[
                f"Root error: {root_error.function_name}: {root_error.error}",
                f"Cascade depth: {len(cascades)} error pairs",
            ],
            affected_spans=[e.span_id for e in errors],
            confidence=0.80,
            recommendations=[
                "Fix the root cause in the upstream function first",
                "Add error boundaries to prevent cascade propagation",
                "Consider circuit breaker patterns for external service calls",
            ],
            source_locations=[
                f"{root_error.source_file}:{root_error.source_line}"
            ],
        )

    def _classify_error(self, error_str: str) -> FindingType:
        """Classify an error string into a FindingType."""
        error_lower = error_str.lower()

        if "timeout" in error_lower or "timed out" in error_lower:
            return FindingType.TIMEOUT
        if "connection" in error_lower or "refused" in error_lower or "unavailable" in error_lower:
            return FindingType.CONNECTION_ERROR
        if "key" in error_lower or "index" in error_lower or "attribute" in error_lower:
            return FindingType.DATA_ERROR
        if "context" in error_lower or "propagat" in error_lower:
            return FindingType.CONTEXT_LOSS
        return FindingType.ERROR_TRACE

    def _assess_severity(
        self, error_str: str, node: SpanNode, graph: ExecutionGraph
    ) -> Severity:
        """Assess the severity of an error."""
        # Root-level errors are more severe
        if graph.graph.in_degree(node.span_id) == 0:
            return Severity.CRITICAL

        # Connection errors are usually high severity
        if self._classify_error(error_str) == FindingType.CONNECTION_ERROR:
            return Severity.HIGH

        # Timeouts are high
        if self._classify_error(error_str) == FindingType.TIMEOUT:
            return Severity.HIGH

        return Severity.MEDIUM

    def _generate_error_recommendations(
        self, finding_type: FindingType, error_str: str
    ) -> List[str]:
        """Generate actionable recommendations based on error type."""
        recs = {
            FindingType.TIMEOUT: [
                "Check upstream service health and response times",
                "Review timeout configuration — current value may be too aggressive",
                "Add retry logic with exponential backoff",
            ],
            FindingType.CONNECTION_ERROR: [
                "Verify the upstream service is running and reachable",
                "Check network connectivity and DNS resolution",
                "Implement circuit breaker to fail fast on repeated failures",
            ],
            FindingType.DATA_ERROR: [
                "Validate input data before processing",
                "Add defensive checks for missing keys/attributes",
                "Review data contracts between services",
            ],
            FindingType.CONTEXT_LOSS: [
                "Use TracedThreadPoolExecutor for thread pool submissions",
                "Ensure asyncio.create_task() preserves context",
                "Add context validation at critical boundaries",
            ],
        }
        return recs.get(finding_type, [
            "Review the error message and stack trace",
            "Check recent code changes that may have introduced this issue",
        ])


class MetricsCorrelationAgent(BaseAgent):
    """
    Specializes in analyzing timing and performance patterns.

    Detects:
    - Slow spans (outliers in duration)
    - Cross-thread performance issues
    - Bottleneck identification (longest path in DAG)
    - Timing anomaly correlation
    """

    def __init__(self, slow_threshold_ms: float = 100.0):
        super().__init__("MetricsCorrelationAgent")
        self._slow_threshold_ms = slow_threshold_ms

    def analyze(
        self,
        graph: ExecutionGraph,
        context: Optional[RetrievedContext] = None,
    ) -> List[Finding]:
        findings = []

        # ── Detect Slow Spans ──
        slow_spans = self._find_slow_spans(graph)
        if slow_spans:
            findings.append(self._report_slow_spans(slow_spans, graph))

        # ── Detect Cross-Thread Overhead ──
        cross_thread = graph.find_cross_thread_edges()
        if cross_thread:
            findings.append(self._analyze_cross_thread(cross_thread, graph))

        # ── Identify Critical Path ──
        critical_path = self._find_critical_path(graph)
        if critical_path:
            findings.append(self._report_critical_path(critical_path))

        return findings

    def _find_slow_spans(self, graph: ExecutionGraph) -> List[SpanNode]:
        """Find spans that exceed the slow threshold."""
        slow = []
        for node in graph.nodes.values():
            if node.duration and (node.duration * 1000) > self._slow_threshold_ms:
                slow.append(node)
        return sorted(slow, key=lambda n: n.duration or 0, reverse=True)

    def _report_slow_spans(
        self, slow_spans: List[SpanNode], graph: ExecutionGraph
    ) -> Finding:
        """Report on slow span detection."""
        total_slow_time = sum(s.duration for s in slow_spans if s.duration)

        return Finding(
            agent_name=self.name,
            finding_type=FindingType.PERFORMANCE_ANOMALY,
            severity=Severity.MEDIUM,
            summary=f"{len(slow_spans)} slow span(s) detected (>{self._slow_threshold_ms}ms)",
            details=(
                f"The following spans exceeded the {self._slow_threshold_ms}ms threshold: "
                + ", ".join(
                    f"{s.function_name} ({s.duration * 1000:.1f}ms)"
                    for s in slow_spans[:5]
                )
            ),
            evidence=[
                f"{s.function_name}: {s.duration * 1000:.1f}ms (thread: {s.thread_name})"
                for s in slow_spans
            ],
            affected_spans=[s.span_id for s in slow_spans],
            confidence=0.90,
            recommendations=[
                "Profile the slowest functions for optimization opportunities",
                "Check if slow spans are I/O-bound (consider async) or CPU-bound (consider threading)",
                "Add caching for repeated computations",
            ],
            source_locations=[
                f"{s.source_file}:{s.source_line}" for s in slow_spans
            ],
        )

    def _analyze_cross_thread(
        self,
        cross_thread: List[Tuple[SpanNode, SpanNode]],
        graph: ExecutionGraph,
    ) -> Finding:
        """Analyze cross-thread transitions for overhead."""
        transitions = []
        for parent, child in cross_thread:
            transitions.append(
                f"{parent.function_name} ({parent.thread_name}) → "
                f"{child.function_name} ({child.thread_name})"
            )

        return Finding(
            agent_name=self.name,
            finding_type=FindingType.THREAD_CONTENTION,
            severity=Severity.LOW,
            summary=f"{len(cross_thread)} cross-thread transition(s) detected",
            details=(
                "Work was offloaded across thread boundaries. While this is normal "
                "for thread pool usage, excessive cross-thread transitions can indicate "
                "unnecessary context switching overhead."
            ),
            evidence=transitions,
            affected_spans=[
                sid
                for p, c in cross_thread
                for sid in [p.span_id, c.span_id]
            ],
            confidence=0.70,
            recommendations=[
                "Verify thread pool sizing is appropriate for the workload",
                "Consider batching small tasks to reduce context switch overhead",
                "Ensure TracedThreadPoolExecutor is used for context propagation",
            ],
            source_locations=[],
        )

    def _find_critical_path(self, graph: ExecutionGraph) -> List[SpanNode]:
        """Find the longest (critical) path through the execution DAG."""
        import networkx as nx

        if not graph.nodes:
            return []

        try:
            # Weight edges by child duration
            for u, v in graph.graph.edges():
                child = graph.nodes.get(v)
                weight = -(child.duration or 0) if child else 0
                graph.graph[u][v]["weight"] = weight

            # Find longest path (shortest with negative weights)
            longest = nx.dag_longest_path(graph.graph, weight="weight")
            return [graph.nodes[n] for n in longest if n in graph.nodes]
        except (nx.NetworkXError, nx.NetworkXUnfeasible):
            return []

    def _report_critical_path(self, path: List[SpanNode]) -> Finding:
        """Report the critical path through the execution."""
        total_time = sum(n.duration or 0 for n in path)

        return Finding(
            agent_name=self.name,
            finding_type=FindingType.PERFORMANCE_ANOMALY,
            severity=Severity.LOW,
            summary=f"Critical path: {len(path)} spans, {total_time * 1000:.1f}ms total",
            details=(
                "The longest execution path through the DAG is: "
                + " → ".join(n.function_name for n in path)
                + f". Total time: {total_time * 1000:.1f}ms."
            ),
            evidence=[
                f"{n.function_name}: {(n.duration or 0) * 1000:.1f}ms"
                for n in path
            ],
            affected_spans=[n.span_id for n in path],
            confidence=0.95,
            recommendations=[
                "Optimize the slowest function on the critical path for maximum impact",
                "Consider parallelizing independent steps",
            ],
            source_locations=[
                f"{n.source_file}:{n.source_line}" for n in path
            ],
        )


class CoordinatorAgent(BaseAgent):
    """
    The lead investigator agent. Orchestrates specialist agents,
    synthesizes their findings, resolves conflicts, and produces
    the final RCA report.

    Architecture:
    1. Receives alert (error/anomaly in execution graph)
    2. Dispatches to specialists (LogAnalysis, MetricsCorrelation) in parallel
    3. Collects and merges findings
    4. Detects conflicts (specialists disagree on root cause)
    5. Synthesizes into a unified RCA report
    """

    def __init__(
        self,
        specialists: Optional[List[BaseAgent]] = None,
        rag_pipeline: Optional[RAGPipeline] = None,
        indexer=None,
    ):
        super().__init__("CoordinatorAgent")

        # Default specialists: rule-based agents
        default_specialists = [
            LogAnalysisAgent(),
            MetricsCorrelationAgent(),
        ]

        # Optionally add LLM-powered deep reasoning agent
        try:
            from inferra.llm_agent import DeepReasoningAgent
            llm_agent = DeepReasoningAgent(indexer=indexer)
            if llm_agent.available:
                default_specialists.append(llm_agent)
                import logging; logging.getLogger("inferra").info("DeepReasoningAgent loaded (Claude API)")
        except ImportError:
            pass

        self._specialists = specialists or default_specialists
        self._rag = rag_pipeline

    def analyze(
        self,
        graph: ExecutionGraph,
        context: Optional[RetrievedContext] = None,
    ) -> List[Finding]:
        """Run all specialists and return all findings."""
        all_findings = []
        for agent in self._specialists:
            findings = agent.analyze(graph, context)
            all_findings.extend(findings)
        return all_findings

    def investigate(self, graph: ExecutionGraph, code_context: str = "") -> RCAReport:
        """
        Run the full investigation pipeline and produce an RCA report.

        This is the main entry point for autonomous debugging.
        """
        # Step 1: Dispatch to specialists
        all_findings = self.analyze(graph)

        # Step 2: Retrieve code context for top findings (if RAG available)
        if self._rag:
            errors = graph.find_errors()
            for error_node in errors:
                retrieved = self._rag.retrieve_for_error(error_node, graph)
                # Enrich findings with source locations from RAG
                for finding in all_findings:
                    if error_node.span_id in finding.affected_spans:
                        for result in retrieved.code_results[:3]:
                            loc = f"{result.code_unit.source_file}:{result.code_unit.start_line}"
                            if loc not in finding.source_locations:
                                finding.source_locations.append(loc)

        # Step 3: Detect conflicts
        conflicts = self._detect_conflicts(all_findings)

        # Step 4: LLM synthesis (if available)
        llm_synthesis = None
        try:
            from inferra.llm_agent import DeepReasoningAgent
            for agent in self._specialists:
                if isinstance(agent, DeepReasoningAgent) and agent.available:
                    llm_synthesis = agent.reason_over_findings(
                        all_findings, graph, code_context=code_context
                    )
                    if llm_synthesis:
                        print("   🧠 LLM synthesis complete")
                    break
        except ImportError:
            pass

        # Step 5: Synthesize into RCA
        report = self._synthesize_rca(graph, all_findings, conflicts)
        if llm_synthesis:
            report.metadata["llm_synthesis"] = llm_synthesis
        return report

    def _detect_conflicts(
        self, findings: List[Finding]
    ) -> List[Tuple[Finding, Finding]]:
        """Detect when specialist agents disagree."""
        conflicts = []
        for i, f1 in enumerate(findings):
            for f2 in findings[i + 1:]:
                # Different agents, same affected spans, different conclusions
                if (
                    f1.agent_name != f2.agent_name
                    and set(f1.affected_spans) & set(f2.affected_spans)
                    and f1.finding_type != f2.finding_type
                ):
                    conflicts.append((f1, f2))
        return conflicts

    def _synthesize_rca(
        self,
        graph: ExecutionGraph,
        findings: List[Finding],
        conflicts: List[Tuple[Finding, Finding]],
    ) -> RCAReport:
        """Synthesize all findings into a unified RCA report."""
        if not findings:
            return RCAReport(
                root_cause="No issues detected",
                severity=Severity.LOW,
                confidence=1.0,
                summary="The execution completed without detectable issues.",
                detailed_analysis="All spans completed successfully with no errors, context gaps, or performance anomalies.",
                causal_chain=[],
                findings=[],
                recommendations=[],
                source_locations=[],
                conflicting_findings=[],
            )

        # Find the highest severity finding as the primary root cause
        primary = max(
            findings,
            key=lambda f: (
                {"critical": 4, "high": 3, "medium": 2, "low": 1}[f.severity.value],
                f.confidence,
            ),
        )

        # Build causal chain from the primary finding
        causal_chain = []
        for span_id in primary.affected_spans:
            chain = graph.get_causal_chain(span_id)
            for node in chain:
                step = node.function_name
                if node.error:
                    step += f" ❌ {node.error}"
                if step not in causal_chain:
                    causal_chain.append(step)

        # Aggregate recommendations (deduplicated)
        all_recs = []
        seen_recs = set()
        for f in findings:
            for rec in f.recommendations:
                if rec not in seen_recs:
                    all_recs.append(rec)
                    seen_recs.add(rec)

        # Aggregate source locations
        all_locations = list(set(
            loc for f in findings for loc in f.source_locations
        ))

        # Build detailed analysis
        analysis_parts = []
        for f in findings:
            analysis_parts.append(
                f"[{f.agent_name}] {f.finding_type.value}: {f.details}"
            )
        detailed_analysis = "\n\n".join(analysis_parts)

        # Calculate overall confidence
        avg_confidence = sum(f.confidence for f in findings) / len(findings)
        if conflicts:
            avg_confidence *= 0.8  # reduce confidence when agents disagree

        return RCAReport(
            root_cause=primary.summary,
            severity=primary.severity,
            confidence=avg_confidence,
            summary=(
                f"Investigation found {len(findings)} issue(s). "
                f"Primary: {primary.summary}"
            ),
            detailed_analysis=detailed_analysis,
            causal_chain=causal_chain,
            findings=findings,
            recommendations=all_recs,
            source_locations=all_locations,
            conflicting_findings=conflicts,
        )
