"""
security_agent.py — Security Vulnerability Scanner (v0.5.0)

Pattern-based static analysis with basic taint tracking:
- SQL injection (string formatting in queries)
- Hardcoded secrets (API keys, passwords in source)
- Missing authentication decorators
- Unsafe deserialization (pickle, eval, exec)
- SSRF risks (user input in HTTP requests)
- Path traversal (user input in file operations)
- XSS (innerHTML, dangerouslySetInnerHTML)                [NEW v0.5.0]
- Open redirects (redirect with user input)               [NEW v0.5.0]
- Weak cryptography (md5/sha1 for passwords)              [NEW v0.5.0]

Improvements over v0.4.0:
- Basic taint tracking: traces function parameters to sinks
- Cross-function analysis: flags sinks in callees receiving user input
- False-positive reduction: skips test files, comments, string literals
"""

import logging
import os
import re
from typing import List, Optional, Set

from inferra.indexer import CodeIndexer, CodeUnit
from inferra.agents import BaseAgent, Finding, FindingType, Severity

log = logging.getLogger(__name__)


# ── Detection Patterns ──────────────────────────────────────────────────────

_SQL_INJECTION_PATTERNS = [
    re.compile(r"""(?:execute|query|raw)\s*\(\s*f[\"']""", re.IGNORECASE),
    re.compile(r"""(?:execute|query|raw)\s*\(\s*[\"'].*?\%s""", re.IGNORECASE),
    re.compile(r"""(?:execute|query|raw)\s*\(\s*.*?\.format\(""", re.IGNORECASE),
    re.compile(r"""(?:execute|query|raw)\s*\(\s*.*?\+\s*(?:request|user|input|param)""", re.IGNORECASE),
]

_SECRET_PATTERNS = [
    re.compile(r"""(?:api_key|apikey|secret|password|token|auth)\s*=\s*[\"'][A-Za-z0-9_\-]{8,}[\"']""", re.IGNORECASE),
    re.compile(r"""(?:AWS_SECRET|AWS_ACCESS|OPENAI_API|ANTHROPIC|sk-[a-zA-Z0-9]{20,})"""),
    re.compile(r"""Bearer\s+[A-Za-z0-9\-_.]{20,}"""),
]

_UNSAFE_DESER_PATTERNS = [
    re.compile(r"""pickle\.loads?\s*\("""),
    re.compile(r"""eval\s*\(\s*(?:request|user|input|data|param)""", re.IGNORECASE),
    re.compile(r"""exec\s*\(\s*(?:request|user|input|data|param)""", re.IGNORECASE),
    re.compile(r"""yaml\.(?:load|unsafe_load)\s*\((?!.*Loader)"""),
]

_SSRF_PATTERNS = [
    re.compile(r"""(?:requests\.get|httpx\.get|urllib\.request\.urlopen)\s*\(\s*(?:request|user|input|param|url)""", re.IGNORECASE),
    re.compile(r"""fetch\s*\(\s*(?:req|user|input|url)""", re.IGNORECASE),
]

_PATH_TRAVERSAL_PATTERNS = [
    re.compile(r"""open\s*\(\s*(?:request|user|input|param|filename)""", re.IGNORECASE),
    re.compile(r"""os\.path\.join\s*\(.*?(?:request|user|input)""", re.IGNORECASE),
]

# ── v0.5.0: New vulnerability patterns ──

_XSS_PATTERNS = [
    re.compile(r"""\.innerHTML\s*=\s*(?:request|user|input|data|param|query)""", re.IGNORECASE),
    re.compile(r"""dangerouslySetInnerHTML\s*=\s*\{"""),
    re.compile(r"""document\.write\s*\(\s*(?:request|user|input|data|param)""", re.IGNORECASE),
    re.compile(r"""\bmark_safe\s*\(\s*(?:request|user|input|data)""", re.IGNORECASE),
    re.compile(r"""\|\s*safe\b"""),  # Django template |safe filter
]

_OPEN_REDIRECT_PATTERNS = [
    re.compile(r"""redirect\s*\(\s*(?:request|req)\.(?:args|params|query|GET|POST)""", re.IGNORECASE),
    re.compile(r"""redirect\s*\(\s*(?:url|next_url|return_url|redirect_url)""", re.IGNORECASE),
    re.compile(r"""(?:Location|location)\s*[:=]\s*(?:request|req)\.(?:args|params|query)""", re.IGNORECASE),
]

_WEAK_CRYPTO_PATTERNS = [
    re.compile(r"""(?:hashlib\.)?(?:md5|sha1)\s*\(.*?(?:password|passwd|secret|token)""", re.IGNORECASE),
    re.compile(r"""DES|RC4|Blowfish""", re.IGNORECASE),
    re.compile(r"""random\.(?:random|randint|choice)\s*\(.*?(?:token|secret|key|nonce|salt)""", re.IGNORECASE),
]

# ── Taint sources: function parameters that represent user input ──
_TAINT_PARAM_NAMES = {
    "request", "req", "user_input", "input_data", "params", "query",
    "body", "payload", "form_data", "args", "kwargs", "data",
    "username", "password", "email", "url", "filename", "path",
}

# ── Files to skip (false-positive reduction) ──
_TEST_PATH_PATTERNS = re.compile(
    r"""(?:test_|_test\.py|tests/|spec/|__tests__|\.test\.|\.spec\.|conftest\.py|fixtures/)""",
    re.IGNORECASE,
)

# ── Comment/string literal detection ──
_COMMENT_LINE = re.compile(r'^\s*(?:#|//|/\*|\*|")')



def _strip_comments_and_strings(body: str) -> str:
    """Remove comments and multi-line string literals to reduce false positives.
    This is a best-effort heuristic, not a full parser."""
    lines = []
    in_multiline = False
    for line in body.split("\n"):
        stripped = line.strip()
        # Skip comment-only lines
        if stripped.startswith("#") or stripped.startswith("//"):
            continue
        # Skip docstrings (triple-quoted)
        if '"""' in stripped or "'''" in stripped:
            count = stripped.count('"""') + stripped.count("'''")
            if count == 1:
                in_multiline = not in_multiline
                continue
            # Both open and close on same line — skip
            continue
        if in_multiline:
            continue
        lines.append(line)
    return "\n".join(lines)


class SecurityAgent(BaseAgent):
    """
    Pattern-based security vulnerability scanner with basic taint tracking.

    v0.5.0 improvements:
    - Traces function parameters to dangerous sinks (taint analysis)
    - Cross-function analysis via call graph
    - Filters out test files and commented code
    - New: XSS, open redirect, weak crypto detection
    """

    def __init__(self, indexer: Optional[CodeIndexer] = None):
        super().__init__("SecurityAgent")
        self._indexer = indexer

    def set_indexer(self, indexer: CodeIndexer):
        self._indexer = indexer

    def analyze(self, graph=None, context=None):
        """Satisfy BaseAgent interface — delegates to analyze_codebase."""
        return self.analyze_codebase()

    def analyze_codebase(self) -> List[Finding]:
        """Run full security scan on the indexed codebase."""
        if not self._indexer or not self._indexer.units:
            return []

        findings = []
        # Build a simple call graph for cross-function analysis
        call_graph = self._build_call_graph()

        for unit in self._indexer.units:
            # ── Skip test files ──
            if _TEST_PATH_PATTERNS.search(unit.source_file):
                continue

            # ── Strip comments/strings for pattern matching ──
            body = _strip_comments_and_strings(unit.body_text)
            if not body.strip():
                continue

            # Check if this function has tainted parameters
            tainted_params = self._get_tainted_params(unit)

            # SQL Injection
            for pattern in _SQL_INJECTION_PATTERNS:
                if pattern.search(body):
                    confidence = 0.85
                    # Boost confidence if tainted param flows into the pattern
                    if tainted_params and self._taint_reaches_pattern(body, tainted_params, pattern):
                        confidence = 0.95
                    findings.append(
                        Finding(
                            agent_name=self.name,
                            summary=f"Potential SQL injection in {unit.qualified_name}",
                            finding_type=FindingType.SQL_INJECTION,
                            severity=Severity.CRITICAL,
                            details=f"String interpolation or concatenation used in SQL query "
                                    f"at {unit.source_file}:{unit.start_line}. "
                                    f"Use parameterized queries instead."
                                    + (f" Tainted param(s): {', '.join(tainted_params)}" if tainted_params else ""),
                            evidence=[self._extract_match_context(body, pattern)],
                            affected_spans=[],
                            confidence=confidence,
                            recommendations=[
                                "Use parameterized queries: cursor.execute('SELECT * WHERE id = ?', (id,))",
                                "Use an ORM with proper sanitization",
                                "Never use f-strings or .format() in SQL queries",
                            ],
                            source_locations=[f"{unit.source_file}:{unit.start_line}"],
                        )
                    )
                    break

            # Hardcoded Secrets
            for pattern in _SECRET_PATTERNS:
                match = pattern.search(body)
                if match:
                    matched_text = match.group(0)
                    redacted = matched_text[:15] + "..." if len(matched_text) > 15 else matched_text
                    findings.append(
                        Finding(
                            agent_name=self.name,
                            summary=f"Hardcoded secret in {unit.qualified_name}",
                            finding_type=FindingType.HARDCODED_SECRET,
                            severity=Severity.HIGH,
                            details=f"Potential hardcoded credential found at "
                                    f"{unit.source_file}:{unit.start_line}: '{redacted}'",
                            evidence=[f"Pattern: {redacted}"],
                            affected_spans=[],
                            confidence=0.75,
                            recommendations=[
                                "Use environment variables: os.environ.get('API_KEY')",
                                "Use a secrets manager (AWS Secrets Manager, HashiCorp Vault)",
                                "Add to .gitignore and use .env files",
                            ],
                            source_locations=[f"{unit.source_file}:{unit.start_line}"],
                        )
                    )
                    break

            # Unsafe Deserialization
            for pattern in _UNSAFE_DESER_PATTERNS:
                if pattern.search(body):
                    findings.append(
                        Finding(
                            agent_name=self.name,
                            summary=f"Unsafe deserialization in {unit.qualified_name}",
                            finding_type=FindingType.UNSAFE_DESERIALIZATION,
                            severity=Severity.CRITICAL,
                            details=f"Code at {unit.source_file}:{unit.start_line} uses "
                                    f"unsafe deserialization (pickle/eval/exec/yaml.load). "
                                    f"This can lead to remote code execution.",
                            evidence=[self._extract_match_context(body, pattern)],
                            affected_spans=[],
                            confidence=0.90,
                            recommendations=[
                                "Use json.loads() instead of pickle for data exchange",
                                "Use yaml.safe_load() instead of yaml.load()",
                                "Never use eval/exec on user-supplied data",
                            ],
                            source_locations=[f"{unit.source_file}:{unit.start_line}"],
                        )
                    )
                    break

            # SSRF
            for pattern in _SSRF_PATTERNS:
                if pattern.search(body):
                    findings.append(
                        Finding(
                            agent_name=self.name,
                            summary=f"Potential SSRF in {unit.qualified_name}",
                            finding_type=FindingType.SSRF,
                            severity=Severity.HIGH,
                            details=f"User-controlled input may be used as a URL at "
                                    f"{unit.source_file}:{unit.start_line}.",
                            evidence=[self._extract_match_context(body, pattern)],
                            affected_spans=[],
                            confidence=0.70,
                            recommendations=[
                                "Validate and sanitize URLs before making requests",
                                "Use an allowlist of permitted domains",
                                "Block requests to internal/private IP ranges",
                            ],
                            source_locations=[f"{unit.source_file}:{unit.start_line}"],
                        )
                    )
                    break

            # Path Traversal
            for pattern in _PATH_TRAVERSAL_PATTERNS:
                if pattern.search(body):
                    findings.append(
                        Finding(
                            agent_name=self.name,
                            summary=f"Potential path traversal in {unit.qualified_name}",
                            finding_type=FindingType.PATH_TRAVERSAL,
                            severity=Severity.HIGH,
                            details=f"User input may be used in file operations at "
                                    f"{unit.source_file}:{unit.start_line}.",
                            evidence=[self._extract_match_context(body, pattern)],
                            affected_spans=[],
                            confidence=0.70,
                            recommendations=[
                                "Use os.path.realpath() and verify the resolved path",
                                "Restrict file access to a specific directory",
                                "Never use user input directly in file paths",
                            ],
                            source_locations=[f"{unit.source_file}:{unit.start_line}"],
                        )
                    )
                    break

            # ── v0.5.0: XSS ──
            for pattern in _XSS_PATTERNS:
                if pattern.search(body):
                    findings.append(
                        Finding(
                            agent_name=self.name,
                            summary=f"Potential XSS in {unit.qualified_name}",
                            finding_type=FindingType.XSS,
                            severity=Severity.HIGH,
                            details=f"User-controlled data may be rendered as HTML without "
                                    f"escaping at {unit.source_file}:{unit.start_line}.",
                            evidence=[self._extract_match_context(body, pattern)],
                            affected_spans=[],
                            confidence=0.75,
                            recommendations=[
                                "Use textContent instead of innerHTML",
                                "Sanitize HTML with a library like DOMPurify",
                                "Use framework auto-escaping (React JSX, Django templates)",
                            ],
                            source_locations=[f"{unit.source_file}:{unit.start_line}"],
                        )
                    )
                    break

            # ── v0.5.0: Open Redirect ──
            for pattern in _OPEN_REDIRECT_PATTERNS:
                if pattern.search(body):
                    findings.append(
                        Finding(
                            agent_name=self.name,
                            summary=f"Potential open redirect in {unit.qualified_name}",
                            finding_type=FindingType.OPEN_REDIRECT,
                            severity=Severity.MEDIUM,
                            details=f"User-controlled URL used in redirect at "
                                    f"{unit.source_file}:{unit.start_line}. "
                                    f"An attacker could redirect users to a malicious site.",
                            evidence=[self._extract_match_context(body, pattern)],
                            affected_spans=[],
                            confidence=0.70,
                            recommendations=[
                                "Validate redirect URLs against an allowlist of domains",
                                "Only allow relative paths for redirects",
                                "Use url_has_allowed_host_and_scheme() in Django",
                            ],
                            source_locations=[f"{unit.source_file}:{unit.start_line}"],
                        )
                    )
                    break

            # ── v0.5.0: Weak Cryptography ──
            for pattern in _WEAK_CRYPTO_PATTERNS:
                if pattern.search(body):
                    findings.append(
                        Finding(
                            agent_name=self.name,
                            summary=f"Weak cryptography in {unit.qualified_name}",
                            finding_type=FindingType.WEAK_CRYPTO,
                            severity=Severity.MEDIUM,
                            details=f"Weak hash or cipher used for sensitive data at "
                                    f"{unit.source_file}:{unit.start_line}. "
                                    f"MD5/SHA1 are broken for security purposes.",
                            evidence=[self._extract_match_context(body, pattern)],
                            affected_spans=[],
                            confidence=0.80,
                            recommendations=[
                                "Use bcrypt or argon2 for password hashing",
                                "Use SHA-256 or SHA-3 for general hashing",
                                "Use secrets.token_hex() for token generation",
                            ],
                            source_locations=[f"{unit.source_file}:{unit.start_line}"],
                        )
                    )
                    break

        # Cross-function analysis: detect tainted data flowing through call chains
        findings.extend(self._cross_function_analysis(call_graph))

        # Check for missing auth decorators on route handlers
        findings.extend(self._check_missing_auth())

        return findings

    # ── Taint Tracking ──────────────────────────────────────────────────────

    def _get_tainted_params(self, unit: CodeUnit) -> Set[str]:
        """Identify function parameters that likely carry user input."""
        tainted = set()
        # Check parameter names against known taint sources
        if unit.params:
            for param in unit.params:
                param_lower = param.lower().split(":")[0].split("=")[0].strip()
                if param_lower in _TAINT_PARAM_NAMES:
                    tainted.add(param_lower)
        return tainted

    def _taint_reaches_pattern(self, body: str, tainted_params: Set[str], pattern) -> bool:
        """Check if a tainted parameter is used near a dangerous pattern match."""
        match = pattern.search(body)
        if not match:
            return False
        # Check if any tainted param appears within 200 chars of the pattern match
        window_start = max(0, match.start() - 200)
        window_end = min(len(body), match.end() + 200)
        window = body[window_start:window_end].lower()
        return any(param in window for param in tainted_params)

    # ── Cross-Function Analysis ─────────────────────────────────────────────

    def _build_call_graph(self) -> dict:
        """Build a simple call graph: {caller_name: [callee_names]}."""
        graph = {}
        func_names = {u.name for u in self._indexer.units if u.name}
        for unit in self._indexer.units:
            callees = []
            for other_name in func_names:
                if other_name != unit.name and other_name in unit.body_text:
                    callees.append(other_name)
            if callees:
                graph[unit.qualified_name] = callees
        return graph

    def _cross_function_analysis(self, call_graph: dict) -> List[Finding]:
        """Detect when a function with tainted params calls a function with a sink."""
        findings = []
        # Map function names to their units for lookup
        units_by_name = {}
        for unit in self._indexer.units:
            units_by_name[unit.name] = unit
            units_by_name[unit.qualified_name] = unit

        sink_patterns = (
            _SQL_INJECTION_PATTERNS + _SSRF_PATTERNS +
            _PATH_TRAVERSAL_PATTERNS + _XSS_PATTERNS
        )

        for caller_qname, callees in call_graph.items():
            caller_unit = units_by_name.get(caller_qname)
            if not caller_unit:
                continue
            if _TEST_PATH_PATTERNS.search(caller_unit.source_file):
                continue

            tainted = self._get_tainted_params(caller_unit)
            if not tainted:
                continue

            for callee_name in callees:
                callee_unit = units_by_name.get(callee_name)
                if not callee_unit:
                    continue
                callee_body = _strip_comments_and_strings(callee_unit.body_text)
                for pattern in sink_patterns:
                    if pattern.search(callee_body):
                        findings.append(
                            Finding(
                                agent_name=self.name,
                                summary=f"Tainted data flows from {caller_unit.name} to sink in {callee_unit.name}",
                                finding_type=FindingType.SQL_INJECTION,
                                severity=Severity.HIGH,
                                details=(
                                    f"Function {caller_qname} receives tainted input "
                                    f"({', '.join(tainted)}) and passes it to "
                                    f"{callee_unit.qualified_name} which contains a "
                                    f"dangerous sink pattern."
                                ),
                                evidence=[
                                    f"Tainted params in {caller_unit.name}: {', '.join(tainted)}",
                                    f"Sink in {callee_unit.name}: {self._extract_match_context(callee_body, pattern)}",
                                ],
                                affected_spans=[],
                                confidence=0.65,
                                recommendations=[
                                    f"Sanitize {', '.join(tainted)} before passing to {callee_unit.name}",
                                    "Use parameterized queries or safe APIs in the callee",
                                ],
                                source_locations=[
                                    f"{caller_unit.source_file}:{caller_unit.start_line}",
                                    f"{callee_unit.source_file}:{callee_unit.start_line}",
                                ],
                            )
                        )
                        break  # One finding per caller→callee pair

        return findings

    # ── Missing Auth Check ──────────────────────────────────────────────────

    def _check_missing_auth(self) -> List[Finding]:
        """Check if route handlers have authentication decorators."""
        findings = []
        auth_decorators = {"login_required", "requires_auth", "authenticated",
                          "jwt_required", "permission_required", "Depends"}

        unprotected = []
        for unit in self._indexer.units:
            if not unit.route_path:
                continue
            # Skip health/status endpoints
            if any(p in (unit.route_path or "") for p in ["/health", "/status", "/ping", "/ready"]):
                continue
            # Skip test files
            if _TEST_PATH_PATTERNS.search(unit.source_file):
                continue
            # Check if any auth decorator is in the body text (simplified check)
            has_auth = any(d in unit.body_text for d in auth_decorators)
            if not has_auth:
                unprotected.append(f"{unit.route_path} ({unit.source_file}:{unit.start_line})")

        if len(unprotected) > 3:
            findings.append(
                Finding(
                    agent_name=self.name,
                    finding_type=FindingType.MISSING_AUTH,
                    severity=Severity.MEDIUM,
                    summary=f"{len(unprotected)} route handlers without authentication",
                    details="The following endpoints may lack authentication checks",
                    evidence=unprotected[:10],
                    affected_spans=[],
                    confidence=0.55,
                    recommendations=[
                        "Add authentication middleware or decorators to protected endpoints",
                        "Review if these endpoints should be publicly accessible",
                    ],
                    source_locations=[],
                )
            )

        return findings

    @staticmethod
    def _extract_match_context(body: str, pattern) -> str:
        """Extract the line containing the pattern match for evidence."""
        match = pattern.search(body)
        if not match:
            return ""
        start = body.rfind("\n", 0, match.start()) + 1
        end = body.find("\n", match.end())
        if end == -1:
            end = len(body)
        line = body[start:end].strip()
        return line[:150]  # Truncate long lines
