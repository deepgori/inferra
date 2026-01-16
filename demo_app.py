#!/usr/bin/env python3
"""
demo_app.py — Instrumented Demo Service with Intentional Bugs

A realistic order-processing microservice with real bugs:
  - N+1 database queries
  - Unhandled None dereference  
  - Timeout on external payment call
  - Race condition in inventory update

Every request auto-sends OTLP spans to the Inferra receiver.

Usage:
    # Terminal 1: Start inferra receiver
    python -m inferra serve --port 4318

    # Terminal 2: Start this demo app
    python demo_app.py

    # Terminal 3: Hit endpoints to generate traces
    curl http://localhost:8000/api/orders
    curl http://localhost:8000/api/orders/42
    curl -X POST http://localhost:8000/api/orders/checkout

    # Terminal 3: Analyze collected traces
    curl -X POST http://localhost:4318/v1/analyze | python3 -m json.tool
"""

import json
import time
import random
import threading
import uuid
import urllib.request
from http.server import HTTPServer, BaseHTTPRequestHandler

# ── Config ────────────────────────────────────────────────────────────

OTLP_ENDPOINT = "http://localhost:4318/v1/traces"
SERVICE_NAME = "order-service"
PORT = 8000

# ── Fake Database ─────────────────────────────────────────────────────

_db_lock = threading.Lock()
_inventory = {"SKU-001": 10, "SKU-002": 5, "SKU-003": 0}

ORDERS = [
    {"id": 1, "user_id": 101, "items": ["SKU-001", "SKU-002"], "total": 79.99, "status": "confirmed"},
    {"id": 2, "user_id": 102, "items": ["SKU-001"], "total": 29.99, "status": "pending"},
    {"id": 3, "user_id": None, "items": ["SKU-003"], "total": 49.99, "status": "pending"},  # BUG: None user
    {"id": 4, "user_id": 104, "items": ["SKU-001", "SKU-001", "SKU-002"], "total": 139.97, "status": "confirmed"},
    {"id": 5, "user_id": 105, "items": [], "total": 0.00, "status": "cancelled"},
]

USERS = {
    101: {"name": "Alice", "email": "alice@example.com", "tier": "premium"},
    102: {"name": "Bob", "email": "bob@example.com", "tier": "basic"},
    104: {"name": "Diana", "email": "diana@example.com", "tier": "premium"},
    105: {"name": "Eve", "email": "eve@example.com", "tier": "basic"},
}


# ── Span Collector & OTLP Sender ─────────────────────────────────────

class SpanCollector:
    """Collects spans during a request and flushes to OTLP endpoint."""

    def __init__(self, trace_id=None):
        self.trace_id = trace_id or uuid.uuid4().hex[:32]
        self.spans = []
        self._stack = []

    def start_span(self, name, attributes=None):
        span_id = uuid.uuid4().hex[:16]
        parent_id = self._stack[-1]["spanId"] if self._stack else ""
        span = {
            "traceId": self.trace_id,
            "spanId": span_id,
            "parentSpanId": parent_id,
            "name": name,
            "kind": 1,  # INTERNAL
            "startTimeUnixNano": str(int(time.time() * 1e9)),
            "endTimeUnixNano": "",
            "status": {"code": 0},
            "attributes": [
                {"key": k, "value": {"stringValue": str(v)}}
                for k, v in (attributes or {}).items()
            ],
            "events": [],
        }
        self._stack.append(span)
        self.spans.append(span)
        return span

    def end_span(self, span, error=None):
        span["endTimeUnixNano"] = str(int(time.time() * 1e9))
        if error:
            span["status"] = {"code": 2, "message": str(error)}
            span["events"].append({
                "name": "exception",
                "timeUnixNano": span["endTimeUnixNano"],
                "attributes": [
                    {"key": "exception.type", "value": {"stringValue": type(error).__name__}},
                    {"key": "exception.message", "value": {"stringValue": str(error)}},
                ],
            })
        if self._stack and self._stack[-1] is span:
            self._stack.pop()

    def flush(self):
        """Send all collected spans to the OTLP receiver."""
        if not self.spans:
            return
        payload = {
            "resourceSpans": [{
                "resource": {
                    "attributes": [
                        {"key": "service.name", "value": {"stringValue": SERVICE_NAME}},
                        {"key": "service.version", "value": {"stringValue": "1.2.0"}},
                    ]
                },
                "scopeSpans": [{
                    "scope": {"name": "demo_app.tracing", "version": "0.1.0"},
                    "spans": self.spans,
                }]
            }]
        }
        try:
            data = json.dumps(payload).encode()
            req = urllib.request.Request(
                OTLP_ENDPOINT, data=data,
                headers={"Content-Type": "application/json"},
            )
            urllib.request.urlopen(req, timeout=2)
        except Exception:
            pass  # Don't crash the app if receiver is down


# ── Simulated Services (with bugs) ───────────────────────────────────

def db_query(collector, query, params=None):
    """Simulate a database query with realistic latency."""
    span = collector.start_span("db.query", {
        "db.system": "postgresql",
        "db.statement": query,
        "db.params": str(params or []),
    })
    time.sleep(random.uniform(0.005, 0.02))  # 5-20ms
    collector.end_span(span)


def fetch_user(collector, user_id):
    """Fetch user info — BUG: crashes on None user_id."""
    span = collector.start_span("fetch_user", {"user_id": str(user_id)})
    try:
        # BUG: No null check — user_id can be None
        db_query(collector, "SELECT * FROM users WHERE id = %s", [user_id])
        user = USERS.get(user_id)
        if user is None:
            raise KeyError(f"User {user_id} not found")
        collector.end_span(span)
        return user
    except Exception as e:
        collector.end_span(span, error=e)
        raise


def check_inventory(collector, sku):
    """Check stock for a SKU."""
    span = collector.start_span("check_inventory", {"sku": sku})
    db_query(collector, "SELECT stock FROM inventory WHERE sku = %s", [sku])
    stock = _inventory.get(sku, 0)
    if stock <= 0:
        err = ValueError(f"Out of stock: {sku}")
        collector.end_span(span, error=err)
        raise err
    collector.end_span(span)
    return stock


def call_payment_gateway(collector, amount, user_id):
    """Simulate external payment API — BUG: random timeouts."""
    span = collector.start_span("payment_gateway.charge", {
        "http.method": "POST",
        "http.url": "https://api.stripe.com/v1/charges",
        "payment.amount": str(amount),
        "payment.currency": "USD",
    })

    # BUG: 40% chance of timeout on payment gateway
    delay = random.uniform(0.05, 0.3)
    if random.random() < 0.4:
        delay = 3.5  # Timeout!
        time.sleep(delay)
        err = TimeoutError(f"Payment gateway timeout after {delay:.1f}s")
        collector.end_span(span, error=err)
        raise err

    time.sleep(delay)
    collector.end_span(span)
    return {"charge_id": f"ch_{uuid.uuid4().hex[:12]}", "status": "succeeded"}


def update_inventory(collector, items):
    """Decrement inventory — BUG: race condition (no lock)."""
    span = collector.start_span("update_inventory", {"items": str(items)})

    # BUG: Not using _db_lock — race condition under concurrent requests
    for sku in items:
        db_query(collector, "UPDATE inventory SET stock = stock - 1 WHERE sku = %s", [sku])
        if sku in _inventory:
            _inventory[sku] -= 1  # No lock!

    collector.end_span(span)


# ── Request Handlers ──────────────────────────────────────────────────

def handle_list_orders(collector):
    """GET /api/orders — BUG: N+1 query pattern."""
    span = collector.start_span("GET /api/orders", {
        "http.method": "GET",
        "http.route": "/api/orders",
    })

    db_query(collector, "SELECT * FROM orders")

    # BUG: N+1 — fetches each user individually instead of batch
    enriched = []
    for order in ORDERS:
        try:
            user = fetch_user(collector, order["user_id"])
            enriched.append({**order, "user_name": user["name"]})
        except (KeyError, TypeError):
            enriched.append({**order, "user_name": "UNKNOWN"})

    collector.end_span(span)
    return 200, enriched


def handle_get_order(collector, order_id):
    """GET /api/orders/:id"""
    span = collector.start_span("GET /api/orders/:id", {
        "http.method": "GET",
        "http.route": "/api/orders/:id",
        "order_id": str(order_id),
    })

    db_query(collector, "SELECT * FROM orders WHERE id = %s", [order_id])
    order = next((o for o in ORDERS if o["id"] == order_id), None)

    if order is None:
        err = KeyError(f"Order {order_id} not found")
        collector.end_span(span, error=err)
        return 404, {"error": str(err)}

    try:
        user = fetch_user(collector, order["user_id"])
        order["user_name"] = user["name"]
    except (KeyError, TypeError) as e:
        collector.end_span(span, error=e)
        return 500, {"error": f"Failed to fetch user for order {order_id}: {e}"}

    collector.end_span(span)
    return 200, order


def handle_checkout(collector):
    """POST /api/orders/checkout — triggers payment + inventory bugs."""
    span = collector.start_span("POST /api/orders/checkout", {
        "http.method": "POST",
        "http.route": "/api/orders/checkout",
    })

    # Pick a random pending order
    pending = [o for o in ORDERS if o["status"] == "pending"]
    if not pending:
        collector.end_span(span)
        return 200, {"message": "No pending orders"}

    order = random.choice(pending)

    try:
        # Step 1: Validate user
        user = fetch_user(collector, order["user_id"])

        # Step 2: Check inventory
        for sku in order["items"]:
            check_inventory(collector, sku)

        # Step 3: Charge payment (may timeout)
        payment = call_payment_gateway(collector, order["total"], order["user_id"])

        # Step 4: Update inventory (race condition)
        update_inventory(collector, order["items"])

        order["status"] = "confirmed"
        collector.end_span(span)
        return 200, {
            "order_id": order["id"],
            "charge_id": payment["charge_id"],
            "status": "confirmed",
        }

    except Exception as e:
        collector.end_span(span, error=e)
        return 500, {"error": str(e)}


# ── HTTP Server ───────────────────────────────────────────────────────

class DemoHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        status = args[1] if len(args) > 1 else ""
        print(f"  {args[0]}  [{status}]") if args else None

    def _respond(self, code, body):
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(body, indent=2).encode())

    def do_GET(self):
        collector = SpanCollector()

        if self.path == "/api/orders":
            code, body = handle_list_orders(collector)
        elif self.path.startswith("/api/orders/"):
            try:
                order_id = int(self.path.split("/")[-1])
                code, body = handle_get_order(collector, order_id)
            except ValueError:
                code, body = 400, {"error": "Invalid order ID"}
        elif self.path == "/healthz":
            code, body = 200, {"status": "ok", "inventory": _inventory}
        else:
            code, body = 404, {"error": "not found"}

        collector.flush()
        self._respond(code, body)

    def do_POST(self):
        collector = SpanCollector()

        if self.path == "/api/orders/checkout":
            code, body = handle_checkout(collector)
        else:
            code, body = 404, {"error": "not found"}

        collector.flush()
        self._respond(code, body)


# ── Main ──────────────────────────────────────────────────────────────

def main():
    print(f"""
{'─' * 60}
  Inferra Demo — Order Service (with intentional bugs)
{'─' * 60}

  Endpoints:
    GET  /api/orders          List orders (N+1 query bug)
    GET  /api/orders/3        Get order 3 (None user bug)
    POST /api/orders/checkout  Checkout (payment timeout bug)
    GET  /healthz             Health check

  Traces sent to: {OTLP_ENDPOINT}

  Try these:
    curl http://localhost:{PORT}/api/orders
    curl http://localhost:{PORT}/api/orders/3
    curl -X POST http://localhost:{PORT}/api/orders/checkout
    curl -X POST http://localhost:4318/v1/analyze | python3 -m json.tool

{'─' * 60}
  Listening on http://localhost:{PORT}
{'─' * 60}
""")

    server = HTTPServer(("0.0.0.0", PORT), DemoHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n  Shutting down.")
        server.server_close()


if __name__ == "__main__":
    main()
