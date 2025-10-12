"""
inferra — AI-First Autonomous Debugging Workspace

An autonomous debugging module that takes production telemetry as input,
indexes the associated codebase into a searchable store, and uses
multi-agent reasoning to produce Root Cause Analysis (RCA).

Architecture:
    1. CodeIndexer — Multi-stack indexer (Python, SQL, YAML, .env, TOML)
    2. RAGPipeline — Retrieves relevant code context given a telemetry signal
    3. Multi-Agent System:
       - LogAnalysisAgent — Specializes in parsing and interpreting log/trace events
       - MetricsCorrelationAgent — Correlates timing anomalies and patterns
       - CoordinatorAgent — Synthesizes findings into a unified RCA
    4. RCAEngine — Orchestrates the full investigation pipeline

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

__version__ = "0.2.0"
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
]
