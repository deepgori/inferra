#!/usr/bin/env python3
"""
run_realworld_instrumented.py — Run the RealWorld Conduit app with OTel.

Requires: Docker PostgreSQL on port 5433 (docker compose up -d db)
          Inferra receiver on port 4318

Usage:
    docker run -d --name realworld_pg -e POSTGRES_PASSWORD=main -e POSTGRES_USER=main -e POSTGRES_DB=main -p 5433:5432 postgres:16
    python -m inferra serve --port 4318 --project test_projects/RealWorldApp/app
    python run_realworld_instrumented.py
"""

import sys, os

REALWORLD_DIR = os.path.join(os.path.dirname(__file__), "test_projects", "RealWorldApp")
sys.path.insert(0, REALWORLD_DIR)

# ── OTel setup ──
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.sdk.resources import Resource
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

resource = Resource.create({"service.name": "conduit-api"})
provider = TracerProvider(resource=resource)
provider.add_span_processor(BatchSpanProcessor(
    OTLPSpanExporter(endpoint="http://localhost:4318/v1/traces")
))
trace.set_tracer_provider(provider)

# ── Import the real app (uses Docker PostgreSQL on port 5433) ──
from app.main import app

# Create tables
from app.db.session import engine
from app.db.base_class import Base
from app.models.user import User       # noqa
from app.models.article import Article  # noqa
from app.models.comment import Comment  # noqa
from app.models.tag import Tag         # noqa
import asyncio

async def _init_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

asyncio.run(_init_db())

# ── Instrument FastAPI ──
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
FastAPIInstrumentor.instrument_app(app)

if __name__ == "__main__":
    import uvicorn
    print("\n" + "━" * 60)
    print("  Conduit (RealWorld) — REAL App + Docker PostgreSQL + OTel")
    print("━" * 60)
    print("  37 Python files | Async | JWT Auth | CRUD | Middleware")
    print("  Real PostgreSQL via Docker. Zero mocking.\n")
    print("  POST /api/users         Register")
    print("  POST /api/users/login   Login")
    print("  GET  /api/articles      List articles")
    print("  POST /api/articles      Create (auth)")
    print("  GET  /api/tags          Tags")
    print("  GET  /api/profiles/{u}  Profile")
    print("  + comments, favorites, feed, update, delete...")
    print("━" * 60 + "\n")
    uvicorn.run(app, host="0.0.0.0", port=8000, log_level="info")
