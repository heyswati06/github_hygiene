"""
github_client.py
Thin wrapper around GitHub Enterprise REST API.
Handles pagination, rate limiting, and retries automatically.
"""

import os
import time
import logging
from datetime import datetime, timezone
from typing import Generator, Optional

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

logger = logging.getLogger(__name__)


class GitHubClient:
    """
    GitHub Enterprise REST API client.
    Reads token from the environment variable named in settings.
    """

    def __init__(self, base_url: str, token_env: str = "GITHUB_TOKEN"):
        token = os.environ.get(token_env)
        if not token:
            raise EnvironmentError(
                f"GitHub token not found. Please export {token_env}=<your_pat>"
            )

        self.base_url = base_url.rstrip("/")
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"token {token}",
            "Accept": "application/vnd.github.v3+json",
            "X-GitHub-Api-Version": "2022-11-28",
        })

        # Retry on transient failures (429, 502, 503, 504)
        retry = Retry(
            total=5,
            backoff_factor=1,
            status_forcelist=[429, 502, 503, 504],
            allowed_methods=["GET"],
        )
        self.session.mount("https://", HTTPAdapter(max_retries=retry))
        self.session.mount("http://", HTTPAdapter(max_retries=retry))

    def _get(self, path: str, params: dict = None) -> dict | list:
        url = f"{self.base_url}{path}"
        resp = self.session.get(url, params=params, timeout=30)
        self._handle_rate_limit(resp)
        resp.raise_for_status()
        return resp.json()

    def _paginate(self, path: str, params: dict = None) -> Generator:
        """Yield all items across paginated responses."""
        params = {**(params or {}), "per_page": 100, "page": 1}
        while True:
            url = f"{self.base_url}{path}"
            resp = self.session.get(url, params=params, timeout=30)
            self._handle_rate_limit(resp)
            resp.raise_for_status()
            data = resp.json()
            if not data:
                break
            yield from data
            if "next" not in resp.links:
                break
            params["page"] += 1

    def _handle_rate_limit(self, resp: requests.Response):
        if resp.status_code == 429 or (
            resp.status_code == 403 and "rate limit" in resp.text.lower()
        ):
            reset_ts = int(resp.headers.get("X-RateLimit-Reset", time.time() + 60))
            wait = max(reset_ts - int(time.time()), 10)
            logger.warning(f"Rate limited. Sleeping {wait}s...")
            time.sleep(wait)

    # ── Repo discovery ────────────────────────────────────────────────────────

    def list_org_repos(self, org: str, include_archived: bool = False) -> list[dict]:
        repos = list(self._paginate(f"/orgs/{org}/repos", {"type": "all"}))
        if not include_archived:
            repos = [r for r in repos if not r.get("archived")]
        return repos

    # ── Branches ─────────────────────────────────────────────────────────────

    def list_branches(self, owner: str, repo: str) -> list[dict]:
        return list(self._paginate(f"/repos/{owner}/{repo}/branches"))

    def get_branch_last_commit_date(self, owner: str, repo: str, branch: str) -> datetime:
        data = self._get(f"/repos/{owner}/{repo}/branches/{branch}")
        date_str = data["commit"]["commit"]["committer"]["date"]
        return datetime.fromisoformat(date_str.replace("Z", "+00:00"))

    def get_branch_protection(self, owner: str, repo: str, branch: str) -> Optional[dict]:
        try:
            return self._get(f"/repos/{owner}/{repo}/branches/{branch}/protection")
        except requests.HTTPError as e:
            if e.response.status_code == 404:
                return None
            raise

    # ── Pull Requests ─────────────────────────────────────────────────────────

    def list_open_prs(self, owner: str, repo: str) -> list[dict]:
        return list(self._paginate(f"/repos/{owner}/{repo}/pulls", {"state": "open"}))

    def get_pr_files(self, owner: str, repo: str, pr_number: int) -> list[dict]:
        return list(self._paginate(f"/repos/{owner}/{repo}/pulls/{pr_number}/files"))

    def get_pr_reviews(self, owner: str, repo: str, pr_number: int) -> list[dict]:
        return list(self._paginate(f"/repos/{owner}/{repo}/pulls/{pr_number}/reviews"))

    def get_pr_commits(self, owner: str, repo: str, pr_number: int) -> list[dict]:
        return list(self._paginate(f"/repos/{owner}/{repo}/pulls/{pr_number}/commits"))

    # ── Commits ───────────────────────────────────────────────────────────────

    def list_commits(self, owner: str, repo: str, since: datetime = None, branch: str = None) -> list[dict]:
        params = {}
        if since:
            params["since"] = since.isoformat()
        if branch:
            params["sha"] = branch
        return list(self._paginate(f"/repos/{owner}/{repo}/commits", params))

    # ── Deployments (for release frequency & LTFD) ───────────────────────────

    def list_deployments(self, owner: str, repo: str, since: datetime = None) -> list[dict]:
        deployments = list(self._paginate(f"/repos/{owner}/{repo}/deployments"))
        if since:
            deployments = [
                d for d in deployments
                if datetime.fromisoformat(d["created_at"].replace("Z", "+00:00")) >= since
            ]
        return deployments

    def list_releases(self, owner: str, repo: str) -> list[dict]:
        return list(self._paginate(f"/repos/{owner}/{repo}/releases"))

    # ── Workflows (Actions / pipelines) ───────────────────────────────────────

    def list_workflow_runs(self, owner: str, repo: str, since: datetime = None) -> list[dict]:
        params = {}
        if since:
            params["created"] = f">={since.strftime('%Y-%m-%d')}"
        return list(self._paginate(f"/repos/{owner}/{repo}/actions/runs", params))

    # ── Direct push detection ─────────────────────────────────────────────────

    def get_push_events(self, owner: str, repo: str) -> list[dict]:
        """Returns recent push events from the repo event stream."""
        events = list(self._paginate(f"/repos/{owner}/{repo}/events"))
        return [e for e in events if e.get("type") == "PushEvent"]
