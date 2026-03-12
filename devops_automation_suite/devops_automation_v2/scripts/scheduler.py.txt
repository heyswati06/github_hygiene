"""
scheduler.py
Runs hygiene checks + alerting on a schedule.
Can be run as a long-running process (e.g. systemd service or Docker container)
OR triggered by your existing CI/CD scheduler (Jenkins, GitLab CI, etc.).

Usage:
    python scheduler.py                   # Run continuously on schedule
    python scheduler.py --run-now hygiene # Run hygiene check immediately
    python scheduler.py --run-now report  # Run weekly report immediately
    python scheduler.py --run-now all     # Run everything right now
"""

import argparse
import logging
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import schedule
import time
import yaml

logger = logging.getLogger(__name__)

CONFIG_PATH = "../config/settings.yaml"


def run_hygiene_and_alert(dry_run: bool = False):
    """Run hygiene check then immediately send alerts."""
    logger.info("=" * 50)
    logger.info("🔍 Starting hygiene check...")
    logger.info("=" * 50)

    result = subprocess.run(
        [sys.executable, "hygiene_checker.py"],
        cwd=str(Path(__file__).parent),
        capture_output=False,
    )

    if result.returncode not in (0, 1):  # 0=pass, 1=violations found (expected)
        logger.error(f"Hygiene checker failed with code {result.returncode}")
        return

    logger.info("\n📬 Sending alerts...")
    alert_args = [sys.executable, "alerter.py"]
    if dry_run:
        alert_args.append("--dry-run")

    subprocess.run(alert_args, cwd=str(Path(__file__).parent), capture_output=False)


def run_weekly_report():
    """Generate and save weekly HTML report."""
    logger.info("=" * 50)
    logger.info("📊 Generating weekly report...")
    logger.info("=" * 50)

    now = datetime.now().strftime("%Y-%m-%d")
    output_path = f"../reports/weekly_{now}.html"

    subprocess.run(
        [sys.executable, "reporter.py", "--output", output_path],
        cwd=str(Path(__file__).parent),
        capture_output=False,
    )

    # Also update the "latest" symlink/copy
    subprocess.run(
        [sys.executable, "reporter.py", "--output", "../reports/weekly_latest.html"],
        cwd=str(Path(__file__).parent),
        capture_output=False,
    )


def setup_schedule(cfg: dict):
    """Set up cron-style schedule from config."""
    hygiene_cron = cfg["schedule"].get("hygiene_check_cron", "0 9 * * 1-5")
    report_cron = cfg["schedule"].get("weekly_report_cron", "0 8 * * 5")

    # Parse simple cron expressions (HH:MM on specific days)
    # For production, use APScheduler or celery for full cron support
    # This handles the most common patterns

    def parse_time(cron_expr: str) -> tuple[str, list[str]]:
        """Returns (HH:MM, [day_names]) from a cron expression."""
        parts = cron_expr.split()
        minute, hour = parts[0], parts[1]
        days_field = parts[4] if len(parts) > 4 else "*"
        time_str = f"{int(hour):02d}:{int(minute):02d}"

        day_map = {
            "0": "sunday", "1": "monday", "2": "tuesday",
            "3": "wednesday", "4": "thursday", "5": "friday", "6": "saturday",
            "1-5": "weekdays",
        }
        days = day_map.get(days_field, "daily")
        return time_str, days

    hygiene_time, hygiene_days = parse_time(hygiene_cron)
    report_time, report_days = parse_time(report_cron)

    # Schedule hygiene check
    if hygiene_days == "weekdays":
        for day in ["monday", "tuesday", "wednesday", "thursday", "friday"]:
            getattr(schedule.every(), day).at(hygiene_time).do(run_hygiene_and_alert)
    elif hygiene_days == "daily":
        schedule.every().day.at(hygiene_time).do(run_hygiene_and_alert)
    else:
        getattr(schedule.every(), hygiene_days).at(hygiene_time).do(run_hygiene_and_alert)

    # Schedule weekly report
    if report_days == "friday":
        schedule.every().friday.at(report_time).do(run_weekly_report)
    elif report_days == "daily":
        schedule.every().day.at(report_time).do(run_weekly_report)
    else:
        getattr(schedule.every(), report_days).at(report_time).do(run_weekly_report)

    logger.info(f"✅ Hygiene check scheduled: {hygiene_days} at {hygiene_time}")
    logger.info(f"✅ Weekly report scheduled: {report_days} at {report_time}")


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )

    parser = argparse.ArgumentParser()
    parser.add_argument("--run-now", choices=["hygiene", "report", "all"],
                        help="Run a job immediately instead of on schedule")
    parser.add_argument("--dry-run", action="store_true", help="Don't send real emails")
    args = parser.parse_args()

    with open(CONFIG_PATH) as f:
        cfg = yaml.safe_load(f)

    if args.run_now:
        if args.run_now in ("hygiene", "all"):
            run_hygiene_and_alert(dry_run=args.dry_run)
        if args.run_now in ("report", "all"):
            run_weekly_report()
        sys.exit(0)

    # Continuous scheduler mode
    setup_schedule(cfg)
    logger.info("\n🚀 Scheduler running. Press Ctrl+C to stop.\n")
    while True:
        schedule.run_pending()
        time.sleep(30)
