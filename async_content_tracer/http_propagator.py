"""
http_propagator.py — Cross-Service Trace Context Propagation

Provides HTTP header injection/extraction for propagating trace context
across service boundaries. Follows the W3C Traceparent pattern (simplified).

Usage — Client side (outgoing request):
    from async_content_tracer import ContextManager, HTTPContextPropagator

    ctx = ContextManager()
    ctx.new_context()
    propagator = HTTPContextPropagator(ctx)

    headers = {}
    propagator.inject(headers)
    # headers now contains: X-Trace-Context-Id, X-Trace-Span-Id, X-Trace-Depth
    response = requests.get("http://service-b/api", headers=headers)

Usage — Server side (incoming request):
    propagator = HTTPContextPropagator(ctx)
    propagator.extract(request.headers)
    # Context is now restored — all traced functions will share the same context_id
"""

from typing import Dict, Optional
from async_content_tracer.context import (
    ContextManager,
    _context_id,
    _parent_span_id,
    _trace_depth,
)


# Header names following common distributed tracing conventions
HEADER_CONTEXT_ID = "X-Trace-Context-Id"
HEADER_SPAN_ID = "X-Trace-Span-Id"
HEADER_DEPTH = "X-Trace-Depth"
HEADER_TRACEPARENT = "traceparent"  # W3C standard


class HTTPContextPropagator:
    """
    Injects and extracts trace context from HTTP headers.

    This enables cross-service tracing: when Service A calls Service B
    over HTTP, the context_id is preserved so that both services'
    trace events appear in the same execution graph.
    """

    def __init__(self, context_manager: ContextManager):
        self._ctx = context_manager

    def inject(self, headers: Dict[str, str]) -> Dict[str, str]:
        """
        Inject the current trace context into outgoing HTTP headers.

        Args:
            headers: The HTTP headers dict to inject into (modified in-place).

        Returns:
            The modified headers dict (for chaining).
        """
        context_id = _context_id.get(None)
        span_id = _parent_span_id.get(None)
        depth = _trace_depth.get(0)

        if context_id:
            headers[HEADER_CONTEXT_ID] = context_id
        if span_id:
            headers[HEADER_SPAN_ID] = span_id
        headers[HEADER_DEPTH] = str(depth)

        # Also emit W3C traceparent for interop
        # Format: version-trace_id-parent_id-flags
        if context_id:
            trace_id = context_id.replace("-", "")[:32].ljust(32, "0")
            parent_id = (span_id or "0" * 16).replace("-", "")[:16].ljust(16, "0")
            headers[HEADER_TRACEPARENT] = f"00-{trace_id}-{parent_id}-01"

        return headers

    def extract(self, headers: Dict[str, str]) -> Optional[str]:
        """
        Extract trace context from incoming HTTP headers and restore it.

        Args:
            headers: The incoming HTTP request headers.

        Returns:
            The extracted context_id, or None if no context was found.
        """
        # Try custom headers first
        context_id = self._get_header(headers, HEADER_CONTEXT_ID)
        span_id = self._get_header(headers, HEADER_SPAN_ID)
        depth_str = self._get_header(headers, HEADER_DEPTH)

        # Fall back to W3C traceparent
        if not context_id:
            traceparent = self._get_header(headers, HEADER_TRACEPARENT)
            if traceparent:
                context_id, span_id = self._parse_traceparent(traceparent)

        if not context_id:
            return None

        # Restore context
        _context_id.set(context_id)
        if span_id:
            _parent_span_id.set(span_id)

        depth = 0
        if depth_str:
            try:
                depth = int(depth_str)
            except ValueError:
                pass
        _trace_depth.set(depth)

        return context_id

    def get_context_headers(self) -> Dict[str, str]:
        """
        Get the current context as a headers dict (convenience method).

        Returns:
            Headers dict ready to attach to an outgoing request.
        """
        headers: Dict[str, str] = {}
        self.inject(headers)
        return headers

    @staticmethod
    def _get_header(headers: Dict[str, str], name: str) -> Optional[str]:
        """Case-insensitive header lookup."""
        # Try exact match first
        if name in headers:
            return headers[name]
        # Case-insensitive fallback
        name_lower = name.lower()
        for key, value in headers.items():
            if key.lower() == name_lower:
                return value
        return None

    @staticmethod
    def _parse_traceparent(traceparent: str):
        """
        Parse W3C traceparent header.
        Format: version-trace_id-parent_id-flags
        Example: 00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01
        """
        parts = traceparent.split("-")
        if len(parts) >= 3:
            trace_id = parts[1]
            parent_id = parts[2]
            # Convert back to UUID-like format
            if len(trace_id) >= 32:
                context_id = (
                    f"{trace_id[:8]}-{trace_id[8:12]}-"
                    f"{trace_id[12:16]}-{trace_id[16:20]}-{trace_id[20:32]}"
                )
            else:
                context_id = trace_id
            return context_id, parent_id
        return None, None

    def create_middleware(self):
        """
        Create WSGI/ASGI middleware functions for automatic context extraction.

        Returns a dict with 'extract_from_request' and 'inject_into_response'
        callables that frameworks can use.
        """
        propagator = self

        def extract_from_request(headers: Dict[str, str]) -> Optional[str]:
            """Call at the start of each request handler."""
            context_id = propagator.extract(headers)
            if not context_id:
                # No incoming context — create a new one
                ctx = propagator._ctx.new_context()
                context_id = ctx.context_id
            return context_id

        def inject_into_response(headers: Dict[str, str]) -> Dict[str, str]:
            """Call before sending the response."""
            return propagator.inject(headers)

        return {
            "extract_from_request": extract_from_request,
            "inject_into_response": inject_into_response,
        }
