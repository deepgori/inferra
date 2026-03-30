"""
storage.py — SQLite Persistence Layer

Stores analysis results for historical tracking and regression detection.

Tables:
    analyses: RCA report summaries with timestamps
    spans:    Raw span data per analysis
    findings: Individual agent findings

Usage:
    storage = Storage()  # defaults to ~/.inferra/history.db
    storage.save_analysis(report, spans, service_name="my-app")
    history = storage.get_history(limit=20)
    regressions = storage.detect_regressions("my-app", window_days=7)
"""

import json
import logging
import os
import sqlite3
import time
from dataclasses import asdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

log = logging.getLogger(__name__)


class Storage:
    """SQLite-backed persistence for Inferra analysis history."""

    def __init__(self, db_path: Optional[str] = None):
        if db_path is None:
            db_dir = Path.home() / ".inferra"
            db_dir.mkdir(exist_ok=True)
            db_path = str(db_dir / "history.db")

        self._db_path = db_path
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._init_schema()
        log.info(f"  Storage: {db_path}")

    def _init_schema(self):
        """Create tables if they don't exist."""
        self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS analyses (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp   TEXT NOT NULL DEFAULT (datetime('now')),
                service     TEXT NOT NULL DEFAULT 'unknown',
                project     TEXT DEFAULT '',
                severity    TEXT DEFAULT 'low',
                confidence  REAL DEFAULT 0.0,
                root_cause  TEXT DEFAULT '',
                summary     TEXT DEFAULT '',
                llm_backend TEXT DEFAULT '',
                total_spans INTEGER DEFAULT 0,
                total_traces INTEGER DEFAULT 0,
                error_count INTEGER DEFAULT 0,
                avg_latency_ms REAL DEFAULT 0.0,
                p95_latency_ms REAL DEFAULT 0.0,
                max_latency_ms REAL DEFAULT 0.0,
                report_path TEXT DEFAULT '',
                metadata    TEXT DEFAULT '{}'
            );

            CREATE TABLE IF NOT EXISTS span_stats (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                analysis_id INTEGER NOT NULL,
                span_name   TEXT NOT NULL,
                count       INTEGER DEFAULT 1,
                avg_ms      REAL DEFAULT 0.0,
                max_ms      REAL DEFAULT 0.0,
                error_rate  REAL DEFAULT 0.0,
                FOREIGN KEY (analysis_id) REFERENCES analyses(id)
            );

            CREATE TABLE IF NOT EXISTS findings (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                analysis_id INTEGER NOT NULL,
                agent_name  TEXT NOT NULL,
                finding_type TEXT DEFAULT '',
                severity    TEXT DEFAULT 'low',
                summary     TEXT DEFAULT '',
                confidence  REAL DEFAULT 0.0,
                FOREIGN KEY (analysis_id) REFERENCES analyses(id)
            );

            CREATE INDEX IF NOT EXISTS idx_analyses_service ON analyses(service);
            CREATE INDEX IF NOT EXISTS idx_analyses_timestamp ON analyses(timestamp);
            CREATE INDEX IF NOT EXISTS idx_span_stats_analysis ON span_stats(analysis_id);

            CREATE TABLE IF NOT EXISTS sessions (
                session_id  TEXT PRIMARY KEY,
                data        TEXT NOT NULL DEFAULT '{}',
                updated_at  TEXT NOT NULL DEFAULT (datetime('now'))
            );
        """)
        self._conn.commit()

    def save_analysis(
        self,
        *,
        service: str = "unknown",
        project: str = "",
        severity: str = "low",
        confidence: float = 0.0,
        root_cause: str = "",
        summary: str = "",
        llm_backend: str = "",
        total_spans: int = 0,
        total_traces: int = 0,
        error_count: int = 0,
        avg_latency_ms: float = 0.0,
        p95_latency_ms: float = 0.0,
        max_latency_ms: float = 0.0,
        report_path: str = "",
        span_stats: Optional[List[Dict]] = None,
        findings_list: Optional[List[Dict]] = None,
        metadata: Optional[Dict] = None,
    ) -> int:
        """
        Save a complete analysis to the database.

        Returns the analysis ID.
        """
        cur = self._conn.execute(
            """INSERT INTO analyses
            (service, project, severity, confidence, root_cause, summary,
             llm_backend, total_spans, total_traces, error_count,
             avg_latency_ms, p95_latency_ms, max_latency_ms, report_path, metadata)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                service, project, severity, confidence, root_cause, summary,
                llm_backend, total_spans, total_traces, error_count,
                avg_latency_ms, p95_latency_ms, max_latency_ms, report_path,
                json.dumps(metadata or {}),
            ),
        )
        analysis_id = cur.lastrowid

        # Save per-span statistics
        if span_stats:
            for ss in span_stats:
                self._conn.execute(
                    """INSERT INTO span_stats
                    (analysis_id, span_name, count, avg_ms, max_ms, error_rate)
                    VALUES (?, ?, ?, ?, ?, ?)""",
                    (
                        analysis_id,
                        ss.get("name", ""),
                        ss.get("count", 1),
                        ss.get("avg_ms", 0.0),
                        ss.get("max_ms", 0.0),
                        ss.get("error_rate", 0.0),
                    ),
                )

        # Save findings
        if findings_list:
            for f in findings_list:
                self._conn.execute(
                    """INSERT INTO findings
                    (analysis_id, agent_name, finding_type, severity, summary, confidence)
                    VALUES (?, ?, ?, ?, ?, ?)""",
                    (
                        analysis_id,
                        f.get("agent_name", ""),
                        f.get("finding_type", ""),
                        f.get("severity", "low"),
                        f.get("summary", ""),
                        f.get("confidence", 0.0),
                    ),
                )

        self._conn.commit()
        log.info(f"  Saved analysis #{analysis_id} for service={service}")
        return analysis_id

    def get_history(
        self,
        service: Optional[str] = None,
        limit: int = 20,
    ) -> List[Dict[str, Any]]:
        """Get recent analysis history, optionally filtered by service."""
        if service:
            rows = self._conn.execute(
                "SELECT * FROM analyses WHERE service = ? ORDER BY timestamp DESC LIMIT ?",
                (service, limit),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM analyses ORDER BY timestamp DESC LIMIT ?",
                (limit,),
            ).fetchall()

        results = []
        for row in rows:
            entry = dict(row)
            entry["metadata"] = json.loads(entry.get("metadata", "{}"))
            # Attach findings
            findings = self._conn.execute(
                "SELECT * FROM findings WHERE analysis_id = ?",
                (entry["id"],),
            ).fetchall()
            entry["findings"] = [dict(f) for f in findings]
            results.append(entry)

        return results

    def detect_regressions(
        self,
        service: str,
        window_days: int = 7,
        threshold: float = 1.5,
    ) -> List[Dict[str, Any]]:
        """
        Detect performance regressions by comparing recent analyses
        against the historical baseline.

        Args:
            service: Service name to check
            window_days: How far back to look
            threshold: Alert if current > baseline * threshold (e.g. 1.5x)

        Returns:
            List of regression alerts with details
        """
        cutoff = (datetime.utcnow() - timedelta(days=window_days)).isoformat()

        rows = self._conn.execute(
            """SELECT avg_latency_ms, p95_latency_ms, max_latency_ms, timestamp
            FROM analyses
            WHERE service = ? AND timestamp >= ?
            ORDER BY timestamp ASC""",
            (service, cutoff),
        ).fetchall()

        if len(rows) < 2:
            return []

        # Compare first half (baseline) vs second half (recent)
        mid = len(rows) // 2
        baseline = rows[:mid]
        recent = rows[mid:]

        def avg(rows_list, field):
            vals = [r[field] for r in rows_list if r[field]]
            return sum(vals) / len(vals) if vals else 0

        regressions = []
        for metric in ["avg_latency_ms", "p95_latency_ms", "max_latency_ms"]:
            baseline_val = avg(baseline, metric)
            recent_val = avg(recent, metric)

            if baseline_val > 0 and recent_val > baseline_val * threshold:
                regressions.append({
                    "metric": metric,
                    "baseline": round(baseline_val, 1),
                    "current": round(recent_val, 1),
                    "change": f"+{((recent_val / baseline_val - 1) * 100):.0f}%",
                    "service": service,
                    "window_days": window_days,
                })

        # Per-span regression detection
        span_rows = self._conn.execute(
            """SELECT ss.span_name, ss.avg_ms, a.timestamp
            FROM span_stats ss
            JOIN analyses a ON ss.analysis_id = a.id
            WHERE a.service = ? AND a.timestamp >= ?
            ORDER BY a.timestamp ASC""",
            (service, cutoff),
        ).fetchall()

        # Group by span name
        span_data = {}
        for r in span_rows:
            name = r["span_name"]
            if name not in span_data:
                span_data[name] = []
            span_data[name].append(r["avg_ms"])

        for name, values in span_data.items():
            if len(values) < 2:
                continue
            mid = len(values) // 2
            base = sum(values[:mid]) / mid if mid > 0 else 0
            curr = sum(values[mid:]) / (len(values) - mid) if len(values) - mid > 0 else 0
            if base > 0 and curr > base * threshold:
                regressions.append({
                    "metric": f"span:{name}",
                    "baseline": round(base, 1),
                    "current": round(curr, 1),
                    "change": f"+{((curr / base - 1) * 100):.0f}%",
                    "service": service,
                })

        return regressions

    def get_span_trends(
        self,
        service: str,
        span_name: str,
        limit: int = 50,
    ) -> List[Dict[str, Any]]:
        """Get historical trend for a specific span."""
        rows = self._conn.execute(
            """SELECT ss.avg_ms, ss.max_ms, ss.count, ss.error_rate, a.timestamp
            FROM span_stats ss
            JOIN analyses a ON ss.analysis_id = a.id
            WHERE a.service = ? AND ss.span_name = ?
            ORDER BY a.timestamp DESC LIMIT ?""",
            (service, span_name, limit),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_services(self) -> List[str]:
        """Get all unique service names."""
        rows = self._conn.execute(
            "SELECT DISTINCT service FROM analyses ORDER BY service"
        ).fetchall()
        return [r["service"] for r in rows]

    def stats(self) -> Dict[str, Any]:
        """Get storage statistics."""
        total = self._conn.execute("SELECT COUNT(*) as c FROM analyses").fetchone()["c"]
        services = len(self.get_services())
        findings = self._conn.execute("SELECT COUNT(*) as c FROM findings").fetchone()["c"]
        return {
            "total_analyses": total,
            "unique_services": services,
            "total_findings": findings,
            "db_path": self._db_path,
        }

    # ------------------------------------------------------------------
    # v0.5.0 — Session persistence for /v1/ask follow-up
    # ------------------------------------------------------------------

    def save_session(self, session_id: str, session_data: Dict[str, Any]) -> None:
        """Save an analysis session context for interactive follow-up.

        Persists to SQLite so /v1/ask survives server restarts.
        """
        self._conn.execute(
            """INSERT OR REPLACE INTO sessions (session_id, data, updated_at)
            VALUES (?, ?, datetime('now'))""",
            (session_id, json.dumps(session_data, default=str)),
        )
        self._conn.commit()

    def load_session(self, session_id: str = "latest") -> Optional[Dict[str, Any]]:
        """Load the most recent analysis session or a specific one."""
        if session_id == "latest":
            row = self._conn.execute(
                "SELECT data FROM sessions ORDER BY updated_at DESC LIMIT 1"
            ).fetchone()
        else:
            row = self._conn.execute(
                "SELECT data FROM sessions WHERE session_id = ?",
                (session_id,),
            ).fetchone()
        if row:
            return json.loads(row["data"])
        return None

    def close(self):
        self._conn.close()

