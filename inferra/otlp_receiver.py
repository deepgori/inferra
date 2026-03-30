"""
inferra.otlp_receiver — Lightweight OTLP/HTTP Span Receiver

Accepts OpenTelemetry trace data over HTTP (OTLP/HTTP JSON protocol),
converts spans to the internal TraceEvent format, and feeds them into
the Inferra RCA engine for live analysis.

Start the receiver:
    python -m inferra.otlp_receiver --port 4318

Then point any OTel-instrumented application at:
    OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318

The receiver exposes:
    POST /v1/traces         — Accepts OTLP JSON spans
    GET  /v1/traces         — Returns collected spans as JSON
    POST /v1/analyze        — Triggers RCA on collected spans
    GET  /healthz           — Health check
"""

import json
import logging
import os
import time
import threading
import concurrent.futures
from http.server import HTTPServer, BaseHTTPRequestHandler
from datetime import datetime

log = logging.getLogger("inferra.otlp")

# ── Configuration ──
LLM_TIMEOUT_SECONDS = int(os.environ.get("INFERRA_LLM_TIMEOUT", "30"))
LLM_MAX_RETRIES = int(os.environ.get("INFERRA_LLM_RETRIES", "1"))

# ---------------------------------------------------------------------------
# Internal span buffer
# ---------------------------------------------------------------------------

class SpanBuffer:
    """Thread-safe buffer for collected OTLP spans."""

    def __init__(self, max_spans=10_000):
        self._lock = threading.Lock()
        self._spans = []
        self._max = max_spans

    def add(self, spans):
        with self._lock:
            self._spans.extend(spans)
            # Ring-buffer: drop oldest if over limit
            if len(self._spans) > self._max:
                self._spans = self._spans[-self._max:]

    def get_all(self):
        with self._lock:
            return list(self._spans)

    def clear(self):
        with self._lock:
            self._spans.clear()

    def __len__(self):
        with self._lock:
            return len(self._spans)


# Singleton buffer and optional code index
_buffer = SpanBuffer()
_indexer = None       # CodeIndexer — populated when --project is used
_project_path = None  # Path to the indexed project
_storage = None       # Storage — auto-persists analyses
_last_analysis = None  # Last analysis session for /v1/ask follow-up


# ---------------------------------------------------------------------------
# OTLP JSON → internal TraceEvent conversion
# ---------------------------------------------------------------------------

def _nano_to_ms(nano):
    """Convert nanosecond timestamp to milliseconds."""
    return nano / 1_000_000 if nano else 0


def _status_to_error(status):
    """Extract error info from OTLP status."""
    if not status:
        return None
    code = status.get("code", 0)
    if code == 2:  # STATUS_CODE_ERROR
        return status.get("message", "unknown error")
    return None


def _parse_protobuf_traces(data):
    """Parse OTLP protobuf binary into the JSON dict our receiver expects."""
    try:
        from google.protobuf.json_format import MessageToDict
        from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
            ExportTraceServiceRequest,
        )
        import base64, binascii

        request = ExportTraceServiceRequest()
        request.ParseFromString(data)
        result = MessageToDict(request, preserving_proto_field_name=False)

        # Fix: MessageToDict base64-encodes bytes fields (traceId, spanId).
        # Our otlp_to_trace_events() expects hex strings, so convert them.
        for rs in result.get("resourceSpans", []):
            for ss in rs.get("scopeSpans", []):
                for span in ss.get("spans", []):
                    for field in ("traceId", "spanId", "parentSpanId"):
                        val = span.get(field, "")
                        if val:
                            try:
                                span[field] = base64.b64decode(val).hex()
                            except (binascii.Error, ValueError):
                                pass  # Already hex string
        return result
    except ImportError:
        log.warning("protobuf deps not installed — install opentelemetry-proto")
        return None
    except Exception as e:
        log.debug("protobuf parse failed: %s", e)
        return None


def _validate_span(span):
    """Validate that a span has the minimum required fields.
    Returns True if valid, False if malformed."""
    span_id = span.get("spanId", "")
    trace_id = span.get("traceId", "")
    if not span_id or not trace_id:
        log.warning("Skipping malformed span: missing spanId or traceId")
        return False
    # Ensure timestamps are parseable
    try:
        int(span.get("startTimeUnixNano", 0))
        int(span.get("endTimeUnixNano", 0))
    except (ValueError, TypeError):
        log.warning("Skipping span %s: invalid timestamps", span_id)
        return False
    return True


def otlp_to_trace_events(otlp_payload):
    """
    Convert an OTLP ExportTraceServiceRequest (JSON) to a flat list
    of simplified span dicts compatible with the Inferra engine.

    OTLP structure:
        resourceSpans[] → scopeSpans[] → spans[]

    Malformed spans (missing spanId/traceId or invalid timestamps)
    are silently skipped with a warning.
    """
    events = []
    skipped = 0

    for resource_span in otlp_payload.get("resourceSpans", []):
        # Extract service name from resource attributes
        resource = resource_span.get("resource", {})
        service_name = "unknown"
        for attr in resource.get("attributes", []):
            if attr.get("key") == "service.name":
                service_name = attr.get("value", {}).get("stringValue", "unknown")
                break

        for scope_span in resource_span.get("scopeSpans", []):
            scope = scope_span.get("scope", {})
            lib_name = scope.get("name", "")

            for span in scope_span.get("spans", []):
                # ── Validate span before processing ──
                if not _validate_span(span):
                    skipped += 1
                    continue

                try:
                    start_ns = int(span.get("startTimeUnixNano", 0))
                    end_ns = int(span.get("endTimeUnixNano", 0))
                    duration_ms = _nano_to_ms(end_ns - start_ns)
                    error = _status_to_error(span.get("status"))

                    # Build attribute dict
                    attrs = {}
                    for attr in span.get("attributes", []):
                        key = attr.get("key", "")
                        val = attr.get("value", {})
                        # Handle different OTLP value types
                        for vtype in ("stringValue", "intValue", "doubleValue", "boolValue"):
                            if vtype in val:
                                attrs[key] = val[vtype]
                                break

                    event = {
                        "trace_id": span.get("traceId", ""),
                        "span_id": span.get("spanId", ""),
                        "parent_span_id": span.get("parentSpanId", ""),
                        "name": span.get("name", "unnamed"),
                        "service": service_name,
                        "library": lib_name,
                        "kind": _span_kind_name(span.get("kind", 0)),
                        "start_time_ms": _nano_to_ms(start_ns),
                        "end_time_ms": _nano_to_ms(end_ns),
                        "duration_ms": round(duration_ms, 2),
                        "status": "ERROR" if error else "OK",
                        "error": error,
                        "attributes": attrs,
                        "events": [
                            {
                                "name": evt.get("name", ""),
                                "time_ms": _nano_to_ms(int(evt.get("timeUnixNano", 0))),
                                "attributes": {
                                    a["key"]: list(a["value"].values())[0]
                                    for a in evt.get("attributes", [])
                                    if a.get("value")
                                },
                            }
                            for evt in span.get("events", [])
                        ],
                    }
                    events.append(event)
                except Exception as e:
                    skipped += 1
                    log.warning("Skipping span %s: %s", span.get("spanId", "?"), e)

    if skipped:
        log.info("Skipped %d malformed span(s) out of %d total", skipped, skipped + len(events))

    return events


def _span_kind_name(kind_int):
    """Convert OTLP SpanKind enum to string."""
    return {
        0: "UNSPECIFIED",
        1: "INTERNAL",
        2: "SERVER",
        3: "CLIENT",
        4: "PRODUCER",
        5: "CONSUMER",
    }.get(kind_int, "UNKNOWN")


def spans_to_tracer_events(otlp_spans):
    """
    Convert simplified span dicts to the TraceEvent format expected
    by the Inferra RCA engine (matching async_content_tracer events).
    """
    from async_content_tracer.tracer import TraceEvent, EventType

    trace_events = []
    for s in otlp_spans:
        evt = TraceEvent(
            event_type=EventType.EXIT,
            function_name=s["name"],
            module=s.get("service", "unknown"),
            source_file=s.get("library", "otlp"),
            source_line=0,
            timestamp=s.get("start_time_ms", 0) / 1000,
            duration=s.get("duration_ms", 0) / 1000 if s.get("duration_ms") else None,
            context_id=s.get("trace_id", "ctx-0"),
            span_id=s.get("span_id", ""),
            parent_span_id=s.get("parent_span_id") or None,
            depth=0,
            thread_id=0,
            thread_name=s.get("service", "MainThread"),
            error=s.get("error"),
            metadata=s.get("attributes", {}),
        )
        trace_events.append(evt)

    return trace_events


# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------

class OTLPHandler(BaseHTTPRequestHandler):
    """Handles OTLP/HTTP JSON requests."""

    def log_message(self, fmt, *args):
        log.debug(fmt, *args)

    def _send(self, code, body=None, content_type="application/json"):
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        if body:
            self.wfile.write(json.dumps(body).encode())

    def do_OPTIONS(self):
        """Handle CORS preflight."""
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "POST, GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self):
        if self.path == "/healthz":
            payload = {
                "status": "ok",
                "spans_buffered": len(_buffer),
                "uptime_s": int(time.time() - _start_time),
            }
            if _storage:
                payload["storage"] = _storage.stats()
            self._send(200, payload)
        elif self.path == "/v1/traces":
            spans = _buffer.get_all()
            self._send(200, {"spans": spans, "count": len(spans)})
        elif self.path == "/v1/history":
            if not _storage:
                self._send(200, {"history": [], "message": "Storage not enabled"})
                return
            # Parse optional ?service=X&limit=N from query string
            from urllib.parse import urlparse, parse_qs
            qs = parse_qs(urlparse(self.path).query)
            svc = qs.get("service", [None])[0]
            limit = int(qs.get("limit", [20])[0])
            history = _storage.get_history(service=svc, limit=limit)
            self._send(200, {"history": history, "count": len(history)})
        elif self.path.startswith("/v1/regressions"):
            if not _storage:
                self._send(200, {"regressions": [], "message": "Storage not enabled"})
                return
            from urllib.parse import urlparse, parse_qs
            qs = parse_qs(urlparse(self.path).query)
            svc = qs.get("service", ["unknown"])[0]
            days = int(qs.get("days", [7])[0])
            regressions = _storage.detect_regressions(svc, window_days=days)
            self._send(200, {"regressions": regressions, "service": svc})
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):
        content_len = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_len) if content_len else b""

        if self.path == "/v1/traces":
            try:
                content_type = self.headers.get("Content-Type", "")
                payload = None

                # Try protobuf first (what real OTel SDKs send)
                if "protobuf" in content_type or "proto" in content_type:
                    payload = _parse_protobuf_traces(body)
                
                # Try JSON
                if payload is None:
                    try:
                        payload = json.loads(body) if body else {}
                    except (json.JSONDecodeError, UnicodeDecodeError):
                        # Binary data that's not valid JSON — try protobuf
                        payload = _parse_protobuf_traces(body)

                if payload is None:
                    payload = {}

                spans = otlp_to_trace_events(payload)
                _buffer.add(spans)
                log.info("Received %d spans  (buffer: %d)", len(spans), len(_buffer))
                self._send(200, {"accepted": len(spans)})
            except Exception as e:
                log.error("Failed to process spans: %s", e)
                self._send(400, {"error": str(e)})

        elif self.path == "/v1/analyze":
            spans = _buffer.get_all()
            if not spans:
                self._send(200, {
                    "status": "no_data",
                    "message": "No spans in buffer. Send traces first.",
                })
                return

            # ── Request-level error boundary ──
            # Even if something goes wrong, we try to return partial results
            partial_result = {
                "status": "analyzed",
                "span_count": len(spans),
                "code_indexed": _indexer is not None,
            }
            report = None
            source_map = {}
            report_path = None

            try:
                trace_events = spans_to_tracer_events(spans)

                # ── Stage 1: Map spans to source code via 8-stage cascade ──
                code_context = ""
                if _indexer:
                    try:
                        source_map = _correlate_spans_to_code(spans, trace_events)
                    except Exception as e:
                        log.warning("Code correlation failed (continuing without): %s", e)
                        source_map = {}

                    # Build full code context for the LLM
                    code_sections = []
                    for span_name, loc in source_map.items():
                        code_sections.append(
                            f"--- {span_name} → {loc['function']} ({loc['file']}:{loc['line']}) ---\n"
                            f"{loc.get('full_code', loc.get('snippet', ''))}"
                        )
                    if code_sections:
                        code_context = (
                            "\n\nSOURCE CODE for correlated spans:\n"
                            + "\n\n".join(code_sections)
                        )

                    # ── Stage 2: RAG retrieval for causal chains ──
                    try:
                        from inferra.rag import RAGPipeline
                        from async_content_tracer.graph import ExecutionGraph

                        graph = ExecutionGraph()
                        graph.build_from_events(trace_events)
                        rag = RAGPipeline(_indexer, max_code_results=3)

                        rag_sections = []
                        for node in graph.nodes.values():
                            if node.error:
                                ctx = rag.retrieve_for_error(node, graph)
                                if ctx.causal_chain:
                                    chain_str = " → ".join(
                                        n.function_name for n in ctx.causal_chain[:5]
                                    )
                                    rag_sections.append(
                                        f"Causal chain for {node.function_name}: "
                                        f"{chain_str}"
                                    )

                        if rag_sections:
                            code_context += (
                                "\n\nCAUSAL ANALYSIS (from execution graph):\n"
                                + "\n".join(rag_sections)
                            )
                    except Exception as e:
                        log.debug("RAG retrieval skipped: %s", e)

                # ── LLM analysis with timeout + retry ──
                engine = RCAEngine()
                llm_failed = False
                try:
                    report = _run_with_timeout(
                        lambda: engine.investigate(trace_events, code_context=code_context),
                        timeout=LLM_TIMEOUT_SECONDS,
                        retries=LLM_MAX_RETRIES,
                        label="RCA investigation",
                    )
                except Exception as e:
                    log.warning("LLM-powered analysis failed: %s — falling back to heuristic-only", e)
                    llm_failed = True
                    # Fallback: run heuristic agents only (no LLM)
                    report = _run_heuristic_only(engine, trace_events)

                partial_result.update({
                    "severity": report.severity.value,
                    "confidence": f"{report.confidence:.0%}",
                    "root_cause": report.root_cause,
                    "summary": report.summary,
                    "source_locations": report.source_locations,
                    "recommendations": report.recommendations,
                    "code_correlations": len(source_map),
                    "findings": [
                        {
                            "agent": f.agent_name,
                            "summary": f.summary,
                            "type": f.finding_type.value,
                            "confidence": f"{f.confidence:.0%}",
                        }
                        for f in report.findings
                    ],
                })

                if llm_failed:
                    partial_result["warnings"] = ["LLM unavailable — results are heuristic-only"]

                # Include LLM synthesis if available
                llm_text = report.metadata.get("llm_synthesis")
                if llm_text:
                    partial_result["llm_synthesis"] = llm_text

                # Generate HTML report (with source correlations)
                try:
                    report_path = _generate_otlp_report(spans, report, source_map)
                    if report_path:
                        partial_result["report_path"] = report_path
                        log.info("HTML report saved: %s", report_path)
                except Exception as e:
                    log.warning("HTML report generation failed: %s", e)

                # ── Persist to storage ──
                if _storage:
                    try:
                        durations = [s.get("duration_ms", 0) for s in spans]
                        durations.sort()
                        avg_lat = sum(durations) / len(durations) if durations else 0
                        p95_idx = int(len(durations) * 0.95)
                        p95_lat = durations[p95_idx] if p95_idx < len(durations) else 0
                        max_lat = max(durations) if durations else 0

                        span_groups = {}
                        for s in spans:
                            name = s.get("name", "unknown")
                            dur = s.get("duration_ms", 0)
                            err = 1 if s.get("error") else 0
                            if name not in span_groups:
                                span_groups[name] = {"durs": [], "errs": 0}
                            span_groups[name]["durs"].append(dur)
                            span_groups[name]["errs"] += err

                        span_stats_list = []
                        for name, data in span_groups.items():
                            durs = data["durs"]
                            span_stats_list.append({
                                "name": name,
                                "count": len(durs),
                                "avg_ms": sum(durs) / len(durs) if durs else 0,
                                "max_ms": max(durs) if durs else 0,
                                "error_rate": data["errs"] / len(durs) if durs else 0,
                            })

                        findings_dicts = [
                            {
                                "agent_name": f.agent_name,
                                "finding_type": f.finding_type.value,
                                "severity": f.severity.value,
                                "summary": f.summary,
                                "confidence": f.confidence,
                            }
                            for f in report.findings
                        ]

                        services = set()
                        for s in spans:
                            svc = s.get("service_name") or s.get("resource", {}).get("service.name", "unknown")
                            services.add(svc)
                        service_name = ", ".join(services) if services else "unknown"

                        _storage.save_analysis(
                            service=service_name,
                            project=_project_path or "",
                            severity=report.severity.value,
                            confidence=report.confidence,
                            root_cause=report.root_cause,
                            summary=report.summary,
                            llm_backend=report.metadata.get("llm_backend", ""),
                            total_spans=len(spans),
                            total_traces=len(set(s.get("trace_id", "") for s in spans)),
                            error_count=sum(1 for s in spans if s.get("error")),
                            avg_latency_ms=avg_lat,
                            p95_latency_ms=p95_lat,
                            max_latency_ms=max_lat,
                            report_path=report_path or "",
                            span_stats=span_stats_list,
                            findings_list=findings_dicts,
                        )
                    except Exception as e:
                        log.warning("Storage persistence failed: %s", e)

                self._send(200, partial_result)
                _buffer.clear()
                log.info("Analysis complete — severity: %s, confidence: %s",
                         report.severity.value, f"{report.confidence:.0%}")
                if source_map:
                    log.info("Code correlations: %d spans mapped to source",
                             len(source_map))

                # Save session context for interactive follow-up
                global _last_analysis
                import hashlib
                session_id = hashlib.sha256(
                    f"{time.time()}-{len(spans)}".encode()
                ).hexdigest()[:12]
                _last_analysis = {
                    "session_id": session_id,
                    "report_root_cause": report.root_cause,
                    "report_severity": report.severity.value,
                    "report_confidence": report.confidence,
                    "report_summary": report.summary,
                    "code_context": code_context[:5000],
                    "span_count": len(spans),
                    "source_map_keys": list(source_map.keys()),
                }

                if _storage:
                    try:
                        _storage.save_session(session_id, _last_analysis)
                    except Exception as e:
                        log.debug("Session persistence failed: %s", e)

                partial_result["session_id"] = session_id

            except Exception as e:
                log.error("Analysis failed: %s", e)
                # Return whatever partial results we have
                partial_result["status"] = "partial_failure"
                partial_result["error"] = str(e)
                if report:
                    partial_result["severity"] = report.severity.value
                    partial_result["root_cause"] = report.root_cause
                self._send(200, partial_result)

        elif self.path == "/v1/ask":
            # Interactive follow-up — ask questions about the last analysis
            try:
                payload = json.loads(body) if body else {}
                question = payload.get("question", "")
                req_session_id = payload.get("session_id", "")

                if not question:
                    self._send(400, {"error": "Missing 'question' field"})
                    return

                # Try in-memory first, then SQLite
                session = _last_analysis
                if not session and _storage:
                    session = _storage.load_session(
                        req_session_id if req_session_id else "latest"
                    )

                if not session:
                    self._send(200, {
                        "answer": "No analysis available yet. Run /v1/analyze first.",
                        "status": "no_context",
                    })
                    return

                # Build follow-up prompt with previous context
                code_ctx = session.get("code_context", "")
                follow_up_prompt = (
                    f"Previous analysis found: {session.get('report_root_cause', 'unknown')}\n"
                    f"Severity: {session.get('report_severity', 'unknown')}, "
                    f"Confidence: {session.get('report_confidence', 0):.0%}\n"
                    f"Summary: {session.get('report_summary', '')}\n\n"
                )
                if code_ctx:
                    follow_up_prompt += f"Code context:\n{code_ctx[:3000]}\n\n"
                follow_up_prompt += f"Follow-up question: {question}\n"
                follow_up_prompt += "\nPlease provide a detailed answer based on the analysis above."

                # Use LLM for follow-up
                try:
                    from inferra.llm_agent import get_llm_backend
                    backend = get_llm_backend()
                    if backend:
                        answer = backend.call(
                            follow_up_prompt,
                            system="You are Inferra, an AI debugging assistant. Answer follow-up questions about the previous RCA analysis.",
                            max_tokens=1500,
                        )
                        self._send(200, {
                            "answer": answer or "Unable to generate response",
                            "status": "answered",
                            "session_id": session.get("session_id", ""),
                            "llm_backend": backend.display_name(),
                        })
                    else:
                        self._send(200, {
                            "answer": "No LLM backend available for follow-up",
                            "status": "no_llm",
                        })
                except Exception as e:
                    log.error("LLM follow-up failed: %s", e)
                    self._send(200, {
                        "answer": f"LLM is unavailable ({e}). The previous analysis found: {session.get('report_root_cause', 'unknown')}",
                        "status": "llm_unavailable",
                    })

            except json.JSONDecodeError:
                self._send(400, {"error": "Invalid JSON"})

        elif self.path == "/v1/topology":
            # Generate topology from buffered spans
            spans = _buffer.get_all()
            if not spans:
                self._send(200, {"error": "No spans in buffer"})
                return
            try:
                from inferra.topology import Topology
                topo = Topology()
                topo.build_from_spans(spans)
                result = {
                    "summary": topo.summary(),
                    "mermaid": topo.to_mermaid(),
                    "graph": topo.to_d3_json(),
                }
                self._send(200, result)
            except Exception as e:
                self._send(500, {"error": str(e)})

        else:
            self._send(404, {"error": "not found"})


# ---------------------------------------------------------------------------
# HTML Report Generation
# ---------------------------------------------------------------------------

def _correlate_spans_to_code(spans, trace_events):
    """Map OTLP span names to source code via the CodeIndexer.

    Uses an 8-stage matching cascade, from most specific to most fuzzy:
      1. code.function span attribute (explicit)
      2. Exact route match (GET /products → @app.get("/products"))
      3. HTTP semantic conventions (http.route + http.method)
      4. Fuzzy route with ID stripping (/products/123 → /products/{id})
      5. Exact function name match
      6. Span name keyword decomposition (mongodb.products.aggregate → keywords)
      7. DB statement / body pattern match (db.statement → function body grep)
      8. TF-IDF fuzzy search (fallback)
    """
    source_map = {}

    if not _indexer:
        return source_map

    def _add_to_map(name, unit, match_type="exact"):
        source_map[name] = {
            "file": os.path.basename(unit.source_file),
            "full_path": unit.source_file,
            "line": unit.start_line,
            "end_line": unit.end_line,
            "function": unit.qualified_name,
            "snippet": unit.body_text[:500].strip(),
            "full_code": unit.body_text.strip(),
            "match_type": match_type,
        }

    # Collect unique span names + attributes to search for
    seen = set()
    for s in spans:
        name = s.get("name", "")
        attrs = s.get("attributes", {})
        http_route = attrs.get("http.route", "")
        http_target = attrs.get("http.target", "") or attrs.get("url.path", "")
        http_method = attrs.get("http.method", "") or attrs.get("http.request.method", "")
        db_statement = attrs.get("db.statement", "")
        db_operation = attrs.get("db.operation", "")
        db_collection = attrs.get("db.mongodb.collection", "") or attrs.get("db.sql.table", "")
        service_name = s.get("service", "")

        if name and name not in seen:
            seen.add(name)

            # ── Stage 1: code.function attribute (explicit) ──
            code_func = attrs.get("code.function", "")
            if code_func:
                unit = _indexer.search_by_function_name(code_func)
                if unit:
                    _add_to_map(name, unit, "code_attr")
                    continue

            # ── Stage 2: Exact route match ──
            unit = _indexer.search_by_route(name)
            if unit:
                _add_to_map(name, unit, "route")
                continue

            # ── Stage 3: HTTP semantic conventions ──
            if http_route and http_method:
                route_span = f"{http_method.upper()} {http_route}"
                unit = _indexer.search_by_route(route_span)
                if unit:
                    _add_to_map(name, unit, "http_semconv")
                    continue

            # ── Stage 4: Fuzzy route with ID stripping ──
            route_path = http_route or http_target
            if route_path:
                unit = _indexer.search_by_route_fuzzy(route_path)
                if unit:
                    _add_to_map(name, unit, "route_fuzzy")
                    continue
                # Also try with method prefix
                if http_method:
                    unit = _indexer.search_by_route(f"{http_method.upper()} {route_path}")
                    if not unit:
                        unit = _indexer.search_by_route_fuzzy(route_path)
                    if unit:
                        _add_to_map(name, unit, "route_fuzzy")
                        continue

            # ── Stage 5: Exact function name match ──
            clean_name = name.split(".")[-1].replace(" ", "_")
            unit = _indexer.search_by_function_name(clean_name)
            if unit:
                _add_to_map(name, unit, "exact")
                continue

            # ── Stage 6: Span name keyword decomposition ──
            # 'mongodb.products.aggregate' → ['mongodb', 'products', 'aggregate']
            # 'express.middleware.authMiddleware' → ['express', 'middleware', 'authMiddleware']
            import re as _re
            keywords = _re.split(r'[.\-_/\s]+', name)
            if len(keywords) >= 2:
                results = _indexer.search_functions_by_keywords(keywords, max_results=1)
                if results:
                    _add_to_map(name, results[0], "keyword_decomp")
                    continue

            # ── Stage 7: DB statement / body pattern matching ──
            # If span has db.statement or db collection info, search code bodies
            body_pattern = None
            if db_statement:
                # Extract table/collection name from SQL: SELECT * FROM users → 'users'
                table_match = _re.search(
                    r'(?:FROM|INTO|UPDATE|JOIN)\s+[`"\']?(\w+)',
                    db_statement, _re.IGNORECASE
                )
                if table_match:
                    body_pattern = table_match.group(1)
            elif db_collection:
                body_pattern = db_collection
            elif db_operation:
                # e.g. db.operation='aggregate', combine with collection
                body_pattern = db_operation

            if body_pattern:
                results = _indexer.search_by_body_pattern(body_pattern, max_results=1)
                if results:
                    _add_to_map(name, results[0], "db_pattern")
                    continue

                # Also try service name + body pattern for better targeting
                if service_name and service_name != "unknown":
                    svc_keyword = service_name.split("-")[0].split("_")[0]
                    svc_units = _indexer.search_by_filepath_keyword(svc_keyword)
                    for su in svc_units:
                        if body_pattern.lower() in su.body_text.lower():
                            _add_to_map(name, su, "service_body")
                            break
                    if name in source_map:
                        continue

            # ── Stage 8: TF-IDF fuzzy search (fallback) ──
            search_results = _indexer.search(name, top_k=1)
            if search_results and search_results[0].score > 0.15:
                sr_unit = search_results[0].code_unit
                source_map[name] = {
                    "file": os.path.basename(sr_unit.source_file),
                    "full_path": sr_unit.source_file,
                    "line": sr_unit.start_line,
                    "end_line": sr_unit.end_line,
                    "function": sr_unit.qualified_name,
                    "snippet": sr_unit.body_text[:500].strip(),
                    "full_code": sr_unit.body_text.strip(),
                    "match_type": "fuzzy",
                    "score": round(search_results[0].score, 3),
                }

    return source_map



def _generate_otlp_report(spans, report, source_map=None):
    """Generate an HTML report from OTLP spans and RCA results."""
    import os
    try:
        from report_html import generate_html_report
    except ImportError:
        return None

    # ── Derive service & trace metadata ──
    services = set()
    for s in spans:
        svc = s.get("service", "unknown")
        if svc != "unknown":
            services.add(svc)
    service_name = ", ".join(sorted(services)) or "otlp-service"

    error_spans = [s for s in spans if s.get("status") == "ERROR"]
    ok_spans = [s for s in spans if s.get("status") != "ERROR"]
    unique_ops = set(s.get("name", "") for s in spans)
    trace_ids = set(s.get("trace_id", "") for s in spans)
    durations = [s.get("duration_ms", 0) for s in spans if s.get("duration_ms", 0) > 0]

    # Latency stats
    avg_latency = sum(durations) / len(durations) if durations else 0
    max_latency = max(durations) if durations else 0
    p95 = sorted(durations)[int(len(durations) * 0.95)] if durations else 0

    # ── Stats with telemetry-native labels ──
    stats = {
        "_section_title": "📡 Telemetry Overview",
        "_labels": {
            "total_units": "Total Spans",
            "files_indexed": "Traces",
            "functions": "Unique Ops",
            "classes": "Services",
            "sql_models": "Avg Latency",
            "config_entries": "P95 Latency",
            "log_patterns": "Errors",
            "unique_tokens": "Max Latency",
        },
        "total_units": len(spans),
        "files_indexed": len(trace_ids),
        "functions": len(unique_ops),
        "classes": len(services),
        "sql_models": f"{avg_latency:.0f}ms",
        "config_entries": f"{p95:.0f}ms",
        "log_patterns": len(error_spans),
        "unique_tokens": f"{max_latency:.0f}ms",
    }

    # ── Discovered operations with status ──
    # For each unique operation, determine overall success/failure
    success_names = set()
    failure_dict = {}
    for s in spans:
        name = s.get("name", "")
        if s.get("error"):
            failure_dict[name] = s["error"]
        elif name not in failure_dict:
            success_names.add(name)

    successes = [(name, "") for name in success_names]
    failures = [(name, err) for name, err in failure_dict.items()]
    discovered_ops = sorted(unique_ops)

    # ── Build hierarchical call tree by trace ──
    total_duration = sum(s.get("duration_ms", 0) for s in spans)

    # ── Pipeline provenance ──
    provenance_lines = [
        f"  ┌─ Pipeline Provenance ─────────────────────────┐",
        f"  │  Trace source:      OTLP/HTTP (port 4318)     │",
        f"  │  Protocol:          {'protobuf' if any(s.get('_from_proto') for s in spans) else 'JSON/proto'}            │",
    ]
    if _indexer:
        idx_stats = _indexer.stats()
        corr_count = len(source_map) if source_map else 0
        provenance_lines.extend([
            f"  │  Code indexed:      ✅ YES                    │",
            f"  │  Project path:      {os.path.basename(_project_path or '')}  │",
            f"  │  Indexed units:     {idx_stats['total_units']} ({idx_stats['functions']}F, {idx_stats['classes']}C)        │",
            f"  │  Span→Code matches: {corr_count}/{len(set(s.get('name','') for s in spans))} unique spans      │",
        ])
    else:
        provenance_lines.extend([
            f"  │  Code indexed:      ❌ NO (OTLP-only mode)    │",
        ])
    provenance_lines.append(
        f"  └──────────────────────────────────────────────┘"
    )
    provenance_block = "\n".join(provenance_lines)

    graph_summary = (
        f"{provenance_block}\n\n"
        f"  Total spans:           {len(spans)}\n"
        f"  Unique traces:         {len(trace_ids)}\n"
        f"  Services:              {', '.join(sorted(services))}\n"
        f"  Errors:                {len(error_spans)}/{len(spans)} spans\n"
        f"  Total duration:        {total_duration:.1f}ms\n"
        f"  Avg span latency:      {avg_latency:.1f}ms\n"
        f"  P95 latency:           {p95:.1f}ms\n"
        f"  Max latency:           {max_latency:.1f}ms\n"
    )

    # Group spans by trace_id and build trees
    traces = {}
    for s in spans:
        tid = s.get("trace_id", "unknown")
        traces.setdefault(tid, []).append(s)

    tree_lines = []
    for i, (tid, trace_spans) in enumerate(traces.items(), 1):
        # Find root spans (no parent or parent not in this trace)
        span_ids = {s.get("span_id", "") for s in trace_spans}
        root_spans = [s for s in trace_spans if s.get("parent_span_id", "") not in span_ids]
        child_map = {}
        for s in trace_spans:
            pid = s.get("parent_span_id", "")
            if pid:
                child_map.setdefault(pid, []).append(s)

        # Determine trace-level status
        trace_errors = [s for s in trace_spans if s.get("error")]
        trace_dur = max((s.get("duration_ms", 0) for s in root_spans), default=0)
        status_tag = "ERROR" if trace_errors else "OK"
        tree_lines.append(
            f"  ━━ Trace {i} ({tid[:12]}...) "
            f"[{len(trace_spans)} spans, {trace_dur:.0f}ms] {status_tag}"
        )

        # Recursive tree renderer
        def render(span, depth=1):
            indent = "  │  " * (depth - 1) + "  ├─ "
            dur = span.get("duration_ms", 0)
            svc = span.get("service", "?")
            err = span.get("error")
            tag = f" ✖ {err}" if err else ""
            # Add source location if available
            src_ref = ""
            if source_map and span.get("name") in source_map:
                loc = source_map[span["name"]]
                src_ref = f"  → {loc['file']}:{loc['line']}"
            tree_lines.append(f"{indent}{span.get('name', '?')} ({dur:.1f}ms) [{svc}]{tag}{src_ref}")
            # Render children
            for child in child_map.get(span.get("span_id", ""), []):
                render(child, depth + 1)

        for root in (root_spans or trace_spans[:1]):
            render(root)
        tree_lines.append("")  # Blank line between traces

    graph_tree = "\n".join(tree_lines[:80])

    # ── Build richer source locations from span attributes + code index ──
    source_locations = set()
    if source_map:
        for span_name, loc in source_map.items():
            source_locations.add(
                f"{loc['file']}:{loc['line']} → {loc['function']}"
            )
    for s in spans:
        attrs = s.get("attributes", {})
        if attrs.get("db.statement"):
            source_locations.add(f"db: {attrs['db.statement'][:60]}")
        if attrs.get("http.url"):
            source_locations.add(f"http: {attrs['http.url']}")
        if attrs.get("http.route"):
            source_locations.add(f"route: {attrs['http.route']}")
    # Add source locations to report if they're richer
    if source_locations and report.source_locations == ["demo_app.tracing:0"]:
        report.source_locations = sorted(source_locations)[:10]

    # ── Diagnosis ──
    err_types = {}
    for s in error_spans:
        err = s.get("error", "unknown")
        err_types.setdefault(err, 0)
        err_types[err] += 1
    err_summary = "; ".join(f"{e} (x{c})" for e, c in err_types.items())

    # ── Build pipeline mode label ──
    if _indexer:
        idx_stats = _indexer.stats()
        corr_count = len(source_map) if source_map else 0
        pipeline_tag = (
            f"Pipeline: UNIFIED (OTLP + Code Index) │ "
            f"Indexed: {idx_stats['total_units']} units from {os.path.basename(_project_path or 'unknown')} │ "
            f"Correlations: {corr_count}"
        )
    else:
        pipeline_tag = "Pipeline: OTLP-ONLY (no code indexed)"

    trace_data = {
        "report": report,
        "successes": successes,
        "failures": failures,
        "entry_points": discovered_ops,
        "graph_summary": graph_summary,
        "graph_tree": graph_tree,
        "diagnosis": (
            f"[{pipeline_tag}]\n\n"
            f"Analyzed {len(spans)} spans across {len(trace_ids)} trace(s) from "
            f"{len(services)} service(s). Found {len(error_spans)} error(s): "
            f"{err_summary}. Root cause: {report.root_cause}"
        ),
    }

    # Write report
    reports_dir = os.path.join(os.getcwd(), "reports")
    os.makedirs(reports_dir, exist_ok=True)
    safe_svc = service_name.lower().replace(" ", "_").replace("-", "_").replace(",", "_")
    output_path = os.path.join(reports_dir, f"otlp_{safe_svc}_report.html")

    generate_html_report(service_name, stats, trace_data, output_path)
    return os.path.abspath(output_path)


# ---------------------------------------------------------------------------
# LLM timeout / retry helpers
# ---------------------------------------------------------------------------

def _run_with_timeout(fn, timeout=30, retries=1, label="operation"):
    """Run a function with a timeout and optional retry.
    Uses a thread pool to enforce the timeout without killing the process.
    """
    last_error = None
    for attempt in range(1 + retries):
        try:
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(fn)
                return future.result(timeout=timeout)
        except concurrent.futures.TimeoutError:
            last_error = TimeoutError(f"{label} timed out after {timeout}s (attempt {attempt + 1}/{1 + retries})")
            log.warning("%s", last_error)
        except Exception as e:
            last_error = e
            if attempt < retries:
                log.warning("%s failed (attempt %d/%d): %s — retrying",
                           label, attempt + 1, 1 + retries, e)
            else:
                raise
    raise last_error


def _run_heuristic_only(engine, trace_events):
    """Fallback: run only heuristic agents (no LLM) when LLM is unavailable."""
    from async_content_tracer.graph import ExecutionGraph
    from inferra.agents import (
        CoordinatorAgent, LogAnalysisAgent, MetricsCorrelationAgent,
    )

    graph = ExecutionGraph()
    graph.build_from_events(trace_events)

    heuristic_coord = CoordinatorAgent(
        specialists=[LogAnalysisAgent(), MetricsCorrelationAgent()],
    )
    report = heuristic_coord.investigate(graph)
    report.metadata["llm_available"] = False
    report.metadata["fallback"] = "heuristic_only"
    return report


# ---------------------------------------------------------------------------
# Lazy import to avoid circular deps
# ---------------------------------------------------------------------------

def _lazy_imports():
    """Import heavy modules only when needed."""
    global RCAEngine
    from inferra.rca_engine import RCAEngine


# ---------------------------------------------------------------------------
# Server entry point
# ---------------------------------------------------------------------------

_start_time = time.time()


def serve(host="0.0.0.0", port=4318, project=None):
    """Start the OTLP receiver, optionally indexing a project for code correlation."""
    global _indexer, _project_path, _storage
    _lazy_imports()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  [%(name)s]  %(message)s",
        datefmt="%H:%M:%S",
    )

    # ── Initialize persistence ──
    try:
        from inferra.storage import Storage
        _storage = Storage()
    except Exception as e:
        log.warning("Storage disabled: %s", e)
        _storage = None

    # ── Index project codebase if provided ──
    if project:
        _project_path = os.path.abspath(project)
        log.info("Indexing codebase: %s", _project_path)
        from inferra.indexer import CodeIndexer
        _indexer = CodeIndexer()
        _indexer.index_directory(_project_path)
        stats = _indexer.stats()
        log.info("  Indexed %d code units across %d files  (%d functions, %d classes)",
                 stats["total_units"], stats["files_indexed"],
                 stats["functions"], stats["classes"])
        log.info("  Code correlation enabled — spans will be mapped to source")
        log.info("")

    server = HTTPServer((host, port), OTLPHandler)
    log.info("Inferra OTLP receiver listening on %s:%d", host, port)
    log.info("  POST /v1/traces      — Accept OTLP spans")
    log.info("  POST /v1/analyze     — Trigger RCA on buffered spans%s",
             " (+ code correlation)" if _indexer else "")
    log.info("  GET  /v1/traces      — View buffered spans")
    log.info("  GET  /v1/history     — View analysis history")
    log.info("  GET  /v1/regressions — Detect performance regressions")
    log.info("  GET  /healthz        — Health check")
    log.info("")
    log.info("Configure your app:")
    log.info("  export OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:%d", port)
    log.info("")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("Shutting down.")
        if _storage:
            _storage.close()
        server.server_close()


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(
        description="Inferra OTLP Receiver — accepts OpenTelemetry spans and runs RCA"
    )
    parser.add_argument("--host", default="0.0.0.0", help="Bind address (default: 0.0.0.0)")
    parser.add_argument("--port", type=int, default=4318, help="Port (default: 4318, OTLP/HTTP standard)")
    args = parser.parse_args()

    serve(host=args.host, port=args.port)
