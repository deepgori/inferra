#!/usr/bin/env python3
"""
run_spendwise_instrumented.py — Run Spend-Wise's REAL FastAPI app with OTel.

This is NOT a simulator. It imports and runs the actual Spend-Wise backend
with real NLP parsing, real expense creation, and real SQLite database ops.
The only modification is swapping PostgreSQL for SQLite (no Postgres needed)
and adding OpenTelemetry auto-instrumentation.

Usage:
    # Terminal 1
    python -m inferra serve --project test_projects/SpendWise/backend

    # Terminal 2
    python run_spendwise_instrumented.py

    # Terminal 3
    curl http://localhost:8000/health
    curl -X POST "http://localhost:8000/api/v1/ai/parse?text=I%20paid%20%2450%20for%20dinner%20with%20Alex"
    curl http://localhost:8000/api/v1/groups/test-group/insights
"""

import sys
import os

SPENDWISE_DIR = os.path.join(
    os.path.dirname(__file__), "test_projects", "SpendWise", "backend"
)
sys.path.insert(0, SPENDWISE_DIR)

# ── Set up OTel BEFORE imports ──
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.sdk.resources import Resource
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

resource = Resource.create({"service.name": "spendwise-api"})
provider = TracerProvider(resource=resource)
otlp_exporter = OTLPSpanExporter(
    endpoint="http://localhost:4318/v1/traces",
)
provider.add_span_processor(BatchSpanProcessor(otlp_exporter))
trace.set_tracer_provider(provider)

# ── Patch database to use SQLite instead of PostgreSQL ──
# This lets us run the REAL app without needing a running Postgres instance.
os.environ["DATABASE_URL"] = "sqlite:///./spendwise_test.db"

# Patch pydantic-settings to not fail on missing env vars
os.environ.setdefault("POSTGRES_SERVER", "localhost")
os.environ.setdefault("POSTGRES_USER", "test")
os.environ.setdefault("POSTGRES_PASSWORD", "test")
os.environ.setdefault("POSTGRES_DB", "test")

# Now import and patch the database module to use SQLite
import app.core.database as db_module
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

# Replace the engine with SQLite
sqlite_engine = create_engine(
    "sqlite:///./spendwise_test.db",
    connect_args={"check_same_thread": False},
)
db_module.engine = sqlite_engine
db_module.SessionLocal = sessionmaker(
    autocommit=False, autoflush=False, bind=sqlite_engine
)

# ── Patch SQLAlchemy UUID for 1.4/2.0 compat + SQLite ──
# Spend-Wise does `from sqlalchemy import UUID` which only works in SA 2.0+
# We create a shim that wraps String(36) and accepts as_uuid=True
import sqlalchemy
import sqlalchemy.types

class _UUIDShim(sqlalchemy.types.TypeDecorator):
    """SQLite-compatible UUID type that stores as string."""
    impl = sqlalchemy.String(36)
    cache_ok = True

    def __init__(self, as_uuid=False, *args, **kwargs):
        super().__init__(*args, **kwargs)

    def process_bind_param(self, value, dialect):
        if value is not None:
            return str(value)
        return value

    def process_result_value(self, value, dialect):
        return value

if not hasattr(sqlalchemy, 'UUID'):
    sqlalchemy.UUID = _UUIDShim

# Import models to register them with Base, then create tables
from app.models.expense import Group, Expense, Split  # noqa
from app.models.user import User  # noqa
db_module.Base.metadata.create_all(bind=sqlite_engine)

# ── Import the REAL FastAPI app ──
from app.main import app

# ── Auto-instrument FastAPI ──
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
FastAPIInstrumentor.instrument_app(app)

# ── Manual tracing for key operations ──
tracer = trace.get_tracer("spendwise.instrumentation")

# Wrap the NLP parser with tracing
from app.ai.nlp import NLPProcessor
_original_parse = NLPProcessor.parse_expense_text

@staticmethod
def traced_parse(text: str):
    with tracer.start_as_current_span("ai.nlp.parse_expense_text", attributes={
        "ai.module": "NLPProcessor",
        "ai.input_length": len(text),
        "ai.operation": "regex_parse",
    }):
        return _original_parse(text)

NLPProcessor.parse_expense_text = traced_parse

# Wrap the InsightAnalyzer with tracing
from app.ai.insights import InsightAnalyzer
_original_insights = InsightAnalyzer.get_spending_insights

@staticmethod
def traced_insights(expenses):
    with tracer.start_as_current_span("ai.insights.get_spending_insights", attributes={
        "ai.module": "InsightAnalyzer",
        "ai.expense_count": len(expenses) if expenses else 0,
        "ai.operation": "spending_analysis",
    }):
        return _original_insights(expenses)

InsightAnalyzer.get_spending_insights = traced_insights


if __name__ == "__main__":
    import uvicorn

    print()
    print("━" * 60)
    print("  Spend-Wise — REAL Instrumented FastAPI App")
    print("━" * 60)
    print()
    print("  This is the REAL Spend-Wise app with OTel instrumentation.")
    print("  SQLite replaces PostgreSQL. All logic is genuine.")
    print()
    print("  Endpoints:")
    print("    GET  /health                    Health check")
    print("    POST /api/v1/ai/parse?text=...  NLP expense parser (real regex)")
    print("    GET  /api/v1/groups/{id}/insights  Spending insights")
    print("    POST /api/v1/groups             Create group (real SQLite)")
    print("    POST /api/v1/expenses           Create expense (real SQLite)")
    print()
    print("  Traces → http://localhost:4318/v1/traces")
    print()
    print("━" * 60)

    uvicorn.run(app, host="0.0.0.0", port=8000, log_level="info")
