#!/usr/bin/env python3
"""
PharmaSight — REAL Instrumented FastAPI App
===========================================
Runs the real PharmaSight backend code with:
  - SQLite in-memory DB (patches PostgreSQL dependency)
  - OpenTelemetry auto-instrumentation → OTLP exporter
  - All real routers, services, and business logic
  - Mock ML model for inference (real service/DB path)

Start the OTLP receiver first:
  python -m inferra serve --project test_projects/PharmaSight/backend --port 4318

Then run this:
  python run_pharmasight_instrumented.py
"""
import os
import sys
import time
import random
import logging
import numpy as np

# ── Setup OTel BEFORE any app imports ──
os.environ.setdefault("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4318")
os.environ.setdefault("OTEL_SERVICE_NAME", "pharmasight-api")

from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource

resource = Resource.create({"service.name": "pharmasight-api"})
provider = TracerProvider(resource=resource)
exporter = OTLPSpanExporter(endpoint="http://localhost:4318/v1/traces")
provider.add_span_processor(BatchSpanProcessor(exporter))
trace.set_tracer_provider(provider)

# Auto-instrument FastAPI, SQLAlchemy, etc.
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

# ── Patch PharmaSight's DB to use SQLite in-memory ──
# Must happen BEFORE importing the app modules
pharma_backend = os.path.join(
    os.path.dirname(__file__),
    "test_projects", "PharmaSight", "backend"
)
sys.path.insert(0, pharma_backend)

# Override the settings before app.config is imported
os.environ["DATABASE_URL"] = "sqlite:///pharmasight_test.db"

# Now import and patch the DB module
from app.config import settings
settings.DATABASE_URL = "sqlite:///pharmasight_test.db"

from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
import app.db.base as db_module

# Create SQLite engine
engine = create_engine(
    "sqlite:///pharmasight_test.db",
    connect_args={"check_same_thread": False},
    echo=False,
)

# Enable WAL mode for better concurrency
@event.listens_for(engine, "connect")
def set_sqlite_pragma(dbapi_conn, connection_record):
    cursor = dbapi_conn.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.close()

# Patch the db module
db_module.engine = engine
db_module.SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

# Create all tables
from app.models.database import Dataset, Feature, ModelVersion, Metric, Prediction, InferenceLog
db_module.Base.metadata.create_all(bind=engine)

# ── Seed test data ──
from datetime import datetime

def seed_data():
    """Seed the DB with realistic PharmaSight data."""
    db = db_module.SessionLocal()
    try:
        # Check if already seeded
        if db.query(ModelVersion).count() > 0:
            return

        # Create dataset
        dataset = Dataset(
            name="Drug Classification Dataset",
            description="WHO drug classification training data with 200 patients",
            file_path="data/drug200.csv",
            row_count=200,
        )
        db.add(dataset)
        db.flush()

        # Create features
        features = [
            Feature(dataset_id=dataset.id, name="Age", data_type="continuous",
                    statistics={"mean": 44.3, "std": 16.5, "min": 15, "max": 74}),
            Feature(dataset_id=dataset.id, name="Sex", data_type="categorical",
                    statistics={"value_counts": {"M": 104, "F": 96}, "unique_count": 2}),
            Feature(dataset_id=dataset.id, name="BP", data_type="categorical",
                    statistics={"value_counts": {"HIGH": 77, "LOW": 64, "NORMAL": 59}, "unique_count": 3}),
            Feature(dataset_id=dataset.id, name="Cholesterol", data_type="categorical",
                    statistics={"value_counts": {"NORMAL": 103, "HIGH": 97}, "unique_count": 2}),
            Feature(dataset_id=dataset.id, name="Na_to_K", data_type="continuous",
                    statistics={"mean": 16.08, "std": 7.22, "min": 6.269, "max": 38.247}),
        ]
        db.add_all(features)

        # Create model versions with realistic metrics
        models = [
            ModelVersion(
                model_type="logistic_regression", version="lr_1.0.0",
                file_path="models/lr_v1.pkl", model_hash="a1b2c3d4e5f6",
                hyperparameters={"C": 1.0, "max_iter": 200, "solver": "lbfgs"},
                is_active=0
            ),
            ModelVersion(
                model_type="svm", version="svm_1.0.0",
                file_path="models/svm_v1.pkl", model_hash="b2c3d4e5f6a7",
                hyperparameters={"kernel": "rbf", "C": 10.0, "gamma": "scale"},
                is_active=0
            ),
            ModelVersion(
                model_type="random_forest", version="rf_1.0.0",
                file_path="models/rf_v1.pkl", model_hash="c3d4e5f6a7b8",
                hyperparameters={"n_estimators": 100, "max_depth": 10, "random_state": 42},
                is_active=0
            ),
            ModelVersion(
                model_type="ensemble", version="ensemble_1.0.0",
                file_path="models/ensemble_v1.pkl", model_hash="d4e5f6a7b8c9",
                hyperparameters={"voting": "soft", "models": ["lr", "svm", "rf"]},
                is_active=1  # Active model
            ),
        ]
        db.add_all(models)
        db.flush()

        # Create metrics for each model
        model_metrics = {
            "logistic_regression": {"accuracy": 0.89, "precision_macro": 0.87, "recall_macro": 0.88, "f1_macro": 0.87},
            "svm": {"accuracy": 0.92, "precision_macro": 0.91, "recall_macro": 0.90, "f1_macro": 0.90},
            "random_forest": {"accuracy": 0.95, "precision_macro": 0.94, "recall_macro": 0.94, "f1_macro": 0.94},
            "ensemble": {"accuracy": 0.97, "precision_macro": 0.96, "recall_macro": 0.96, "f1_macro": 0.96},
        }
        for model in models:
            metrics = model_metrics.get(model.model_type, {})
            for metric_name, value in metrics.items():
                db.add(Metric(
                    model_version_id=model.id,
                    metric_type=metric_name,
                    metric_value=value,
                    split_type="test",
                ))

        # Seed some prediction history
        drug_classes = ["DrugA", "DrugB", "DrugC", "drugX", "DrugY"]
        for i in range(10):
            db.add(Prediction(
                dataset_id=dataset.id,
                model_version_id=models[-1].id,
                age=str(random.randint(15, 74)),
                sex=random.choice(["M", "F"]),
                bp=random.choice(["LOW", "NORMAL", "HIGH"]),
                cholesterol=random.choice(["NORMAL", "HIGH"]),
                na_to_k=str(round(random.uniform(6.3, 38.2), 2)),
                predicted_drug=random.choice(drug_classes),
                confidence=round(random.uniform(0.7, 0.99), 3),
                confidence_scores={cls: round(random.uniform(0.01, 0.99), 3) for cls in drug_classes},
            ))

        db.commit()
        logging.info("Seeded PharmaSight DB with models, metrics, and predictions")
    finally:
        db.close()

seed_data()

# ── Mock the ML inference to avoid needing pickled models ──
import app.pipelines.inference as inf_module


class MockInferencePipeline:
    """Mock inference pipeline that returns realistic predictions."""
    
    DRUG_CLASSES = ["DrugA", "DrugB", "DrugC", "drugX", "DrugY"]
    
    def __init__(self, model_path=None):
        self.model_path = model_path
        self.model_type = "ensemble"
        self.version = "ensemble_1.0.0"
    
    def load_model(self, model_path):
        self.model_path = model_path
        # Simulate model load time (real I/O)
        time.sleep(random.uniform(0.005, 0.015))
        logging.info(f"[mock] Loaded model from {model_path}")
    
    def predict(self, data, return_probabilities=True):
        """Simulate real inference with realistic timing."""
        start = time.time()
        
        # Simulate feature preprocessing (~2-5ms)
        time.sleep(random.uniform(0.002, 0.005))
        
        # Simulate inference (~3-8ms)
        time.sleep(random.uniform(0.003, 0.008))
        
        # Deterministic-ish prediction based on Na_to_K
        na_to_k = float(data.get("Na_to_K", 15.0))
        if na_to_k > 25:
            predicted = "DrugY"
        elif na_to_k > 15:
            predicted = "drugX"
        elif data.get("BP") == "HIGH":
            predicted = "DrugA" if data.get("Cholesterol") == "HIGH" else "DrugC"
        else:
            predicted = "DrugB"
        
        # Generate probability distribution
        probs = np.random.dirichlet(np.ones(5) * 0.5)
        idx = self.DRUG_CLASSES.index(predicted)
        probs[idx] = max(probs) + 0.3  # Boost predicted class
        probs = probs / probs.sum()
        
        elapsed_ms = (time.time() - start) * 1000
        
        result = {
            "predicted_drug": predicted,
            "confidence": float(probs[idx]),
            "processing_time_ms": elapsed_ms,
            "model_type": self.model_type,
            "model_version": self.version,
        }
        if return_probabilities:
            result["probabilities"] = {
                cls: round(float(p), 4) for cls, p in zip(self.DRUG_CLASSES, probs)
            }
        return result
    
    def predict_batch(self, data_list, return_probabilities=True):
        return [self.predict(d, return_probabilities) for d in data_list]


# Patch inference pipeline
inf_module.InferencePipeline = MockInferencePipeline

# Also patch PredictionService.load_model to use our mock
import app.services.prediction_service as pred_svc_module
_orig_load = pred_svc_module.PredictionService.load_model

def _patched_load_model(self, model_path):
    self.inference_pipeline = MockInferencePipeline(model_path)
    logging.info(f"Loaded mock inference pipeline from {model_path}")

pred_svc_module.PredictionService.load_model = _patched_load_model


# ── Build FastAPI app from real routers ──
from fastapi import FastAPI
from app.api.endpoints import data as data_router
from app.api.endpoints import models as models_router
from app.api.endpoints import predictions as predictions_router
from app.config import settings as app_settings

app = FastAPI(
    title=app_settings.PROJECT_NAME,
    version=app_settings.VERSION,
    description="PharmaSight Drug Classification API — Instrumented with OpenTelemetry",
)

# Health check
@app.get("/")
async def health():
    return {
        "service": "pharmasight-api",
        "status": "healthy",
        "version": app_settings.VERSION,
    }

# Mount real routers under /api/v1
app.include_router(data_router.router, prefix="/api/v1")
app.include_router(models_router.router, prefix="/api/v1")
app.include_router(predictions_router.router, prefix="/api/v1")

# Instrument
FastAPIInstrumentor().instrument_app(app)

# ── Run ──
if __name__ == "__main__":
    import uvicorn

    print("""
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  PharmaSight — REAL Instrumented FastAPI App
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  Real PharmaSight code with OTel auto-instrumentation.
  DB: SQLite in-memory | ML: Mock inference (real service path)

  Endpoints:
    GET  /                           Health check
    GET  /api/v1/models              List all model versions
    GET  /api/v1/models/active       Get active model
    GET  /api/v1/models/{id}/metrics Get model metrics
    POST /api/v1/predict             Single drug prediction
    POST /api/v1/predict/batch       Batch predictions
    GET  /api/v1/predict/history     Prediction history
    GET  /api/v1/data/summary        Dataset summary

  Traces → http://localhost:4318/v1/traces

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
""")
    uvicorn.run(app, host="0.0.0.0", port=8000, log_level="info")
