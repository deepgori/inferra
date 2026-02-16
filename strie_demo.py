#!/usr/bin/env python3
"""
STRIE OTLP Demo — Spatial-Temporal Risk Intelligence Engine
============================================================
Simulates the STRIE API and analytics pipeline with intentional bugs,
sending OTLP traces to the Inferra receiver for analysis.

Bugs planted:
  1. N+1 query in event retrieval (per-event geocoding)
  2. H3 resolution mismatch causing silent data loss
  3. Risk score division by zero when rolling average is 0
  4. Unbounded tile cache in Redis (no TTL set)
  5. Missing transaction isolation in concurrent analytics runs
"""

import json, time, random, os, struct, math
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.request import Request, urlopen
from urllib.error import URLError

OTLP_ENDPOINT = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4318")
SERVICE_NAME = "strie-api"
PORT = 8000

# ── Simulated Data ──
EVENT_TYPES = ["wildfire", "crime", "flood", "power_outage", "accident"]
H3_CELLS = [f"872830{i:03d}ffff" for i in range(15)]
RISK_LEVELS = ["low", "medium", "high", "critical"]
_tile_cache = {}  # BUG 4: grows forever


# ── OTLP Span Collector ──
class SpanCollector:
    def __init__(self, service, endpoint):
        self.service, self.endpoint, self.spans = service, endpoint, []

    def _id(self, n=16):
        return struct.pack(f">{n}B", *[random.randint(0, 255) for _ in range(n)]).hex()

    def start(self, name, parent=None, attrs=None):
        return {"trace_id": parent["trace_id"] if parent else self._id(16),
                "span_id": self._id(8),
                "parent_span_id": parent["span_id"] if parent else "",
                "name": name, "start_ns": time.time_ns(),
                "attributes": attrs or {}, "status": "OK", "error": None}

    def end(self, s, error=None):
        s["end_ns"] = time.time_ns()
        s["duration_ms"] = (s["end_ns"] - s["start_ns"]) / 1e6
        if error:
            s["status"], s["error"] = "ERROR", str(error)[:200]
        self.spans.append(s)

    def flush(self):
        if not self.spans:
            return
        payload = {"resourceSpans": [{"resource": {"attributes": [
            {"key": "service.name", "value": {"stringValue": self.service}}]},
            "scopeSpans": [{"scope": {"name": "strie.tracing"},
                "spans": [self._otlp(s) for s in self.spans]}]}]}
        try:
            req = Request(f"{self.endpoint}/v1/traces",
                          data=json.dumps(payload).encode(),
                          headers={"Content-Type": "application/json"}, method="POST")
            urlopen(req, timeout=2)
        except (URLError, OSError):
            pass
        self.spans.clear()

    def _otlp(self, s):
        span = {"traceId": s["trace_id"], "spanId": s["span_id"], "name": s["name"],
                "kind": 2, "startTimeUnixNano": str(s["start_ns"]),
                "endTimeUnixNano": str(s.get("end_ns", s["start_ns"])),
                "attributes": [{"key": k, "value": {"stringValue": str(v)}} for k, v in s["attributes"].items()],
                "status": {"code": 2 if s["status"] == "ERROR" else 1}}
        if s["parent_span_id"]:
            span["parentSpanId"] = s["parent_span_id"]
        if s["error"]:
            span["status"]["message"] = s["error"]
            span["events"] = [{"name": "exception", "timeUnixNano": str(s.get("end_ns", s["start_ns"])),
                "attributes": [{"key": "exception.message", "value": {"stringValue": s["error"]}}]}]
        return span


C = SpanCollector(SERVICE_NAME, OTLP_ENDPOINT)


# ── API Handlers ──

def handle_event_upload(body):
    """POST /v1/events/upload — Bulk event ingestion."""
    root = C.start("POST /v1/events/upload", attrs={
        "http.method": "POST", "http.route": "/v1/events/upload",
        "http.url": "http://localhost:8000/v1/events/upload"})
    try:
        events = json.loads(body) if isinstance(body, (str, bytes)) else body
        if not isinstance(events, list):
            events = [events]

        # Validate
        v = C.start("schema.validate_events", parent=root, attrs={"event.count": str(len(events))})
        time.sleep(0.005)
        C.end(v)

        # Insert with PostGIS point creation
        ins = C.start("db.bulk_insert_events", parent=root, attrs={
            "db.system": "postgresql", "db.name": "strie",
            "db.statement": "INSERT INTO events (event_type, event_timestamp, geom, attributes_json) VALUES ($1, $2, ST_SetSRID(ST_MakePoint($3, $4), 4326), $5)",
            "event.count": str(len(events))})
        time.sleep(random.uniform(0.02, 0.05))
        C.end(ins)

        C.end(root)
        return 201, {"ingested": len(events)}
    except Exception as e:
        C.end(root, error=str(e))
        return 500, {"error": str(e)}


def handle_get_events():
    """GET /v1/events — Event retrieval with N+1 geocoding bug."""
    root = C.start("GET /v1/events", attrs={
        "http.method": "GET", "http.route": "/v1/events",
        "http.url": "http://localhost:8000/v1/events?type=wildfire&limit=20"})

    # Main query
    q = C.start("db.query_events", parent=root, attrs={
        "db.system": "postgresql",
        "db.statement": "SELECT id, event_type, event_timestamp, ST_X(geom), ST_Y(geom) FROM events WHERE event_type = $1 ORDER BY event_timestamp DESC LIMIT 20"})
    time.sleep(random.uniform(0.01, 0.03))
    C.end(q)

    # BUG 1: N+1 — reverse geocoding each event individually
    events = []
    for i in range(8):
        geo = C.start("geocode.reverse_lookup", parent=root, attrs={
            "geocode.lat": str(37.7749 + random.uniform(-0.1, 0.1)),
            "geocode.lon": str(-122.4194 + random.uniform(-0.1, 0.1)),
            "geocode.provider": "nominatim",
            "db.statement": f"SELECT name FROM places WHERE ST_DWithin(geom, ST_MakePoint($1,$2)::geography, 500) LIMIT 1"})
        time.sleep(random.uniform(0.015, 0.035))  # Each geocode is slow
        C.end(geo)
        events.append({"id": i, "type": "wildfire", "location": f"Zone-{i}"})

    C.end(root)
    return 200, {"events": events, "total": len(events)}


def handle_run_analytics():
    """POST /v1/analytics/run — Full analytics pipeline."""
    root = C.start("POST /v1/analytics/run", attrs={
        "http.method": "POST", "http.route": "/v1/analytics/run",
        "http.url": "http://localhost:8000/v1/analytics/run?resolution=8",
        "analytics.resolution": "8"})

    try:
        # Step 1: H3 binning
        h3 = C.start("analytics.h3_binning", parent=root, attrs={
            "h3.resolution": "8", "h3.cell_count": str(len(H3_CELLS)),
            "db.statement": "SELECT h3_index, COUNT(*) FROM events WHERE event_timestamp >= $1 GROUP BY h3_index"})
        time.sleep(random.uniform(0.03, 0.06))

        # BUG 2: resolution mismatch — query uses res 8 but index is res 7
        h3["attributes"]["h3.warning"] = "Resolution mismatch: query=8, index=7 — some events not binned"
        C.end(h3)

        # Step 2: Daily aggregation
        agg = C.start("analytics.daily_aggregation", parent=root, attrs={
            "db.system": "postgresql",
            "db.statement": "INSERT INTO cell_aggregates (h3_index, agg_date, event_count) SELECT h3_index, date_trunc('day', event_timestamp), COUNT(*) FROM events GROUP BY 1, 2 ON CONFLICT UPDATE"})
        time.sleep(random.uniform(0.02, 0.04))
        C.end(agg)

        # Step 3: Rolling mean
        roll = C.start("analytics.rolling_mean_7d", parent=root, attrs={
            "db.statement": "UPDATE cell_aggregates SET rolling_7d_avg = (SELECT AVG(event_count) FROM cell_aggregates c2 WHERE c2.h3_index = cell_aggregates.h3_index AND c2.agg_date BETWEEN cell_aggregates.agg_date - 7 AND cell_aggregates.agg_date)"})
        time.sleep(random.uniform(0.03, 0.05))
        C.end(roll)

        # Step 4: Growth rate
        growth = C.start("analytics.compute_growth_rate", parent=root, attrs={
            "db.statement": "UPDATE cell_aggregates SET growth_rate = (event_count - rolling_7d_avg) / rolling_7d_avg"})
        time.sleep(random.uniform(0.01, 0.03))

        # BUG 3: Division by zero when rolling_7d_avg is 0 for new cells
        if random.random() < 0.3:
            C.end(growth, error="ZeroDivisionError: division by zero — rolling_7d_avg is 0 for newly observed H3 cells")
        else:
            C.end(growth)

        # Step 5: Risk scoring
        risk = C.start("analytics.compute_risk_scores", parent=root, attrs={
            "risk.formula": "event_count*0.5 + growth_rate*0.3 + rolling_7d_avg*0.2",
            "risk.normalization": "min_max_0_100",
            "db.statement": "INSERT INTO risk_scores (h3_index, score_date, risk_score, risk_level) SELECT h3_index, agg_date, normalized_score, CASE WHEN score <= 25 THEN 'low' ... END"})
        time.sleep(random.uniform(0.02, 0.04))
        C.end(risk)

        # Step 6: Anomaly detection
        anom = C.start("analytics.anomaly_detection", parent=root, attrs={
            "anomaly.method": "z_score", "anomaly.threshold": "2.0",
            "db.statement": "INSERT INTO anomaly_flags (h3_index, flag_date, z_score, flagged) SELECT h3_index, agg_date, z, z >= 2.0"})
        time.sleep(random.uniform(0.02, 0.04))
        flagged = random.randint(1, 5)
        anom["attributes"]["anomaly.flagged_cells"] = str(flagged)
        C.end(anom)

        # Step 7: Refresh materialized view
        mv = C.start("analytics.refresh_mv_daily_risk", parent=root, attrs={
            "db.system": "postgresql",
            "db.statement": "REFRESH MATERIALIZED VIEW CONCURRENTLY mv_daily_risk"})
        time.sleep(random.uniform(0.05, 0.10))
        C.end(mv)

        C.end(root)
        return 200, {"status": "pipeline_complete", "cells_processed": len(H3_CELLS), "anomalies_flagged": flagged}

    except Exception as e:
        C.end(root, error=str(e))
        return 500, {"error": str(e)}


def handle_get_risk(date_str):
    """GET /v1/risk/{date} — Risk scores for a date."""
    root = C.start(f"GET /v1/risk/{date_str}", attrs={
        "http.method": "GET", "http.route": "/v1/risk/:date",
        "http.url": f"http://localhost:8000/v1/risk/{date_str}"})

    q = C.start("db.query_risk_scores", parent=root, attrs={
        "db.system": "postgresql",
        "db.statement": f"SELECT h3_index, risk_score, risk_level FROM risk_scores WHERE score_date = '{date_str}' ORDER BY risk_score DESC"})
    time.sleep(random.uniform(0.01, 0.02))
    C.end(q)

    scores = [{"h3": c, "risk_score": random.randint(10, 95),
               "risk_level": random.choice(RISK_LEVELS)} for c in H3_CELLS[:8]]

    C.end(root)
    return 200, {"date": date_str, "risk_scores": scores}


def handle_vector_tile(z, x, y):
    """GET /v1/tiles/{z}/{x}/{y}.mvt — PostGIS vector tiles."""
    root = C.start(f"GET /v1/tiles/{z}/{x}/{y}.mvt", attrs={
        "http.method": "GET", "http.route": "/v1/tiles/:z/:x/:y.mvt",
        "tile.z": str(z), "tile.x": str(x), "tile.y": str(y)})

    # Check Redis cache
    cache_key = f"tile:{z}:{x}:{y}"
    cache = C.start("redis.get_tile_cache", parent=root, attrs={
        "db.system": "redis", "db.statement": f"GET {cache_key}"})
    time.sleep(0.003)
    hit = cache_key in _tile_cache
    cache["attributes"]["cache.hit"] = str(hit)
    C.end(cache)

    if not hit:
        # Generate tile from PostGIS
        tile_q = C.start("db.generate_vector_tile", parent=root, attrs={
            "db.system": "postgresql",
            "db.statement": f"SELECT ST_AsMVT(q, 'risk_layer') FROM (SELECT ST_AsMVTGeom(ST_Transform(geom, 3857), ST_TileEnvelope({z},{x},{y})) AS geom, risk_score, risk_level FROM mv_daily_risk WHERE ST_Intersects(geom, ST_Transform(ST_TileEnvelope({z},{x},{y}), 4326))) q"})
        time.sleep(random.uniform(0.02, 0.06))
        C.end(tile_q)

        # BUG 4: Cache with no TTL — never evicted
        _tile_cache[cache_key] = b"fake_mvt_data"
        set_cache = C.start("redis.set_tile_cache", parent=root, attrs={
            "db.system": "redis",
            "db.statement": f"SET {cache_key} <mvt_bytes>",
            "cache.total_entries": str(len(_tile_cache)),
            "cache.warning": "No TTL set — cache grows unbounded"})
        time.sleep(0.002)
        C.end(set_cache)

    C.end(root)
    return 200, {"tile": f"{z}/{x}/{y}", "cached": hit}


def handle_hotspots():
    """GET /v1/hotspots — Emerging hotspot feed."""
    root = C.start("GET /v1/hotspots", attrs={
        "http.method": "GET", "http.route": "/v1/hotspots",
        "http.url": "http://localhost:8000/v1/hotspots?start_date=2026-03-01&end_date=2026-03-07"})

    q = C.start("db.query_hotspots", parent=root, attrs={
        "db.system": "postgresql",
        "db.statement": "SELECT h3_index, risk_score, growth_rate, flagged FROM mv_daily_risk WHERE risk_level IN ('high', 'critical') AND flagged = true ORDER BY risk_score DESC LIMIT 10"})
    time.sleep(random.uniform(0.01, 0.03))
    C.end(q)

    hotspots = [{"h3": random.choice(H3_CELLS),
                 "risk_score": random.randint(60, 98),
                 "growth_rate": round(random.uniform(0.5, 3.0), 2),
                 "event_type": random.choice(EVENT_TYPES)} for _ in range(random.randint(2, 6))]

    C.end(root)
    return 200, {"hotspots": hotspots}


# ── HTTP Server ──

class STRIEHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        print(f"  {args[0]}  [{args[1]}]")

    def _respond(self, status, data, content_type="application/json"):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.end_headers()
        self.wfile.write(json.dumps(data, indent=2).encode())

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/v1/events":
            s, d = handle_get_events()
        elif path.startswith("/v1/risk/"):
            date_str = path.split("/")[-1]
            s, d = handle_get_risk(date_str)
        elif path.endswith(".mvt"):
            parts = path.replace("/v1/tiles/", "").replace(".mvt", "").split("/")
            s, d = handle_vector_tile(int(parts[0]), int(parts[1]), int(parts[2]))
        elif path == "/v1/hotspots":
            s, d = handle_hotspots()
        elif path in ("/v1/health/live", "/v1/health/ready", "/healthz"):
            s, d = 200, {"status": "ok", "tile_cache_size": len(_tile_cache)}
        else:
            s, d = 404, {"error": "Not found"}
        self._respond(s, d)
        C.flush()

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length) if length else b"[]"
        path = self.path.split("?")[0]
        if path == "/v1/events/upload":
            s, d = handle_event_upload(body)
        elif path == "/v1/analytics/run":
            s, d = handle_run_analytics()
        else:
            s, d = 404, {"error": "Not found"}
        self._respond(s, d)
        C.flush()


def main():
    print(f"""
{'━' * 60}
  STRIE OTLP Demo — Risk Intelligence Engine
{'━' * 60}

  API Endpoints:
    POST /v1/events/upload         Bulk event ingestion
    GET  /v1/events                Event query (N+1 geocoding)
    POST /v1/analytics/run         Analytics pipeline (div/0 bug)
    GET  /v1/risk/2026-03-07       Risk scores by date
    GET  /v1/tiles/8/40/98.mvt     Vector tiles (cache leak)
    GET  /v1/hotspots              Emerging hotspot feed
    GET  /v1/health/live           Health check

  Traces → {OTLP_ENDPOINT}/v1/traces

{'━' * 60}
  Listening on http://localhost:{PORT}
{'━' * 60}
""")
    HTTPServer(("0.0.0.0", PORT), STRIEHandler).serve_forever()


if __name__ == "__main__":
    main()
