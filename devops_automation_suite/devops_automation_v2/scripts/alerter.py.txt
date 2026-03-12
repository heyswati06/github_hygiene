"""
alerter.py
Reads the latest hygiene check results and emails each team lead
only about THEIR repo's violations. Respects cooldown so teams
aren't spammed. CC's the DevOps champion on every alert.

Usage:
    python alerter.py                        # Process latest_hygiene.json
    python alerter.py --dry-run              # Print emails, don't send
    python alerter.py --repo owner/my-repo  # Alert for one repo only
"""

import argparse
import json
import logging
import os
import smtplib
import sqlite3
import sys
from datetime import datetime, timezone, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

import yaml

logger = logging.getLogger(__name__)

SEVERITY_EMOJI = {
    "critical": "🔴",
    "warning":  "🟡",
    "info":     "🔵",
}
SEVERITY_COLOR = {
    "critical": "#c0392b",
    "warning":  "#e67e22",
    "info":     "#2980b9",
}
CHECK_FRIENDLY = {
    "stale_branch":           "Stale Branch",
    "pr_size":                "Oversized PR",
    "pr_review_sla":          "Unreviewed PR",
    "commit_message_format":  "Commit Message Format",
    "direct_push_to_main":    "Direct Push to Main",
    "branch_protection":      "Missing Branch Protection",
    "api_error":              "API Check Error",
}


class AlertCooldownDB:
    """
    SQLite-backed cooldown tracker so we don't re-alert
    the same violation within the configured cooldown window.
    """

    def __init__(self, db_path: str = "../reports/alert_cooldown.db"):
        self.conn = sqlite3.connect(db_path)
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS alerts (
                repo TEXT,
                check_name TEXT,
                identifier TEXT,
                alerted_at TEXT,
                PRIMARY KEY (repo, check_name, identifier)
            )
        """)
        self.conn.commit()

    def was_recently_alerted(self, repo: str, check: str, identifier: str, cooldown_hours: int) -> bool:
        row = self.conn.execute(
            "SELECT alerted_at FROM alerts WHERE repo=? AND check_name=? AND identifier=?",
            (repo, check, identifier)
        ).fetchone()
        if not row:
            return False
        last = datetime.fromisoformat(row[0])
        return (datetime.now(timezone.utc) - last) < timedelta(hours=cooldown_hours)

    def mark_alerted(self, repo: str, check: str, identifier: str):
        self.conn.execute(
            """INSERT OR REPLACE INTO alerts (repo, check_name, identifier, alerted_at)
               VALUES (?, ?, ?, ?)""",
            (repo, check, identifier, datetime.now(timezone.utc).isoformat())
        )
        self.conn.commit()

    def close(self):
        self.conn.close()


class Alerter:

    def __init__(self, config_path: str = "../config/settings.yaml", dry_run: bool = False):
        with open(config_path) as f:
            self.cfg = yaml.safe_load(f)
        self.dry_run = dry_run
        self.alert_cfg = self.cfg["alerts"]["email"]
        self.cooldown_hours = self.cfg["schedule"].get("alert_cooldown_hours", 24)
        self.team_leads = self.cfg.get("team_leads", {})
        self.default_lead = self.team_leads.get("default_lead", "")
        self.champion_email = self.alert_cfg.get("cc_champion", "")
        self.cooldown_db = AlertCooldownDB()

    def get_team_lead_email(self, repo_name: str) -> str:
        """Return team lead email for a repo. Falls back to default."""
        short_name = repo_name.split("/")[-1]
        return self.team_leads.get(short_name, self.team_leads.get(repo_name, self.default_lead))

    def process_results(self, results: list[dict], target_repo: str = None):
        """Main entry — process all results and send alerts where needed."""
        stats = {"sent": 0, "skipped_cooldown": 0, "skipped_no_email": 0, "errors": 0}

        for repo_result in results:
            repo = repo_result["repo"]
            if target_repo and repo != target_repo:
                continue
            if not repo_result["violations"]:
                continue

            lead_email = self.get_team_lead_email(repo)
            if not lead_email:
                logger.warning(f"No team lead email for {repo} — skipping alert")
                stats["skipped_no_email"] += 1
                continue

            # Filter violations that are past cooldown
            new_violations = []
            for v in repo_result["violations"]:
                identifier = v.get("metadata", {}).get("branch") or \
                             str(v.get("metadata", {}).get("pr_number", "")) or \
                             v["check"]
                if not self.cooldown_db.was_recently_alerted(repo, v["check"], identifier, self.cooldown_hours):
                    new_violations.append((v, identifier))

            if not new_violations:
                logger.info(f"{repo}: all violations within cooldown window — skipped")
                stats["skipped_cooldown"] += 1
                continue

            try:
                subject, html_body = self._build_email(repo, repo_result["score"], new_violations)
                self._send_email(lead_email, subject, html_body)

                # Mark all as alerted AFTER successful send
                for v, identifier in new_violations:
                    self.cooldown_db.mark_alerted(repo, v["check"], identifier)

                logger.info(f"✅ Alert sent to {lead_email} for {repo} ({len(new_violations)} violation(s))")
                stats["sent"] += 1
            except Exception as e:
                logger.error(f"Failed to send alert for {repo}: {e}")
                stats["errors"] += 1

        self.cooldown_db.close()
        return stats

    def _build_email(self, repo: str, score: int, violations_with_ids: list) -> tuple[str, str]:
        """Build the subject line and HTML email body."""
        violations = [v for v, _ in violations_with_ids]
        critical_count = sum(1 for v in violations if v["severity"] == "critical")
        warning_count = sum(1 for v in violations if v["severity"] == "warning")

        severity_label = "🔴 ACTION REQUIRED" if critical_count > 0 else "🟡 Attention Needed"
        subject = f"{severity_label}: Git Hygiene — {repo} | Score: {score}/100"

        rows_html = ""
        for v in violations:
            emoji = SEVERITY_EMOJI.get(v["severity"], "⚪")
            color = SEVERITY_COLOR.get(v["severity"], "#555")
            check_label = CHECK_FRIENDLY.get(v["check"], v["check"])
            link = f'<a href="{v["url"]}" style="color:#2980b9;">View →</a>' if v.get("url") else ""
            rows_html += f"""
            <tr>
              <td style="padding:10px 12px; border-bottom:1px solid #eee; white-space:nowrap;">
                <span style="color:{color}; font-weight:bold;">{emoji} {v['severity'].upper()}</span>
              </td>
              <td style="padding:10px 12px; border-bottom:1px solid #eee; font-weight:600; color:#2c3e50;">
                {check_label}
              </td>
              <td style="padding:10px 12px; border-bottom:1px solid #eee; color:#555;">
                {v['title']}
              </td>
              <td style="padding:10px 12px; border-bottom:1px solid #eee; color:#555; font-size:13px;">
                {v['detail']}
              </td>
              <td style="padding:10px 12px; border-bottom:1px solid #eee;">{link}</td>
            </tr>"""

        score_color = "#27ae60" if score >= 80 else "#e67e22" if score >= 60 else "#c0392b"
        now_str = datetime.now().strftime("%A, %d %b %Y at %H:%M UTC")

        html = f"""<!DOCTYPE html>
<html>
<head><meta charset="UTF-8"></head>
<body style="font-family: Arial, sans-serif; background:#f5f6fa; margin:0; padding:20px;">
  <div style="max-width:900px; margin:0 auto; background:#fff; border-radius:8px;
              box-shadow:0 2px 8px rgba(0,0,0,0.1); overflow:hidden;">

    <!-- Header -->
    <div style="background:#1F3864; padding:28px 32px;">
      <h1 style="color:#fff; margin:0; font-size:22px;">🔍 Git Hygiene Alert</h1>
      <p style="color:#A9C4E0; margin:6px 0 0; font-size:14px;">{repo} &nbsp;|&nbsp; {now_str}</p>
    </div>

    <!-- Score banner -->
    <div style="background:#EBF3FB; padding:20px 32px; display:flex; align-items:center; gap:24px;">
      <div>
        <div style="font-size:14px; color:#555; margin-bottom:4px;">Hygiene Score</div>
        <div style="font-size:42px; font-weight:bold; color:{score_color};">{score}<span style="font-size:20px; color:#888;">/100</span></div>
      </div>
      <div style="margin-left:32px;">
        <div style="font-size:14px; color:#555; margin-bottom:4px;">Violations Found</div>
        <div>
          <span style="display:inline-block; background:#c0392b; color:#fff; border-radius:4px;
                       padding:4px 12px; font-weight:bold; margin-right:8px;">{critical_count} Critical</span>
          <span style="display:inline-block; background:#e67e22; color:#fff; border-radius:4px;
                       padding:4px 12px; font-weight:bold;">{warning_count} Warning</span>
        </div>
      </div>
    </div>

    <!-- Violations table -->
    <div style="padding:24px 32px;">
      <h2 style="font-size:16px; color:#1F3864; margin:0 0 16px;">Violations Requiring Action</h2>
      <table style="width:100%; border-collapse:collapse; font-size:14px;">
        <thead>
          <tr style="background:#f8f9fa;">
            <th style="padding:10px 12px; text-align:left; color:#555; font-weight:600; border-bottom:2px solid #ddd;">Severity</th>
            <th style="padding:10px 12px; text-align:left; color:#555; font-weight:600; border-bottom:2px solid #ddd;">Check</th>
            <th style="padding:10px 12px; text-align:left; color:#555; font-weight:600; border-bottom:2px solid #ddd;">Issue</th>
            <th style="padding:10px 12px; text-align:left; color:#555; font-weight:600; border-bottom:2px solid #ddd;">Details & Action</th>
            <th style="padding:10px 12px; text-align:left; color:#555; font-weight:600; border-bottom:2px solid #ddd;">Link</th>
          </tr>
        </thead>
        <tbody>{rows_html}</tbody>
      </table>
    </div>

    <!-- Standards reminder -->
    <div style="margin:0 32px 24px; padding:16px 20px; background:#FFF8E1;
                border-left:4px solid #F9A825; border-radius:4px;">
      <strong style="color:#5D4037;">📋 Reminder — Git Standards</strong>
      <ul style="margin:8px 0 0; padding-left:20px; color:#555; font-size:13px; line-height:1.8;">
        <li>Feature branches must be merged or deleted within <strong>2 days</strong></li>
        <li>PRs must not exceed <strong>400 lines changed</strong> — split large changes behind feature flags</li>
        <li>All PRs must receive a review within <strong>4 business hours</strong></li>
        <li>Commit messages must follow <strong>Conventional Commits</strong>: <code>type(scope): description</code></li>
        <li>No direct pushes to <code>main</code> or <code>master</code> — always use a PR</li>
      </ul>
    </div>

    <!-- Footer -->
    <div style="background:#f5f6fa; padding:16px 32px; font-size:12px; color:#888;">
      This alert was automatically generated by the DevOps Automation Suite.
      Violations recur in the next check if not resolved.
      Reply to this email or contact your DevOps Champion with questions.
      <br><br>
      <em>You are receiving this because you are the registered team lead for <strong>{repo}</strong>.</em>
    </div>
  </div>
</body>
</html>"""

        return subject, html

    def _send_email(self, to_email: str, subject: str, html_body: str):
        if self.dry_run:
            print(f"\n{'='*60}")
            print(f"DRY RUN — Would send to: {to_email}")
            print(f"Subject: {subject}")
            print(f"{'='*60}\n")
            return

        smtp_user = os.environ.get(self.alert_cfg["smtp_user_env"], "")
        smtp_pass = os.environ.get(self.alert_cfg["smtp_pass_env"], "")

        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = self.alert_cfg["from_address"]
        msg["To"] = to_email
        if self.champion_email and self.champion_email != to_email:
            msg["Cc"] = self.champion_email

        msg.attach(MIMEText(html_body, "html"))

        recipients = [to_email]
        if self.champion_email and self.champion_email != to_email:
            recipients.append(self.champion_email)

        with smtplib.SMTP(self.alert_cfg["smtp_host"], self.alert_cfg["smtp_port"]) as server:
            server.ehlo()
            server.starttls()
            if smtp_user and smtp_pass:
                server.login(smtp_user, smtp_pass)
            server.sendmail(self.alert_cfg["from_address"], recipients, msg.as_string())


# ── CLI entry point ────────────────────────────────────────────────────────────

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )

    parser = argparse.ArgumentParser(description="Send Git hygiene alerts to team leads")
    parser.add_argument("--dry-run", action="store_true", help="Print emails without sending")
    parser.add_argument("--repo", help="Only alert for a specific repo (owner/name)")
    parser.add_argument("--results", default="../reports/latest_hygiene.json",
                        help="Path to hygiene results JSON")
    parser.add_argument("--config", default="../config/settings.yaml")
    args = parser.parse_args()

    results_path = Path(args.results)
    if not results_path.exists():
        print(f"ERROR: Results file not found: {results_path}")
        print("Run hygiene_checker.py first.")
        sys.exit(1)

    with open(results_path) as f:
        results = json.load(f)

    alerter = Alerter(config_path=args.config, dry_run=args.dry_run)
    stats = alerter.process_results(results, target_repo=args.repo)

    print(f"\n📬 Alert Summary:")
    print(f"   Sent              : {stats['sent']}")
    print(f"   Skipped (cooldown): {stats['skipped_cooldown']}")
    print(f"   Skipped (no email): {stats['skipped_no_email']}")
    print(f"   Errors            : {stats['errors']}")
