"""
llm_agent.py — LLM-Powered Deep Reasoning Agent

Uses Claude (Anthropic) to perform deep root cause analysis that goes
beyond heuristic pattern matching. The LLM agent:

1. Receives the execution graph summary and trace events
2. Receives the rule-based agents' findings
3. Performs deep causal reasoning to explain WHY failures occurred
4. Generates actionable, context-specific recommendations
5. Identifies subtle issues that heuristics miss (race conditions,
   architectural problems, design smells)

Architecture:
- Plugs into the existing multi-agent coordinator as a specialist
- Falls back gracefully if no API key is available
- Uses structured prompts to minimize hallucination
"""

import os
import json
from typing import Any, Dict, List, Optional

from async_content_tracer.tracer import TraceEvent, EventType
from async_content_tracer.graph import ExecutionGraph, SpanNode
from inferra.rag import RAGPipeline, RetrievedContext
from inferra.agents import (
    BaseAgent,
    Finding,
    FindingType,
    Severity,
)


def _get_api_key():
    """Get Anthropic API key, or None if unavailable."""
    return os.environ.get("ANTHROPIC_API_KEY")


def _call_claude(prompt: str, system: str = "", max_tokens: int = 1500) -> Optional[str]:
    """Call Claude API directly via urllib to avoid SDK version conflicts."""
    api_key = _get_api_key()
    if not api_key:
        return None
    try:
        import urllib.request
        import urllib.error
        import ssl

        data = json.dumps({
            "model": "claude-sonnet-4-20250514",
            "max_tokens": max_tokens,
            "system": system,
            "messages": [{"role": "user", "content": prompt}],
        }).encode("utf-8")

        req = urllib.request.Request(
            "https://api.anthropic.com/v1/messages",
            data=data,
            headers={
                "Content-Type": "application/json",
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
            },
            method="POST",
        )

        # Try with certifi first, then system certs, then unverified
        ctx = None
        try:
            import certifi
            ctx = ssl.create_default_context(cafile=certifi.where())
        except ImportError:
            try:
                ctx = ssl.create_default_context()
            except Exception:
                ctx = ssl._create_unverified_context()

        with urllib.request.urlopen(req, timeout=30, context=ctx) as resp:
            result = json.loads(resp.read().decode("utf-8"))
            return result["content"][0]["text"]

    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        try:
            err = json.loads(body)
            msg = err.get("error", {}).get("message", body[:200])
        except Exception:
            msg = body[:200]
        print(f"   ⚠️  Claude API error (HTTP {e.code}): {msg}")
        return None
    except Exception as e:
        return None


SYSTEM_PROMPT = """You are an expert production systems debugger performing root cause analysis.

You receive two kinds of input:
1. STRUCTURED FINDINGS from deterministic analyzers (latency, error classification, pattern detection)
2. CORRELATED SOURCE CODE mapped from trace spans to exact file:line locations via AST analysis

Your job is to synthesize these into a precise diagnosis. Follow this protocol:

## Analysis Protocol

### TIMELINE
Reconstruct what happened chronologically. Reference span durations and parent-child relationships.

### CODE EVIDENCE
For each suspicious span, reference the correlated source code by file:line. Identify:
- Blocking operations in async contexts
- N+1 query patterns (repeated DB calls in loops)
- Missing error handling or swallowed exceptions
- Synchronous calls that should be async
- Resource leaks (unclosed connections, files)

### HYPOTHESIS
State your working hypothesis. Consider at least two alternative explanations before settling on one.
Explicitly reason: "The evidence supports X because..., but Y is also possible if..."

### VERDICT
State the root cause with a confidence level (HIGH/MEDIUM/LOW) and justification.
Format: [CONFIDENCE: HIGH|MEDIUM|LOW] Root cause is...

## Rules
- Cite specific function names, file paths, and line numbers
- Distinguish symptoms (what you observe) from causes (why it happens)
- If evidence is insufficient, say so — never fabricate certainty
- Keep response under 400 words"""


class DeepReasoningAgent(BaseAgent):
    """
    LLM-powered specialist agent that performs deep causal reasoning.

    Uses Claude to analyze trace events and code context, producing
    findings that go beyond pattern matching — explaining WHY failures
    occurred and identifying subtle architectural issues.
    """

    def __init__(self):
        super().__init__("DeepReasoningAgent")

    @property
    def available(self) -> bool:
        """Check if LLM is available (API key set)."""
        return _get_api_key() is not None

    def analyze(
        self,
        graph: ExecutionGraph,
        context: Optional[RetrievedContext] = None,
    ) -> List[Finding]:
        """Analyze execution graph with LLM reasoning."""
        if not self.available:
            return []

        findings = []

        # Build context for the LLM
        prompt = self._build_analysis_prompt(graph, context)

        try:
            analysis_text = _call_claude(prompt, system=SYSTEM_PROMPT, max_tokens=1500)

            if analysis_text:
                # Parse the LLM response into structured findings
                findings = self._parse_llm_response(analysis_text, graph)
            else:
                findings.append(
                    Finding(
                        agent_name=self.name,
                        finding_type=FindingType.UNKNOWN,
                        severity=Severity.LOW,
                        summary="LLM analysis: no response received",
                        details="Claude API returned empty response",
                        evidence=[],
                        affected_spans=[],
                        confidence=0.0,
                        recommendations=[],
                        source_locations=[],
                    )
                )

        except Exception as e:
            # Graceful fallback — don't crash the pipeline
            findings.append(
                Finding(
                    agent_name=self.name,
                    finding_type=FindingType.UNKNOWN,
                    severity=Severity.LOW,
                    summary=f"LLM analysis unavailable: {type(e).__name__}",
                    details=str(e)[:200],
                    evidence=[],
                    affected_spans=[],
                    confidence=0.0,
                    recommendations=[],
                    source_locations=[],
                )
            )

        return findings

    def reason_over_findings(
        self,
        findings: List[Finding],
        graph: ExecutionGraph,
        context: Optional[RetrievedContext] = None,
        code_context: str = "",
    ) -> Optional[str]:
        """
        Take the rule-based agents' findings and produce a deep
        reasoning synthesis using the LLM.

        Returns a detailed explanation string, or None if unavailable.
        """
        if not self.available or not findings:
            return None

        prompt = self._build_synthesis_prompt(findings, graph, context, code_context)

        try:
            return _call_claude(prompt, system=SYSTEM_PROMPT, max_tokens=1000)
        except Exception:
            return None

    def _build_analysis_prompt(
        self,
        graph: ExecutionGraph,
        context: Optional[RetrievedContext] = None,
    ) -> str:
        """Build a structured prompt for the LLM from the execution graph."""
        parts = ["## Execution Trace Analysis\n"]

        # Graph summary
        parts.append(f"### Graph Summary\n{graph.summary()}\n")

        # Errors
        errors = graph.find_errors()
        if errors:
            parts.append("### Errors Detected")
            for err in errors:
                chain = graph.get_causal_chain(err.span_id)
                chain_str = " → ".join(n.function_name for n in chain)
                parts.append(
                    f"- **{err.function_name}** at `{err.source_file}:{err.source_line}`\n"
                    f"  Error: `{err.error}`\n"
                    f"  Thread: {err.thread_name}\n"
                    f"  Call chain: {chain_str}\n"
                    f"  Duration: {(err.duration or 0) * 1000:.1f}ms"
                )

        # Span details
        parts.append("\n### All Spans")
        for node in list(graph.nodes.values())[:20]:  # Cap at 20
            status = "❌ ERROR" if node.error else "✅ OK"
            dur = f"{node.duration * 1000:.1f}ms" if node.duration else "N/A"
            parts.append(
                f"- `{node.function_name}` [{status}] {dur} "
                f"(thread: {node.thread_name})"
            )

        # Code context from RAG
        if context and context.code_results:
            parts.append("\n### Relevant Source Code")
            for r in context.code_results[:5]:
                u = r.code_unit
                parts.append(
                    f"- `{u.qualified_name}` at `{u.source_file}:{u.start_line}`\n"
                    f"  Signature: `{u.signature}`\n"
                    f"  Score: {r.score:.2f}"
                )
                if u.docstring:
                    parts.append(f"  Doc: {u.docstring[:100]}")

        parts.append(
            "\n\n## Task\n"
            "Analyze the execution trace above. Identify:\n"
            "1. The ROOT CAUSE (not symptoms)\n"
            "2. The causal chain that led to the failure\n"
            "3. Any subtle issues (race conditions, design smells, "
            "missing error handling)\n"
            "4. Specific, actionable recommendations\n\n"
            "Format your response as:\n"
            "ROOT_CAUSE: <one line>\n"
            "SEVERITY: critical|high|medium|low\n"
            "CONFIDENCE: 0.0-1.0\n"
            "EXPLANATION: <detailed explanation>\n"
            "RECOMMENDATIONS:\n- <rec 1>\n- <rec 2>"
        )

        return "\n".join(parts)

    def _build_synthesis_prompt(
        self,
        findings: List[Finding],
        graph: ExecutionGraph,
        context: Optional[RetrievedContext] = None,
        code_context: str = "",
    ) -> str:
        """Build a structured prompt for synthesizing rule-based findings."""
        parts = [
            "## Analyzer Findings\n",
            "The following findings come from deterministic analyzers "
            "(not LLM-generated). Each has been verified by code inspection.\n",
        ]

        for f in findings:
            parts.append(
                f"### [{f.agent_name}] {f.finding_type.value}\n"
                f"- Summary: {f.summary}\n"
                f"- Severity: {f.severity.value}\n"
                f"- Confidence: {f.confidence:.0%}\n"
                f"- Evidence: {'; '.join(f.evidence[:3])}\n"
                f"- Affected spans: {', '.join(f.affected_spans[:5])}\n"
                f"- Details: {f.details[:300]}"
            )
            if f.source_locations:
                parts.append(
                    f"- Source: {', '.join(f.source_locations[:3])}"
                )

        # Add span timing summary from the execution graph
        parts.append("\n## Span Timing Summary\n")
        if hasattr(graph, 'nodes') and graph.nodes:
            sorted_nodes = sorted(
                graph.nodes.values(),
                key=lambda n: n.duration_ms if hasattr(n, 'duration_ms') else 0,
                reverse=True,
            )
            for node in sorted_nodes[:10]:
                dur = getattr(node, 'duration_ms', 0)
                err = ' [ERROR]' if getattr(node, 'error', None) else ''
                parts.append(
                    f"- {node.name}: {dur:.1f}ms{err}"
                )

        # Inject correlated source code
        if code_context:
            parts.append(
                "\n## Correlated Source Code\n"
                "The following source code was mapped from trace spans via "
                "AST analysis (not guessed). Each function was resolved from "
                "route decorators and include_router() prefix chains.\n"
                f"{code_context}"
            )

        parts.append(
            "\n## Task\n"
            "Follow the analysis protocol (TIMELINE → CODE EVIDENCE → "
            "HYPOTHESIS → VERDICT).\n"
            "Consider at least two possible causes before concluding.\n"
            "Cite file:line numbers from the correlated code above.\n"
            "End with [CONFIDENCE: HIGH|MEDIUM|LOW] and a one-line verdict."
        )

        return "\n".join(parts)

    def _parse_llm_response(
        self, text: str, graph: ExecutionGraph
    ) -> List[Finding]:
        """Parse structured LLM response into Finding objects."""
        # Extract fields from the response
        root_cause = "LLM analysis complete"
        severity = Severity.MEDIUM
        confidence = 0.85
        explanation = text
        recommendations = []

        for line in text.split("\n"):
            line_stripped = line.strip()
            if line_stripped.startswith("ROOT_CAUSE:"):
                root_cause = line_stripped[len("ROOT_CAUSE:"):].strip()
            elif line_stripped.startswith("SEVERITY:"):
                sev_str = line_stripped[len("SEVERITY:"):].strip().lower()
                severity = {
                    "critical": Severity.CRITICAL,
                    "high": Severity.HIGH,
                    "medium": Severity.MEDIUM,
                    "low": Severity.LOW,
                }.get(sev_str, Severity.MEDIUM)
            elif line_stripped.startswith("CONFIDENCE:"):
                try:
                    confidence = float(
                        line_stripped[len("CONFIDENCE:"):].strip()
                    )
                except ValueError:
                    pass
            elif line_stripped.startswith("EXPLANATION:"):
                explanation = line_stripped[len("EXPLANATION:"):].strip()
            elif line_stripped.startswith("- ") and recommendations is not None:
                recommendations.append(line_stripped[2:])

        # Get source locations from errors
        errors = graph.find_errors()
        source_locs = [
            f"{e.source_file}:{e.source_line}" for e in errors
        ]

        return [
            Finding(
                agent_name=self.name,
                finding_type=FindingType.ERROR_TRACE,
                severity=severity,
                summary=root_cause,
                details=explanation,
                evidence=["AI-powered deep analysis via Claude"],
                affected_spans=[e.span_id for e in errors],
                confidence=confidence,
                recommendations=recommendations or [
                    "Review the root cause identified above",
                    "Check related code paths for similar issues",
                ],
                source_locations=source_locs,
                metadata={"llm_model": "claude-sonnet-4-20250514", "raw_response": text},
            )
        ]
