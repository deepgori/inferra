"""
topology.py — Multi-Service Topology Visualization

Builds a service dependency graph from multi-service trace data.
Uses trace parent-child relationships to map service interactions.

Features:
- Service graph construction from spans
- Edge weight calculation (call count, avg latency)
- Mermaid diagram generation
- D3.js-compatible JSON export
"""

import json
import logging
from collections import defaultdict
from typing import Any, Dict, List, Optional, Set, Tuple

log = logging.getLogger(__name__)


class ServiceNode:
    """Represents a service in the topology."""

    def __init__(self, name: str):
        self.name = name
        self.span_count = 0
        self.error_count = 0
        self.avg_latency_ms = 0.0
        self.endpoints: Set[str] = set()
        self._latencies: List[float] = []

    def add_span(self, duration_ms: float, error: bool = False, endpoint: str = ""):
        self.span_count += 1
        self._latencies.append(duration_ms)
        self.avg_latency_ms = sum(self._latencies) / len(self._latencies)
        if error:
            self.error_count += 1
        if endpoint:
            self.endpoints.add(endpoint)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "span_count": self.span_count,
            "error_count": self.error_count,
            "avg_latency_ms": round(self.avg_latency_ms, 1),
            "endpoints": sorted(self.endpoints),
        }


class ServiceEdge:
    """Represents a dependency between two services."""

    def __init__(self, source: str, target: str):
        self.source = source
        self.target = target
        self.call_count = 0
        self.error_count = 0
        self.avg_latency_ms = 0.0
        self._latencies: List[float] = []

    def add_call(self, duration_ms: float, error: bool = False):
        self.call_count += 1
        self._latencies.append(duration_ms)
        self.avg_latency_ms = sum(self._latencies) / len(self._latencies)
        if error:
            self.error_count += 1

    def to_dict(self) -> Dict[str, Any]:
        return {
            "source": self.source,
            "target": self.target,
            "call_count": self.call_count,
            "error_count": self.error_count,
            "avg_latency_ms": round(self.avg_latency_ms, 1),
        }


class Topology:
    """
    Multi-service topology builder.

    Usage:
        topo = Topology()
        topo.build_from_spans(spans)
        print(topo.to_mermaid())
        print(json.dumps(topo.to_d3_json()))
    """

    def __init__(self):
        self.nodes: Dict[str, ServiceNode] = {}
        self.edges: Dict[Tuple[str, str], ServiceEdge] = {}

    def build_from_spans(self, spans: List[Dict]) -> "Topology":
        """Build topology from a list of OTLP span dicts."""
        # Index spans by ID for parent lookup
        span_index = {}
        for span in spans:
            span_id = span.get("span_id") or span.get("spanId", "")
            if span_id:
                span_index[span_id] = span

        for span in spans:
            service = (
                span.get("service_name")
                or span.get("resource", {}).get("service.name", "")
                or "unknown"
            )
            name = span.get("name", "")
            duration = span.get("duration_ms", 0)
            error = bool(span.get("error"))

            # Add/update service node
            if service not in self.nodes:
                self.nodes[service] = ServiceNode(service)
            self.nodes[service].add_span(duration, error, name)

            # Find parent span and check if it's from a different service
            parent_id = span.get("parent_span_id") or span.get("parentSpanId", "")
            if parent_id and parent_id in span_index:
                parent = span_index[parent_id]
                parent_service = (
                    parent.get("service_name")
                    or parent.get("resource", {}).get("service.name", "")
                    or "unknown"
                )
                if parent_service != service:
                    # Cross-service call
                    edge_key = (parent_service, service)
                    if edge_key not in self.edges:
                        self.edges[edge_key] = ServiceEdge(parent_service, service)
                    self.edges[edge_key].add_call(duration, error)

        return self

    def to_mermaid(self) -> str:
        """Generate a Mermaid flowchart diagram."""
        lines = ["graph LR"]

        for name, node in sorted(self.nodes.items()):
            safe_name = name.replace("-", "_").replace(".", "_")
            label = f"{name}<br/>spans: {node.span_count}<br/>avg: {node.avg_latency_ms:.0f}ms"
            if node.error_count > 0:
                lines.append(f'    {safe_name}["{label}"]:::error')
            else:
                lines.append(f'    {safe_name}["{label}"]')

        for (src, tgt), edge in self.edges.items():
            safe_src = src.replace("-", "_").replace(".", "_")
            safe_tgt = tgt.replace("-", "_").replace(".", "_")
            label = f"{edge.call_count} calls<br/>{edge.avg_latency_ms:.0f}ms"
            if edge.error_count > 0:
                lines.append(f'    {safe_src} -->|"{label}"| {safe_tgt}')
            else:
                lines.append(f'    {safe_src} -->|"{label}"| {safe_tgt}')

        lines.append("")
        lines.append("    classDef error fill:#ef4444,color:#fff")

        return "\n".join(lines)

    def to_d3_json(self) -> Dict[str, Any]:
        """Export topology as D3.js-compatible JSON."""
        return {
            "nodes": [node.to_dict() for node in self.nodes.values()],
            "edges": [edge.to_dict() for edge in self.edges.values()],
        }

    def to_html(self, output_path: str = "topology.html") -> str:
        """Generate a standalone HTML visualization."""
        d3_data = json.dumps(self.to_d3_json(), indent=2)
        mermaid = self.to_mermaid()

        html = f"""<!DOCTYPE html>
<html><head>
<title>Service Topology — Inferra</title>
<script src="https://cdn.jsdelivr.net/npm/mermaid/dist/mermaid.min.js"></script>
<style>
body {{ font-family: Inter, sans-serif; background: #1e293b; color: #e2e8f0; padding: 2rem; }}
.container {{ max-width: 900px; margin: 0 auto; }}
h1 {{ color: #818cf8; }}
.mermaid {{ background: #0f172a; padding: 2rem; border-radius: 12px; margin: 1rem 0; }}
.stats {{
    display: grid; grid-template-columns: repeat(3, 1fr); gap: 1rem; margin: 1.5rem 0;
}}
.stat {{
    background: #334155; padding: 1rem; border-radius: 8px; text-align: center;
}}
.stat .v {{ font-size: 1.5rem; font-weight: 700; color: #818cf8; }}
.stat .l {{ font-size: 0.8rem; color: #94a3b8; margin-top: 0.3rem; }}
pre {{ background: #0f172a; padding: 1rem; border-radius: 8px; overflow-x: auto; font-size: 0.85rem; }}
</style>
</head><body>
<div class="container">
  <h1>🗺️ Service Topology</h1>
  <div class="stats">
    <div class="stat"><div class="v">{len(self.nodes)}</div><div class="l">Services</div></div>
    <div class="stat"><div class="v">{len(self.edges)}</div><div class="l">Dependencies</div></div>
    <div class="stat"><div class="v">{sum(n.span_count for n in self.nodes.values())}</div><div class="l">Total Spans</div></div>
  </div>
  <div class="mermaid">{mermaid}</div>
  <h2>Raw Data</h2>
  <pre>{d3_data}</pre>
</div>
<script>mermaid.initialize({{ startOnLoad: true, theme: 'dark' }});</script>
</body></html>"""

        with open(output_path, "w") as f:
            f.write(html)

        return output_path

    def summary(self) -> str:
        """One-line summary."""
        return (
            f"{len(self.nodes)} services, {len(self.edges)} dependencies, "
            f"{sum(n.span_count for n in self.nodes.values())} total spans"
        )
