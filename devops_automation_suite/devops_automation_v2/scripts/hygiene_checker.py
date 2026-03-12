"""
hygiene_checker.py
Core engine that runs all Git hygiene checks against every repo.

Checks performed:
  1. Stale branches (age > max_branch_age_days)
  2. Oversized PRs (lines changed > max_pr_lines_changed)
  3. Unreviewed PRs (open > max_pr_review_hours without a review)
  4. Commit message format violations (Conventional Commits)
  5. Direct pushes to main/master (branch protection bypass)
  6. Missing branch protection rules

Run directly:  python hygiene_checker.py
Or import:     from hygiene_checker import HygieneChecker
"""

import re
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Optional

import yaml

from github_client import GitHubClient

logger = logging.getLogger(__name__)


# ── Data classes ──────────────────────────────────────────────────────────────

@dataclass
class Violation:
    repo: str
    check: str                          # e.g. "stale_branch", "pr_size"
    severity: str                       # "critical" | "warning" | "info"
    title: str
    detail: str
    url: str = ""
    metadata: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "repo": self.repo,
            "check": self.check,
            "severity": self.severity,
            "title": self.title,
            "detail": self.detail,
            "url": self.url,
            "metadata": self.metadata,
            "detected_at": datetime.now(timezone.utc).isoformat(),
        }


@dataclass
class RepoHygieneResult:
    repo_full_name: str                 # e.g. "my-org/my-repo"
    violations: list[Violation] = field(default_factory=list)
    score: int = 100                    # Starts at 100, deducted per violation
    checked_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def add(self, v: Violation):
        self.violations.append(v)
        deduction = {"critical": 20, "warning": 10, "info": 3}.get(v.severity, 5)
        self.score = max(0, self.score - deduction)

    @property
    def passed(self) -> bool:
        return not any(v.severity == "critical" for v in self.violations)

    @property
    def critical_count(self) -> int:
        return sum(1 for v in self.violations if v.severity == "critical")

    @property
    def warning_count(self) -> int:
        return sum(1 for v in self.violations if v.severity == "warning")


# ── Main checker ──────────────────────────────────────────────────────────────

class HygieneChecker:
    """
    Runs all hygiene checks for a list of repos.
    Returns a list of RepoHygieneResult objects.
    """

    CHECK_NAMES = [
        "stale_branches",
        "pr_size",
        "pr_review_sla",
        "commit_message_format",
        "direct_push_to_main",
        "branch_protection",
    ]

    def __init__(self, config_path: str = "../config/settings.yaml"):
        with open(config_path) as f:
            self.cfg = yaml.safe_load(f)

        self.gh = GitHubClient(
            base_url=self.cfg["github"]["base_url"],
            token_env=self.cfg["github"]["token_env"],
        )
        self.h = self.cfg["hygiene"]
        self.org = self.cfg["github"]["org"]

    # ── Repo discovery ────────────────────────────────────────────────────────

    def discover_repos(self) -> list[tuple[str, str]]:
        """Return list of (owner, repo) tuples for all configured repos."""
        repo_cfg = self.cfg["repos"]
        if repo_cfg.get("scan_all_repos"):
            repos = self.gh.list_org_repos(
                self.org,
                include_archived=repo_cfg.get("include_archived", False)
            )
            return [(r["owner"]["login"], r["name"]) for r in repos]
        else:
            result = []
            for full_name in repo_cfg.get("explicit_list", []):
                owner, repo = full_name.split("/", 1)
                result.append((owner, repo))
            return result

    # ── Run all checks ────────────────────────────────────────────────────────

    def run_all(self, repos: list[tuple[str, str]] = None) -> list[RepoHygieneResult]:
        if repos is None:
            repos = self.discover_repos()

        results = []
        total = len(repos)
        for i, (owner, repo) in enumerate(repos, 1):
            full_name = f"{owner}/{repo}"
            logger.info(f"[{i}/{total}] Checking {full_name}...")
            try:
                result = self.check_repo(owner, repo)
                results.append(result)
                status = "✅ PASS" if result.passed else f"❌ FAIL ({result.critical_count} critical)"
                logger.info(f"  → {status} | Score: {result.score}/100")
            except Exception as e:
                logger.error(f"  → ERROR checking {full_name}: {e}")
                # Still return a result so the team is represented
                r = RepoHygieneResult(repo_full_name=full_name)
                r.add(Violation(
                    repo=full_name,
                    check="api_error",
                    severity="warning",
                    title="Could not check repo",
                    detail=str(e),
                ))
                results.append(r)

        return results

    def check_repo(self, owner: str, repo: str) -> RepoHygieneResult:
        full_name = f"{owner}/{repo}"
        result = RepoHygieneResult(repo_full_name=full_name)

        self._check_stale_branches(owner, repo, result)
        self._check_open_prs(owner, repo, result)
        self._check_branch_protection(owner, repo, result)
        self._check_direct_pushes(owner, repo, result)

        return result

    # ── Individual checks ─────────────────────────────────────────────────────

    def _check_stale_branches(self, owner: str, repo: str, result: RepoHygieneResult):
        """Flag branches that haven't had a commit in > max_branch_age_days."""
        max_age = self.h.get("max_branch_age_days", 2)
        protected = self.h.get("protected_branches", ["main", "master"])
        now = datetime.now(timezone.utc)
        cutoff = now - timedelta(days=max_age)

        branches = self.gh.list_branches(owner, repo)
        for branch in branches:
            name = branch["name"]

            # Skip protected/release branches
            if any(
                name == p or (p.endswith("*") and name.startswith(p[:-1]))
                for p in protected
            ):
                continue

            try:
                last_commit_date = self.gh.get_branch_last_commit_date(owner, repo, name)
            except Exception:
                continue

            if last_commit_date < cutoff:
                age_days = (now - last_commit_date).days
                severity = "critical" if age_days > max_age * 3 else "warning"
                result.add(Violation(
                    repo=f"{owner}/{repo}",
                    check="stale_branch",
                    severity=severity,
                    title=f"Stale branch: `{name}`",
                    detail=(
                        f"Branch '{name}' has not been updated in {age_days} days "
                        f"(limit: {max_age} days). Last commit: {last_commit_date.strftime('%Y-%m-%d')}. "
                        f"Action: merge, delete, or use a feature flag instead of a long-lived branch."
                    ),
                    url=f"https://github-enterprise/repos/{owner}/{repo}/branches",
                    metadata={"branch": name, "age_days": age_days},
                ))

    def _check_open_prs(self, owner: str, repo: str, result: RepoHygieneResult):
        """Check all open PRs for: size, review SLA, and commit message format."""
        max_lines = self.h.get("max_pr_lines_changed", 400)
        max_review_hours = self.h.get("max_pr_review_hours", 4)
        commit_pattern = re.compile(
            self.h.get("required_commit_pattern",
                       r"^(feat|fix|chore|docs|refactor|test|ci)(\(.*\))?: .{10,}")
        )
        now = datetime.now(timezone.utc)

        prs = self.gh.list_open_prs(owner, repo)
        for pr in prs:
            pr_num = pr["number"]
            pr_title = pr["title"]
            pr_url = pr["html_url"]
            author = pr["user"]["login"]
            created_at = datetime.fromisoformat(pr["created_at"].replace("Z", "+00:00"))
            age_hours = (now - created_at).total_seconds() / 3600

            # --- PR Size check ---
            files = self.gh.get_pr_files(owner, repo, pr_num)
            total_lines = sum(f.get("additions", 0) + f.get("deletions", 0) for f in files)
            if total_lines > max_lines:
                overage_pct = round((total_lines - max_lines) / max_lines * 100)
                result.add(Violation(
                    repo=f"{owner}/{repo}",
                    check="pr_size",
                    severity="critical",
                    title=f"Oversized PR #{pr_num}: {total_lines} lines changed",
                    detail=(
                        f"PR #{pr_num} '{pr_title}' by @{author} has {total_lines} lines changed "
                        f"({overage_pct}% over the {max_lines}-line limit). "
                        f"Action: split into smaller PRs, use feature flags for incomplete work."
                    ),
                    url=pr_url,
                    metadata={"pr_number": pr_num, "lines_changed": total_lines, "author": author},
                ))

            # --- Review SLA check ---
            reviews = self.gh.get_pr_reviews(owner, repo, pr_num)
            has_review = any(
                r["state"] in ("APPROVED", "CHANGES_REQUESTED", "COMMENTED")
                for r in reviews
            )
            if not has_review and age_hours > max_review_hours:
                result.add(Violation(
                    repo=f"{owner}/{repo}",
                    check="pr_review_sla",
                    severity="warning" if age_hours < max_review_hours * 3 else "critical",
                    title=f"PR #{pr_num} unreviewed for {int(age_hours)}h",
                    detail=(
                        f"PR #{pr_num} '{pr_title}' by @{author} has been open for {int(age_hours)} hours "
                        f"without any review (SLA: {max_review_hours}h). "
                        f"Action: assign a reviewer immediately."
                    ),
                    url=pr_url,
                    metadata={"pr_number": pr_num, "age_hours": round(age_hours, 1), "author": author},
                ))

            # --- Commit message format check ---
            commits = self.gh.get_pr_commits(owner, repo, pr_num)
            bad_commits = []
            for commit in commits:
                msg = commit["commit"]["message"].split("\n")[0].strip()  # first line only
                if not commit_pattern.match(msg):
                    bad_commits.append((commit["sha"][:7], msg))

            if bad_commits:
                examples = "; ".join([f"`{sha}`: {msg[:60]}" for sha, msg in bad_commits[:3]])
                result.add(Violation(
                    repo=f"{owner}/{repo}",
                    check="commit_message_format",
                    severity="warning",
                    title=f"PR #{pr_num} has {len(bad_commits)} non-compliant commit message(s)",
                    detail=(
                        f"PR #{pr_num} '{pr_title}' contains commits not following Conventional Commits format. "
                        f"Examples: {examples}. "
                        f"Required format: type(scope): description (min 10 chars). "
                        f"Types: feat | fix | chore | docs | refactor | test | ci"
                    ),
                    url=pr_url,
                    metadata={"pr_number": pr_num, "bad_commit_count": len(bad_commits)},
                ))

    def _check_branch_protection(self, owner: str, repo: str, result: RepoHygieneResult):
        """Ensure main/master has branch protection rules enabled."""
        protected = ["main", "master"]
        for branch_name in protected:
            branches = self.gh.list_branches(owner, repo)
            branch_exists = any(b["name"] == branch_name for b in branches)
            if not branch_exists:
                continue

            protection = self.gh.get_branch_protection(owner, repo, branch_name)
            if protection is None:
                result.add(Violation(
                    repo=f"{owner}/{repo}",
                    check="branch_protection",
                    severity="critical",
                    title=f"Branch `{branch_name}` has NO protection rules",
                    detail=(
                        f"The `{branch_name}` branch in {owner}/{repo} has no branch protection enabled. "
                        f"This allows direct pushes and bypasses CI. "
                        f"Action: Enable protection requiring: PR reviews, status checks, no force-push."
                    ),
                    url=f"https://github-enterprise/{owner}/{repo}/settings/branches",
                    metadata={"branch": branch_name},
                ))

    def _check_direct_pushes(self, owner: str, repo: str, result: RepoHygieneResult):
        """
        Detect recent direct commits to main/master (not via PR).
        Note: This uses the push events API which covers last ~90 events.
        """
        if not self.h.get("allow_direct_push_to_main", False):
            return

        protected = ["main", "master"]
        try:
            events = self.gh.get_push_events(owner, repo)
        except Exception:
            return

        cutoff = datetime.now(timezone.utc) - timedelta(days=7)
        for event in events:
            ref = event.get("payload", {}).get("ref", "")
            branch_name = ref.replace("refs/heads/", "")
            if branch_name not in protected:
                continue

            pushed_at = datetime.fromisoformat(
                event["created_at"].replace("Z", "+00:00")
            )
            if pushed_at < cutoff:
                continue

            actor = event.get("actor", {}).get("login", "unknown")
            commits = event.get("payload", {}).get("commits", [])

            result.add(Violation(
                repo=f"{owner}/{repo}",
                check="direct_push_to_main",
                severity="critical",
                title=f"Direct push to `{branch_name}` by @{actor}",
                detail=(
                    f"@{actor} pushed {len(commits)} commit(s) directly to `{branch_name}` "
                    f"on {pushed_at.strftime('%Y-%m-%d %H:%M UTC')} — bypassing the PR process. "
                    f"Action: Enable branch protection to prevent this. Review the pushed commits."
                ),
                url=f"https://github-enterprise/{owner}/{repo}/commits/{branch_name}",
                metadata={"actor": actor, "commit_count": len(commits), "pushed_at": pushed_at.isoformat()},
            ))


# ── CLI entry point ───────────────────────────────────────────────────────────

if __name__ == "__main__":
    import json
    import sys

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )

    checker = HygieneChecker(config_path="../config/settings.yaml")
    results = checker.run_all()

    total_violations = sum(len(r.violations) for r in results)
    total_critical = sum(r.critical_count for r in results)
    passed = sum(1 for r in results if r.passed)

    print("\n" + "="*60)
    print(f"  HYGIENE CHECK SUMMARY — {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print("="*60)
    print(f"  Repos checked    : {len(results)}")
    print(f"  Passed           : {passed}")
    print(f"  Failed           : {len(results) - passed}")
    print(f"  Total violations : {total_violations} ({total_critical} critical)")
    print("="*60)

    # Save JSON for the alerter and reporter to consume
    output = [
        {
            "repo": r.repo_full_name,
            "score": r.score,
            "passed": r.passed,
            "critical": r.critical_count,
            "warnings": r.warning_count,
            "violations": [v.as_dict() for v in r.violations],
        }
        for r in results
    ]

    with open("../reports/latest_hygiene.json", "w") as f:
        json.dump(output, f, indent=2, default=str)

    print("\n  Results saved to: reports/latest_hygiene.json")
    print("  Run alerter.py to send notifications.\n")

    sys.exit(0 if total_critical == 0 else 1)
