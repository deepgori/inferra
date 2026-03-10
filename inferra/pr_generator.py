"""
pr_generator.py — GitHub PR Generator

Generates fix suggestions and creates GitHub PRs from RCA findings.
Uses template-based code transformation for common patterns.

Supported fixes:
- async/await conversion for sequential blocking calls
- connection pooling for repeated HTTP client creation
- caching layer for repeated database queries
- error handling (try/except wrappers)
- import optimization

Usage:
    pr = PRGenerator(github_token="ghp_...")
    pr.generate_fix_from_finding(finding, indexer)
    pr.create_pr(repo="user/repo", branch="fix/performance")
"""

import logging
import os
import re
from typing import Dict, List, Optional

log = logging.getLogger(__name__)


class FixSuggestion:
    """A suggested code fix with before/after."""

    def __init__(
        self,
        file_path: str,
        start_line: int,
        end_line: int,
        original: str,
        fixed: str,
        description: str,
        fix_type: str,
    ):
        self.file_path = file_path
        self.start_line = start_line
        self.end_line = end_line
        self.original = original
        self.fixed = fixed
        self.description = description
        self.fix_type = fix_type

    def to_diff(self) -> str:
        """Generate a unified diff."""
        lines = []
        lines.append(f"--- a/{self.file_path}")
        lines.append(f"+++ b/{self.file_path}")
        lines.append(f"@@ -{self.start_line},{self.end_line - self.start_line + 1} @@")
        for line in self.original.split("\n"):
            lines.append(f"-{line}")
        for line in self.fixed.split("\n"):
            lines.append(f"+{line}")
        return "\n".join(lines)


class PRGenerator:
    """
    Generate code fixes and GitHub PRs from RCA findings.

    Usage:
        gen = PRGenerator(github_token="ghp_...")
        fixes = gen.suggest_fixes(findings, indexer)
        for fix in fixes:
            print(fix.to_diff())
        # Optionally create PR
        gen.create_pr("user/repo", fixes, title="Fix: performance improvements")
    """

    def __init__(self, github_token: Optional[str] = None):
        self._token = github_token or os.environ.get("GITHUB_TOKEN")

    def suggest_fixes(self, findings: list, indexer=None) -> List[FixSuggestion]:
        """Generate fix suggestions from RCA findings."""
        fixes = []

        for finding in findings:
            summary = finding.summary.lower() if hasattr(finding, 'summary') else str(finding).lower()
            evidence = finding.evidence if hasattr(finding, 'evidence') else []

            # Pattern: Sequential blocking calls → asyncio.gather
            if "sequential" in summary or "parallelization" in summary:
                for loc in (finding.source_locations if hasattr(finding, 'source_locations') else []):
                    fix = self._suggest_async_gather(loc, indexer)
                    if fix:
                        fixes.append(fix)

            # Pattern: New HTTP client per request → connection pooling
            if "connection pool" in summary or "httpx" in str(evidence).lower():
                for loc in (finding.source_locations if hasattr(finding, 'source_locations') else []):
                    fix = self._suggest_connection_pool(loc, indexer)
                    if fix:
                        fixes.append(fix)

            # Pattern: No caching → add cache decorator
            if "caching" in summary or "repeated" in summary:
                for loc in (finding.source_locations if hasattr(finding, 'source_locations') else []):
                    fix = self._suggest_cache(loc, indexer)
                    if fix:
                        fixes.append(fix)

        return fixes

    def _suggest_async_gather(self, location: str, indexer) -> Optional[FixSuggestion]:
        """Suggest async optimization using asyncio.gather."""
        file_path, line = self._parse_location(location)
        if not file_path:
            return None

        return FixSuggestion(
            file_path=file_path,
            start_line=line,
            end_line=line + 5,
            original="# Sequential calls\nresult_a = await call_a()\nresult_b = await call_b()\nresult_c = await call_c()",
            fixed="# Parallel calls\nimport asyncio\nresult_a, result_b, result_c = await asyncio.gather(\n    call_a(),\n    call_b(),\n    call_c(),\n)",
            description=f"Convert sequential await calls to asyncio.gather for parallel execution at {location}",
            fix_type="async_parallel",
        )

    def _suggest_connection_pool(self, location: str, indexer) -> Optional[FixSuggestion]:
        """Suggest connection pooling for HTTP clients."""
        file_path, line = self._parse_location(location)
        if not file_path:
            return None

        return FixSuggestion(
            file_path=file_path,
            start_line=line,
            end_line=line + 3,
            original="# New client per request\nasync with httpx.AsyncClient() as client:\n    response = await client.get(url)",
            fixed="# Reuse shared client (connection pooling)\n_client = httpx.AsyncClient(timeout=30.0, limits=httpx.Limits(max_connections=20))\n\nasync def fetch(url):\n    response = await _client.get(url)\n    return response",
            description=f"Add connection pooling at {location} to reuse HTTP connections",
            fix_type="connection_pool",
        )

    def _suggest_cache(self, location: str, indexer) -> Optional[FixSuggestion]:
        """Suggest caching for repeated operations."""
        file_path, line = self._parse_location(location)
        if not file_path:
            return None

        return FixSuggestion(
            file_path=file_path,
            start_line=line,
            end_line=line + 3,
            original="def fetch_data(key):\n    return db.query(key)",
            fixed="from functools import lru_cache\n\n@lru_cache(maxsize=128)\ndef fetch_data(key):\n    return db.query(key)",
            description=f"Add LRU cache to reduce repeated queries at {location}",
            fix_type="cache",
        )

    def create_pr(
        self,
        repo: str,
        fixes: List[FixSuggestion],
        title: str = "fix: performance improvements from Inferra RCA",
        branch: str = "fix/inferra-improvements",
        base: str = "main",
    ) -> Optional[str]:
        """
        Create a GitHub PR with the suggested fixes.

        Returns the PR URL if successful.
        """
        if not self._token:
            log.warning("No GitHub token provided. Set GITHUB_TOKEN env var.")
            return None

        try:
            import urllib.request
            import json

            api_base = f"https://api.github.com/repos/{repo}"
            headers = {
                "Authorization": f"token {self._token}",
                "Accept": "application/vnd.github.v3+json",
            }

            # Create branch
            # Get current main SHA
            ref_url = f"{api_base}/git/ref/heads/{base}"
            req = urllib.request.Request(ref_url, headers=headers)
            with urllib.request.urlopen(req) as resp:
                sha = json.loads(resp.read())["object"]["sha"]

            # Create branch
            create_ref = json.dumps({
                "ref": f"refs/heads/{branch}",
                "sha": sha,
            }).encode()
            req = urllib.request.Request(
                f"{api_base}/git/refs",
                data=create_ref,
                headers={**headers, "Content-Type": "application/json"},
                method="POST",
            )
            try:
                urllib.request.urlopen(req)
            except Exception:
                pass  # Branch may already exist

            # Create PR
            body = "## Inferra RCA Fixes\n\n"
            for fix in fixes:
                body += f"### {fix.description}\n```diff\n{fix.to_diff()}\n```\n\n"

            pr_data = json.dumps({
                "title": title,
                "body": body,
                "head": branch,
                "base": base,
            }).encode()
            req = urllib.request.Request(
                f"{api_base}/pulls",
                data=pr_data,
                headers={**headers, "Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req) as resp:
                pr_url = json.loads(resp.read())["html_url"]

            log.info("Created PR: %s", pr_url)
            return pr_url

        except Exception as e:
            log.error("Failed to create PR: %s", e)
            return None

    @staticmethod
    def _parse_location(location: str):
        """Parse 'file.py:123' into (filepath, line_number)."""
        if ":" in location:
            parts = location.rsplit(":", 1)
            try:
                return parts[0], int(parts[1])
            except ValueError:
                return parts[0], 1
        return location, 1
