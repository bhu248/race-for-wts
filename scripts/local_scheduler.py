"""Local replacement for GitHub Actions' `schedule:` trigger.

GitHub's own cron scheduler was observed missing scheduled runs by 15-20+
minutes or more during the Week 1 opener (see CLAUDE.md), with nothing
diagnosable from outside GitHub. This script is meant to be run on a plain
Windows Task Scheduler timer every 3 minutes, always, and calls
`gh workflow run` directly instead of relying on GitHub's scheduler. It's a
no-op most ticks (either outside game windows, or inside one but not yet
due for a dispatch at the current cadence — see DENSE_INTERVAL_MIN /
SPARSE_INTERVAL_MIN below), so running it unconditionally every 3 minutes
is intentional and safe — `gh` decides nothing here, this script does.

CHANGED 2026-09-14: this script now decides its OWN dispatch cadence
rather than dispatching on every tick: DENSE_INTERVAL_MIN (3) when 2+ NFL
games are live across the whole scoreboard (a full concurrent slate
justifies tighter polling), SPARSE_INTERVAL_MIN (6) when only one game
(or zero, though that shouldn't happen inside a real window) is live --
e.g. the tail end of a Sunday afternoon down to a single late game, or a
standalone Thursday/Sunday/Monday night game with nothing else on. The
OS-level tick briefly moved to every 1 minute (the GCD of 3 and an
originally-requested 5) so both cadences would land on their exact
target instead of rounding up to some multiple of a coarser tick; once
the sparse target changed to 6 (a clean multiple of 3), the GCD went
back to 3 and the tick moved back down to match — no precision lost, and
3x fewer wake-ups than the 1-minute tick needed. If SPARSE_INTERVAL_MIN
is ever changed again to something that ISN'T a multiple of
DENSE_INTERVAL_MIN, the OS tick needs to shrink back to their GCD, same
as it did the first time. Actual dispatches (and therefore actual
Sleeper polls / git commits) still only happen at the 3- or 6-minute
cadence -- this doesn't poll Sleeper any more often, it just wakes up
enough to CHECK whether it's time to.

Needs LAST_DISPATCH_PATH (gitignored, next to local_scheduler.log) to
remember when it last actually dispatched across separate invocations --
each run is a fresh process, nothing persists in memory between ticks.

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

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import common  # noqa: E402

REPO = "bhu248/race-for-wts"
WORKFLOW = "scoreboard.yml"
LOG_PATH = pathlib.Path(__file__).resolve().parent.parent / "local_scheduler.log"
LAST_DISPATCH_PATH = pathlib.Path(__file__).resolve().parent.parent / "local_scheduler_last_dispatch.txt"

DENSE_INTERVAL_MIN = 3
SPARSE_INTERVAL_MIN = 6
DENSE_LIVE_GAME_THRESHOLD = 2  # 2+ concurrent live games counts as "dense"

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


def load_last_dispatch():
    """Timestamp of the last successful dispatch, or None if there hasn't been one (yet, or ever)."""
    try:
        text = LAST_DISPATCH_PATH.read_text(encoding="utf-8").strip()
        return datetime.datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=datetime.timezone.utc)
    except (FileNotFoundError, ValueError):
        return None


def save_last_dispatch(stamp: str) -> None:
    LAST_DISPATCH_PATH.write_text(stamp, encoding="utf-8")


def main() -> int:
    now = datetime.datetime.now(datetime.timezone.utc)
    stamp = now.strftime("%Y-%m-%dT%H:%M:%SZ")

    if not in_game_window(now):
        log(f"{stamp} outside game window, skipping")
        return 0

    # How many real NFL games are concurrently live right now decides
    # whether this is a "dense" or "sparse" moment -- a fetch/parse
    # failure defaults to dense (the tighter cadence) rather than risking
    # under-polling on bad information.
    try:
        sleeper_state = common.get_state()
        season = sleeper_state["season"]
        week = sleeper_state.get("display_week") or sleeper_state["week"]
        season_type = sleeper_state["season_type"]
        live_games = common.count_live_games(season, week, season_type)
        dense = live_games >= DENSE_LIVE_GAME_THRESHOLD
        density_desc = f"{live_games} live game(s) -> {'dense' if dense else 'sparse'}"
    except Exception as exc:  # noqa: BLE001 - a flaky density check should never block dispatching
        dense = True
        density_desc = f"density check failed, defaulting dense ({exc})"

    interval_min = DENSE_INTERVAL_MIN if dense else SPARSE_INTERVAL_MIN

    last_dispatch = load_last_dispatch()
    if last_dispatch is not None:
        elapsed_min = (now - last_dispatch).total_seconds() / 60.0
        if elapsed_min < interval_min - 0.5:  # small tolerance against tick-timing jitter
            log(f"{stamp} in game window but only {elapsed_min:.1f}m since last dispatch "
                f"(need {interval_min}m, {density_desc}) — skipping")
            return 0

    log(f"{stamp} in game window, dispatching {WORKFLOW} ({density_desc}, {interval_min}m cadence)")
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

    save_last_dispatch(stamp)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
