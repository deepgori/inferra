"""
static_tracer.py — Universal Static Call Graph & Trace Generator

For non-Python projects (JS, TS, Go, Java) or Python projects that fail
to import, build a call graph from the indexed CodeUnits and generate
synthetic TraceEvents that feed directly into the existing RCA pipeline.

This makes Inferra produce rich reports for ANY language.
"""

import re
import time
import uuid
import os
import logging
from typing import Dict, List, Optional, Set, Tuple
from collections import defaultdict

from async_content_tracer.tracer import TraceEvent, EventType

log = logging.getLogger("inferra")


class StaticTracer:
    """Build call graphs from indexed code units without executing code.

    Walks function bodies to extract call expressions, resolves them
    to indexed CodeUnits, and generates synthetic TraceEvents for
    the existing ExecutionGraph + RCA pipeline.
    """

    def __init__(self, indexer, project_path: str):
        self.indexer = indexer
        self.project_path = project_path
        self.call_graph: Dict[str, List[str]] = {}        # qname → [callee qnames]
        self.reverse_graph: Dict[str, List[str]] = {}     # qname → [caller qnames]
        self._name_to_unit = {}                            # name → CodeUnit
        self._qname_to_unit = {}                           # qualified_name → CodeUnit

    def build(self):
        """Build the full call graph from indexed code units."""
        # Build lookup indices
        for unit in self.indexer._units:
            self._qname_to_unit[unit.qualified_name] = unit
            # For simple name lookups, last-writer-wins (good enough)
            self._name_to_unit[unit.name] = unit

        # Extract calls for units that have empty calls lists (JS/TS/Go/Java)
        for unit in self.indexer._units:
            if not unit.calls and unit.body_text:
                unit.calls = self._extract_calls_from_body(unit)

        # Build the graph
        for unit in self.indexer._units:
            resolved = []
            for call_name in unit.calls:
                target = self._resolve_call(call_name, unit)
                if target:
                    resolved.append(target.qualified_name)

            self.call_graph[unit.qualified_name] = resolved
            for callee in resolved:
                if callee not in self.reverse_graph:
                    self.reverse_graph[callee] = []
                self.reverse_graph[callee].append(unit.qualified_name)

        log.info("Static call graph: %d nodes, %d edges",
                 len(self.call_graph),
                 sum(len(v) for v in self.call_graph.values()))

    def _extract_calls_from_body(self, unit) -> List[str]:
        """Extract function call names from source code body using regex.

        Handles: foo(), this.foo(), self.foo(), obj.foo(), await foo(),
                 new Foo(), Foo.bar(), module.exports = ...
        """
        body = unit.body_text
        if not body:
            return []

        calls = set()

        # Standard function calls: foo(, bar(, baz(
        for m in re.finditer(r'\b([a-zA-Z_]\w*)\s*\(', body):
            name = m.group(1)
            # Skip language keywords and common builtins
            if name in _SKIP_NAMES:
                continue
            calls.add(name)

        # Method calls: obj.method(, this.method(, self.method(
        for m in re.finditer(r'(?:this|self|\w+)\.([a-zA-Z_]\w*)\s*\(', body):
            name = m.group(1)
            if name not in _SKIP_NAMES:
                calls.add(name)

        # Constructor calls: new ClassName(
        for m in re.finditer(r'\bnew\s+([A-Z]\w*)\s*\(', body):
            calls.add(m.group(1))

        # Remove the function's own name to avoid self-reference from recursion detection
        calls.discard(unit.name)

        return list(calls)

    def _resolve_call(self, call_name: str, caller_unit) -> Optional[object]:
        """Resolve a call name to an indexed CodeUnit.

        Resolution order:
        1. Exact qualified name match
        2. Same-file function match
        3. Same-module function match
        4. Global name match
        """
        # 1. Exact qualified name
        if call_name in self._qname_to_unit:
            return self._qname_to_unit[call_name]

        # 2. Same file first (most likely for internal calls)
        for unit in self.indexer._units:
            if (unit.source_file == caller_unit.source_file and
                    unit.name == call_name and
                    unit.qualified_name != caller_unit.qualified_name):
                return unit

        # 3. Same module prefix
        caller_mod_parts = caller_unit.qualified_name.split(".")
        if len(caller_mod_parts) > 1:
            caller_package = ".".join(caller_mod_parts[:-1])
            candidate = f"{caller_package}.{call_name}"
            if candidate in self._qname_to_unit:
                return self._qname_to_unit[candidate]

        # 4. Global name match
        if call_name in self._name_to_unit:
            target = self._name_to_unit[call_name]
            if target.qualified_name != caller_unit.qualified_name:
                return target

        return None

    def find_entry_points(self) -> List:
        """Find functions that are likely entry points.

        Entry points are functions that:
        - Are never called by other functions in the codebase
        - OR match known entry point patterns
        - OR are exported / decorated as routes
        """
        all_called = set()
        for callees in self.call_graph.values():
            all_called.update(callees)

        entry_points = []

        # Functions never called by anything (roots of the call graph)
        for unit in self.indexer._units:
            if unit.unit_type in ("function", "async_function", "method"):
                if unit.qualified_name not in all_called:
                    entry_points.append(unit)

        # Also include route handlers and known patterns
        for unit in self.indexer._units:
            if unit.route_path and unit not in entry_points:
                entry_points.append(unit)
            elif unit.name in _ENTRY_NAMES and unit not in entry_points:
                entry_points.append(unit)

        # Sort: routes first, then main-like, then alphabetical
        def sort_key(u):
            if u.route_path:
                return (0, u.route_path)
            if u.name in ("main", "run", "start", "init", "setup"):
                return (1, u.name)
            return (2, u.name)

        entry_points.sort(key=sort_key)
        return entry_points

    def to_trace_events(self, max_depth: int = 12) -> List[TraceEvent]:
        """Generate synthetic TraceEvents by walking the call graph from entry points.

        These events feed the existing ExecutionGraph + RCA pipeline unchanged.
        """
        entries = self.find_entry_points()
        if not entries:
            entries = list(self.indexer._units)[:10]

        events = []
        visited_in_path = set()  # prevent infinite loops from cycles
        t = time.monotonic()
        trace_id = uuid.uuid4().hex[:16]

        for ep in entries[:15]:  # cap to avoid explosion
            self._walk_and_emit(
                ep.qualified_name, events, visited_in_path,
                depth=0, max_depth=max_depth,
                t_offset=[t], trace_id=trace_id,
            )

        return events

    def _walk_and_emit(
        self, qname: str, events: List[TraceEvent],
        visited: set, depth: int, max_depth: int,
        t_offset: list, trace_id: str,
    ):
        """Recursively walk the call graph emitting entry/exit events."""
        if depth >= max_depth or qname in visited:
            return
        if qname not in self._qname_to_unit:
            return

        unit = self._qname_to_unit[qname]
        visited.add(qname)

        span_id = uuid.uuid4().hex[:16]
        start_t = t_offset[0]
        t_offset[0] += 0.001  # 1ms synthetic gap

        # ENTRY
        events.append(TraceEvent(
            event_type=EventType.ENTRY,
            function_name=unit.name,
            module=unit.qualified_name.rsplit(".", 1)[0] if "." in unit.qualified_name else "",
            source_file=unit.source_file,
            source_line=unit.start_line,
            timestamp=start_t,
            duration=None,
            context_id=trace_id,
            span_id=span_id,
            parent_span_id=None,
            depth=depth,
            thread_id=0,
            thread_name="static-analysis",
        ))

        # Walk children
        for callee in self.call_graph.get(qname, []):
            self._walk_and_emit(callee, events, visited, depth + 1, max_depth, t_offset, trace_id)

        end_t = t_offset[0]
        t_offset[0] += 0.001

        # EXIT (or ERROR if the function body has obvious issues)
        error = self._detect_static_error(unit)
        events.append(TraceEvent(
            event_type=EventType.ERROR if error else EventType.EXIT,
            function_name=unit.name,
            module=unit.qualified_name.rsplit(".", 1)[0] if "." in unit.qualified_name else "",
            source_file=unit.source_file,
            source_line=unit.start_line,
            timestamp=end_t,
            duration=end_t - start_t,
            context_id=trace_id,
            span_id=span_id,
            parent_span_id=None,
            depth=depth,
            thread_id=0,
            thread_name="static-analysis",
            error=error,
        ))

        visited.discard(qname)  # allow the same function in different paths

    def _detect_static_error(self, unit) -> Optional[str]:
        """Detect obvious issues via static analysis."""
        body = unit.body_text

        # TODO / FIXME / HACK comments
        for marker in ("TODO", "FIXME", "HACK", "XXX", "BUG"):
            if marker in body:
                # Find the line
                for line in body.split("\n"):
                    if marker in line:
                        clean = line.strip().lstrip("/#*- ")
                        return f"Code quality: {clean[:120]}"

        # Bare except
        if re.search(r'\bexcept\s*:', body):
            return "Anti-pattern: bare except clause (swallows all exceptions)"

        # console.log / print left in production code
        if unit.route_path:  # only flag in route handlers
            if "console.log(" in body or ("print(" in body and "print_" not in body):
                return "Debug artifact: logging in production route handler"

        return None

    def find_issues(self) -> List[dict]:
        """Find structural issues in the codebase.

        Returns list of {type, severity, summary, source, evidence} dicts.
        """
        issues = []

        # ── Dead Code ──
        all_called = set()
        for callees in self.call_graph.values():
            all_called.update(callees)

        for unit in self.indexer._units:
            if unit.unit_type in ("function", "async_function"):
                if (unit.qualified_name not in all_called and
                        unit.name not in _ENTRY_NAMES and
                        not unit.route_path and
                        not unit.name.startswith("_") and
                        not unit.name.startswith("test")):
                    issues.append({
                        "type": "dead_code",
                        "severity": "info",
                        "summary": f"Unused function: {unit.name}() is never called",
                        "source": f"{_rel(unit.source_file, self.project_path)}:{unit.start_line}",
                        "evidence": [f"No callers found in the codebase for {unit.qualified_name}"],
                    })

        # ── Circular Dependencies ──
        cycles = self._find_cycles()
        for cycle in cycles[:5]:  # cap
            cycle_str = " → ".join(c.split(".")[-1] for c in cycle)
            issues.append({
                "type": "circular_dependency",
                "severity": "warning",
                "summary": f"Circular call chain: {cycle_str}",
                "source": cycle[0],
                "evidence": [f"Full cycle: {' → '.join(cycle)}"],
            })

        # ── Large Functions ──
        for unit in self.indexer._units:
            if unit.unit_type in ("function", "async_function"):
                lines = (unit.end_line - unit.start_line)
                if lines > 80:
                    issues.append({
                        "type": "complexity",
                        "severity": "info",
                        "summary": f"Large function: {unit.name}() is {lines} lines",
                        "source": f"{_rel(unit.source_file, self.project_path)}:{unit.start_line}",
                        "evidence": [f"{lines} lines — consider breaking into smaller functions"],
                    })

        # ── High Fan-Out ──
        for qname, callees in self.call_graph.items():
            if len(callees) > 10:
                unit = self._qname_to_unit.get(qname)
                if unit:
                    issues.append({
                        "type": "complexity",
                        "severity": "info",
                        "summary": f"High fan-out: {unit.name}() calls {len(callees)} other functions",
                        "source": f"{_rel(unit.source_file, self.project_path)}:{unit.start_line}",
                        "evidence": [f"Calls: {', '.join(c.split('.')[-1] for c in callees[:8])}..."],
                    })

        # ── Arity Mismatches (Python only) ──
        for unit in self.indexer._units:
            if unit.signature and "def " in unit.signature:
                # Count expected params
                sig = unit.signature
                params_match = re.search(r'\(([^)]*)\)', sig)
                if params_match:
                    params = [p.strip() for p in params_match.group(1).split(",") if p.strip()]
                    params = [p for p in params if p != "self" and p != "cls" and not p.startswith("*")]
                    required = [p for p in params if "=" not in p]

                    # Check callers
                    for caller_qname, callees in self.call_graph.items():
                        if unit.qualified_name in callees:
                            caller = self._qname_to_unit.get(caller_qname)
                            if caller and caller.body_text:
                                # Try to find the call and count args
                                call_pattern = re.search(
                                    rf'\b{re.escape(unit.name)}\s*\(([^)]*)\)',
                                    caller.body_text
                                )
                                if call_pattern:
                                    call_args = [a.strip() for a in call_pattern.group(1).split(",") if a.strip()]
                                    if len(call_args) < len(required):
                                        issues.append({
                                            "type": "arity_mismatch",
                                            "severity": "critical",
                                            "summary": f"Arity mismatch: {unit.name}() expects {len(required)} args, called with {len(call_args)}",
                                            "source": f"{_rel(caller.source_file, self.project_path)}:{caller.start_line}",
                                            "evidence": [
                                                f"Function signature: {sig}",
                                                f"Called with {len(call_args)} argument(s)",
                                                f"Expected at least {len(required)} required argument(s)",
                                            ],
                                        })

        return issues

    def _find_cycles(self, max_cycles: int = 10) -> List[List[str]]:
        """Find cycles in the call graph using DFS."""
        cycles = []
        visited = set()
        path = []
        path_set = set()

        def dfs(node):
            if len(cycles) >= max_cycles:
                return
            if node in path_set:
                idx = path.index(node)
                cycles.append(path[idx:] + [node])
                return
            if node in visited:
                return
            visited.add(node)
            path.append(node)
            path_set.add(node)
            for neighbor in self.call_graph.get(node, []):
                dfs(neighbor)
            path.pop()
            path_set.discard(node)

        for node in self.call_graph:
            if node not in visited:
                dfs(node)

        return cycles

    def get_summary(self) -> str:
        """Get a human-readable summary of the call graph."""
        total_nodes = len(self.call_graph)
        total_edges = sum(len(v) for v in self.call_graph.values())
        entries = self.find_entry_points()
        issues = self.find_issues()

        lines = [
            "════════════════════════════════════════════════════════════",
            "  STATIC CALL GRAPH ANALYSIS",
            "════════════════════════════════════════════════════════════",
            f"  Functions analyzed:    {total_nodes}",
            f"  Call relationships:    {total_edges}",
            f"  Entry points:         {len(entries)}",
            f"  Structural issues:    {len(issues)}",
            "",
        ]

        if entries:
            lines.append("  📍 ENTRY POINTS:")
            for ep in entries[:10]:
                route = f"  [{ep.route_path}]" if ep.route_path else ""
                lines.append(f"    → {ep.name}(){route}")

        crit = [i for i in issues if i["severity"] == "critical"]
        warns = [i for i in issues if i["severity"] == "warning"]
        if crit:
            lines.append(f"\n  ❌ CRITICAL ISSUES ({len(crit)}):")
            for issue in crit[:5]:
                lines.append(f"    → {issue['summary']}")
        if warns:
            lines.append(f"\n  ⚠️  WARNINGS ({len(warns)}):")
            for issue in warns[:5]:
                lines.append(f"    → {issue['summary']}")

        lines.append("════════════════════════════════════════════════════════════")
        return "\n".join(lines)

    def get_call_tree(self, max_depth: int = 6) -> str:
        """Get a visual call tree from entry points."""
        entries = self.find_entry_points()
        if not entries:
            return "(no entry points found)"

        lines = []
        shown = set()

        for ep in entries[:8]:
            self._print_tree(ep.qualified_name, lines, shown, depth=0, max_depth=max_depth)

        return "\n".join(lines) if lines else "(empty call tree)"

    def _print_tree(self, qname: str, lines: list, shown: set, depth: int, max_depth: int):
        """Recursively print a call tree."""
        if depth >= max_depth or qname in shown:
            if qname in shown and self.call_graph.get(qname):
                indent = "  " * depth
                short = qname.split(".")[-1]
                lines.append(f"{indent}↻ {short}() (see above)")
            return

        shown.add(qname)
        unit = self._qname_to_unit.get(qname)
        if not unit:
            return

        indent = "  " * depth
        short = unit.name
        loc = _rel(unit.source_file, self.project_path)
        callees = self.call_graph.get(qname, [])
        route = f" [{unit.route_path}]" if unit.route_path else ""
        error = self._detect_static_error(unit)
        err_str = f" ⚠ {error[:60]}" if error else ""

        connector = "├─" if depth > 0 else ""
        lines.append(f"{indent}{connector}{short}(){route}  {loc}:{unit.start_line}{err_str}")

        for callee in callees[:8]:
            self._print_tree(callee, lines, shown, depth + 1, max_depth)


def _rel(path, project_path):
    """Convert absolute path to relative."""
    if project_path and path and path.startswith(project_path):
        return path[len(project_path):].lstrip("/")
    return path or ""


def detect_primary_language(indexer) -> str:
    """Detect the primary language of an indexed codebase."""
    lang_counts = defaultdict(int)
    for unit in indexer._units:
        ext = os.path.splitext(unit.source_file)[1].lower()
        if ext == ".py":
            lang_counts["python"] += 1
        elif ext in (".js", ".jsx", ".mjs", ".cjs"):
            lang_counts["javascript"] += 1
        elif ext in (".ts", ".tsx"):
            lang_counts["typescript"] += 1
        elif ext == ".go":
            lang_counts["go"] += 1
        elif ext in (".java", ".kt"):
            lang_counts["java"] += 1

    if not lang_counts:
        return "python"  # default

    return max(lang_counts, key=lang_counts.get)


# ── Skip lists ──

_SKIP_NAMES = {
    # JS/TS keywords
    "if", "else", "for", "while", "switch", "case", "return", "throw",
    "try", "catch", "finally", "typeof", "instanceof", "new", "delete",
    "import", "export", "from", "require",
    # JS builtins
    "console", "log", "error", "warn", "info", "debug",
    "parseInt", "parseFloat", "isNaN", "isFinite",
    "setTimeout", "setInterval", "clearTimeout", "clearInterval",
    "Promise", "Array", "Object", "String", "Number", "Boolean",
    "Math", "Date", "JSON", "Error", "Map", "Set", "RegExp",
    "encodeURIComponent", "decodeURIComponent",
    # Python builtins
    "print", "len", "range", "enumerate", "zip", "map", "filter",
    "sorted", "reversed", "list", "dict", "set", "tuple", "int",
    "str", "float", "bool", "type", "isinstance", "issubclass",
    "super", "property", "staticmethod", "classmethod",
    "open", "input", "iter", "next", "hash", "id", "repr",
    "hasattr", "getattr", "setattr", "delattr", "callable",
    "max", "min", "sum", "abs", "round", "any", "all",
    # Go builtins
    "make", "append", "cap", "copy", "close", "panic", "recover",
    "fmt", "Println", "Printf", "Sprintf", "Errorf",
    # Common framework calls to skip
    "useState", "useEffect", "useCallback", "useMemo", "useRef",
    "useContext", "useReducer", "useLayoutEffect",
    "createElement", "Fragment",
}

_ENTRY_NAMES = {
    "main", "run", "start", "init", "setup", "execute",
    "serve", "listen", "create_app", "create_server",
    "handler", "middleware", "App", "Main",
    "render", "getServerSideProps", "getStaticProps",
    "default",  # JS default export
}
