"""
live_demo_app.py - A real FastAPI app for testing Inferra's OTLP mode.

Run:
  1. Terminal 1: inferra serve --port 4318 --project .
  2. Terminal 2: python live_demo_app.py
  3. Terminal 3: python hit_live_demo.py
  4. Then: curl -X POST http://localhost:4318/v1/analyze
"""

import time
import random
from fastapi import FastAPI, HTTPException
import uvicorn

# ── OpenTelemetry Setup ──────────────────────────────────────────────────────
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.sdk.resources import Resource
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

resource = Resource.create({"service.name": "live-demo-api"})
provider = TracerProvider(resource=resource)
exporter = OTLPSpanExporter(endpoint="http://localhost:4318/v1/traces")
provider.add_span_processor(BatchSpanProcessor(exporter))
trace.set_tracer_provider(provider)

tracer = trace.get_tracer("live-demo")

# ── App ──────────────────────────────────────────────────────────────────────
app = FastAPI(title="Live Demo API")

# In-memory "database"
articles_db = {}
users_db = {
    "admin": {"username": "admin", "email": "admin@demo.com", "bio": "Admin user"},
}


def validate_token(token: str) -> dict:
    """Validate JWT token and return user info."""
    time.sleep(0.02)
    if token == "invalid":
        raise HTTPException(status_code=401, detail="Invalid token")
    return users_db.get("admin", {})


def fetch_from_external_api(url: str) -> dict:
    """Simulate calling an external API (slow)."""
    time.sleep(random.uniform(0.3, 0.8))
    return {"status": "ok", "data": {"source": url}}


def enrich_content(content: str) -> str:
    """Enrich content with metadata."""
    time.sleep(0.05)
    words = content.split()
    return f"[enriched:{len(words)}w] {content}"


def save_to_database(collection: str, data: dict) -> str:
    """Simulate database write."""
    time.sleep(random.uniform(0.1, 0.3))
    doc_id = f"{collection}_{len(articles_db) + 1}"
    articles_db[doc_id] = data
    return doc_id


def run_sentiment_analysis(text: str) -> dict:
    """Simulate ML model inference."""
    time.sleep(random.uniform(0.2, 0.5))
    return {"score": round(random.uniform(0.5, 1.0), 2), "label": "positive"}


def check_spam(text: str) -> bool:
    """Check content for spam patterns."""
    time.sleep(0.03)
    spam_words = ["buy now", "free money", "click here"]
    return any(w in text.lower() for w in spam_words)


# ── Routes ───────────────────────────────────────────────────────────────────

@app.get("/api/articles")
async def list_articles():
    with tracer.start_as_current_span("list_articles"):
        return {"articles": list(articles_db.values()), "count": len(articles_db)}


@app.post("/api/articles")
async def create_article(title: str = "Demo Article", body: str = "This is a demo article for testing."):
    with tracer.start_as_current_span("create_article", attributes={"code.function": "create_article"}) as span:
        span.set_attribute("article.title", title)

        with tracer.start_as_current_span("validate_token", attributes={"code.function": "validate_token"}):
            user = validate_token("valid")

        with tracer.start_as_current_span("check_spam", attributes={"code.function": "check_spam"}):
            is_spam = check_spam(body)
            if is_spam:
                raise HTTPException(status_code=400, detail="Spam detected")

        with tracer.start_as_current_span("enrich_content", attributes={"code.function": "enrich_content"}):
            enriched = enrich_content(body)

        # SLOW: These 2 calls are sequential but could be parallel
        with tracer.start_as_current_span("run_sentiment_analysis", attributes={"code.function": "run_sentiment_analysis"}):
            sentiment = run_sentiment_analysis(body)

        with tracer.start_as_current_span("fetch_external_recommendations", attributes={"code.function": "fetch_from_external_api"}):
            recs = fetch_from_external_api("https://api.recommendations.io/related")

        with tracer.start_as_current_span("save_to_database", attributes={"code.function": "save_to_database"}):
            doc_id = save_to_database("articles", {
                "title": title,
                "body": enriched,
                "author": user.get("username"),
                "sentiment": sentiment,
            })

        return {"id": doc_id, "title": title, "status": "created"}


@app.get("/api/articles/{article_id}")
async def get_article(article_id: str):
    with tracer.start_as_current_span("get_article") as span:
        span.set_attribute("article.id", article_id)
        with tracer.start_as_current_span("db_lookup"):
            time.sleep(0.05)
            article = articles_db.get(article_id)
        if not article:
            raise HTTPException(status_code=404, detail="Article not found")
        return article


@app.get("/api/users/{username}")
async def get_user(username: str):
    with tracer.start_as_current_span("get_user") as span:
        span.set_attribute("user.username", username)
        with tracer.start_as_current_span("validate_token", attributes={"code.function": "validate_token"}):
            validate_token("valid")
        with tracer.start_as_current_span("db_lookup_user"):
            time.sleep(0.08)
            user = users_db.get(username)
        if not user:
            raise HTTPException(status_code=404, detail="User not found")
        return user


@app.get("/healthz")
async def health():
    return {"status": "ok"}


if __name__ == "__main__":
    print("\n  Live Demo API starting on http://localhost:8000")
    print("  Sending traces to http://localhost:4318 (Inferra)\n")
    uvicorn.run(app, host="0.0.0.0", port=8000, log_level="info")
