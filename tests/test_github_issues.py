"""
test_github_issues.py — Tests for filing RCA reports as GitHub issues via Composio

Covers:
- Fingerprint stability across LLM rewording, and sensitivity to real changes
- Issue title/body formatting
- Repository parsing
- Filing flow against a fake Composio session: connection, creation,
  duplicate detection, pagination, and error reporting
"""

from types import SimpleNamespace

import pytest

from inferra.agents import Finding, FindingType, RCAReport, Severity
from inferra.github_issues import (
    COMMENT_ON_ISSUE,
    CREATE_ISSUE,
    FINGERPRINT_LABEL,
    LIST_ISSUES,
    GitHubIssueFiler,
    GitHubNotConnected,
    IssueFilingError,
    fingerprint,
    format_issue,
    split_repo,
)


def make_report(root_cause="Sequential pipeline blocks the request",
                locations=("routes.py:195", "cve_extractor.py:43")):
    finding = Finding(
        agent_name="MetricsCorrelation",
        finding_type=FindingType.PERFORMANCE_ANOMALY,
        severity=Severity.HIGH,
        summary="3 agents run sequentially",
        details="",
        evidence=["cve_extractor 1.2s", "attack_classifier 1.1s", "playbook_generator 1.2s", "FOURTH_EVIDENCE"],
        affected_spans=[],
        confidence=0.9,
        recommendations=[],
        source_locations=list(locations),
    )
    return RCAReport(
        root_cause=root_cause,
        severity=Severity.HIGH,
        confidence=0.92,
        summary="POST /api/analyze takes 3.5s",
        detailed_analysis="",
        causal_chain=["request arrives", "graph.invoke() runs agents one by one"],
        findings=[finding],
        recommendations=["Use asyncio.gather() to parallelize the agents"],
        source_locations=list(locations),
        conflicting_findings=[],
    )


class FakeSession:
    """Mirrors live Composio responses, e.g. list returns {"issues": [...]} (observed 2026-09-18)."""

    def __init__(self, connected=True, pages=None, fail_on=None, created_shape="nested"):
        self.connected = connected
        self.pages = pages if pages is not None else [[]]
        self.fail_on = fail_on
        self.created_shape = created_shape
        self.calls = []

    def toolkits(self, toolkits):
        connection = SimpleNamespace(is_active=self.connected)
        return SimpleNamespace(items=[SimpleNamespace(slug="github", connection=connection)])

    def authorize(self, toolkit):
        return SimpleNamespace(redirect_url="https://connect.example/abc", wait_for_connection=lambda timeout: None)

    def execute(self, slug, arguments):
        self.calls.append((slug, arguments))
        if slug == self.fail_on:
            return SimpleNamespace(data={"successful": False, "error": "Not Found", "data": {}},
                                   error=None, log_id="log_123")
        if slug == LIST_ISSUES:
            page = arguments["page"]
            issues = self.pages[page - 1] if page <= len(self.pages) else []
            return SimpleNamespace(data={"issues": issues}, error=None, log_id="log_list")
        if slug == CREATE_ISSUE:
            issue = {"number": 7, "html_url": "https://github.com/acme/app/issues/7"}
            data = {"direct": issue, "nested": {"issue": issue}, "empty": {}}[self.created_shape]
            return SimpleNamespace(data=data, error=None, log_id="log_create")
        return SimpleNamespace(data={"data": {}, "successful": True, "error": None},
                               error=None, log_id="log_comment")

    def slugs(self):
        return [slug for slug, _ in self.calls]


class TestFingerprint:
    """The fingerprint must survive LLM rewording but change when the problem does."""

    def test_stable_when_root_cause_is_reworded(self):
        a = make_report(root_cause="Sequential pipeline blocks the request")
        b = make_report(root_cause="The agents run one after another, blocking I/O")
        assert fingerprint(a) == fingerprint(b)

    def test_ignores_location_order(self):
        a = make_report(locations=("routes.py:195", "cve_extractor.py:43"))
        b = make_report(locations=("cve_extractor.py:43", "routes.py:195"))
        assert fingerprint(a) == fingerprint(b)

    def test_changes_when_location_changes(self):
        a = make_report(locations=("routes.py:195",))
        b = make_report(locations=("routes.py:210",))
        assert fingerprint(a) != fingerprint(b)

    def test_falls_back_to_root_cause_without_locations_or_findings(self):
        a = make_report(root_cause="A", locations=())
        b = make_report(root_cause="B", locations=())
        a.findings, b.findings = [], []
        assert fingerprint(a) != fingerprint(b)


class TestFormatIssue:
    """Title and body carry the diagnosis and the dedupe marker."""

    def test_title_has_severity_and_is_bounded(self):
        title, _ = format_issue(make_report(root_cause="x" * 300), "app")
        assert title.startswith("[Inferra] HIGH: ")
        assert len(title) <= len("[Inferra] HIGH: ") + 100

    def test_body_includes_locations_recommendations_and_fingerprint(self):
        report = make_report()
        _, body = format_issue(report, "app")
        assert "`routes.py:195`" in body
        assert "asyncio.gather()" in body
        assert "92%" in body
        assert f"{FINGERPRINT_LABEL} `{fingerprint(report)}`" in body

    def test_evidence_is_capped_per_finding(self):
        _, body = format_issue(make_report(), "app")
        assert "playbook_generator 1.2s" in body
        assert "FOURTH_EVIDENCE" not in body


class TestSplitRepo:
    def test_parses_owner_and_name(self):
        assert split_repo("acme/app") == ("acme", "app")

    def test_strips_git_suffix(self):
        assert split_repo("acme/app.git") == ("acme", "app")

    @pytest.mark.parametrize("bad", ["app", "acme/", "/app", "a/b/c"])
    def test_rejects_malformed(self, bad):
        with pytest.raises(ValueError):
            split_repo(bad)


class TestFiler:
    """Filing flow against a fake Composio session."""

    def test_not_connected_returns_connect_link_without_executing(self):
        session = FakeSession(connected=False)
        with pytest.raises(GitHubNotConnected) as exc:
            GitHubIssueFiler(session=session).file(make_report(), "acme/app", "app")
        assert exc.value.connect_url == "https://connect.example/abc"
        assert session.calls == []

    def test_creates_issue_when_no_duplicate(self):
        session = FakeSession()
        issue = GitHubIssueFiler(session=session).file(make_report(), "acme/app", "app")
        assert (issue.number, issue.duplicate) == (7, False)
        assert issue.url.endswith("/issues/7")
        slug, args = session.calls[-1]
        assert slug == CREATE_ISSUE
        assert (args["owner"], args["repo"]) == ("acme", "app")
        assert args["title"].startswith("[Inferra] HIGH")
        assert "labels" not in args

    @pytest.mark.parametrize("shape", ["direct", "nested"])
    def test_reads_issue_from_either_response_shape(self, shape):
        session = FakeSession(created_shape=shape)
        issue = GitHubIssueFiler(session=session).file(make_report(), "acme/app", "app")
        assert issue.number == 7

    def test_missing_issue_in_response_raises(self):
        session = FakeSession(created_shape="empty")
        with pytest.raises(IssueFilingError):
            GitHubIssueFiler(session=session).file(make_report(), "acme/app", "app")

    def test_passes_labels_through(self):
        session = FakeSession()
        GitHubIssueFiler(session=session).file(make_report(), "acme/app", "app", labels=["bug"])
        assert session.calls[-1][1]["labels"] == ["bug"]

    def test_comments_on_existing_issue_instead_of_duplicating(self):
        report = make_report()
        existing = {"number": 3, "html_url": "https://github.com/acme/app/issues/3",
                    "body": f"... {FINGERPRINT_LABEL} `{fingerprint(report)}`"}
        session = FakeSession(pages=[[existing]])
        issue = GitHubIssueFiler(session=session).file(report, "acme/app", "app")
        assert (issue.number, issue.duplicate) == (3, True)
        assert CREATE_ISSUE not in session.slugs()
        assert session.calls[-1][0] == COMMENT_ON_ISSUE
        assert session.calls[-1][1]["issue_number"] == 3

    def test_ignores_pull_requests_carrying_the_marker(self):
        report = make_report()
        pr = {"number": 9, "html_url": "x", "pull_request": {},
              "body": f"{FINGERPRINT_LABEL} `{fingerprint(report)}`"}
        session = FakeSession(pages=[[pr]])
        issue = GitHubIssueFiler(session=session).file(report, "acme/app", "app")
        assert issue.duplicate is False

    def test_finds_duplicate_on_a_later_page(self):
        report = make_report()
        filler = [{"number": n, "html_url": "x", "body": "unrelated"} for n in range(100)]
        match = {"number": 42, "html_url": "https://github.com/acme/app/issues/42",
                 "body": f"{FINGERPRINT_LABEL} `{fingerprint(report)}`"}
        session = FakeSession(pages=[filler, [match]])
        issue = GitHubIssueFiler(session=session).file(report, "acme/app", "app")
        assert issue.number == 42
        assert [a["page"] for s, a in session.calls if s == LIST_ISSUES] == [1, 2]

    def test_tool_failure_raises_with_log_id(self):
        session = FakeSession(fail_on=CREATE_ISSUE)
        with pytest.raises(IssueFilingError) as exc:
            GitHubIssueFiler(session=session).file(make_report(), "acme/app", "app")
        assert exc.value.log_id == "log_123"
        assert "Not Found" in str(exc.value)

    def test_wait_timeout_becomes_not_connected(self):
        exceptions = pytest.importorskip("composio.exceptions")
        ComposioSDKTimeoutError = exceptions.ComposioSDKTimeoutError

        def time_out(timeout):
            raise ComposioSDKTimeoutError(message="timed out")

        session = FakeSession(connected=False)
        session.authorize = lambda toolkit: SimpleNamespace(
            redirect_url="https://connect.example/abc", wait_for_connection=time_out)
        filer = GitHubIssueFiler(session=session)
        assert filer.connect_url() == "https://connect.example/abc"
        with pytest.raises(GitHubNotConnected):
            filer.wait_for_connection(timeout=0.01)
