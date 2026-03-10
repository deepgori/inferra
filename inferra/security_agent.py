"""
security_agent.py — Security Vulnerability Scanner

Pattern-based static analysis for common security issues:
- SQL injection (string formatting in queries)
- Hardcoded secrets (API keys, passwords in source)
- Missing authentication decorators
- Unsafe deserialization (pickle, eval, exec)
- SSRF risks (user input in HTTP requests)
- Path traversal (user input in file operations)
"""

import logging
import re
from typing import List, Optional

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


class SecurityAgent(BaseAgent):
    """
    Pattern-based security vulnerability scanner.

    Scans indexed code for common security anti-patterns
    without requiring external tools or ML models.
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

        for unit in self._indexer.units:
            body = unit.body_text

            # SQL Injection
            for pattern in _SQL_INJECTION_PATTERNS:
                if pattern.search(body):
                    findings.append(
                        Finding(
                            agent_name=self.name,
                            summary=f"Potential SQL injection in {unit.qualified_name}",
                            finding_type=FindingType.SQL_INJECTION,
                            severity=Severity.CRITICAL,
                            details=f"String interpolation or concatenation used in SQL query "
                                    f"at {unit.source_file}:{unit.start_line}. "
                                    f"Use parameterized queries instead.",
                            evidence=[self._extract_match_context(body, pattern)],
                            affected_spans=[],
                            confidence=0.85,
                            recommendations=[
                                "Use parameterized queries: cursor.execute('SELECT * WHERE id = ?', (id,))",
                                "Use an ORM with proper sanitization",
                                "Never use f-strings or .format() in SQL queries",
                            ],
                            source_locations=[f"{unit.source_file}:{unit.start_line}"],
                        )
                    )
                    break  # One finding per unit per category

            # Hardcoded Secrets
            for pattern in _SECRET_PATTERNS:
                match = pattern.search(body)
                if match:
                    # Redact the actual value
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

        # Check for missing auth decorators on route handlers
        findings.extend(self._check_missing_auth())

        return findings

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
        # Find the line containing the match
        start = body.rfind("\n", 0, match.start()) + 1
        end = body.find("\n", match.end())
        if end == -1:
            end = len(body)
        line = body[start:end].strip()
        return line[:150]  # Truncate long lines
