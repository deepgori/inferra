#!/usr/bin/env python3
"""
PharmaSight OTLP Demo — Drug Classification Service
====================================================
Simulates the PharmaSight API endpoints with intentional bugs,
sending OTLP traces to the Inferra receiver for analysis.

Bugs planted:
  1. N+1 query in model metrics fetching
  2. Unhandled None when model artifact is missing
  3. ML inference memory leak (growing prediction cache)
  4. Race condition in concurrent model training
  5. Slow feature engineering with redundant SMOTE re-fitting
"""

import json
import time
import random
import uuid
import os
import struct
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.request import Request, urlopen
from urllib.error import URLError
from threading import Lock

# ── Configuration ──
OTLP_ENDPOINT = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4318")
SERVICE_NAME = "pharmasight-api"
PORT = 8000

# ── Simulated Database ──
DRUG_CLASSES = ["DrugA", "DrugB", "DrugC", "drugX", "DrugY"]

MODELS_DB = {
    "lr_v1.0": {"type": "logistic_regression", "version": "1.0.0", "is_active": False,
                "accuracy": 0.89, "f1": 0.87, "artifact_path": "models/lr_v1.pkl"},
    "svm_v1.0": {"type": "svm", "version": "1.0.0", "is_active": False,
                 "accuracy": 0.91, "f1": 0.90, "artifact_path": "models/svm_v1.pkl"},
    "rf_v1.0": {"type": "random_forest", "version": "1.0.0", "is_active": False,
                "accuracy": 0.94, "f1": 0.93, "artifact_path": "models/rf_v1.pkl"},
    "ensemble_v1.0": {"type": "ensemble", "version": "1.0.0", "is_active": True,
                      "accuracy": 0.96, "f1": 0.95, "artifact_path": "models/ensemble_v1.pkl"},
    # BUG 2: This model has a None artifact path
    "ensemble_v2.0": {"type": "ensemble", "version": "2.0.0", "is_active": False,
                      "accuracy": None, "f1": None, "artifact_path": None},
}

PREDICTIONS_LOG = []
# BUG 3: prediction cache that never gets cleared — memory leak
_prediction_cache = {}
_training_lock = Lock()
_training_in_progress = False


# ── OTLP Span Collector ──
class SpanCollector:
    """Collects spans and sends them as OTLP/HTTP JSON."""

    def __init__(self, service_name, endpoint):
        self.service = service_name
        self.endpoint = endpoint
        self.spans = []

    def _new_id(self, n=16):
        return struct.pack(f">{n}B", *[random.randint(0, 255) for _ in range(n)]).hex()

    def start_span(self, name, parent=None, attributes=None):
        span = {
            "trace_id": parent["trace_id"] if parent else self._new_id(16),
            "span_id": self._new_id(8),
            "parent_span_id": parent["span_id"] if parent else "",
            "name": name,
            "service": self.service,
            "start_ns": time.time_ns(),
            "attributes": attributes or {},
            "status": "OK",
            "error": None,
        }
        return span

    def end_span(self, span, error=None):
        span["end_ns"] = time.time_ns()
        span["duration_ms"] = (span["end_ns"] - span["start_ns"]) / 1e6
        if error:
            span["status"] = "ERROR"
            span["error"] = str(error)[:200]
        self.spans.append(span)

    def flush(self):
        if not self.spans:
            return
        resource_spans = {
            "resourceSpans": [{
                "resource": {"attributes": [
                    {"key": "service.name", "value": {"stringValue": self.service}}
                ]},
                "scopeSpans": [{
                    "scope": {"name": "pharmasight.tracing"},
                    "spans": [self._to_otlp(s) for s in self.spans],
                }],
            }]
        }
        try:
            req = Request(
                f"{self.endpoint}/v1/traces",
                data=json.dumps(resource_spans).encode(),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            urlopen(req, timeout=2)
        except (URLError, OSError):
            pass
        self.spans.clear()

    def _to_otlp(self, s):
        attrs = [{"key": k, "value": {"stringValue": str(v)}} for k, v in s["attributes"].items()]
        span = {
            "traceId": s["trace_id"],
            "spanId": s["span_id"],
            "name": s["name"],
            "kind": 2,
            "startTimeUnixNano": str(s["start_ns"]),
            "endTimeUnixNano": str(s.get("end_ns", s["start_ns"])),
            "attributes": attrs,
            "status": {"code": 2 if s["status"] == "ERROR" else 1},
        }
        if s["parent_span_id"]:
            span["parentSpanId"] = s["parent_span_id"]
        if s["error"]:
            span["status"]["message"] = s["error"]
            span["events"] = [{
                "name": "exception",
                "timeUnixNano": str(s.get("end_ns", s["start_ns"])),
                "attributes": [
                    {"key": "exception.message", "value": {"stringValue": s["error"]}},
                ],
            }]
        return span


collector = SpanCollector(SERVICE_NAME, OTLP_ENDPOINT)


# ── Simulated Operations ──

def db_query(parent, sql, params=None, delay_range=(0.005, 0.02)):
    """Simulate a database query."""
    span = collector.start_span("db.query", parent=parent, attributes={
        "db.system": "postgresql",
        "db.statement": sql,
        "db.params": str(params or {}),
    })
    time.sleep(random.uniform(*delay_range))
    collector.end_span(span)
    return span


def load_model_artifact(parent, model_id):
    """Load ML model from disk (simulated)."""
    model = MODELS_DB.get(model_id)
    span = collector.start_span("ml.load_model", parent=parent, attributes={
        "ml.model_id": model_id,
        "ml.model_type": model["type"] if model else "unknown",
        "ml.artifact_path": str(model.get("artifact_path")) if model else "None",
    })
    time.sleep(random.uniform(0.01, 0.03))

    # BUG 2: model with None artifact path crashes
    if model and model["artifact_path"] is None:
        collector.end_span(span, error="FileNotFoundError: Model artifact path is None — model was registered but never trained")
        return None, span

    collector.end_span(span)
    return model, span


def feature_engineering(parent, patient_data):
    """Preprocess patient features for inference."""
    span = collector.start_span("ml.feature_engineering", parent=parent, attributes={
        "ml.input_features": str(list(patient_data.keys())),
        "ml.pipeline": "preprocessing_v1",
    })

    # One-hot encode categoricals
    encode_span = collector.start_span("ml.encode_categoricals", parent=span, attributes={
        "ml.encoder": "one_hot",
        "ml.categorical_features": "Sex,BP,Cholesterol",
    })
    time.sleep(random.uniform(0.005, 0.015))
    collector.end_span(encode_span)

    # Bin continuous features
    bin_span = collector.start_span("ml.bin_continuous", parent=span, attributes={
        "ml.continuous_features": "Age,Na_to_K",
        "ml.bin_strategy": "quantile",
    })
    time.sleep(random.uniform(0.003, 0.010))
    collector.end_span(bin_span)

    collector.end_span(span)
    return [0.5] * 10  # Simulated feature vector


def ml_predict(parent, model, features):
    """Run model inference."""
    span = collector.start_span("ml.predict", parent=parent, attributes={
        "ml.model_type": model["type"],
        "ml.model_version": model["version"],
        "ml.feature_count": str(len(features)),
    })
    time.sleep(random.uniform(0.008, 0.025))

    # Generate probabilities
    probs = {drug: random.random() for drug in DRUG_CLASSES}
    total = sum(probs.values())
    probs = {k: round(v / total, 4) for k, v in probs.items()}
    predicted = max(probs, key=probs.get)
    confidence = probs[predicted]

    span["attributes"]["ml.predicted_class"] = predicted
    span["attributes"]["ml.confidence"] = str(round(confidence, 3))
    collector.end_span(span)
    return predicted, confidence, probs


def smote_resample(parent, data_size):
    """BUG 5: Redundant SMOTE re-fitting on every training batch."""
    span = collector.start_span("ml.smote_resample", parent=parent, attributes={
        "ml.original_size": str(data_size),
        "ml.strategy": "SMOTE",
        "ml.k_neighbors": "5",
    })
    # This should be cached but re-fits every time — slow
    time.sleep(random.uniform(0.08, 0.15))
    new_size = int(data_size * 1.3)
    span["attributes"]["ml.resampled_size"] = str(new_size)
    collector.end_span(span)
    return new_size


# ── API Handlers ──

def handle_predict(body, collector):
    """POST /api/v1/predict — Drug classification inference."""
    root = collector.start_span("POST /api/v1/predict", attributes={
        "http.method": "POST",
        "http.route": "/api/v1/predict",
        "http.url": "http://localhost:8000/api/v1/predict",
    })

    try:
        patient = json.loads(body) if isinstance(body, (str, bytes)) else body

        # Validate input
        validate_span = collector.start_span("schema.validate", parent=root, attributes={
            "validation.schema": "PredictionRequest",
        })
        required = ["Age", "Sex", "BP", "Cholesterol", "Na_to_K"]
        missing = [f for f in required if f not in patient]
        if missing:
            collector.end_span(validate_span, error=f"ValidationError: missing fields {missing}")
            collector.end_span(root, error=f"ValidationError: missing fields {missing}")
            return 422, {"error": f"Missing fields: {missing}"}
        time.sleep(0.003)
        collector.end_span(validate_span)

        # Load active model
        active_model_id = next((k for k, v in MODELS_DB.items() if v["is_active"]), None)
        model, _ = load_model_artifact(root, active_model_id)

        # Feature engineering
        features = feature_engineering(root, patient)

        # Predict
        predicted, confidence, probs = ml_predict(root, model, features)

        # Persist prediction
        persist_span = collector.start_span("db.persist_prediction", parent=root, attributes={
            "db.system": "postgresql",
            "db.statement": "INSERT INTO predictions (model_id, input_payload, predicted_class, confidence) VALUES (%s, %s, %s, %s)",
        })
        time.sleep(random.uniform(0.005, 0.015))

        # BUG 3: Cache grows forever
        cache_key = json.dumps(patient, sort_keys=True)
        _prediction_cache[cache_key] = {
            "predicted": predicted, "confidence": confidence,
            "timestamp": time.time(), "features": features,
        }
        persist_span["attributes"]["cache.size"] = str(len(_prediction_cache))
        collector.end_span(persist_span)

        collector.end_span(root)
        return 200, {
            "predicted_drug": predicted,
            "confidence": confidence,
            "probabilities": probs,
            "model_type": model["type"],
            "model_version": model["version"],
        }

    except Exception as e:
        collector.end_span(root, error=str(e))
        return 500, {"error": str(e)}


def handle_get_models(collector):
    """GET /api/v1/models — Model registry with N+1 query bug."""
    root = collector.start_span("GET /api/v1/models", attributes={
        "http.method": "GET",
        "http.route": "/api/v1/models",
        "http.url": "http://localhost:8000/api/v1/models",
    })

    # List all models
    list_span = collector.start_span("db.list_models", parent=root, attributes={
        "db.system": "postgresql",
        "db.statement": "SELECT * FROM model_versions ORDER BY created_at DESC",
    })
    time.sleep(random.uniform(0.005, 0.015))
    collector.end_span(list_span)

    models = []
    # BUG 1: N+1 query — fetching metrics for each model individually
    for model_id, model in MODELS_DB.items():
        metrics_span = collector.start_span("db.fetch_metrics", parent=root, attributes={
            "db.system": "postgresql",
            "db.statement": f"SELECT * FROM metrics WHERE model_id = '{model_id}'",
            "db.model_id": model_id,
        })
        time.sleep(random.uniform(0.010, 0.025))  # Each query is slow
        collector.end_span(metrics_span)

        models.append({
            "id": model_id,
            "type": model["type"],
            "version": model["version"],
            "is_active": model["is_active"],
            "accuracy": model["accuracy"],
            "f1": model["f1"],
        })

    collector.end_span(root)
    return 200, {"models": models, "total": len(models)}


def handle_train(body, collector):
    """POST /api/v1/models/train — Training with race condition."""
    global _training_in_progress
    root = collector.start_span("POST /api/v1/models/train", attributes={
        "http.method": "POST",
        "http.route": "/api/v1/models/train",
        "http.url": "http://localhost:8000/api/v1/models/train",
    })

    # BUG 4: Race condition — no proper locking
    if _training_in_progress:
        collector.end_span(root, error="ConflictError: Training already in progress — concurrent training may corrupt model artifacts")
        return 409, {"error": "Training already in progress"}

    _training_in_progress = True
    try:
        # Load dataset
        load_span = collector.start_span("data.load_dataset", parent=root, attributes={
            "db.system": "postgresql",
            "db.statement": "SELECT * FROM drug_data",
            "data.source": "drug200.csv",
        })
        time.sleep(random.uniform(0.02, 0.05))
        data_size = 200
        collector.end_span(load_span)

        # BUG 5: Redundant SMOTE re-fitting
        data_size = smote_resample(root, data_size)

        # Feature engineering for training
        feat_span = collector.start_span("ml.feature_engineering", parent=root, attributes={
            "ml.pipeline": "training_preprocessing",
            "ml.data_size": str(data_size),
        })
        time.sleep(random.uniform(0.03, 0.06))
        collector.end_span(feat_span)

        # Train models
        for model_type in ["logistic_regression", "svm", "random_forest"]:
            train_span = collector.start_span(f"ml.train.{model_type}", parent=root, attributes={
                "ml.model_type": model_type,
                "ml.training_samples": str(data_size),
                "ml.hyperparameter_search": "GridSearchCV",
            })
            time.sleep(random.uniform(0.04, 0.08))
            collector.end_span(train_span)

        # Train ensemble
        ensemble_span = collector.start_span("ml.train.ensemble", parent=root, attributes={
            "ml.model_type": "soft_voting_ensemble",
            "ml.base_models": "lr,svm,rf",
        })
        time.sleep(random.uniform(0.03, 0.06))
        collector.end_span(ensemble_span)

        # Save models
        save_span = collector.start_span("ml.save_artifacts", parent=root, attributes={
            "ml.artifact_dir": "models/",
            "ml.models_saved": "4",
        })
        time.sleep(random.uniform(0.01, 0.03))
        collector.end_span(save_span)

        collector.end_span(root)
        return 200, {"status": "training_complete", "models_trained": 4}

    except Exception as e:
        collector.end_span(root, error=str(e))
        return 500, {"error": str(e)}
    finally:
        _training_in_progress = False


def handle_get_model_v2(collector):
    """GET /api/v1/models/ensemble_v2.0/metrics — triggers None artifact bug."""
    root = collector.start_span("GET /api/v1/models/ensemble_v2.0/metrics", attributes={
        "http.method": "GET",
        "http.route": "/api/v1/models/:id/metrics",
        "http.url": "http://localhost:8000/api/v1/models/ensemble_v2.0/metrics",
    })

    model, _ = load_model_artifact(root, "ensemble_v2.0")
    if model is None:
        collector.end_span(root, error="ModelNotFoundError: ensemble_v2.0 artifact not available")
        return 404, {"error": "Model artifact not found"}

    # This would load metrics but model is None
    db_query(root, "SELECT * FROM metrics WHERE model_id = 'ensemble_v2.0'")

    collector.end_span(root)
    return 200, {"model_id": "ensemble_v2.0", "metrics": model}


def handle_data_summary(collector):
    """GET /api/v1/data/summary — Dataset statistics."""
    root = collector.start_span("GET /api/v1/data/summary", attributes={
        "http.method": "GET",
        "http.route": "/api/v1/data/summary",
        "http.url": "http://localhost:8000/api/v1/data/summary",
    })

    db_query(root, "SELECT COUNT(*) FROM drug_data")
    db_query(root, "SELECT drug, COUNT(*) FROM drug_data GROUP BY drug")
    db_query(root, "SELECT AVG(age), AVG(na_to_k) FROM drug_data")

    collector.end_span(root)
    return 200, {
        "total_records": 200,
        "drug_distribution": {d: random.randint(20, 60) for d in DRUG_CLASSES},
        "avg_age": 44.3,
        "avg_na_to_k": 13.7,
    }


# ── HTTP Server ──

class PharmaSightHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        print(f"  {args[0]}  [{args[1]}]")

    def _respond(self, status, data):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(data, indent=2).encode())

    def do_GET(self):
        if self.path == "/api/v1/models":
            status, data = handle_get_models(collector)
        elif self.path == "/api/v1/models/ensemble_v2.0/metrics":
            status, data = handle_get_model_v2(collector)
        elif self.path == "/api/v1/data/summary":
            status, data = handle_data_summary(collector)
        elif self.path == "/healthz":
            status, data = 200, {"status": "healthy", "cache_size": len(_prediction_cache)}
        else:
            status, data = 404, {"error": "Not found"}
        self._respond(status, data)
        collector.flush()

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length) if length else b"{}"
        if self.path == "/api/v1/predict":
            status, data = handle_predict(body, collector)
        elif self.path == "/api/v1/models/train":
            status, data = handle_train(body, collector)
        else:
            status, data = 404, {"error": "Not found"}
        self._respond(status, data)
        collector.flush()


def main():
    print(f"""
{'━' * 60}
  PharmaSight OTLP Demo — Drug Classification API
{'━' * 60}

  Endpoints:
    POST /api/v1/predict              Drug prediction (cache leak)
    GET  /api/v1/models               Model registry (N+1 bug)
    POST /api/v1/models/train         Train models (race condition)
    GET  /api/v1/models/ensemble_v2.0/metrics
                                      Get v2.0 metrics (None artifact)
    GET  /api/v1/data/summary         Dataset stats
    GET  /healthz                     Health check

  Traces sent to: {OTLP_ENDPOINT}/v1/traces

  Try these:
    curl http://localhost:{PORT}/api/v1/models
    curl -X POST http://localhost:{PORT}/api/v1/predict \\
         -H 'Content-Type: application/json' \\
         -d '{{"Age":45,"Sex":"M","BP":"HIGH","Cholesterol":"NORMAL","Na_to_K":15.5}}'
    curl http://localhost:{PORT}/api/v1/models/ensemble_v2.0/metrics
    curl -X POST http://localhost:{PORT}/api/v1/models/train
    curl -X POST {OTLP_ENDPOINT}/v1/analyze | python3 -m json.tool

{'━' * 60}
  Listening on http://localhost:{PORT}
{'━' * 60}
""")
    server = HTTPServer(("0.0.0.0", PORT), PharmaSightHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n  Shutting down.")
        server.shutdown()


if __name__ == "__main__":
    main()
