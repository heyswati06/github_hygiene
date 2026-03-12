"""
tests/test_hygiene_checker.py

Unit tests for hygiene_checker.py
Tests all 6 check types using mocked GitHub API responses.
No real GitHub connection needed — safe to run in any CI environment.

Run:
    pytest tests/test_hygiene_checker.py -v
    pytest tests/test_hygiene_checker.py -v --tb=short   # compact output
    pytest tests/ -v --cov=scripts --cov-report=term     # with coverage
"""

import sys
import os
import pytest
from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock, patch, PropertyMock

# Allow imports from scripts/
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../scripts"))

from hygiene_checker import HygieneChecker, RepoHygieneResult, Violation


# ─── Fixtures ─────────────────────────────────────────────────────────────────

MOCK_CONFIG = {
    "github": {
        "base_url": "https://github.example.com/api/v3",
        "token_env": "GITHUB_TOKEN",
        "org": "test-org",
    },
    "repos": {
        "scan_all_repos": False,
        "include_archived": False,
        "explicit_list": ["test-org/repo-alpha"],
    },
    "team_leads": {
        "repo-alpha": "lead@example.com",
        "default_lead": "champion@example.com",
    },
    "hygiene": {
        "max_branch_age_days": 2,
        "max_pr_lines_changed": 400,
        "max_pr_review_hours": 4,
        "required_commit_pattern": r"^(feat|fix|chore|docs|refactor|test|ci)(\(.*\))?: .{10,}",
        "protected_branches": ["main", "master", "release/*"],
        "allow_direct_push_to_main": True,
    },
    "alerts": {
        "email": {
            "enabled": True,
            "smtp_host": "smtp.example.com",
            "smtp_port": 587,
            "smtp_user_env": "SMTP_USER",
            "smtp_pass_env": "SMTP_PASS",
            "from_address": "bot@example.com",
            "cc_champion": "champion@example.com",
        }
    },
    "schedule": {
        "hygiene_check_cron": "0 9 * * 1-5",
        "weekly_report_cron": "0 8 * * 5",
        "alert_cooldown_hours": 24,
    },
    "reporting": {
        "output_dir": "./reports",
        "weekly_report_day": "friday",
        "leaderboard_top_n": 10,
    },
}


def make_checker():
    """Return a HygieneChecker with mocked config and GitHub client."""
    with patch("builtins.open"), \
         patch("yaml.safe_load", return_value=MOCK_CONFIG), \
         patch("hygiene_checker.GitHubClient") as MockGH:
        checker = HygieneChecker.__new__(HygieneChecker)
        checker.cfg = MOCK_CONFIG
        checker.h = MOCK_CONFIG["hygiene"]
        checker.org = "test-org"
        checker.gh = MagicMock()
        return checker


def now_utc():
    return datetime.now(timezone.utc)


def days_ago(n):
    return now_utc() - timedelta(days=n)


def hours_ago(n):
    return now_utc() - timedelta(hours=n)


def make_branch(name, days_old=0):
    return {"name": name}


def make_pr(number, title, author, created_hours_ago, additions=100, deletions=50):
    return {
        "number": number,
        "title": title,
        "html_url": f"https://github.example.com/test-org/repo/pull/{number}",
        "user": {"login": author},
        "created_at": hours_ago(created_hours_ago).isoformat().replace("+00:00", "Z"),
    }


def make_commit(sha, message):
    return {
        "sha": sha,
        "commit": {"message": message},
    }


def make_file(additions, deletions):
    return {"additions": additions, "deletions": deletions, "filename": "src/file.py"}


def make_review(state):
    return {"state": state, "user": {"login": "reviewer"}}


# ─── RepoHygieneResult unit tests ────────────────────────────────────────────

class TestRepoHygieneResult:

    def test_starts_with_perfect_score(self):
        r = RepoHygieneResult("org/repo")
        assert r.score == 100
        assert r.passed is True
        assert r.critical_count == 0
        assert r.warning_count == 0

    def test_critical_deducts_20(self):
        r = RepoHygieneResult("org/repo")
        r.add(Violation("org/repo", "test", "critical", "T", "D"))
        assert r.score == 80
        assert r.critical_count == 1
        assert r.passed is False

    def test_warning_deducts_10(self):
        r = RepoHygieneResult("org/repo")
        r.add(Violation("org/repo", "test", "warning", "T", "D"))
        assert r.score == 90
        assert r.warning_count == 1
        assert r.passed is True   # warnings don't fail

    def test_score_never_below_zero(self):
        r = RepoHygieneResult("org/repo")
        for _ in range(10):
            r.add(Violation("org/repo", "test", "critical", "T", "D"))
        assert r.score == 0

    def test_violation_as_dict(self):
        v = Violation("org/repo", "stale_branch", "warning", "Title", "Detail", "http://url")
        d = v.as_dict()
        assert d["repo"] == "org/repo"
        assert d["check"] == "stale_branch"
        assert d["severity"] == "warning"
        assert "detected_at" in d


# ─── Stale branch checks ──────────────────────────────────────────────────────

class TestStaleBranchCheck:

    def test_fresh_branch_no_violation(self):
        checker = make_checker()
        result = RepoHygieneResult("org/repo")
        checker.gh.list_branches.return_value = [make_branch("feat/new-feature")]
        checker.gh.get_branch_last_commit_date.return_value = hours_ago(12)  # 12h old

        checker._check_stale_branches("org", "repo", result)

        assert len(result.violations) == 0

    def test_branch_over_limit_raises_warning(self):
        checker = make_checker()
        result = RepoHygieneResult("org/repo")
        checker.gh.list_branches.return_value = [make_branch("feat/old-stuff")]
        checker.gh.get_branch_last_commit_date.return_value = days_ago(3)

        checker._check_stale_branches("org", "repo", result)

        assert len(result.violations) == 1
        assert result.violations[0].check == "stale_branch"
        assert result.violations[0].severity == "warning"

    def test_very_old_branch_raises_critical(self):
        checker = make_checker()
        result = RepoHygieneResult("org/repo")
        checker.gh.list_branches.return_value = [make_branch("feat/ancient")]
        checker.gh.get_branch_last_commit_date.return_value = days_ago(10)  # >2*3=6 days

        checker._check_stale_branches("org", "repo", result)

        assert result.violations[0].severity == "critical"

    def test_protected_main_branch_skipped(self):
        checker = make_checker()
        result = RepoHygieneResult("org/repo")
        checker.gh.list_branches.return_value = [make_branch("main")]
        checker.gh.get_branch_last_commit_date.return_value = days_ago(30)

        checker._check_stale_branches("org", "repo", result)

        assert len(result.violations) == 0

    def test_protected_master_branch_skipped(self):
        checker = make_checker()
        result = RepoHygieneResult("org/repo")
        checker.gh.list_branches.return_value = [make_branch("master")]
        checker.gh.get_branch_last_commit_date.return_value = days_ago(30)

        checker._check_stale_branches("org", "repo", result)

        assert len(result.violations) == 0

    def test_release_wildcard_branch_skipped(self):
        checker = make_checker()
        result = RepoHygieneResult("org/repo")
        checker.gh.list_branches.return_value = [make_branch("release/v1.0")]
        checker.gh.get_branch_last_commit_date.return_value = days_ago(30)

        checker._check_stale_branches("org", "repo", result)

        assert len(result.violations) == 0

    def test_multiple_stale_branches_all_flagged(self):
        checker = make_checker()
        result = RepoHygieneResult("org/repo")
        checker.gh.list_branches.return_value = [
            make_branch("feat/stale-a"),
            make_branch("feat/stale-b"),
            make_branch("feat/fresh"),
        ]
        checker.gh.get_branch_last_commit_date.side_effect = [
            days_ago(5), days_ago(4), hours_ago(6)
        ]

        checker._check_stale_branches("org", "repo", result)

        assert len(result.violations) == 2

    def test_api_error_on_branch_date_skipped_gracefully(self):
        checker = make_checker()
        result = RepoHygieneResult("org/repo")
        checker.gh.list_branches.return_value = [make_branch("feat/broken")]
        checker.gh.get_branch_last_commit_date.side_effect = Exception("API error")

        checker._check_stale_branches("org", "repo", result)

        assert len(result.violations) == 0  # silently skipped


# ─── PR checks ────────────────────────────────────────────────────────────────

class TestPRSizeCheck:

    def _run_pr_check(self, pr, files, reviews=None, commits=None):
        checker = make_checker()
        result = RepoHygieneResult("org/repo")
        checker.gh.list_open_prs.return_value = [pr]
        checker.gh.get_pr_files.return_value = files
        checker.gh.get_pr_reviews.return_value = reviews or []
        checker.gh.get_pr_commits.return_value = commits or [
            make_commit("abc1234", "feat(core): add something meaningful here")
        ]
        checker._check_open_prs("org", "repo", result)
        return result

    def test_pr_within_limit_no_violation(self):
        pr = make_pr(1, "Small change", "dev1", created_hours_ago=2)
        files = [make_file(100, 50)]  # 150 lines
        result = self._run_pr_check(pr, files)
        size_violations = [v for v in result.violations if v.check == "pr_size"]
        assert len(size_violations) == 0

    def test_pr_exactly_at_limit_no_violation(self):
        pr = make_pr(1, "Boundary PR", "dev1", created_hours_ago=2)
        files = [make_file(200, 200)]  # exactly 400
        result = self._run_pr_check(pr, files)
        size_violations = [v for v in result.violations if v.check == "pr_size"]
        assert len(size_violations) == 0

    def test_pr_over_limit_raises_critical(self):
        pr = make_pr(1, "Big PR", "dev1", created_hours_ago=2)
        files = [make_file(300, 200)]  # 500 lines
        result = self._run_pr_check(pr, files)
        size_violations = [v for v in result.violations if v.check == "pr_size"]
        assert len(size_violations) == 1
        assert size_violations[0].severity == "critical"

    def test_pr_size_detail_mentions_author(self):
        pr = make_pr(42, "Huge change", "johndoe", created_hours_ago=2)
        files = [make_file(500, 100)]
        result = self._run_pr_check(pr, files)
        v = next(v for v in result.violations if v.check == "pr_size")
        assert "johndoe" in v.detail
        assert "42" in v.title

    def test_pr_size_counts_additions_and_deletions(self):
        pr = make_pr(1, "Refactor", "dev1", created_hours_ago=2)
        files = [make_file(0, 450)]  # deletions count too
        result = self._run_pr_check(pr, files)
        size_violations = [v for v in result.violations if v.check == "pr_size"]
        assert len(size_violations) == 1

    def test_multiple_files_summed_correctly(self):
        pr = make_pr(1, "Multi-file", "dev1", created_hours_ago=2)
        files = [make_file(100, 50), make_file(100, 50), make_file(100, 50)]  # 450 total
        result = self._run_pr_check(pr, files)
        size_violations = [v for v in result.violations if v.check == "pr_size"]
        assert len(size_violations) == 1


class TestPRReviewSLACheck:

    def _run_pr_check(self, pr, reviews=None, commits=None):
        checker = make_checker()
        result = RepoHygieneResult("org/repo")
        checker.gh.list_open_prs.return_value = [pr]
        checker.gh.get_pr_files.return_value = [make_file(50, 20)]
        checker.gh.get_pr_reviews.return_value = reviews or []
        checker.gh.get_pr_commits.return_value = commits or [
            make_commit("abc1234", "feat(core): add something meaningful here")
        ]
        checker._check_open_prs("org", "repo", result)
        return result

    def test_pr_reviewed_within_sla_no_violation(self):
        pr = make_pr(1, "Quick PR", "dev1", created_hours_ago=6)
        result = self._run_pr_check(pr, reviews=[make_review("APPROVED")])
        review_violations = [v for v in result.violations if v.check == "pr_review_sla"]
        assert len(review_violations) == 0

    def test_pr_within_sla_window_no_violation(self):
        pr = make_pr(1, "New PR", "dev1", created_hours_ago=2)  # under 4h SLA
        result = self._run_pr_check(pr, reviews=[])
        review_violations = [v for v in result.violations if v.check == "pr_review_sla"]
        assert len(review_violations) == 0

    def test_pr_unreviewed_over_sla_raises_warning(self):
        pr = make_pr(1, "Waiting PR", "dev1", created_hours_ago=6)
        result = self._run_pr_check(pr, reviews=[])
        review_violations = [v for v in result.violations if v.check == "pr_review_sla"]
        assert len(review_violations) == 1
        assert review_violations[0].severity == "warning"

    def test_pr_unreviewed_very_long_raises_critical(self):
        pr = make_pr(1, "Ancient PR", "dev1", created_hours_ago=15)  # >4*3=12h
        result = self._run_pr_check(pr, reviews=[])
        review_violations = [v for v in result.violations if v.check == "pr_review_sla"]
        assert len(review_violations) == 1
        assert review_violations[0].severity == "critical"

    def test_changes_requested_counts_as_reviewed(self):
        pr = make_pr(1, "Review needed", "dev1", created_hours_ago=8)
        result = self._run_pr_check(pr, reviews=[make_review("CHANGES_REQUESTED")])
        review_violations = [v for v in result.violations if v.check == "pr_review_sla"]
        assert len(review_violations) == 0

    def test_comment_review_counts_as_reviewed(self):
        pr = make_pr(1, "Commented", "dev1", created_hours_ago=8)
        result = self._run_pr_check(pr, reviews=[make_review("COMMENTED")])
        review_violations = [v for v in result.violations if v.check == "pr_review_sla"]
        assert len(review_violations) == 0


# ─── Commit message checks ────────────────────────────────────────────────────

class TestCommitMessageCheck:

    VALID_MESSAGES = [
        "feat(auth): add OAuth2 token refresh on 401",
        "fix(api): handle null response from upstream",
        "chore: update dependencies to latest versions",
        "docs(readme): add setup instructions for new devs",
        "refactor(core): extract payment processor to service",
        "test(unit): add coverage for edge cases in validator",
        "ci(pipeline): enable parallel test execution stage",
        "feat: implement user preference persistence layer",
    ]

    INVALID_MESSAGES = [
        "fixed stuff",
        "WIP",
        "update",
        "JIRA-1234 some change",
        "Merge branch 'feat/old' into main",
        "feat: short",          # description too short (<10 chars)
        "Feature: add login",   # capital F not matching pattern
        "",
    ]

    def _run_with_commit(self, commit_message):
        checker = make_checker()
        result = RepoHygieneResult("org/repo")
        pr = make_pr(1, "Test PR", "dev1", created_hours_ago=2)
        checker.gh.list_open_prs.return_value = [pr]
        checker.gh.get_pr_files.return_value = [make_file(50, 20)]
        checker.gh.get_pr_reviews.return_value = [make_review("APPROVED")]
        checker.gh.get_pr_commits.return_value = [make_commit("abc1234", commit_message)]
        checker._check_open_prs("org", "repo", result)
        return result

    @pytest.mark.parametrize("msg", VALID_MESSAGES)
    def test_valid_commit_message_no_violation(self, msg):
        result = self._run_with_commit(msg)
        commit_violations = [v for v in result.violations if v.check == "commit_message_format"]
        assert len(commit_violations) == 0, f"False positive for: {msg}"

    @pytest.mark.parametrize("msg", INVALID_MESSAGES)
    def test_invalid_commit_message_raises_warning(self, msg):
        result = self._run_with_commit(msg)
        commit_violations = [v for v in result.violations if v.check == "commit_message_format"]
        assert len(commit_violations) == 1, f"Missed violation for: '{msg}'"
        assert commit_violations[0].severity == "warning"

    def test_only_first_line_checked_for_multiline_commit(self):
        """Multi-line commit: first line valid, body irrelevant."""
        msg = "feat(core): add payment gateway integration\n\nThis is the body of the commit message."
        result = self._run_with_commit(msg)
        commit_violations = [v for v in result.violations if v.check == "commit_message_format"]
        assert len(commit_violations) == 0

    def test_multiple_bad_commits_in_one_pr(self):
        checker = make_checker()
        result = RepoHygieneResult("org/repo")
        pr = make_pr(1, "Big messy PR", "dev1", created_hours_ago=2)
        checker.gh.list_open_prs.return_value = [pr]
        checker.gh.get_pr_files.return_value = [make_file(50, 20)]
        checker.gh.get_pr_reviews.return_value = [make_review("APPROVED")]
        checker.gh.get_pr_commits.return_value = [
            make_commit("aaa", "fixed stuff"),
            make_commit("bbb", "feat(core): valid message here"),
            make_commit("ccc", "wip"),
        ]
        checker._check_open_prs("org", "repo", result)
        commit_violations = [v for v in result.violations if v.check == "commit_message_format"]
        assert len(commit_violations) == 1  # one violation per PR
        assert commit_violations[0].metadata["bad_commit_count"] == 2


# ─── Branch protection checks ─────────────────────────────────────────────────

class TestBranchProtectionCheck:

    def test_main_with_protection_no_violation(self):
        checker = make_checker()
        result = RepoHygieneResult("org/repo")
        checker.gh.list_branches.return_value = [{"name": "main"}, {"name": "master"}]
        checker.gh.get_branch_protection.return_value = {"required_status_checks": {}}

        checker._check_branch_protection("org", "repo", result)

        assert len(result.violations) == 0

    def test_main_without_protection_raises_critical(self):
        checker = make_checker()
        result = RepoHygieneResult("org/repo")
        checker.gh.list_branches.return_value = [{"name": "main"}]
        checker.gh.get_branch_protection.return_value = None  # 404 = no protection

        checker._check_branch_protection("org", "repo", result)

        assert len(result.violations) == 1
        assert result.violations[0].check == "branch_protection"
        assert result.violations[0].severity == "critical"

    def test_repo_with_no_main_or_master_skipped(self):
        checker = make_checker()
        result = RepoHygieneResult("org/repo")
        checker.gh.list_branches.return_value = [{"name": "develop"}, {"name": "feat/x"}]
        checker.gh.get_branch_protection.return_value = None

        checker._check_branch_protection("org", "repo", result)

        assert len(result.violations) == 0


# ─── Direct push checks ───────────────────────────────────────────────────────

class TestDirectPushCheck:

    def _make_push_event(self, branch, actor, hours_ago_n=1, commit_count=1):
        return {
            "type": "PushEvent",
            "actor": {"login": actor},
            "created_at": hours_ago(hours_ago_n).isoformat().replace("+00:00", "Z"),
            "payload": {
                "ref": f"refs/heads/{branch}",
                "commits": [{"sha": "abc"} for _ in range(commit_count)],
            },
        }

    def test_direct_push_to_main_raises_critical(self):
        checker = make_checker()
        result = RepoHygieneResult("org/repo")
        checker.gh.get_push_events.return_value = [
            self._make_push_event("main", "bad-actor", hours_ago_n=2)
        ]

        checker._check_direct_pushes("org", "repo", result)

        assert len(result.violations) == 1
        assert result.violations[0].check == "direct_push_to_main"
        assert result.violations[0].severity == "critical"
        assert "bad-actor" in result.violations[0].detail

    def test_push_to_feature_branch_not_flagged(self):
        checker = make_checker()
        result = RepoHygieneResult("org/repo")
        checker.gh.get_push_events.return_value = [
            self._make_push_event("feat/new-login", "dev1", hours_ago_n=2)
        ]

        checker._check_direct_pushes("org", "repo", result)

        assert len(result.violations) == 0

    def test_old_push_event_beyond_7_days_ignored(self):
        checker = make_checker()
        result = RepoHygieneResult("org/repo")
        checker.gh.get_push_events.return_value = [
            self._make_push_event("main", "old-actor", hours_ago_n=24 * 8)  # 8 days ago
        ]

        checker._check_direct_pushes("org", "repo", result)

        assert len(result.violations) == 0

    def test_multiple_pushes_all_flagged(self):
        checker = make_checker()
        result = RepoHygieneResult("org/repo")
        checker.gh.get_push_events.return_value = [
            self._make_push_event("main", "dev1", hours_ago_n=2),
            self._make_push_event("master", "dev2", hours_ago_n=3),
        ]

        checker._check_direct_pushes("org", "repo", result)

        assert len(result.violations) == 2


# ─── Integration: full repo check ────────────────────────────────────────────

class TestFullRepoCheck:

    def test_clean_repo_scores_100(self):
        checker = make_checker()

        checker.gh.list_branches.return_value = [
            {"name": "main"}, {"name": "feat/fresh"}
        ]
        checker.gh.get_branch_last_commit_date.return_value = hours_ago(10)
        checker.gh.get_branch_protection.return_value = {"required_status_checks": {}}
        checker.gh.list_open_prs.return_value = []
        checker.gh.get_push_events.return_value = []

        result = checker.check_repo("test-org", "clean-repo")

        assert result.score == 100
        assert result.passed is True
        assert len(result.violations) == 0

    def test_repo_with_mixed_violations(self):
        checker = make_checker()

        checker.gh.list_branches.return_value = [
            {"name": "main"},
            {"name": "feat/very-old"},
        ]
        checker.gh.get_branch_last_commit_date.return_value = days_ago(5)
        checker.gh.get_branch_protection.return_value = None   # missing protection
        checker.gh.list_open_prs.return_value = [
            make_pr(1, "Huge PR", "dev1", created_hours_ago=2)
        ]
        checker.gh.get_pr_files.return_value = [make_file(500, 100)]  # oversized
        checker.gh.get_pr_reviews.return_value = [make_review("APPROVED")]
        checker.gh.get_pr_commits.return_value = [
            make_commit("abc", "feat(x): valid commit message here")
        ]
        checker.gh.get_push_events.return_value = []

        result = checker.check_repo("test-org", "messy-repo")

        checks = {v.check for v in result.violations}
        assert "stale_branch" in checks
        assert "branch_protection" in checks
        assert "pr_size" in checks
        assert result.score < 100
        assert result.passed is False   # has critical violations

    def test_repo_discovery_explicit_list(self):
        checker = make_checker()
        repos = checker.discover_repos()
        assert ("test-org", "repo-alpha") in repos

    def test_run_all_handles_api_error_gracefully(self):
        checker = make_checker()
        checker.gh.list_branches.side_effect = Exception("Connection refused")

        results = checker.run_all([("org", "broken-repo")])

        assert len(results) == 1
        assert any(v.check == "api_error" for v in results[0].violations)
