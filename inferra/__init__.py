"""
inferra — AI-First Autonomous Debugging Workspace

An autonomous debugging module that takes production telemetry as input,
indexes the associated codebase into a searchable store, and uses
multi-agent reasoning to produce Root Cause Analysis (RCA).

Architecture:
    1. CodeIndexer — Multi-stack indexer (Python, JS/TS, Go, Java, SQL, YAML)
    2. RAGPipeline — Retrieves relevant code context given a telemetry signal
    3. Multi-Agent System:
       - LogAnalysisAgent — Specializes in parsing and interpreting log/trace events
       - MetricsCorrelationAgent — Correlates timing anomalies and patterns
       - DependencyAgent — Analyzes call graphs and module coupling
       - SecurityAgent — Scans for common security vulnerabilities
       - CoordinatorAgent — Synthesizes findings into a unified RCA
    4. RCAEngine — Orchestrates the full investigation pipeline
    5. Storage — SQLite persistence for historical analysis tracking
    6. StreamingAnalyzer — Real-time anomaly detection
    7. Topology — Multi-service graph visualization

The module integrates with async_content_tracer's TraceEvents and ExecutionGraph
to form a complete observability → debugging pipeline.
"""

from inferra.indexer import CodeIndexer
from inferra.rag import RAGPipeline
from inferra.agents import (
    LogAnalysisAgent,
    MetricsCorrelationAgent,
    CoordinatorAgent,
)
from inferra.rca_engine import RCAEngine
from inferra.embeddings import (
    EmbeddingBackend,
    LocalEmbedding,
    VectorStore,
    get_best_backend,
)
from inferra.sql_indexer import SQLIndexer, SQLModel
from inferra.config_indexer import ConfigIndexer, ConfigElement

from inferra.llm_agent import (
    LLMBackend,
    ClaudeBackend,
    GroqBackend,
    OllamaBackend,
    get_llm_backend,
)

from inferra.api import analyze, AnalysisResult
from inferra.storage import Storage
from inferra.streaming import StreamingAnalyzer
from inferra.dependency_agent import DependencyAgent
from inferra.security_agent import SecurityAgent
from inferra.topology import Topology
from inferra.pr_generator import PRGenerator

__version__ = "0.4.0"
__all__ = [
    "CodeIndexer",
    "RAGPipeline",
    "LogAnalysisAgent",
    "MetricsCorrelationAgent",
    "CoordinatorAgent",
    "RCAEngine",
    "EmbeddingBackend",
    "LocalEmbedding",
    "VectorStore",
    "get_best_backend",
    "SQLIndexer",
    "SQLModel",
    "ConfigIndexer",
    "ConfigElement",
    "LLMBackend",
    "ClaudeBackend",
    "GroqBackend",
    "OllamaBackend",
    "get_llm_backend",
    "Storage",
    "StreamingAnalyzer",
    "DependencyAgent",
    "SecurityAgent",
    "Topology",
    "PRGenerator",
]

