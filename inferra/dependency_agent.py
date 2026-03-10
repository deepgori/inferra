"""
dependency_agent.py — Dependency & Call Chain Analysis Agent

Analyzes the codebase's call graph to identify architectural issues:
- Circular dependencies between modules
- Deep call chains (>N levels)
- Unused/dead functions
- High fan-out functions (calling too many others)
- Tight coupling between modules
"""

import logging
from collections import defaultdict
from typing import Dict, List, Optional, Set, Tuple

from inferra.indexer import CodeIndexer, CodeUnit
from inferra.agents import BaseAgent, Finding, FindingType, Severity

log = logging.getLogger(__name__)


class DependencyAgent(BaseAgent):
    """
    Analyzes inter-module dependencies and call chains.

    Uses the CodeIndexer's `calls` field to build a call graph,
    then runs analyses for architectural anti-patterns.
    """

    def __init__(self, indexer: Optional[CodeIndexer] = None, max_depth: int = 10):
        super().__init__("DependencyAgent")
        self._indexer = indexer
        self._max_depth = max_depth

    def set_indexer(self, indexer: CodeIndexer):
        self._indexer = indexer

    def analyze(self, graph=None, context=None):
        """Satisfy BaseAgent interface — delegates to analyze_codebase."""
        return self.analyze_codebase()

    def analyze_codebase(self) -> List[Finding]:
        """Run full dependency analysis on the indexed codebase."""
        if not self._indexer or not self._indexer.units:
            return []

        findings = []

        # Build call graph
        call_graph = self._build_call_graph()

        # 1. Detect circular dependencies
        findings.extend(self._detect_circular_deps(call_graph))

        # 2. Detect deep call chains
        findings.extend(self._detect_deep_chains(call_graph))

        # 3. Detect high fan-out
        findings.extend(self._detect_high_fanout(call_graph))

        # 4. Detect unused functions
        findings.extend(self._detect_unused_functions(call_graph))

        # 5. Detect module coupling
        findings.extend(self._detect_tight_coupling())

        return findings

    def _build_call_graph(self) -> Dict[str, Set[str]]:
        """Build adjacency list: function_name → set of called function names."""
        graph = defaultdict(set)
        all_names = {u.name for u in self._indexer.units}

        for unit in self._indexer.units:
            for call in unit.calls:
                if call in all_names and call != unit.name:
                    graph[unit.name].add(call)

        return dict(graph)

    def _detect_circular_deps(self, graph: Dict[str, Set[str]]) -> List[Finding]:
        """Find cycles in the call graph using DFS."""
        findings = []
        visited = set()
        path = []
        path_set = set()
        cycles_found = set()

        def dfs(node):
            if node in path_set:
                # Found a cycle
                cycle_start = path.index(node)
                cycle = path[cycle_start:] + [node]
                cycle_key = " → ".join(sorted(cycle))
                if cycle_key not in cycles_found:
                    cycles_found.add(cycle_key)
                    findings.append(
                        Finding(
                            agent_name=self.name,
                            finding_type=FindingType.CIRCULAR_DEPENDENCY,
                            severity=Severity.MEDIUM,
                            summary=f"Circular dependency: {' → '.join(cycle)}",
                            details=f"Functions form a circular call chain: {' → '.join(cycle)}",
                            evidence=[f"{a} calls {b}" for a, b in zip(cycle, cycle[1:])],
                            affected_spans=[],
                            confidence=0.95,
                            recommendations=[
                                "Break the cycle by introducing an interface or callback",
                                "Move shared logic to a common utility module",
                            ],
                            source_locations=[],
                        )
                    )
                return

            if node in visited:
                return

            visited.add(node)
            path.append(node)
            path_set.add(node)

            for neighbor in graph.get(node, set()):
                dfs(neighbor)

            path.pop()
            path_set.discard(node)

        for node in graph:
            if node not in visited:
                dfs(node)

        return findings

    def _detect_deep_chains(self, graph: Dict[str, Set[str]]) -> List[Finding]:
        """Find call chains deeper than max_depth."""
        findings = []

        def dfs_depth(node, depth, visited):
            if depth > self._max_depth:
                return depth
            if node in visited:
                return depth
            visited.add(node)
            max_d = depth
            for neighbor in graph.get(node, set()):
                max_d = max(max_d, dfs_depth(neighbor, depth + 1, visited))
            visited.discard(node)
            return max_d

        for start in graph:
            depth = dfs_depth(start, 0, set())
            if depth > self._max_depth:
                findings.append(
                    Finding(
                        agent_name=self.name,
                        finding_type=FindingType.DEEP_CALL_CHAIN,
                        severity=Severity.LOW,
                        summary=f"Deep call chain from {start} ({depth} levels)",
                        details=f"Function {start} has a call chain {depth} levels deep, "
                                f"exceeding threshold of {self._max_depth}.",
                        evidence=[f"Chain depth: {depth}"],
                        affected_spans=[],
                        confidence=0.80,
                        recommendations=[
                            "Consider flattening the call hierarchy",
                            "Introduce intermediate aggregation points",
                        ],
                        source_locations=[],
                    )
                )

        return findings

    def _detect_high_fanout(self, graph: Dict[str, Set[str]]) -> List[Finding]:
        """Find functions that call too many other functions (>10)."""
        findings = []
        threshold = 10

        for func, callees in graph.items():
            if len(callees) > threshold:
                findings.append(
                    Finding(
                        agent_name=self.name,
                        finding_type=FindingType.HIGH_FAN_OUT,
                        severity=Severity.LOW,
                        summary=f"High fan-out: {func} calls {len(callees)} functions",
                        details=f"Function {func} has high fan-out, calling: {', '.join(sorted(callees)[:10])}...",
                        evidence=[f"Calls: {', '.join(sorted(callees))}"],
                        affected_spans=[],
                        confidence=0.75,
                        recommendations=[
                            "Break into smaller, more focused functions",
                            "Consider the Single Responsibility Principle",
                        ],
                        source_locations=[],
                    )
                )

        return findings

    def _detect_unused_functions(self, graph: Dict[str, Set[str]]) -> List[Finding]:
        """Find functions that are never called by anything else."""
        all_called = set()
        for callees in graph.values():
            all_called.update(callees)

        all_functions = {u.name for u in self._indexer.units if u.unit_type in ("function", "method", "async_function")}
        # Exclude entry points (routes, main, test functions)
        entry_points = {
            u.name for u in self._indexer.units
            if u.route_path or u.name.startswith("test_") or u.name in ("main", "__init__")
        }

        unused = (all_functions - all_called - entry_points) - set(graph.keys())

        if len(unused) > 5:  # Only report if significant
            findings = [
                Finding(
                    agent_name=self.name,
                    finding_type=FindingType.DEAD_CODE,
                    severity=Severity.LOW,
                    summary=f"{len(unused)} potentially unused functions detected",
                    details="Functions not called by any other indexed function",
                    evidence=sorted(list(unused))[:20],
                    affected_spans=[],
                    confidence=0.60,
                    recommendations=["Review and remove dead code if confirmed unused"],
                    source_locations=[],
                )
            ]
            return findings
        return []

    def _detect_tight_coupling(self) -> List[Finding]:
        """Detect modules that are too tightly coupled."""
        findings = []
        # Count cross-file call references
        file_deps = defaultdict(lambda: defaultdict(int))

        for unit in self._indexer.units:
            for call in unit.calls:
                # Find which file the called function lives in
                for other in self._indexer.units:
                    if other.name == call and other.source_file != unit.source_file:
                        file_deps[unit.source_file][other.source_file] += 1

        for source, targets in file_deps.items():
            for target, count in targets.items():
                if count > 15:
                    findings.append(
                        Finding(
                            agent_name=self.name,
                            finding_type=FindingType.TIGHT_COUPLING,
                            severity=Severity.LOW,
                            summary=f"Tight coupling: {source} → {target} ({count} calls)",
                            details=f"File {source} makes {count} calls to {target}",
                            evidence=[f"{count} cross-module calls"],
                            affected_spans=[],
                            confidence=0.70,
                            recommendations=[
                                "Consider introducing an interface or faÇade",
                                "Move shared logic to a common module",
                            ],
                            source_locations=[source, target],
                        )
                    )

        return findings
