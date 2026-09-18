"""
inferra.github_issues — File Inferra diagnoses as GitHub issues through Composio.

Usage:
    from inferra.github_issues import GitHubIssueFiler

    filer = GitHubIssueFiler()
    url = filer.connect_url()          # None once GitHub is connected
    if url:
        print("Connect GitHub:", url)
        filer.wait_for_connection()
    issue = filer.file(rca_report, "owner/repo", project_name="my_app")

Requires `pip install 'inferra[github]'` and COMPOSIO_API_KEY in the environment.
"""

import getpass
import hashlib
import os
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

CREATE_ISSUE = "GITHUB_CREATE_AN_ISSUE"
LIST_ISSUES = "GITHUB_LIST_REPOSITORY_ISSUES"
COMMENT_ON_ISSUE = "GITHUB_CREATE_AN_ISSUE_COMMENT"

FINGERPRINT_LABEL = "Inferra fingerprint:"
MAX_PAGES_SCANNED = 3
MAX_FINDINGS_IN_BODY = 5
MAX_EVIDENCE_PER_FINDING = 3


class GitHubNotConnected(Exception):
    """GitHub is not connected for this Composio user yet."""

    def __init__(self, connect_url: str):
        super().__init__(f"GitHub is not connected. Authorize here: {connect_url}")
        self.connect_url = connect_url


class IssueFilingError(Exception):
    """A Composio tool call failed. `log_id` locates it in the Composio dashboard."""

    def __init__(self, message: str, log_id: Optional[str] = None):
        super().__init__(f"{message} (Composio log: {log_id})" if log_id else message)
        self.log_id = log_id


@dataclass
class FiledIssue:
    number: int
    url: str
    duplicate: bool


def default_user_id() -> str:
    return os.environ.get("INFERRA_COMPOSIO_USER_ID") or getpass.getuser()


def fingerprint(report) -> str:
    # Keyed on source locations and finding types because the LLM-written root
    # cause is worded differently on every run and would defeat deduplication.
    locations = sorted({loc.strip() for loc in report.source_locations if loc and loc.strip()})
    kinds = sorted({finding.finding_type.value for finding in report.findings})
    basis = "|".join(locations + kinds) if (locations or kinds) else report.root_cause
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()[:12]


def format_issue(report, project_name: str) -> Tuple[str, str]:
    headline = report.root_cause.strip().splitlines()[0] if report.root_cause.strip() else "Issue detected"
    if len(headline) > 100:
        headline = headline[:97].rstrip() + "..."
    title = f"[Inferra] {report.severity.value.upper()}: {headline}"

    lines = [
        f"**Severity:** {report.severity.value} · **Confidence:** {report.confidence:.0%}",
        "",
        "### Root cause",
        report.root_cause.strip(),
    ]

    if report.summary and report.summary.strip() != report.root_cause.strip():
        lines += ["", "### Summary", report.summary.strip()]

    if report.causal_chain:
        lines += ["", "### Causal chain"]
        lines += [f"{i}. {step}" for i, step in enumerate(report.causal_chain, 1)]

    if report.source_locations:
        lines += ["", "### Where"]
        lines += [f"- `{loc}`" for loc in report.source_locations]

    if report.findings:
        ranked = sorted(report.findings, key=lambda f: f.confidence, reverse=True)
        lines += ["", "### Findings"]
        for finding in ranked[:MAX_FINDINGS_IN_BODY]:
            lines.append(
                f"- **{finding.agent_name}** ({finding.severity.value}, "
                f"{finding.confidence:.0%}): {finding.summary}"
            )
            lines += [f"  - {item}" for item in finding.evidence[:MAX_EVIDENCE_PER_FINDING]]

    if report.recommendations:
        lines += ["", "### Recommendations"]
        lines += [f"- {rec}" for rec in report.recommendations]

    lines += [
        "",
        "---",
        f"<sub>Filed by [Inferra](https://github.com/deepgori/inferra) for `{project_name}` · "
        f"{FINGERPRINT_LABEL} `{fingerprint(report)}`</sub>",
    ]
    return title, "\n".join(lines)


def split_repo(repo: str) -> Tuple[str, str]:
    parts = repo.strip().removesuffix(".git").split("/")
    if len(parts) != 2 or not all(parts):
        raise ValueError(f"Expected a repository as 'owner/name', got {repo!r}")
    return parts[0], parts[1]


def _client_errors() -> Tuple[type, ...]:
    # composio is an optional extra; with no SDK installed there are no client errors to catch.
    try:
        from composio_client import APIError
    except ImportError:
        return ()
    return (APIError,)


def _create_session(user_id: str):
    try:
        from composio import Composio
    except ImportError as exc:
        raise ImportError(
            "Filing GitHub issues needs the Composio SDK: pip install 'inferra[github]'"
        ) from exc
    if not os.environ.get("COMPOSIO_API_KEY"):
        raise RuntimeError("COMPOSIO_API_KEY is not set in the environment")
    return Composio().create(user_id=user_id)


def _unwrap(payload: Any) -> Any:
    if isinstance(payload, dict) and "data" in payload and set(payload) <= {"data", "error", "successful"}:
        return payload["data"]
    return payload


def _find_issue(payload: Any) -> Optional[Dict[str, Any]]:
    # The create tool's output schema is undocumented; list responses nest under a key, so accept both shapes.
    if isinstance(payload, dict):
        if "number" in payload and "html_url" in payload:
            return payload
        for value in payload.values():
            if isinstance(value, dict) and "number" in value and "html_url" in value:
                return value
    return None


def _as_issue_list(payload: Any) -> List[Dict[str, Any]]:
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("details", "issues", "items"):
            if isinstance(payload.get(key), list):
                return payload[key]
        for value in payload.values():
            if isinstance(value, list):
                return value
    return []


class GitHubIssueFiler:
    """Files RCA reports as GitHub issues, commenting on an existing open issue instead of duplicating it."""

    def __init__(self, user_id: Optional[str] = None, session=None):
        self._session = session if session is not None else _create_session(user_id or default_user_id())
        self._pending_connection = None

    def connect_url(self) -> Optional[str]:
        """Return a Composio Connect Link if GitHub still needs authorizing, else None."""
        try:
            details = self._session.toolkits(toolkits=["github"])
            for item in details.items:
                if item.slug == "github" and item.connection and item.connection.is_active:
                    return None
            self._pending_connection = self._session.authorize("github")
        except _client_errors() as exc:
            raise IssueFilingError(f"Could not check the GitHub connection: {exc}") from exc
        return self._pending_connection.redirect_url

    def wait_for_connection(self, timeout: float = 180.0) -> None:
        if self._pending_connection is None:
            return
        from composio.exceptions import ComposioError

        try:
            self._pending_connection.wait_for_connection(timeout=timeout)
        except ComposioError as exc:
            raise GitHubNotConnected(self._pending_connection.redirect_url) from exc
        self._pending_connection = None

    def file(self, report, repo: str, project_name: str, labels: Optional[List[str]] = None) -> FiledIssue:
        owner, name = split_repo(repo)
        url = self.connect_url()
        if url:
            raise GitHubNotConnected(url)

        fp = fingerprint(report)
        existing = self._find_open_issue(owner, name, fp)
        if existing is not None:
            self._run(
                COMMENT_ON_ISSUE,
                owner=owner,
                repo=name,
                issue_number=existing["number"],
                body=(
                    f"Inferra diagnosed this again for `{project_name}` "
                    f"(severity {report.severity.value}, confidence {report.confidence:.0%})."
                ),
            )
            return FiledIssue(existing["number"], existing["html_url"], duplicate=True)

        title, body = format_issue(report, project_name)
        arguments: Dict[str, Any] = {"owner": owner, "repo": name, "title": title, "body": body}
        if labels:
            arguments["labels"] = labels
        created = _find_issue(_unwrap(self._run(CREATE_ISSUE, **arguments)))
        if created is None:
            raise IssueFilingError(f"{CREATE_ISSUE} returned no issue number; check the Composio log")
        return FiledIssue(created["number"], created["html_url"], duplicate=False)

    def _find_open_issue(self, owner: str, name: str, fp: str) -> Optional[Dict[str, Any]]:
        marker = f"{FINGERPRINT_LABEL} `{fp}`"
        for page in range(1, MAX_PAGES_SCANNED + 1):
            issues = _as_issue_list(_unwrap(self._run(
                LIST_ISSUES, owner=owner, repo=name, state="open",
                sort="created", direction="desc", per_page=100, page=page,
            )))
            for issue in issues:
                if "pull_request" not in issue and marker in (issue.get("body") or ""):
                    return issue
            if len(issues) < 100:
                return None
        return None

    def _run(self, slug: str, **arguments) -> Any:
        try:
            response = self._session.execute(slug, arguments=arguments)
        except _client_errors() as exc:
            raise IssueFilingError(f"{slug} failed: {exc}") from exc
        payload = response.data
        failed = response.error or (isinstance(payload, dict) and payload.get("successful") is False)
        if failed:
            detail = response.error or (payload.get("error") if isinstance(payload, dict) else None)
            raise IssueFilingError(f"{slug} failed: {detail or 'unknown error'}", log_id=response.log_id)
        return payload
