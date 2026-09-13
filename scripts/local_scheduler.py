"""Local replacement for GitHub Actions' `schedule:` trigger.

GitHub's own cron scheduler was observed missing scheduled runs by 15-20+
minutes or more during the Week 1 opener (see CLAUDE.md), with nothing
diagnosable from outside GitHub. This script is meant to be run on a plain
Windows Task Scheduler timer every 5 minutes, always, and calls
`gh workflow run` directly instead of relying on GitHub's scheduler. It's a
no-op outside game windows, so running it unconditionally every 5 minutes is
intentional and safe — `gh` decides nothing here, this script does.

The window logic mirrors what used to live in the `schedule:` block of
.github/workflows/scoreboard.yml (kept there only as comments now, for
reference). Same caveat carries over: the season-specific Friday/Saturday
DATE_WINDOWS below have no year field and will need reviewing before the
2027 season.
"""

import datetime
import pathlib
import subprocess
import sys

REPO = "bhu248/race-for-wts"
WORKFLOW = "scoreboard.yml"
LOG_PATH = pathlib.Path(__file__).resolve().parent.parent / "local_scheduler.log"

# (weekday, hour_start, hour_end) — Python .weekday(): Mon=0 ... Sun=6, UTC hours, inclusive.
WEEKLY_WINDOWS = [
    (6, 17, 23),  # Sunday day + evening slate
    (0, 0, 4),    # Sunday Night Football, after midnight UTC (UTC Monday)
    (1, 0, 4),    # Monday Night Football (UTC Tuesday)
    (3, 0, 4),    # Wednesday night game (UTC Thursday) - kept in case of a future Wednesday game
    (4, 0, 4),    # Thursday Night Football (UTC Friday)
]

# 2026-season-specific Friday/Saturday dates (month, day, hour_start, hour_end, UTC).
# No year field - review/remove before the 2027 season, same as the old cron.
DATE_WINDOWS = [
    (11, 27, 20, 23),  # Black Friday: Broncos @ Steelers
    (11, 28, 0, 1),    #   ...overflow past midnight UTC
    (12, 25, 18, 23),  # Christmas Day tripleheader
    (12, 26, 0, 4),    #   ...overflow past midnight UTC
    (12, 19, 22, 23),  # Week 15 Saturday doubleheader
    (12, 20, 0, 4),    #   ...overflow past midnight UTC
    (12, 26, 21, 23),  # Week 16 Saturday doubleheader
    (12, 27, 0, 4),    #   ...overflow past midnight UTC
    (1, 2, 21, 23),    # Week 17 Saturday doubleheader
    (1, 3, 0, 4),      #   ...overflow past midnight UTC
    (1, 9, 18, 23),    # Week 18 Saturday tripleheader (tentative)
    (1, 10, 0, 4),     #   ...overflow past midnight UTC
]


def in_game_window(now: datetime.datetime) -> bool:
    for weekday, h_start, h_end in WEEKLY_WINDOWS:
        if now.weekday() == weekday and h_start <= now.hour <= h_end:
            return True
    for month, day, h_start, h_end in DATE_WINDOWS:
        if now.month == month and now.day == day and h_start <= now.hour <= h_end:
            return True
    return False


def log(message: str) -> None:
    print(message)
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write(message + "\n")


def main() -> int:
    now = datetime.datetime.now(datetime.timezone.utc)
    stamp = now.strftime("%Y-%m-%dT%H:%M:%SZ")

    if not in_game_window(now):
        log(f"{stamp} outside game window, skipping")
        return 0

    log(f"{stamp} in game window, dispatching {WORKFLOW}")
    result = subprocess.run(
        ["gh", "workflow", "run", WORKFLOW, "--repo", REPO],
        capture_output=True,
        text=True,
    )
    if result.stdout.strip():
        log(result.stdout.strip())
    if result.returncode != 0:
        log(f"{stamp} gh workflow run failed (exit {result.returncode}): {result.stderr.strip()}")
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
