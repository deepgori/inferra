#!/usr/bin/env python3
"""
run_planit_instrumented.py — Run PlanIt's REAL FastAPI app with OpenTelemetry instrumentation.

This is NOT a simulator. It imports and runs PlanIt's actual main.py, with
real CSV data, real pandas operations, real haversine calculations, and
real KMeans clustering. The only addition is OTel auto-instrumentation
that sends REAL traces to Inferra's OTLP receiver.

Usage:
    # Terminal 1: Start Inferra with code correlation
    python -m inferra serve --project test_projects/planIt/BackendFastAPI

    # Terminal 2: Run PlanIt with real instrumentation
    python run_planit_instrumented.py

    # Terminal 3: Hit real endpoints
    curl http://localhost:8000/places
    curl http://localhost:8000/hotels/mumbai
    curl -X POST http://localhost:8000/restaurants/mumbai -H 'Content-Type: application/json' -d '{"latitude":19.076,"longitude":72.8777}'
"""

import sys
import os

# Add PlanIt to the path
PLANIT_DIR = os.path.join(os.path.dirname(__file__), "test_projects", "planIt", "BackendFastAPI")
sys.path.insert(0, PLANIT_DIR)
os.chdir(PLANIT_DIR)  # So CSV reads work (they use relative paths)

# ── Set up OpenTelemetry BEFORE importing the app ──
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.sdk.resources import Resource
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

resource = Resource.create({"service.name": "planit-api"})
provider = TracerProvider(resource=resource)

# Send traces to Inferra's OTLP receiver
otlp_exporter = OTLPSpanExporter(
    endpoint="http://localhost:4318/v1/traces",
)
provider.add_span_processor(BatchSpanProcessor(otlp_exporter))
trace.set_tracer_provider(provider)

# ── Now import PlanIt's REAL FastAPI app ──
# Skip Firebase initialization (we don't have credentials)
# by mocking firebase_admin before import
import types
mock_firebase = types.ModuleType("firebase_admin")
mock_firebase.initialize_app = lambda *a, **kw: None

mock_creds = types.ModuleType("firebase_admin.credentials")
mock_creds.Certificate = lambda *a, **kw: None

mock_auth = types.ModuleType("firebase_admin.auth")
mock_firestore = types.ModuleType("firebase_admin.firestore")
mock_firestore.client = lambda: None

sys.modules["firebase_admin"] = mock_firebase
sys.modules["firebase_admin.credentials"] = mock_creds
sys.modules["firebase_admin.auth"] = mock_auth
sys.modules["firebase_admin.firestore"] = mock_firestore

# Import the REAL app
from main import app

# ── Auto-instrument FastAPI ──
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
FastAPIInstrumentor.instrument_app(app)

# ── Add a custom tracer for manual spans on heavy operations ──
tracer = trace.get_tracer("planit.instrumentation")

# Wrap some of PlanIt's real functions with manual tracing
import main as planit_main

# Patch getPlaces to add a span around CSV reading
_original_getPlaces = planit_main.getPlaces
async def traced_getPlaces():
    with tracer.start_as_current_span("csv.read_places_dataset", attributes={
        "csv.file": "Dataset Attributes - All4.csv",
        "csv.operation": "pd.read_csv",
    }):
        return await _original_getPlaces()
app.routes  # force route registration
planit_main.getPlaces = traced_getPlaces

# Patch getHotels to trace CSV reading
_original_getHotels = planit_main.getHotels
async def traced_getHotels(city: str):
    with tracer.start_as_current_span("csv.read_hotels_dataset", attributes={
        "csv.file": "hotelsDataset.csv",
        "csv.operation": "pd.read_csv",
        "query.city": city,
    }):
        return await _original_getHotels(city)
planit_main.getHotels = traced_getHotels

# Patch getRestaurants to trace CSV reading + haversine
from pydantic import BaseModel
class Location(BaseModel):
    latitude: float
    longitude: float

_original_getRestaurants = planit_main.getRestaurants
async def traced_getRestaurants(city: str, location: Location):
    with tracer.start_as_current_span("csv.read_restaurant_dataset", attributes={
        "csv.file": f"{city.capitalize()}Restaurant.csv",
        "csv.operation": "pd.read_csv",
        "query.city": city,
        "query.latitude": location.latitude,
        "query.longitude": location.longitude,
    }):
        with tracer.start_as_current_span("geo.haversine_distance_calc", attributes={
            "geo.operation": "haversine",
            "geo.result_limit": 100,
        }):
            return await _original_getRestaurants(city, location)
planit_main.getRestaurants = traced_getRestaurants


if __name__ == "__main__":
    import uvicorn

    print()
    print("━" * 60)
    print("  PlanIt — REAL Instrumented FastAPI App")
    print("━" * 60)
    print()
    print("  This is the REAL PlanIt app with OTel auto-instrumentation.")
    print("  All traces are GENUINE — real CSV reads, real pandas, real math.")
    print()
    print("  Endpoints (no Firebase needed):")
    print("    GET  /                     Health check")
    print("    GET  /places               All tourist places (real CSV)")
    print("    GET  /hotels/mumbai        Hotels in Mumbai (real CSV)")
    print("    POST /restaurants/mumbai   Nearby restaurants (real haversine)")
    print()
    print("  Traces → http://localhost:4318/v1/traces")
    print()
    print("━" * 60)

    uvicorn.run(app, host="0.0.0.0", port=8000, log_level="info")
