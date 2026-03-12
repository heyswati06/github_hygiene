# DevOps Automation Suite
### Git Hygiene Enforcement + Auto-Alerting for GitHub Enterprise
#### 70 Teams | Release Frequency & LTFD Improvement | 2026

---

## What This Does

| Script | What it automates |
|--------|------------------|
| `hygiene_checker.py` | Scans ALL repos for violations: stale branches, oversized PRs, bad commit messages, missing branch protection, direct pushes to main |
| `alerter.py` | Emails each team lead ONLY their repo's violations — formatted, actionable HTML emails. CC's you on everything. Respects 24h cooldown so teams aren't spammed. |
| `reporter.py` | Generates a full weekly HTML leaderboard — release frequency, LTFD, hygiene score, composite rank |
| `scheduler.py` | Ties it all together on a schedule. Run as a service or trigger from Jenkins/pipeline. |

---

## Quick Start (5 minutes)

### 1. Install dependencies
```bash
pip install -r requirements.txt
```

### 2. Set environment variables
```bash
export GITHUB_TOKEN=ghp_your_personal_access_token_here
export SMTP_USER=devops-bot@company.com
export SMTP_PASS=your_smtp_password
```

**GitHub PAT needs these scopes:**
- `repo` (full repo access — needed to read branches, PRs, commits)
- `read:org` (to list org repos)
- `admin:repo_hook` (to read branch protection)

### 3. Configure settings
Edit `config/settings.yaml`:
```yaml
github:
  base_url: "https://YOUR-GHE-HOSTNAME/api/v3"   # ← your GitHub Enterprise URL
  org: "your-org-name"                             # ← your GitHub org

team_leads:
  my-repo-name: "teamlead@company.com"             # ← map each repo to its lead's email
  another-repo: "anotherlead@company.com"
  default_lead: "you@company.com"                  # ← fallback (you get everything)

alerts:
  email:
    smtp_host: "smtp.company.com"                  # ← your SMTP server
    from_address: "devops-bot@company.com"
    cc_champion: "you@company.com"                 # ← you're CC'd on everything
```

### 4. Test with a dry run (no emails sent)
```bash
cd scripts/
python hygiene_checker.py          # Run checks, save results to reports/
python alerter.py --dry-run        # Preview emails without sending
python reporter.py                 # Generate weekly HTML report
```

### 5. Run for real
```bash
python hygiene_checker.py && python alerter.py
```

---

## Running on a Schedule

### Option A: As a long-running Python process (simplest)
```bash
cd scripts/
python scheduler.py
```
Runs hygiene checks every weekday at 9am, weekly report every Friday at 8am.
Change times in `config/settings.yaml` under `schedule:`.

### Option B: Jenkins Pipeline (recommended for on-premise)
```groovy
// Jenkinsfile — add to a dedicated DevOps-Automation repo
pipeline {
    agent any
    triggers {
        cron('0 9 * * 1-5')   // Weekdays 9am — hygiene check
    }
    environment {
        GITHUB_TOKEN = credentials('github-enterprise-pat')
        SMTP_USER    = credentials('smtp-user')
        SMTP_PASS    = credentials('smtp-pass')
    }
    stages {
        stage('Hygiene Check') {
            steps {
                sh 'pip install -r requirements.txt --quiet'
                sh 'cd scripts && python hygiene_checker.py'
            }
        }
        stage('Send Alerts') {
            steps {
                sh 'cd scripts && python alerter.py'
            }
        }
        stage('Weekly Report') {
            when {
                // Only run report on Fridays
                expression { new Date().format('EEEE') == 'Friday' }
            }
            steps {
                sh 'cd scripts && python reporter.py'
                archiveArtifacts artifacts: 'reports/weekly_*.html'
            }
        }
    }
    post {
        failure {
            emailext subject: 'DevOps Automation FAILED',
                     body: 'The hygiene check pipeline failed. Check Jenkins logs.',
                     to: 'devops-champion@company.com'
        }
    }
}
```

### Option C: GitLab CI (if any teams use GitLab internally)
```yaml
# .gitlab-ci.yml
hygiene-check:
  image: python:3.11
  script:
    - pip install -r requirements.txt
    - cd scripts && python hygiene_checker.py
    - cd scripts && python alerter.py
  only:
    - schedules   # Set up a GitLab scheduled pipeline for weekday 9am
```

### Option D: Linux cron (minimal setup)
```bash
# Add to crontab: crontab -e
0 9 * * 1-5  cd /path/to/devops_automation/scripts && python hygiene_checker.py && python alerter.py
0 8 * * 5    cd /path/to/devops_automation/scripts && python reporter.py
```

---

## Email Alert Example

Team leads receive an email like this when violations are found:

```
Subject: 🔴 ACTION REQUIRED: Git Hygiene — myorg/my-repo | Score: 55/100

[Visual HTML email with:]
  - Hygiene score: 55/100 (red)
  - 2 Critical violations | 1 Warning
  - Table of violations with:
      • Stale branch `feat/old-feature` — 8 days old (limit: 2 days)
      • Oversized PR #47 — 620 lines changed (limit: 400)
      • Action required + direct link for each
  - Git standards reminder
```

You (the champion) are CC'd on every email automatically.

---

## Checks Performed

| Check | Trigger | Severity |
|-------|---------|----------|
| Stale branch | Branch not updated in >2 days | Warning (>6 days = Critical) |
| Oversized PR | PR has >400 lines changed | Critical |
| Unreviewed PR | No review after 4 hours | Warning (>12h = Critical) |
| Bad commit message | Doesn't match Conventional Commits | Warning |
| Missing branch protection | main/master has no protection rules | Critical |
| Direct push to main | Commit pushed directly without PR | Critical |

All thresholds are configurable in `config/settings.yaml`.

---

## Tuning the Checks

```yaml
hygiene:
  max_branch_age_days: 2         # Change to 3 if teams need more time initially
  max_pr_lines_changed: 400      # Lower to 200 once culture improves
  max_pr_review_hours: 4         # Your SLA
  required_commit_pattern: "^(feat|fix|chore|docs|refactor|test|ci)(\\(.*\\))?: .{10,}"
```

**Suggested ramp-up:**
- Weeks 1–4: Warnings only, educate teams
- Weeks 5–8: Critical violations block pipeline (add status check in branch protection)
- Weeks 9+: Full enforcement, score impacts leaderboard

---

## Scaling to 70 Teams

The suite handles 70 repos in a single run. Typical timing:
- With GitHub Enterprise on-prem: ~15–25 seconds per repo (API calls)
- 70 repos ≈ 20–30 minutes total (due to rate limiting)

**To speed it up**, enable parallel execution by setting in settings.yaml:
```yaml
github:
  parallel_workers: 5    # Run 5 repos simultaneously (add to config, then use ThreadPoolExecutor)
```

---

## File Structure

```
devops_automation/
├── config/
│   └── settings.yaml          # ← All configuration here
├── scripts/
│   ├── github_client.py       # GitHub Enterprise API wrapper
│   ├── hygiene_checker.py     # Core checks engine
│   ├── alerter.py             # Email alerting + cooldown
│   ├── reporter.py            # Weekly HTML leaderboard
│   └── scheduler.py           # Cron-style orchestrator
├── reports/
│   ├── latest_hygiene.json    # Output of last hygiene check
│   ├── alert_cooldown.db      # SQLite — tracks sent alerts
│   └── weekly_YYYY-MM-DD.html # Weekly reports (archived)
├── requirements.txt
└── README.md
```

---

## Troubleshooting

**"GitHub token not found"**
→ `export GITHUB_TOKEN=your_token` before running

**"No team lead email for repo X"**
→ Add the repo to `team_leads:` in settings.yaml, or set `default_lead:`

**API 403 errors**
→ Your PAT may be missing the `repo` or `admin:repo_hook` scope

**Emails not sending**
→ Test SMTP: `python -c "import smtplib; s=smtplib.SMTP('smtp.company.com',587); s.ehlo(); print('OK')"`

**Slow runs (>60 min for 70 repos)**
→ You're being rate-limited. Add `time.sleep(1)` between repo calls, or request a higher rate limit from your GHE admin.
