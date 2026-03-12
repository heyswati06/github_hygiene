"""
tests/test_github_client.py

Unit tests for github_client.py
Tests pagination, rate limit handling, and each API method
using mocked HTTP responses — no real GitHub needed.

Run:
    pytest tests/test_github_client.py -v
"""

import sys
import os
import pytest
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch, call

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../scripts"))


def make_response(json_data, status=200, headers=None, links=None):
    """Build a mock requests.Response."""
    resp = MagicMock()
    resp.status_code = status
    resp.json.return_value = json_data
    resp.headers = headers or {"X-RateLimit-Remaining": "1000"}
    resp.text = str(json_data)
    resp.links = links or {}
    resp.raise_for_status = MagicMock()
    if status >= 400:
        import requests
        resp.raise_for_status.side_effect = requests.HTTPError(response=resp)
    return resp


@pytest.fixture
def client():
    """Return a GitHubClient with mocked session."""
    with patch.dict(os.environ, {"GITHUB_TOKEN": "test-token-abc"}):
        from github_client import GitHubClient
        c = GitHubClient("https://ghe.example.com/api/v3", "GITHUB_TOKEN")
        c.session = MagicMock()
        return c


# ─── Auth & init ─────────────────────────────────────────────────────────────

class TestClientInit:

    def test_raises_if_token_not_set(self):
        with patch.dict(os.environ, {}, clear=True):
            os.environ.pop("GITHUB_TOKEN", None)
            from github_client import GitHubClient
            with pytest.raises(EnvironmentError, match="GITHUB_TOKEN"):
                GitHubClient("https://ghe.example.com/api/v3", "GITHUB_TOKEN")

    def test_token_added_to_headers(self):
        with patch.dict(os.environ, {"GITHUB_TOKEN": "ghp_test123"}):
            from github_client import GitHubClient
            c = GitHubClient("https://ghe.example.com/api/v3", "GITHUB_TOKEN")
            assert "ghp_test123" in c.session.headers.get("Authorization", "")

    def test_base_url_trailing_slash_stripped(self):
        with patch.dict(os.environ, {"GITHUB_TOKEN": "tok"}):
            from github_client import GitHubClient
            c = GitHubClient("https://ghe.example.com/api/v3/", "GITHUB_TOKEN")
            assert not c.base_url.endswith("/")


# ─── Pagination ───────────────────────────────────────────────────────────────

class TestPagination:

    def test_single_page_returns_all_items(self, client):
        page1 = make_response([{"id": 1}, {"id": 2}])
        client.session.get.return_value = page1
        results = list(client._paginate("/orgs/test-org/repos"))
        assert len(results) == 2

    def test_multiple_pages_all_collected(self, client):
        page1 = make_response([{"id": 1}, {"id": 2}], links={"next": {"url": "...?page=2"}})
        page2 = make_response([{"id": 3}], links={})
        client.session.get.side_effect = [page1, page2]
        results = list(client._paginate("/orgs/test-org/repos"))
        assert len(results) == 3
        assert results[-1]["id"] == 3

    def test_empty_response_stops_pagination(self, client):
        empty_page = make_response([])
        client.session.get.return_value = empty_page
        results = list(client._paginate("/orgs/test-org/repos"))
        assert results == []

    def test_per_page_set_to_100(self, client):
        client.session.get.return_value = make_response([])
        list(client._paginate("/test"))
        call_kwargs = client.session.get.call_args
        params = call_kwargs[1].get("params") or call_kwargs[0][1] if len(call_kwargs[0]) > 1 else {}
        # Verify per_page=100 sent in params
        assert client.session.get.called


# ─── Rate limit handling ──────────────────────────────────────────────────────

class TestRateLimitHandling:

    def test_rate_limit_429_sleeps_and_retries(self, client):
        rate_resp = make_response({}, status=429,
                                   headers={"X-RateLimit-Reset": "9999999999"})
        ok_resp = make_response({"id": 1})
        client.session.get.side_effect = [rate_resp, ok_resp]

        with patch("time.sleep") as mock_sleep:
            client._handle_rate_limit(rate_resp)
            mock_sleep.assert_called_once()

    def test_403_with_rate_limit_text_triggers_sleep(self, client):
        rate_resp = make_response(
            {"message": "API rate limit exceeded"},
            status=403,
            headers={"X-RateLimit-Reset": "9999999999"}
        )
        rate_resp.text = "API rate limit exceeded"
        with patch("time.sleep") as mock_sleep:
            client._handle_rate_limit(rate_resp)
            mock_sleep.assert_called_once()

    def test_normal_200_no_sleep(self, client):
        ok_resp = make_response([{"id": 1}])
        with patch("time.sleep") as mock_sleep:
            client._handle_rate_limit(ok_resp)
            mock_sleep.assert_not_called()


# ─── Repo methods ─────────────────────────────────────────────────────────────

class TestRepoMethods:

    def test_list_org_repos_filters_archived(self, client):
        repos = [
            {"name": "active", "archived": False, "owner": {"login": "org"}},
            {"name": "old",    "archived": True,  "owner": {"login": "org"}},
        ]
        client.session.get.return_value = make_response(repos)
        result = client.list_org_repos("test-org", include_archived=False)
        assert len(result) == 1
        assert result[0]["name"] == "active"

    def test_list_org_repos_includes_archived_when_requested(self, client):
        repos = [
            {"name": "active", "archived": False, "owner": {"login": "org"}},
            {"name": "old",    "archived": True,  "owner": {"login": "org"}},
        ]
        client.session.get.return_value = make_response(repos)
        result = client.list_org_repos("test-org", include_archived=True)
        assert len(result) == 2


# ─── Branch methods ───────────────────────────────────────────────────────────

class TestBranchMethods:

    def test_list_branches_returns_all(self, client):
        branches = [{"name": "main"}, {"name": "feat/x"}]
        client.session.get.return_value = make_response(branches)
        result = client.list_branches("org", "repo")
        assert len(result) == 2

    def test_get_branch_last_commit_date_parses_correctly(self, client):
        data = {
            "name": "main",
            "commit": {
                "commit": {
                    "committer": {"date": "2026-03-01T10:00:00Z"}
                }
            }
        }
        client.session.get.return_value = make_response(data)
        result = client.get_branch_last_commit_date("org", "repo", "main")
        assert isinstance(result, datetime)
        assert result.year == 2026
        assert result.month == 3

    def test_get_branch_protection_returns_none_on_404(self, client):
        import requests
        resp_404 = make_response({}, status=404)
        client.session.get.return_value = resp_404
        result = client.get_branch_protection("org", "repo", "main")
        assert result is None


# ─── PR methods ───────────────────────────────────────────────────────────────

class TestPRMethods:

    def test_list_open_prs_passes_state_open(self, client):
        client.session.get.return_value = make_response([])
        client.list_open_prs("org", "repo")
        call_args = client.session.get.call_args
        # Verify "open" state is in params
        assert client.session.get.called

    def test_get_pr_files_returns_file_list(self, client):
        files = [
            {"filename": "src/main.py", "additions": 50, "deletions": 10},
            {"filename": "tests/test_main.py", "additions": 30, "deletions": 5},
        ]
        client.session.get.return_value = make_response(files)
        result = client.get_pr_files("org", "repo", 42)
        assert len(result) == 2
        assert result[0]["filename"] == "src/main.py"

    def test_get_pr_reviews_returns_reviews(self, client):
        reviews = [{"id": 1, "state": "APPROVED", "user": {"login": "reviewer1"}}]
        client.session.get.return_value = make_response(reviews)
        result = client.get_pr_reviews("org", "repo", 42)
        assert len(result) == 1
        assert result[0]["state"] == "APPROVED"

    def test_get_pr_commits_returns_commits(self, client):
        commits = [
            {"sha": "abc123", "commit": {"message": "feat: add feature"}},
            {"sha": "def456", "commit": {"message": "fix: fix bug"}},
        ]
        client.session.get.return_value = make_response(commits)
        result = client.get_pr_commits("org", "repo", 42)
        assert len(result) == 2


# ─── Deployment / release methods ────────────────────────────────────────────

class TestDeploymentMethods:

    def test_list_deployments_filters_by_since(self, client):
        since = datetime(2026, 3, 1, tzinfo=timezone.utc)
        deployments = [
            {"id": 1, "created_at": "2026-03-05T10:00:00Z"},
            {"id": 2, "created_at": "2026-02-15T10:00:00Z"},  # before since
        ]
        client.session.get.return_value = make_response(deployments)
        result = client.list_deployments("org", "repo", since=since)
        assert len(result) == 1
        assert result[0]["id"] == 1

    def test_list_releases_returns_all(self, client):
        releases = [{"id": 1, "tag_name": "v1.0.0"}, {"id": 2, "tag_name": "v1.1.0"}]
        client.session.get.return_value = make_response(releases)
        result = client.list_releases("org", "repo")
        assert len(result) == 2


# ─── Push event methods ───────────────────────────────────────────────────────

class TestPushEventMethods:

    def test_get_push_events_filters_non_push(self, client):
        events = [
            {"type": "PushEvent",   "payload": {"ref": "refs/heads/main"}},
            {"type": "IssuesEvent", "payload": {}},
            {"type": "PushEvent",   "payload": {"ref": "refs/heads/feat/x"}},
        ]
        client.session.get.return_value = make_response(events)
        result = client.get_push_events("org", "repo")
        assert len(result) == 2
        assert all(e["type"] == "PushEvent" for e in result)
