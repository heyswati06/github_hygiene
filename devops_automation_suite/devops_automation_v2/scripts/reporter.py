"""
reporter.py
Generates the weekly HTML leaderboard + hygiene report.
Pulls release frequency, LTFD, and hygiene scores per team.

Usage:
    python reporter.py                     # Generate weekly report
    python reporter.py --output my.html    # Custom output path
    python reporter.py --email             # Also email to all team leads
"""

import argparse
import json
import logging
import os
from datetime import datetime, timezone, timedelta
from pathlib import Path

import yaml

from github_client import GitHubClient

logger = logging.getLogger(__name__)


class WeeklyReporter:

    def __init__(self, config_path: str = "../config/settings.yaml"):
        with open(config_path) as f:
            self.cfg = yaml.safe_load(f)
        self.gh = GitHubClient(
            base_url=self.cfg["github"]["base_url"],
            token_env=self.cfg["github"]["token_env"],
        )
        self.org = self.cfg["github"]["org"]

    def collect_metrics(self, repos: list[tuple[str, str]], since: datetime) -> list[dict]:
        """Collect release frequency and LTFD for each repo over the past week."""
        metrics = []
        total = len(repos)

        for i, (owner, repo) in enumerate(repos, 1):
            full_name = f"{owner}/{repo}"
            logger.info(f"[{i}/{total}] Collecting metrics for {full_name}...")
            try:
                deployments = self.gh.list_deployments(owner, repo, since=since)
                releases = self.gh.list_releases(owner, repo)
                recent_releases = [
                    r for r in releases
                    if datetime.fromisoformat(r["created_at"].replace("Z", "+00:00")) >= since
                ]

                # Release frequency = deployments + releases in the period
                release_count = len(deployments) + len(recent_releases)

                # LTFD: median time from first commit in a PR to deployment
                # Approximated as: avg time between PR creation and merge for merged PRs
                ltfd_days = self._estimate_ltfd(owner, repo, since)

                metrics.append({
                    "repo": full_name,
                    "release_count": release_count,
                    "ltfd_days": ltfd_days,
                    "deployment_count": len(deployments),
                    "release_tag_count": len(recent_releases),
                })
            except Exception as e:
                logger.error(f"Error collecting for {full_name}: {e}")
                metrics.append({
                    "repo": full_name,
                    "release_count": 0,
                    "ltfd_days": None,
                    "error": str(e),
                })

        return metrics

    def _estimate_ltfd(self, owner: str, repo: str, since: datetime) -> float | None:
        """Estimate LTFD from PR creation → merge time for recent merged PRs."""
        try:
            import requests
            # Get recently merged PRs
            merged_prs = list(self.gh._paginate(
                f"/repos/{owner}/{repo}/pulls",
                {"state": "closed", "sort": "updated", "direction": "desc"}
            ))
            durations = []
            for pr in merged_prs[:20]:  # Check last 20 PRs
                if not pr.get("merged_at"):
                    continue
                created = datetime.fromisoformat(pr["created_at"].replace("Z", "+00:00"))
                merged = datetime.fromisoformat(pr["merged_at"].replace("Z", "+00:00"))
                if created >= since:
                    durations.append((merged - created).total_seconds() / 86400)

            if not durations:
                return None
            return round(sum(durations) / len(durations), 1)
        except Exception:
            return None

    def load_hygiene_scores(self) -> dict[str, int]:
        """Load latest hygiene scores from the checker's JSON output."""
        path = Path("../reports/latest_hygiene.json")
        if not path.exists():
            return {}
        with open(path) as f:
            data = json.load(f)
        return {item["repo"]: item["score"] for item in data}

    def build_report(self, metrics: list[dict], hygiene_scores: dict[str, int]) -> str:
        """Build the full HTML report."""
        now = datetime.now()
        week_start = (now - timedelta(days=7)).strftime("%d %b")
        week_end = now.strftime("%d %b %Y")

        # Enrich with hygiene scores and compute rank
        for m in metrics:
            m["hygiene_score"] = hygiene_scores.get(m["repo"], None)
            # Composite score for ranking: 60% release freq (normalised) + 40% hygiene
            rel_score = min(m["release_count"] / 10 * 100, 100)  # 10 releases = 100%
            hyg_score = m.get("hygiene_score") or 0
            m["composite"] = round(0.6 * rel_score + 0.4 * hyg_score)

        ranked = sorted(metrics, key=lambda x: x["composite"], reverse=True)
        total_releases = sum(m["release_count"] for m in metrics)
        avg_ltfd = [m["ltfd_days"] for m in metrics if m.get("ltfd_days") is not None]
        avg_ltfd_val = round(sum(avg_ltfd) / len(avg_ltfd), 1) if avg_ltfd else "N/A"
        avg_hygiene = [m["hygiene_score"] for m in metrics if m.get("hygiene_score") is not None]
        avg_hyg_val = round(sum(avg_hygiene) / len(avg_hygiene)) if avg_hygiene else "N/A"

        rows = ""
        for rank, m in enumerate(ranked, 1):
            medal = {1: "🥇", 2: "🥈", 3: "🥉"}.get(rank, str(rank))
            ltfd = f"{m['ltfd_days']}d" if m.get("ltfd_days") is not None else "—"
            hygiene = f"{m['hygiene_score']}/100" if m.get("hygiene_score") is not None else "—"
            trend = "↑" if m["release_count"] > 0 else "—"
            score_color = "#27ae60" if m["composite"] >= 70 else "#e67e22" if m["composite"] >= 40 else "#c0392b"
            bg = "#fffde7" if rank == 1 else "#f0f4ff" if rank == 2 else "#f5fff5" if rank == 3 else ("white" if rank % 2 else "#fafafa")

            rows += f"""
              <tr style="background:{bg};">
                <td style="padding:12px 16px; font-size:18px; text-align:center;">{medal}</td>
                <td style="padding:12px 16px; font-weight:600; color:#1F3864;">{m['repo'].split('/')[-1]}</td>
                <td style="padding:12px 16px; text-align:center; font-size:20px; font-weight:bold; color:#1F3864;">{m['release_count']}</td>
                <td style="padding:12px 16px; text-align:center; color:#555;">{ltfd}</td>
                <td style="padding:12px 16px; text-align:center; color:#555;">{hygiene}</td>
                <td style="padding:12px 16px; text-align:center;">
                  <span style="background:{score_color}; color:white; padding:3px 10px;
                               border-radius:12px; font-weight:bold; font-size:13px;">
                    {m['composite']}
                  </span>
                </td>
                <td style="padding:12px 16px; text-align:center; font-size:18px; color:#27ae60;">{trend}</td>
              </tr>"""

        html = f"""<!DOCTYPE html>
<html>
<head>
  <meta charset="UTF-8">
  <title>DevOps Weekly Report — {week_end}</title>
  <style>
    body {{ font-family: Arial, sans-serif; background: #f0f4f8; margin: 0; padding: 20px; }}
    .container {{ max-width: 1100px; margin: 0 auto; }}
    .card {{ background: white; border-radius: 8px; box-shadow: 0 2px 8px rgba(0,0,0,0.1);
             margin-bottom: 24px; overflow: hidden; }}
    table {{ width: 100%; border-collapse: collapse; }}
    th {{ background: #1F3864; color: white; padding: 12px 16px; text-align: left; font-size: 14px; }}
    th.center {{ text-align: center; }}
    tr:hover {{ background: #f0f7ff !important; }}
    .stat-grid {{ display: grid; grid-template-columns: repeat(4, 1fr); gap: 16px; padding: 24px; }}
    .stat {{ text-align: center; }}
    .stat-value {{ font-size: 36px; font-weight: bold; }}
    .stat-label {{ font-size: 13px; color: #888; margin-top: 4px; }}
    .header {{ background: #1F3864; color: white; padding: 28px 32px; }}
  </style>
</head>
<body>
<div class="container">

  <div class="card">
    <div class="header">
      <h1 style="margin:0; font-size:24px;">📊 DevOps Weekly Report</h1>
      <p style="margin:6px 0 0; color:#A9C4E0; font-size:14px;">
        Week of {week_start} – {week_end} &nbsp;|&nbsp; {len(metrics)} teams tracked
      </p>
    </div>
    <div class="stat-grid">
      <div class="stat">
        <div class="stat-value" style="color:#1F3864;">{total_releases}</div>
        <div class="stat-label">Total Releases This Week</div>
        <div style="font-size:12px; color:#c0392b; margin-top:4px;">Target: 300/month (≈75/week)</div>
      </div>
      <div class="stat">
        <div class="stat-value" style="color:{'#27ae60' if isinstance(avg_ltfd_val, float) and avg_ltfd_val <= 1.8 else '#e67e22'};">{avg_ltfd_val}{'d' if isinstance(avg_ltfd_val, float) else ''}</div>
        <div class="stat-label">Avg Lead Time to Deploy</div>
        <div style="font-size:12px; color:#c0392b; margin-top:4px;">Target: ≤1.8 days</div>
      </div>
      <div class="stat">
        <div class="stat-value" style="color:{'#27ae60' if isinstance(avg_hyg_val, int) and avg_hyg_val >= 80 else '#e67e22'};">{avg_hyg_val}</div>
        <div class="stat-label">Avg Git Hygiene Score</div>
        <div style="font-size:12px; color:#c0392b; margin-top:4px;">Target: ≥80/100</div>
      </div>
      <div class="stat">
        <div class="stat-value" style="color:#1F3864;">{sum(1 for m in metrics if m['release_count'] == 0)}</div>
        <div class="stat-label">Teams with Zero Releases</div>
        <div style="font-size:12px; color:#c0392b; margin-top:4px;">Target: 0 teams</div>
      </div>
    </div>
  </div>

  <div class="card">
    <div style="padding:20px 24px 0; font-size:18px; font-weight:bold; color:#1F3864;">🏆 Team Leaderboard</div>
    <div style="padding:8px 24px 4px; font-size:13px; color:#888;">
      Ranked by composite score: 60% release frequency + 40% hygiene score
    </div>
    <table style="margin-top:12px;">
      <thead>
        <tr>
          <th class="center" style="width:60px;">Rank</th>
          <th>Team / Repo</th>
          <th class="center">Releases</th>
          <th class="center">LTFD</th>
          <th class="center">Hygiene</th>
          <th class="center">Score</th>
          <th class="center">Trend</th>
        </tr>
      </thead>
      <tbody>{rows}</tbody>
    </table>
    <div style="padding:16px 24px; font-size:12px; color:#aaa;">
      Generated: {now.strftime('%Y-%m-%d %H:%M UTC')} &nbsp;|&nbsp;
      DevOps Automation Suite &nbsp;|&nbsp; Data sourced from GitHub Enterprise
    </div>
  </div>

</div>
</body>
</html>"""
        return html


# ── CLI entry point ────────────────────────────────────────────────────────────

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )

    parser = argparse.ArgumentParser(description="Generate weekly DevOps report")
    parser.add_argument("--config", default="../config/settings.yaml")
    parser.add_argument("--output", default="../reports/weekly_report.html")
    parser.add_argument("--days", type=int, default=7, help="Lookback period in days")
    args = parser.parse_args()

    reporter = WeeklyReporter(config_path=args.config)
    since = datetime.now(timezone.utc) - timedelta(days=args.days)

    repos = []
    repo_cfg = reporter.cfg["repos"]
    if repo_cfg.get("scan_all_repos"):
        raw = reporter.gh.list_org_repos(reporter.org)
        repos = [(r["owner"]["login"], r["name"]) for r in raw]
    else:
        for full in repo_cfg.get("explicit_list", []):
            owner, repo = full.split("/", 1)
            repos.append((owner, repo))

    print(f"Collecting metrics for {len(repos)} repos (last {args.days} days)...")
    metrics = reporter.collect_metrics(repos, since)
    hygiene_scores = reporter.load_hygiene_scores()
    html = reporter.build_report(metrics, hygiene_scores)

    Path(args.output).parent.mkdir(parents=True, exist_ok=True)
    with open(args.output, "w") as f:
        f.write(html)

    print(f"\n✅ Report saved to: {args.output}")
    total = sum(m["release_count"] for m in metrics)
    zero_teams = sum(1 for m in metrics if m["release_count"] == 0)
    print(f"   Total releases this week : {total}")
    print(f"   Teams with zero releases : {zero_teams}")
