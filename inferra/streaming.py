"""
streaming.py — Real-Time Streaming Analyzer

Continuously analyzes spans as they arrive instead of buffer-then-analyze.
Uses a background thread with configurable sliding window and alert thresholds.

Features:
- Sliding window statistics (rolling P95, error rate)
- Automatic anomaly detection
- Configurable alert callbacks
- Auto-trigger full RCA when anomaly score exceeds threshold
"""

import logging
import threading
import time
from collections import deque
from typing import Any, Callable, Dict, List, Optional

log = logging.getLogger(__name__)


class StreamingAnalyzer:
    """
    Real-time span analyzer with sliding window anomaly detection.

    Usage:
        analyzer = StreamingAnalyzer(
            window_seconds=60,
            alert_callback=lambda alert: print(alert),
        )
        analyzer.start()

        # Feed spans as they arrive
        analyzer.ingest(span_batch)

        # Stop when done
        analyzer.stop()
    """

    def __init__(
        self,
        window_seconds: int = 60,
        error_rate_threshold: float = 0.05,
        latency_threshold_ms: float = 1000.0,
        anomaly_score_threshold: float = 0.7,
        check_interval_seconds: float = 5.0,
        alert_callback: Optional[Callable[[Dict[str, Any]], None]] = None,
        rca_trigger_callback: Optional[Callable[[], None]] = None,
    ):
        self._window_seconds = window_seconds
        self._error_rate_threshold = error_rate_threshold
        self._latency_threshold_ms = latency_threshold_ms
        self._anomaly_score_threshold = anomaly_score_threshold
        self._check_interval = check_interval_seconds
        self._alert_callback = alert_callback
        self._rca_trigger_callback = rca_trigger_callback

        # Sliding window data
        self._spans: deque = deque()
        self._lock = threading.Lock()

        # Stats
        self._total_ingested = 0
        self._total_errors = 0
        self._total_alerts = 0

        # Background thread
        self._running = False
        self._thread: Optional[threading.Thread] = None

    def start(self):
        """Start the background analysis thread."""
        self._running = True
        self._thread = threading.Thread(
            target=self._analysis_loop, daemon=True, name="streaming-analyzer"
        )
        self._thread.start()
        log.info("Streaming analyzer started (window=%ds, check=%0.1fs)",
                 self._window_seconds, self._check_interval)

    def stop(self):
        """Stop the background analysis thread."""
        self._running = False
        if self._thread:
            self._thread.join(timeout=5)
        log.info("Streaming analyzer stopped (total ingested: %d, alerts: %d)",
                 self._total_ingested, self._total_alerts)

    def ingest(self, spans: List[Dict]):
        """Add a batch of spans to the sliding window."""
        now = time.time()
        with self._lock:
            for span in spans:
                entry = {**span, "_ingest_time": now}  # copy — don't mutate caller's dict
                self._spans.append(entry)
                self._total_ingested += 1
                if span.get("error"):
                    self._total_errors += 1

    def _analysis_loop(self):
        """Background loop that checks for anomalies periodically."""
        while self._running:
            try:
                self._check_window()
            except Exception as e:
                log.error("Streaming analysis error: %s", e)
            time.sleep(self._check_interval)

    def _check_window(self):
        """Analyze the current sliding window for anomalies."""
        now = time.time()
        cutoff = now - self._window_seconds

        with self._lock:
            # Evict old spans
            while self._spans and self._spans[0].get("_ingest_time", 0) < cutoff:
                self._spans.popleft()

            if not self._spans:
                return

            window = list(self._spans)

        # Compute window statistics
        durations = [s.get("duration_ms", 0) for s in window]
        errors = sum(1 for s in window if s.get("error"))
        total = len(window)

        if not durations:
            return

        durations.sort()
        avg_ms = sum(durations) / len(durations)
        p95_idx = int(len(durations) * 0.95)
        p95_ms = durations[p95_idx] if p95_idx < len(durations) else durations[-1]
        max_ms = durations[-1]
        error_rate = errors / total if total > 0 else 0

        # Compute anomaly score (0-1)
        score = 0.0

        # Error rate contribution
        if error_rate > self._error_rate_threshold:
            score += min(0.4, error_rate * 4)

        # Latency contribution
        if p95_ms > self._latency_threshold_ms:
            ratio = p95_ms / self._latency_threshold_ms
            score += min(0.4, (ratio - 1) * 0.2)

        # Outlier contribution (max >> avg)
        if avg_ms > 0 and max_ms > avg_ms * 5:
            score += 0.2

        score = min(1.0, score)

        # Alert if anomaly detected
        if score >= self._anomaly_score_threshold:
            alert = {
                "type": "anomaly_detected",
                "score": round(score, 2),
                "window_seconds": self._window_seconds,
                "spans_in_window": total,
                "error_rate": round(error_rate, 3),
                "avg_ms": round(avg_ms, 1),
                "p95_ms": round(p95_ms, 1),
                "max_ms": round(max_ms, 1),
                "timestamp": time.time(),
            }

            self._total_alerts += 1
            log.warning(
                "🚨 Anomaly detected! score=%.2f  errors=%d/%d  p95=%.0fms  max=%.0fms",
                score, errors, total, p95_ms, max_ms,
            )

            if self._alert_callback:
                self._alert_callback(alert)

            if self._rca_trigger_callback:
                log.info("Auto-triggering RCA analysis...")
                self._rca_trigger_callback()

    def stats(self) -> Dict[str, Any]:
        """Get streaming analyzer statistics."""
        with self._lock:
            window_size = len(self._spans)
        return {
            "running": self._running,
            "total_ingested": self._total_ingested,
            "total_errors": self._total_errors,
            "total_alerts": self._total_alerts,
            "window_size": window_size,
            "window_seconds": self._window_seconds,
        }
