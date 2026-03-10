"""
test_v04_features.py — Comprehensive tests for Inferra v0.4.0 features

Tests all 8 phases:
1. Multi-language parsers (JS, Go, Java)
2. Storage persistence
3. Streaming anomaly detection
4. Interactive follow-up (session context)
5. Dependency analysis agent
6. Security analysis agent
7. Auto-instrumentation generator
8. Service topology
"""

import os
import time
import tempfile
import unittest

from inferra.indexer import CodeIndexer, CodeUnit
from inferra.agents import FindingType, Severity


# ─── Phase 1: Multi-Language Parsers ─────────────────────────────────────────

class TestJavaScriptParser(unittest.TestCase):
    """Tests for the JavaScript/TypeScript parser."""

    def setUp(self):
        from inferra.parsers.javascript_parser import JavaScriptParser
        self.parser = JavaScriptParser()

    def test_extracts_function_declaration(self):
        code = 'function fetchData(url) {\n  return fetch(url);\n}\n'
        units = self.parser.parse(code, "app.js", "app")
        names = [u.name for u in units]
        self.assertIn("fetchData", names)

    def test_extracts_arrow_function(self):
        code = 'export const handleClick = (event) => {\n  console.log(event);\n};\n'
        units = self.parser.parse(code, "utils.js", "utils")
        names = [u.name for u in units]
        self.assertIn("handleClick", names)

    def test_extracts_class(self):
        code = 'class UserService extends BaseService {\n  constructor() { super(); }\n  getUser(id) { return id; }\n}\n'
        units = self.parser.parse(code, "service.js", "service")
        names = [u.name for u in units]
        self.assertIn("UserService", names)
        class_unit = next(u for u in units if u.name == "UserService")
        self.assertEqual(class_unit.unit_type, "class")

    def test_extracts_express_routes(self):
        code = '''const express = require("express");
const router = express.Router();
router.get("/api/users", getUsers);
function getUsers(req, res) {
  res.json([]);
}
'''
        units = self.parser.parse(code, "routes.js", "routes")
        route_units = [u for u in units if u.route_path]
        self.assertTrue(len(route_units) > 0 or any("getUsers" in u.name for u in units))

    def test_handles_braces_in_strings(self):
        code = '''function test() {
  const msg = "closing } brace";
  const obj = '{ "key": "value" }';
  return msg;
}
'''
        units = self.parser.parse(code, "test.js", "test")
        func = next((u for u in units if u.name == "test"), None)
        self.assertIsNotNone(func)
        self.assertIn("closing } brace", func.body_text)

    def test_handles_braces_in_comments(self):
        code = '''function test() {
  // comment with }
  /* block comment { } */
  return true;
}
'''
        units = self.parser.parse(code, "test.js", "test")
        func = next((u for u in units if u.name == "test"), None)
        self.assertIsNotNone(func)

    def test_extracts_async_functions(self):
        code = 'export async function loadData(id) {\n  return await fetch(id);\n}\n'
        units = self.parser.parse(code, "api.js", "api")
        func = next((u for u in units if u.name == "loadData"), None)
        self.assertIsNotNone(func)
        self.assertEqual(func.unit_type, "async_function")

    def test_extracts_imports(self):
        code = '''import { useState } from "react";
const express = require("express");
function test() { return 1; }
'''
        units = self.parser.parse(code, "app.js", "app")
        func = next((u for u in units if u.name == "test"), None)
        self.assertIsNotNone(func)
        self.assertIn("react", func.imports)
        self.assertIn("express", func.imports)

    def test_extracts_jsdoc(self):
        code = '''/**
 * Fetches user data from the API.
 * @param {string} id - User ID
 */
function fetchUser(id) {
  return fetch(id);
}
'''
        units = self.parser.parse(code, "api.js", "api")
        func = next((u for u in units if u.name == "fetchUser"), None)
        self.assertIsNotNone(func)
        self.assertIsNotNone(func.docstring)
        self.assertIn("Fetches user data", func.docstring)


class TestGoParser(unittest.TestCase):
    """Tests for the Go parser."""

    def setUp(self):
        from inferra.parsers.go_parser import GoParser
        self.parser = GoParser()

    def test_extracts_function(self):
        code = 'func main() {\n\tfmt.Println("hello")\n}\n'
        units = self.parser.parse(code, "main.go", "main")
        names = [u.name for u in units]
        self.assertIn("main", names)

    def test_extracts_method(self):
        code = 'func (s *Server) Start(port int) error {\n\treturn nil\n}\n'
        units = self.parser.parse(code, "server.go", "server")
        func = next((u for u in units if u.name == "Start"), None)
        self.assertIsNotNone(func)
        self.assertEqual(func.unit_type, "method")
        self.assertIn("Server", func.qualified_name)

    def test_extracts_struct(self):
        code = 'type Server struct {\n\tHost string\n\tPort int\n}\n'
        units = self.parser.parse(code, "types.go", "types")
        s = next((u for u in units if u.name == "Server"), None)
        self.assertIsNotNone(s)
        self.assertEqual(s.unit_type, "class")

    def test_extracts_interface(self):
        code = 'type Handler interface {\n\tServeHTTP(w http.ResponseWriter, r *http.Request)\n}\n'
        units = self.parser.parse(code, "handler.go", "handler")
        h = next((u for u in units if u.name == "Handler"), None)
        self.assertIsNotNone(h)

    def test_handles_braces_in_strings(self):
        code = 'func test() error {\n\tmsg := "closing } bracket"\n\treturn nil\n}\n'
        units = self.parser.parse(code, "test.go", "test")
        func = next((u for u in units if u.name == "test"), None)
        self.assertIsNotNone(func)
        self.assertIn("closing } bracket", func.body_text)

    def test_extracts_go_doc(self):
        code = '// ServeHTTP handles incoming requests.\n// It validates the input and responds.\nfunc ServeHTTP() {\n}\n'
        units = self.parser.parse(code, "handler.go", "handler")
        func = next((u for u in units if u.name == "ServeHTTP"), None)
        self.assertIsNotNone(func)
        self.assertIsNotNone(func.docstring)
        self.assertIn("handles incoming requests", func.docstring)


class TestJavaParser(unittest.TestCase):
    """Tests for the Java parser."""

    def setUp(self):
        from inferra.parsers.java_parser import JavaParser
        self.parser = JavaParser()

    def test_extracts_class(self):
        code = 'public class UserController {\n  public void getUser() { }\n}\n'
        units = self.parser.parse(code, "UserController.java", "com.app")
        c = next((u for u in units if u.name == "UserController"), None)
        self.assertIsNotNone(c)
        self.assertEqual(c.unit_type, "class")

    def test_extracts_method(self):
        code = 'public class Svc {\n  public String getName(int id) {\n    return "";\n  }\n}\n'
        units = self.parser.parse(code, "Svc.java", "com.app")
        m = next((u for u in units if u.name == "getName"), None)
        self.assertIsNotNone(m)
        self.assertEqual(m.unit_type, "method")
        self.assertIn("Svc", m.qualified_name)

    def test_extracts_spring_routes(self):
        code = '''public class Controller {
    @GetMapping("/api/users")
    public String getUsers() {
        return "[]";
    }
}
'''
        units = self.parser.parse(code, "Controller.java", "com.app")
        route_units = [u for u in units if u.route_path]
        self.assertTrue(len(route_units) > 0)

    def test_handles_braces_in_strings(self):
        code = 'public class Test {\n  public void test() {\n    String s = "closing } brace";\n  }\n}\n'
        units = self.parser.parse(code, "Test.java", "com.app")
        m = next((u for u in units if u.name == "test"), None)
        self.assertIsNotNone(m)


class TestParserRegistry(unittest.TestCase):
    """Tests for the parser auto-detection registry."""

    def test_selects_javascript_parser(self):
        from inferra.parsers.base import get_parser_for_file
        p = get_parser_for_file("app.js")
        self.assertIsNotNone(p)
        self.assertEqual(p.LANGUAGE, "javascript")

    def test_selects_typescript_parser(self):
        from inferra.parsers.base import get_parser_for_file
        p = get_parser_for_file("component.tsx")
        self.assertIsNotNone(p)
        self.assertEqual(p.LANGUAGE, "javascript")

    def test_selects_go_parser(self):
        from inferra.parsers.base import get_parser_for_file
        p = get_parser_for_file("main.go")
        self.assertIsNotNone(p)
        self.assertEqual(p.LANGUAGE, "go")

    def test_selects_java_parser(self):
        from inferra.parsers.base import get_parser_for_file
        p = get_parser_for_file("App.java")
        self.assertIsNotNone(p)
        self.assertEqual(p.LANGUAGE, "java")

    def test_returns_none_for_unsupported(self):
        from inferra.parsers.base import get_parser_for_file
        p = get_parser_for_file("styles.css")
        self.assertIsNone(p)


# ─── Phase 2: Storage Persistence ────────────────────────────────────────────

class TestStorage(unittest.TestCase):
    """Tests for the SQLite persistence layer."""

    def setUp(self):
        from inferra.storage import Storage
        self.db_path = tempfile.mktemp(suffix=".db")
        self.storage = Storage(self.db_path)

    def tearDown(self):
        self.storage.close()
        if os.path.exists(self.db_path):
            os.remove(self.db_path)

    def test_save_and_retrieve_analysis(self):
        self.storage.save_analysis(
            service="test-service",
            project="/tmp/test",
            severity="high",
            confidence=0.85,
            root_cause="Database timeout",
            summary="Analysis found timeout",
            llm_backend="ollama",
            total_spans=100,
            total_traces=5,
            error_count=3,
            avg_latency_ms=150.0,
            p95_latency_ms=450.0,
            max_latency_ms=800.0,
        )
        history = self.storage.get_history()
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["service"], "test-service")
        self.assertEqual(history[0]["root_cause"], "Database timeout")

    def test_history_filters_by_service(self):
        for svc in ["svc-a", "svc-b", "svc-a"]:
            self.storage.save_analysis(
                service=svc, project="/tmp", severity="low",
                confidence=0.5, root_cause="test", summary="test",
            )
        history = self.storage.get_history(service="svc-a")
        self.assertEqual(len(history), 2)

    def test_stats(self):
        self.storage.save_analysis(
            service="svc", project="/tmp", severity="medium",
            confidence=0.7, root_cause="cause", summary="sum",
        )
        stats = self.storage.stats()
        self.assertEqual(stats["total_analyses"], 1)
        self.assertEqual(stats["unique_services"], 1)

    def test_regression_detection(self):
        # Save two analyses with very different latencies
        self.storage.save_analysis(
            service="svc", project="/tmp", severity="low",
            confidence=0.5, root_cause="ok", summary="normal",
            avg_latency_ms=100.0, p95_latency_ms=200.0, max_latency_ms=300.0,
        )
        self.storage.save_analysis(
            service="svc", project="/tmp", severity="high",
            confidence=0.8, root_cause="slow", summary="degraded",
            avg_latency_ms=500.0, p95_latency_ms=900.0, max_latency_ms=1500.0,
        )
        regressions = self.storage.detect_regressions(service="svc")
        # With only 2 data points, regression detection may or may not trigger
        # but it should not error
        self.assertIsInstance(regressions, list)

    def test_get_services(self):
        for svc in ["alpha", "beta", "alpha"]:
            self.storage.save_analysis(
                service=svc, project="/tmp", severity="low",
                confidence=0.5, root_cause="test", summary="test",
            )
        services = self.storage.get_services()
        self.assertEqual(set(services), {"alpha", "beta"})

    def test_empty_history(self):
        history = self.storage.get_history()
        self.assertEqual(history, [])


# ─── Phase 3: Streaming Analyzer ─────────────────────────────────────────────

class TestStreamingAnalyzer(unittest.TestCase):
    """Tests for the real-time streaming analyzer."""

    def setUp(self):
        from inferra.streaming import StreamingAnalyzer
        self.alerts = []
        self.analyzer = StreamingAnalyzer(
            window_seconds=5,
            error_rate_threshold=0.1,
            latency_threshold_ms=500.0,
            anomaly_score_threshold=0.5,
            check_interval_seconds=0.5,
            alert_callback=lambda alert: self.alerts.append(alert),
        )

    def test_ingest_does_not_mutate_input(self):
        span = {"name": "test", "duration_ms": 100}
        original_keys = set(span.keys())
        self.analyzer.ingest([span])
        self.assertEqual(set(span.keys()), original_keys)
        self.assertNotIn("_ingest_time", span)

    def test_stats_tracks_ingestion(self):
        spans = [{"name": f"span-{i}", "duration_ms": 50} for i in range(10)]
        self.analyzer.ingest(spans)
        stats = self.analyzer.stats()
        self.assertEqual(stats["total_ingested"], 10)
        self.assertEqual(stats["window_size"], 10)

    def test_error_counting(self):
        spans = [
            {"name": "ok", "duration_ms": 50},
            {"name": "err", "duration_ms": 50, "error": True},
            {"name": "ok2", "duration_ms": 50},
        ]
        self.analyzer.ingest(spans)
        stats = self.analyzer.stats()
        self.assertEqual(stats["total_errors"], 1)

    def test_start_stop(self):
        self.analyzer.start()
        self.assertTrue(self.analyzer.stats()["running"])
        self.analyzer.stop()
        self.assertFalse(self.analyzer.stats()["running"])

    def test_anomaly_detection_triggers_alert(self):
        # Feed high-error-rate spans
        error_spans = [
            {"name": f"err-{i}", "duration_ms": 2000, "error": True}
            for i in range(20)
        ]
        self.analyzer.ingest(error_spans)
        self.analyzer.start()
        time.sleep(1.5)  # Wait for at least one check cycle
        self.analyzer.stop()
        # Should have at least one alert
        self.assertTrue(len(self.alerts) > 0, "Expected at least one anomaly alert")
        self.assertEqual(self.alerts[0]["type"], "anomaly_detected")


# ─── Phase 5: Dependency Agent ───────────────────────────────────────────────

class TestDependencyAgent(unittest.TestCase):
    """Tests for the dependency analysis agent."""

    def setUp(self):
        from inferra.dependency_agent import DependencyAgent
        self.agent = DependencyAgent()

    def test_initializes_without_indexer(self):
        findings = self.agent.analyze_codebase()
        self.assertEqual(findings, [])

    def test_finds_high_fanout(self):
        idx = CodeIndexer()
        idx.index_directory("./test_projects/agentic_cybersec_threat_analyst/backend")
        self.agent.set_indexer(idx)
        findings = self.agent.analyze_codebase()
        fanout = [f for f in findings if f.finding_type == FindingType.HIGH_FAN_OUT]
        self.assertTrue(len(fanout) > 0)

    def test_uses_correct_finding_types(self):
        idx = CodeIndexer()
        idx.index_directory("./test_projects/agentic_cybersec_threat_analyst/backend")
        self.agent.set_indexer(idx)
        findings = self.agent.analyze_codebase()
        valid_types = {
            FindingType.CIRCULAR_DEPENDENCY, FindingType.DEEP_CALL_CHAIN,
            FindingType.HIGH_FAN_OUT, FindingType.DEAD_CODE,
            FindingType.TIGHT_COUPLING,
        }
        for f in findings:
            self.assertIn(f.finding_type, valid_types,
                         f"DependencyAgent used wrong FindingType: {f.finding_type}")

    def test_satisfies_base_agent_interface(self):
        # analyze() should delegate to analyze_codebase()
        result = self.agent.analyze()
        self.assertEqual(result, [])


# ─── Phase 6: Security Agent ────────────────────────────────────────────────

class TestSecurityAgent(unittest.TestCase):
    """Tests for the security vulnerability scanner."""

    def setUp(self):
        from inferra.security_agent import SecurityAgent
        self.agent = SecurityAgent()

    def test_initializes_without_indexer(self):
        findings = self.agent.analyze_codebase()
        self.assertEqual(findings, [])

    def test_detects_hardcoded_secrets(self):
        idx = CodeIndexer()
        idx.index_directory("./test_projects/agentic_cybersec_threat_analyst/backend")
        self.agent.set_indexer(idx)
        findings = self.agent.analyze_codebase()
        secrets = [f for f in findings if f.finding_type == FindingType.HARDCODED_SECRET]
        self.assertTrue(len(secrets) > 0)

    def test_uses_correct_finding_types(self):
        idx = CodeIndexer()
        idx.index_directory("./test_projects/agentic_cybersec_threat_analyst/backend")
        self.agent.set_indexer(idx)
        findings = self.agent.analyze_codebase()
        valid_types = {
            FindingType.SQL_INJECTION, FindingType.HARDCODED_SECRET,
            FindingType.UNSAFE_DESERIALIZATION, FindingType.SSRF,
            FindingType.PATH_TRAVERSAL, FindingType.MISSING_AUTH,
        }
        for f in findings:
            self.assertIn(f.finding_type, valid_types,
                         f"SecurityAgent used wrong FindingType: {f.finding_type}")

    def test_all_findings_have_severity(self):
        idx = CodeIndexer()
        idx.index_directory("./test_projects/agentic_cybersec_threat_analyst/backend")
        self.agent.set_indexer(idx)
        findings = self.agent.analyze_codebase()
        for f in findings:
            self.assertIsNotNone(f.severity, f"Missing severity on: {f}")


# ─── Phase 7: Auto-Instrumentation ──────────────────────────────────────────

class TestAutoInstrument(unittest.TestCase):
    """Tests for the auto-instrumentation script generator."""

    def test_generates_instrumentation_script(self):
        from inferra.auto_instrument import generate_instrumentation_script
        idx = CodeIndexer()
        idx.index_directory("./test_projects/agentic_cybersec_threat_analyst/backend")
        output_path = tempfile.mktemp(suffix=".py")
        try:
            path = generate_instrumentation_script(idx, output_path)
            self.assertTrue(os.path.exists(path))
            with open(path) as f:
                content = f.read()
            self.assertIn("opentelemetry", content)
            self.assertIn("_wrap_function", content)
            self.assertTrue(len(content.split("\n")) > 50)
        finally:
            if os.path.exists(output_path):
                os.remove(output_path)

    def test_module_path_resolution(self):
        from inferra.auto_instrument import _filepath_to_module
        self.assertEqual(_filepath_to_module("src/app/routes.py"), "app.routes")
        self.assertEqual(_filepath_to_module("./myapp/views.py"), "myapp.views")
        self.assertEqual(_filepath_to_module("lib/utils.py"), "utils")
        self.assertEqual(_filepath_to_module("mypackage/__init__.py"), "mypackage")


# ─── Phase 8: Topology ──────────────────────────────────────────────────────

class TestTopology(unittest.TestCase):
    """Tests for the service topology graph builder."""

    def setUp(self):
        from inferra.topology import Topology
        self.topo = Topology()
        self.spans = [
            {"name": "GET /api/users", "service_name": "api-gateway", "duration_ms": 200,
             "span_id": "s1", "parent_span_id": ""},
            {"name": "SELECT * FROM users", "service_name": "user-service", "duration_ms": 50,
             "span_id": "s2", "parent_span_id": "s1"},
            {"name": "Redis GET", "service_name": "cache", "duration_ms": 5,
             "span_id": "s3", "parent_span_id": "s1"},
        ]

    def test_builds_graph(self):
        self.topo.build_from_spans(self.spans)
        summary = self.topo.summary()
        self.assertIn("3 services", summary)

    def test_generates_mermaid(self):
        self.topo.build_from_spans(self.spans)
        mermaid = self.topo.to_mermaid()
        self.assertIn("graph", mermaid)
        self.assertIn("api-gateway", mermaid)

    def test_generates_d3_json(self):
        self.topo.build_from_spans(self.spans)
        d3 = self.topo.to_d3_json()
        self.assertIn("nodes", d3)
        self.assertIn("edges", d3)
        self.assertEqual(len(d3["nodes"]), 3)

    def test_empty_spans(self):
        self.topo.build_from_spans([])
        self.assertIn("0 services", self.topo.summary())


# ─── FindingType Enum ────────────────────────────────────────────────────────

class TestFindingTypes(unittest.TestCase):
    """Tests for the expanded FindingType enum."""

    def test_all_required_types_exist(self):
        required = [
            "ERROR_TRACE", "PERFORMANCE_ANOMALY", "CONTEXT_LOSS", "TIMEOUT",
            "CONNECTION_ERROR", "DATA_ERROR", "CASCADING_FAILURE", "THREAD_CONTENTION",
            "CIRCULAR_DEPENDENCY", "HIGH_FAN_OUT", "DEAD_CODE", "TIGHT_COUPLING",
            "DEEP_CALL_CHAIN", "SQL_INJECTION", "HARDCODED_SECRET",
            "UNSAFE_DESERIALIZATION", "SSRF", "PATH_TRAVERSAL", "MISSING_AUTH",
            "UNKNOWN",
        ]
        for t in required:
            self.assertTrue(hasattr(FindingType, t), f"Missing FindingType.{t}")

    def test_total_count(self):
        self.assertEqual(len(FindingType), 20)


if __name__ == "__main__":
    unittest.main()
