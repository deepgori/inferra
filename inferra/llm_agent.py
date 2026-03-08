"""
llm_agent.py — LLM-Powered Deep Reasoning Agent

Multi-backend LLM integration for deep root cause analysis. The agent:

1. Receives execution graph summaries and trace events
2. Iteratively requests source code via [NEED_CODE: name] (agentic loop)
3. Performs deep causal reasoning to explain WHY failures occurred
4. Generates actionable, context-specific recommendations

Supported backends:
- Claude (Anthropic) — cloud API, highest quality
- Ollama (local) — run Qwen, Llama, etc. locally on Mac M3/M4

Backend selection (auto-detected or explicit):
    export INFERRA_LLM_BACKEND=claude   # or 'ollama'
    export INFERRA_LLM_MODEL=qwen3-coder:8b  # optional model override
"""

import os
import json
import re
import urllib.request
import urllib.error
from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from inferra.indexer import CodeIndexer

from async_content_tracer.tracer import TraceEvent, EventType
from async_content_tracer.graph import ExecutionGraph, SpanNode
from inferra.rag import RAGPipeline, RetrievedContext
from inferra.agents import (
    BaseAgent,
    Finding,
    FindingType,
    Severity,
)


# ── LLM Backend Abstraction ──────────────────────────────────────────────────


class LLMBackend(ABC):
    """Abstract interface for LLM providers."""

    @abstractmethod
    def call(self, prompt: str, system: str = "", max_tokens: int = 1500) -> Optional[str]:
        """Send a prompt to the LLM and return the response text, or None on failure."""
        ...

    @property
    @abstractmethod
    def available(self) -> bool:
        """Check if this backend is ready to use."""
        ...

    @property
    @abstractmethod
    def display_name(self) -> str:
        """Human-readable name for reports (e.g., 'Claude', 'Qwen3-Coder')."""
        ...


class ClaudeBackend(LLMBackend):
    """Anthropic Claude API backend."""

    def __init__(self, model: str = "claude-sonnet-4-20250514"):
        self._model = model
        self._api_key = os.environ.get("ANTHROPIC_API_KEY")

    @property
    def available(self) -> bool:
        return self._api_key is not None

    @property
    def display_name(self) -> str:
        return "Claude"

    def call(self, prompt: str, system: str = "", max_tokens: int = 1500) -> Optional[str]:
        if not self._api_key:
            return None
        try:
            import ssl

            data = json.dumps({
                "model": self._model,
                "max_tokens": max_tokens,
                "system": system,
                "messages": [{"role": "user", "content": prompt}],
            }).encode("utf-8")

            req = urllib.request.Request(
                "https://api.anthropic.com/v1/messages",
                data=data,
                headers={
                    "Content-Type": "application/json",
                    "x-api-key": self._api_key,
                    "anthropic-version": "2023-06-01",
                },
                method="POST",
            )

            # SSL: prefer certifi → system certs → unverified
            ctx = None
            try:
                import certifi
                ctx = ssl.create_default_context(cafile=certifi.where())
            except ImportError:
                try:
                    ctx = ssl.create_default_context()
                except ssl.SSLError:
                    print("   ⚠️  SSL fallback to unverified")
                    ctx = ssl._create_unverified_context()

            with urllib.request.urlopen(req, timeout=60, context=ctx) as resp:
                result = json.loads(resp.read().decode("utf-8"))
                return result["content"][0]["text"]

        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")
            try:
                err = json.loads(body)
                msg = err.get("error", {}).get("message", body[:200])
            except (json.JSONDecodeError, ValueError):
                msg = body[:200]
            print(f"   ⚠️  Claude API error (HTTP {e.code}): {msg}")
            return None
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            print(f"   ⚠️  Claude connection error: {e}")
            return None


class OllamaBackend(LLMBackend):
    """
    Local LLM backend via Ollama (http://localhost:11434).

    Supports any Ollama-compatible model. Recommended for this project:
      - qwen3-coder:8b   (best for code reasoning, MoE, 256K context)
      - qwen3.5:14b      (stronger general reasoning, needs 12GB+)
      - qwen3.5:7b       (good fallback, 8GB RAM)

    Setup:
      brew install ollama
      ollama pull qwen3-coder:8b
    """

    DEFAULT_MODEL = "qwen3-coder:8b"
    FALLBACK_MODELS = ["qwen3.5:7b", "qwen3:8b", "llama3.1:8b"]

    def __init__(
        self,
        model: Optional[str] = None,
        base_url: str = "http://localhost:11434",
    ):
        self._base_url = base_url.rstrip("/")
        self._model = model or os.environ.get("INFERRA_LLM_MODEL", self.DEFAULT_MODEL)
        self._available: Optional[bool] = None  # Lazy check

    @property
    def available(self) -> bool:
        if self._available is None:
            self._available = self._check_available()
        return self._available

    @property
    def display_name(self) -> str:
        return f"Ollama ({self._model})"

    def _check_available(self) -> bool:
        """Check if Ollama is running and the model is available."""
        try:
            req = urllib.request.Request(
                f"{self._base_url}/api/tags",
                method="GET",
            )
            with urllib.request.urlopen(req, timeout=3) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                models = [m.get("name", "") for m in data.get("models", [])]

                # Check if requested model is installed
                if any(self._model in m for m in models):
                    return True

                # Try fallbacks
                for fallback in self.FALLBACK_MODELS:
                    if any(fallback in m for m in models):
                        print(f"   ℹ️  {self._model} not found, using {fallback}")
                        self._model = fallback
                        return True

                if models:
                    # Use whatever is available
                    self._model = models[0].split(":")[0] + ":latest"
                    print(f"   ℹ️  Using available model: {self._model}")
                    return True

                print(f"   ⚠️  Ollama running but no models installed. Run: ollama pull {self.DEFAULT_MODEL}")
                return False
        except (urllib.error.URLError, TimeoutError, OSError):
            return False

    def call(self, prompt: str, system: str = "", max_tokens: int = 1500) -> Optional[str]:
        if not self.available:
            return None
        try:
            messages = []
            if system:
                messages.append({"role": "system", "content": system})
            messages.append({"role": "user", "content": prompt})

            data = json.dumps({
                "model": self._model,
                "messages": messages,
                "stream": False,
                "options": {
                    "num_predict": max_tokens,
                    "temperature": 0.3,  # Low temp for analytical reasoning
                },
            }).encode("utf-8")

            req = urllib.request.Request(
                f"{self._base_url}/api/chat",
                data=data,
                headers={"Content-Type": "application/json"},
                method="POST",
            )

            # Ollama can be slow for first call (model loading) — generous timeout
            with urllib.request.urlopen(req, timeout=120) as resp:
                result = json.loads(resp.read().decode("utf-8"))
                content = result.get("message", {}).get("content", "")

                # Strip <think>...</think> blocks from reasoning models
                content = re.sub(
                    r"<think>.*?</think>", "", content, flags=re.DOTALL
                ).strip()

                return content if content else None

        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")
            print(f"   ⚠️  Ollama error (HTTP {e.code}): {body[:200]}")
            return None
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            print(f"   ⚠️  Ollama connection error: {e}")
            return None


# ── Backend Factory ──────────────────────────────────────────────────────────

_active_backend: Optional[LLMBackend] = None


def get_llm_backend(preference: Optional[str] = None) -> Optional[LLMBackend]:
    """
    Get the best available LLM backend.

    Priority:
    1. Explicit preference ("claude", "ollama", "local")
    2. INFERRA_LLM_BACKEND env var
    3. Auto-detect: Claude if API key set, else Ollama if running

    Returns None if no backend is available.
    """
    global _active_backend

    choice = (
        preference
        or os.environ.get("INFERRA_LLM_BACKEND", "")
    ).lower().strip()

    if choice in ("claude", "anthropic"):
        backend = ClaudeBackend()
        if backend.available:
            _active_backend = backend
            return backend
        print("   ⚠️  Claude requested but ANTHROPIC_API_KEY not set")
        return None

    if choice in ("ollama", "local", "qwen"):
        backend = OllamaBackend()
        if backend.available:
            _active_backend = backend
            return backend
        print("   ⚠️  Ollama requested but not running. Start with: ollama serve")
        return None

    # Auto-detect: prefer Claude (higher quality), fall back to Ollama
    claude = ClaudeBackend()
    if claude.available:
        _active_backend = claude
        return claude

    ollama = OllamaBackend()
    if ollama.available:
        print(f"   ℹ️  No API key found, using local LLM: {ollama.display_name}")
        _active_backend = ollama
        return ollama

    return None


def get_active_backend() -> Optional[LLMBackend]:
    """Get the currently active backend (set by get_llm_backend)."""
    return _active_backend


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

## Tool Use
If you need to see source code for a function not already provided, respond with:
    [NEED_CODE: function_or_class_name]
on its own line. The system will retrieve the code and re-prompt you.
Only request code that is directly relevant to the diagnosis. Max 2 requests per response.

## Rules
- Cite specific function names, file paths, and line numbers
- Distinguish symptoms (what you observe) from causes (why it happens)
- If evidence is insufficient, say so — never fabricate certainty
- Keep response under 400 words"""


class DeepReasoningAgent(BaseAgent):
    """
    LLM-powered specialist agent that performs deep causal reasoning.

    Uses Claude or a local LLM (via Ollama) to analyze trace events and
    code context, producing findings that go beyond pattern matching —
    explaining WHY failures occurred and identifying architectural issues.
    """

    def __init__(self, indexer: "CodeIndexer" = None, backend: Optional[LLMBackend] = None):
        super().__init__("DeepReasoningAgent")
        self._indexer = indexer  # For agentic code retrieval
        self._backend = backend or get_llm_backend()

    @property
    def available(self) -> bool:
        """Check if an LLM backend is available."""
        return self._backend is not None and self._backend.available

    @property
    def backend_name(self) -> str:
        """Name of the active LLM backend for reports."""
        return self._backend.display_name if self._backend else "None"

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
            analysis_text = self._backend.call(prompt, system=SYSTEM_PROMPT, max_tokens=1500)

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
                        details="LLM returned empty response",
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

        Uses an agentic loop: the LLM can request additional source code
        via [NEED_CODE: function_name] markers. The system retrieves the
        code and re-prompts, up to max_iterations times.

        Returns a detailed explanation string, or None if unavailable.
        """
        if not self.available or not findings:
            return None

        max_iterations = 2
        prompt = self._build_synthesis_prompt(findings, graph, context, code_context)

        try:
            for iteration in range(max_iterations + 1):
                response = self._backend.call(
                    prompt, system=SYSTEM_PROMPT, max_tokens=1200
                )
                if response is None:
                    return None

                # Check if LLM requested additional code
                code_requests = self._extract_code_requests(response)

                if not code_requests or not self._indexer or iteration == max_iterations:
                    # No more requests, or no indexer, or max iterations reached
                    return response

                # Retrieve requested code and re-prompt
                retrieved_code = self._retrieve_requested_code(code_requests)
                if not retrieved_code:
                    return response  # Nothing found, return as-is

                # Build follow-up prompt with the retrieved code
                prompt = (
                    f"You previously requested additional source code. "
                    f"Here it is:\n\n{retrieved_code}\n\n"
                    f"Now complete your analysis. Follow the analysis protocol "
                    f"(TIMELINE → CODE EVIDENCE → HYPOTHESIS → VERDICT).\n\n"
                    f"Your previous partial response:\n{response}"
                )

            return response
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            print(f"   ⚠️  Agentic loop failed: {e}")
            return None

    def _extract_code_requests(self, response: str) -> List[str]:
        """Parse [NEED_CODE: name] markers from LLM response."""
        pattern = r'\[NEED_CODE:\s*([^\]]+)\]'
        matches = re.findall(pattern, response)
        return [m.strip() for m in matches[:2]]  # Max 2 per response

    def _retrieve_requested_code(self, names: List[str]) -> str:
        """Retrieve source code for requested function/class names."""
        if not self._indexer:
            return ""

        sections = []
        for name in names:
            results = self._indexer.search(name, top_k=2)
            for r in results:
                unit = r.code_unit
                sections.append(
                    f"--- {unit.qualified_name} ({unit.source_file}:{unit.start_line}) ---\n"
                    f"{unit.body_text}"
                )

        return "\n\n".join(sections) if sections else ""

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
        if graph.nodes:
            sorted_nodes = sorted(
                graph.nodes.values(),
                key=lambda n: (n.duration or 0) * 1000,
                reverse=True,
            )
            for node in sorted_nodes[:10]:
                dur = (node.duration or 0) * 1000
                err = ' [ERROR]' if node.error else ''
                parts.append(
                    f"- {node.function_name}: {dur:.1f}ms{err}"
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
