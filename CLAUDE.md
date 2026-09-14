# Sunday Scoreboard — project notes for Claude Code

This file exists to hand off context from an earlier Claude session (Cowork)
to Claude Code. Read this before making changes — several bugs have already
been found and fixed here, and it's easy to accidentally re-introduce one of
them if you reason about the scoring model from scratch.

## What this is

A zero-secrets GitHub Actions + GitHub Pages system. Every few minutes during
NFL game windows, `scripts/poll.py` hits Sleeper's public, unauthenticated API
for league `1393377829990727680`, appends one timestamped snapshot to
`data/week<N>.jsonl`, and `scripts/render.py` rebuilds `docs/week<N>.html` — a
live dual-bar (actual + live-projected) time-lapse race chart — from the full
snapshot history. GitHub Pages serves `docs/`. No API keys, no secrets, no
auth anywhere in this project — keep it that way.

## File layout

- `scripts/common.py` — all Sleeper API calls, plus `team_game_progress()`
  (the one non-Sleeper external call, to ESPN's public scoreboard — see
  below), `score_stats()` (generic dot-product scorer using the league's own
  `scoring_settings`), and the players cache (`load_players_cache()` for
  display labels, `load_player_teams()` for each player's current team).
- `scripts/poll.py` — one poll → one snapshot. `compute_projected_total()` is
  the scoring model; read its docstring in full before touching it.
- `scripts/render.py` — rebuilds the HTML chart from the full `.jsonl`
  history: time-lapse bars, the 5 biggest plays, the week-winning-play
  marker, chronological slate labels ("Thursday Night Football" etc.),
  click-to-jump on markers/legend.
- `scripts/seed_dummy_data.py` — generates fake week-0 demo data with fake
  player-id labels like `"WR1 — deep TD catch"`. Dummy-data-only; if you ever
  see a garbled name like that in real output, it means dummy data leaked in,
  not a labeling bug.
- `.github/workflows/scoreboard.yml` — now `workflow_dispatch`-only. It used
  to also have a `schedule:` cron trigger; that was removed 2026-09-10 (see
  "Scheduling moved off GitHub, onto the local machine" below) because
  GitHub's scheduler was unreliable in production. The old cron windows are
  kept as comments in the YAML for reference only — they are not live.
- `scripts/local_scheduler.py` — the actual timer now. Ticks every 1
  minute via a Windows Task Scheduler job (`SundayScoreboardLocalTrigger`)
  on bhu24's machine, checks `WEEKLY_WINDOWS`/`DATE_WINDOWS` (the local
  equivalent of the old cron list) against current UTC time, and — if
  inside a window and due per its own adaptive cadence (3min dense / 5min
  sparse, see "Adaptive polling cadence" below) — calls
  `gh workflow run scoreboard.yml --repo bhu248/race-for-wts`. No-op
  outside game windows or between due dispatches. Logs every decision
  (dispatched or skipped) to `local_scheduler.log` in the repo root
  (gitignored). The 2026-specific Friday/Saturday `DATE_WINDOWS` have no
  year field, so — same caveat as the old cron — review/remove that block
  before the 2027 season.
- `selftest.py` (repo root) — **not committed to the repo**, dev-only. Mocks
  every Sleeper call and runs `poll.py` + `render.py` against fake data,
  asserting exact expected numbers. Run this before shipping any change to
  `compute_projected_total` or the render logic. It has caught every
  regression described below.

## The scoring model — read this before touching `compute_projected_total`

Four different bugs have been found and fixed here, each one a plausible-
looking mistake:

1. **The kickoff-cliff bug (original).** A player contributed their full
   pre-game projection only while their actual points were exactly `0.0`;
   the instant they recorded ANY stat, their contribution dropped to just
   their actual, zeroing the rest of their projection. This made every
   team's "projected" total crater toward "actual" right at kickoff —
   backwards, since a player who just scored is usually still early in their
   game.

2. **The uniform wall-clock-decay bug.** While investigating a later issue,
   a retroactive "fix" decayed every roster's projection using a single
   assumed kickoff-time + fixed game-duration, applied to ALL rosters
   uniformly — including rosters whose players had recorded zero actual
   points. Result: scoreless teams' projections cratered too, just because
   time had passed, with no in-game justification. **Fix:** decay must be
   gated on the player having a real, nonzero actual — see the gate in
   `compute_projected_total` (the `if pts != 0.0: ... else: elapsed = 0.0`
   block). A player who hasn't scored yet keeps their full, undecayed
   projection no matter how much wall-clock time passes; "hasn't scored"
   isn't evidence their opportunity is used up, it's just as likely their
   game hasn't gotten to them yet.

3. **The two-formulas-in-one-timeline bug.** The retroactive backfill from
   bug #2 used a fabricated wall-clock timer; live polls use a DIFFERENT
   formula based on `common.team_game_progress()` (ESPN's real scoreboard
   clock). These disagreed more and more as time passed, so the chart showed
   values sliding down through the middle of a game and then snapping back
   up near pregame levels the moment a fresh live poll landed — a visible
   "dip in the middle of the time-lapse." **Fix:** there must be exactly ONE
   formula for "projected," ever: `max(actual, pregame_projection)` per
   player when `team_game_progress` returns no real clock data (elapsed=0),
   decaying smoothly toward actual as elapsed approaches 1.0 when it does.
   Never retroactively rewrite history with a different formula than the one
   live polls are currently using — reconstruct historical "projected"
   values using the SAME formula live polls use, with real per-player
   pre-game projections (fetch each one individually from
   `https://api.sleeper.app/projections/nfl/player/<id>?season=...&week=...`
   — the bulk `/v1/projections/nfl/...` endpoint is too large for reliable
   single-key lookups by an LLM; the per-player endpoint is small and exact).

4. **The negative-actual gate bug (2026-09-11).** Bug #2's fix gated decay
   on `actual > 0`, which is wrong: a team DEF/ST slot can have a
   NEGATIVE actual (this league's `pts_allow` is a per-point penalty, so a
   defense that gets torched nets negative) even once its game is
   completely over. `> 0` treated that exactly like "hasn't played yet,"
   pinning a finished, leaky defense at its full pregame projection
   instead of its real (negative) final score. Confirmed against
   production Week 1 data: Team Mehta's Rams DEF had actual -2.9 with the
   SF@LAR game already `STATUS_FINAL`, but projected still showed the
   full +4.78 pregame number — a user-reported ~10-point gap against
   Sleeper's own displayed total (93.10 vs. our 103.53) that led straight
   to this. **Fix:** gate on `actual != 0` instead — see
   `compute_projected_total`'s `if pts != 0.0:` block, and the matching
   fix in `common.estimate_scoring_fallback_progress`'s `fold_in()` (was
   `if not pts or pts <= 0.0: continue`, now `if pts is None or pts ==
   0.0: continue`, for the same reason: a negative first stat is just as
   much proof of kickoff as a positive one). Regression test:
   `test_negative_actual_decay()` in `selftest.py`. Two historical rows in
   `data/week1.jsonl` (roster 1's SEA DEF at one frame, roster 12's LAR
   DEF at two frames) had already been written with the buggy formula and
   were retroactively recomputed by hand to match what the fixed live
   poller would have produced at those timestamps — same "reconstruct
   history with the SAME formula, never a different one" principle as bug
   #3. If you ever need to do this again: check `players_points` for ANY
   negative value across `data/week<N>.jsonl` — `pts < 0.0`, not
   `<= 0.0` — since that's the only condition the old gate got wrong.

5. **The stale-read floor trap (2026-09-13).** Sleeper's live matchup
   endpoint occasionally serves a transiently stale read (a lagging
   replica during high-traffic windows): a player's `players_points` value
   briefly drops below what an earlier poll already recorded, then
   self-corrects a poll or two later. Confirmed in production: Jahmyr
   Gibbs (roster 14, pid 9221) read 12.3 -> 6.2 -> 12.3 across three
   consecutive 3-minute polls, with several other players across several
   other rosters dropping the same minute. Effect: that frame's real
   points briefly regressed on the bars, AND — this is what actually
   surfaced it — the pop-up "flash" for that frame vanished entirely,
   since the flash logic only fires on a point *increase*, never a
   decrease.

   **First attempt at a fix (reverted, do NOT redo this):** floor every
   player's `players_points` at poll time in `poll.py` at whatever was
   already recorded for them this week — "a real player's points only
   ever go up." This is WRONG and was caught before shipping to the full
   season: also confirmed in production, Blake Corum (roster 1, pid
   11586) read `...29.3 -> 31.2 -> 29.4...` on 2026-09-10 and never came
   back anywhere near 31.2 for the rest of that game or the rest of the
   week. Querying Sleeper's own current (long-since-final) week 1 stats
   for him returns 5.4 points — i.e. the LOWER ~29.4-ish reading was
   closer to correct, and 31.2 was itself the bad read (a spurious
   *upward* spike), not the other way around. A "never decrease" floor
   applied forever would have permanently overstated that roster's score
   by ~1.8 points for the rest of the season — a real fantasy scoreboard
   silently lying about who's winning is a much worse failure than one
   frame missing a pop-up. **The general lesson: a value moving backward
   is not proof it's wrong. Which direction is "the glitch" can only be
   told by what happens NEXT, not by assuming stats are monotonic.**

   **The actual fix:** lives in `render.py`'s `build_frames`
   (`_smooth_regressions`, `STALE_READ_LOOKAHEAD = 2`), not in `poll.py`,
   and never rewrites `data/week<N>.jsonl` — the stored file always stays
   exactly what Sleeper reported. At render time (which always has the
   benefit of hindsight over the full already-stored history), a drop is
   only smoothed over if one of the next `STALE_READ_LOOKAHEAD` readings
   recovers back to at least the pre-drop level — that's the signature of
   a transient stale read. A drop that never recovers within that window
   is accepted as the new real value, and comparisons going forward
   measure against it, not the old (apparently wrong) peak. A live poll
   can't apply this — it can't see "the next poll or two" before they
   happen — so the newest frame in any given render may briefly show an
   unsmoothed dip until the next poll's data lets this function look back
   far enough to correct it retroactively; this is expected, and is
   exactly the ~3-6 minute self-heal window observed in production. DEF/ST
   slots (`players_team.get(pid, pid) == pid`) are exempt from smoothing
   entirely, same reasoning as bug #4: their `pts_allow` penalty
   legitimately moves in either direction. Regression test:
   `test_regression_smoothing()` in `selftest.py`, which encodes both the
   Gibbs case (must smooth) and the Corum case (must NOT smooth) so
   neither failure mode can silently come back.

6. **The pregame-ceiling ceiling (2026-09-13).** Not a bug in the sense of
   1-5 above — the old formula was doing exactly what it was designed to
   do — but a deliberate, user-requested methodology change worth
   documenting with the same rigor, since it touches the same function.
   `remaining` used to be `max(pregame_projection - actual, 0) * (1 -
   elapsed)`: once a player's actual EXCEEDED their full pregame
   projection, `max(...)` hit zero and stayed there, so the model
   credited them with ZERO further upside for the rest of their game, no
   matter how much time was left. Confirmed in production (Team
   lynnbear, week 1): Trevor Lawrence, Derrick Henry, and Parker
   Washington had each already exceeded their full pregame projection
   with roughly half their game still to play, and the roster's
   projected total (120.18) barely moved past its actual (87.34) despite
   three starters clearly having big days — user-reported as "her
   players are doing very well" but the total not reflecting it.
   **Change:** `remaining = max(pregame_projection, 0) * (1 - elapsed) **
   2` — no longer subtracts `actual` at all, so scoring more always
   raises the projected total by exactly that much, with no ceiling to
   run into. The `** 2` (not linear `(1 - elapsed)`) is deliberate: it's
   an extra discount against trusting a still-small, possibly-noisy
   sample as durable for the rest of the game, so residual upside decays
   faster than time alone would suggest as elapsed grows, while barely
   discounted (~1) right after kickoff. This isn't a special case needing
   its own branch: at `pts == 0.0` (elapsed forced to 0 by bug #2's gate),
   it reduces to exactly `pregame_projection`, identical to the existing
   scoreless-player behavior. Verified against Lynn's real live roster
   before shipping: old formula gave 120.18, new formula gives ~133.9,
   matching her own expectation (~136) far better than either the old
   number or a naive `actual/elapsed` pace-extrapolation would (which
   overshoots wildly — computed ~200 for the same roster — since it has
   no discount for small-sample noise and is numerically unstable near
   elapsed=0). Regression test: `test_overperformer_upside()` in
   `selftest.py`. Not retroactively applied to already-stored `projected`
   values in `data/week<N>.jsonl` — only future polls use the new
   formula, so expect a one-time step up in projected totals once this
   ships, which is a normal formula-version bump, not a repeat of bug
   #3's disagreeing-live-formulas flapping (there is still only ONE
   formula live at any given moment).

7. **The scoreless-player progress gate (2026-09-13).** `elapsed` used to
   only ever apply to a player once THEY personally recorded a nonzero
   stat — see bug #2's "hasn't scored yet" reasoning. That reasoning only
   holds when we don't know whether their game has started. Confirmed in
   production it can be flatly wrong once we DO know: ESPN reported
   Colston Loveland's (TE, roster lynnbear) Bears game at 98% elapsed
   while he personally sat at 0 actual the entire game, but his projected
   total stayed frozen at his full 11.21 pregame number regardless —
   user-reported as obviously too high for a game that's basically over.
   **Fix:** removed the `if pts != 0.0` gate entirely; `elapsed` now comes
   straight from `team_progress.get(team)` for every starter, scoring or
   not. This does NOT reintroduce bug #2 (a generic wall-clock timer
   decaying teams that hadn't started): `team_progress` is already
   properly gated at its source — `common.team_game_progress` only
   reports real elapsed for a game ESPN is actually tracking, and
   `estimate_scoring_fallback_progress` only fills in a team ESPN is
   silently treating as not-yet-started once at least one of THAT team's
   players (anywhere in the league) has posted a real stat. A team with
   genuinely zero signal either way still resolves to elapsed=0.0 for
   every one of its players no matter how much wall-clock time passes.
   What changed is narrow: once a team's elapsed IS known, it now applies
   to that team's quiet players too, not just its scorers. Regression
   test: `test_scoreless_player_uses_known_team_progress()` in
   `selftest.py`, which encodes both halves (decay when team progress is
   known; stay pinned when it genuinely isn't) so neither direction can
   silently regress.

## ESPN dependency — currently dormant, not broken

`common.team_game_progress()` reads ESPN's public, unauthenticated scoreboard
(`site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard`) to get each
game's live quarter/clock, because **Sleeper's own projections endpoint is
confirmed static** — it returns one unchanging pre-game number for the whole
week, verified by both the lack of any live-projection endpoint in Sleeper's
docs and by this project's own production data (a roster with zero scoring
players held an exactly unchanged projected total for over an hour of live
play). ESPN's clock is how "live" decay actually happens.

As of this hand-off, ESPN has **never once** reported the league's real Week
1 game (NE @ SEA, 2026-09-09) as anything but `STATUS_SCHEDULED`, checked
repeatedly over several hours including well after its actual kickoff time.
This means `team_game_progress()` has been returning empty/zero progress for
this entire game, so the decay feature has been sitting dormant — every
player's projection just holds at `max(actual, pregame_projection)` with no
time-decay, which is correct, safe, degraded behavior, not a bug. Don't
"fix" this without first re-checking ESPN's live status — the code is
working as designed for a game ESPN hasn't started reporting on.

**Update 2026-09-10: this is no longer purely dormant.**
`common.estimate_scoring_fallback_progress()` now fills the gap for exactly
this scenario: when a team's players are already posting real Sleeper stats
but ESPN still has that team at elapsed=0 (missing or `STATUS_SCHEDULED`),
it estimates elapsed from `(now - that team's first observed nonzero-score
snapshot) / FALLBACK_GAME_DURATION_SEC` (a fixed 3.5h stand-in for a game's
length), reading the fallback timestamp from this week's own
`data/week<N>.jsonl` history. ESPN's real clock is still trusted the moment
it actually reports one (frac > 0) for a team — the fallback only fires for
teams ESPN is silently treating as not-yet-started. `poll.py` blends this in
right after calling `team_game_progress()`, before `compute_projected_total`
ever sees it, so the existing `pts > 0.0` gate (see bug #2 above) still
applies exactly the same way regardless of which source produced `elapsed`.
Known tradeoff: because the fallback is a fixed-duration guess rather than a
real clock, there can be a one-time jump in a team's projected total at the
moment ESPN's status finally catches up and takes back over. See
`test_scoring_fallback()` in `selftest.py` for the regression coverage.

## Other known behaviors (not bugs)

- **The first ~90 minutes of Week 1's data (2026-09-09T23:38:43Z through
  2026-09-10T01:49:45Z) predate the ESPN-dormant fallback mechanism
  existing at all** (it was added at commit `62440f0`, after these frames
  had already been polled and stored). Live polls during that window had
  no way to decay a scoring player's projection — the code before that
  commit had nothing but `team_game_progress()`, which returns nothing
  useful for a game ESPN keeps reporting as `STATUS_SCHEDULED` — so the
  six rosters with a player in the NE@SEA opener sat frozen at their full
  pregame total for that entire window despite real actual points already
  climbing, then jumped straight to a properly-decaying number the moment
  the next poll ran with the newer code. Fixed 2026-09-11 by retroactively
  recomputing those specific (roster, player, frame) rows — 58 in total —
  using `common.estimate_scoring_fallback_progress` exactly as production
  would have if it had existed from kickoff, so the time-lapse now decays
  smoothly from the very start instead of freezing then snapping. If you
  ever add a new decay mechanism, remember historical rows polled before
  its commit landed will need the same treatment — check for any
  actual-is-moving-but-projected-isn't frame range, not just the specific
  symptom you were chasing.
- **Sleeper's pregame projections can drift slightly over time**, even
  before a player takes the field — a player's own projection number was
  observed shifting by a couple of points between polls taken hours apart,
  presumably from upstream injury/inactive-list updates. This is legitimate
  and expected; don't assume a small shift on a scoreless player's number is
  a sign of a decay bug resurfacing — check whether it's actually the
  player's source projection that moved.
- **Scheduling moved off GitHub, onto the local machine (2026-09-10).**
  Scheduled (`schedule:`) GitHub Actions triggers were never confirmed to
  fire reliably — manual `workflow_dispatch` runs always worked, but
  scheduled runs went missing for 15-20+ minutes or more at a time during
  the Week 1 opener (confirmed again right at the start of the Thursday
  Night Football window: no scheduled run fired for 19+ hours across a gap
  that should have had none, since the previous window had already closed
  cleanly), with nothing diagnosable from outside GitHub (workflow wasn't
  disabled, default branch was correct, no GitHub status incident, YAML/cron
  was valid). Root cause was never found and, per bhu24, isn't worth chasing
  further — instead, the `schedule:` trigger was removed entirely and
  replaced with `scripts/local_scheduler.py`, driven by a Windows Task
  Scheduler job on bhu24's own machine, which calls `gh workflow run`
  (`workflow_dispatch`) directly during game windows — see the Windows
  Task Scheduler job's trigger `Repetition.Interval` for the OS-level
  tick (3 minutes as of 2026-09-14 — briefly 1 minute for a few hours
  that same day, before the sparse cadence below settled on a
  multiple-of-3 value; up from 3 minutes on 2026-09-13, up from 5 minutes
  originally — not a value in this repo's code), but `local_scheduler.py`
  itself decides on top of that whether a given tick actually dispatches:
  3 minutes when 2+ NFL games are concurrently live, 6 minutes when only
  one (or zero) is, per `common.count_live_games` — see the "adaptive
  polling cadence" note below. This has an obvious tradeoff worth surfacing if it comes up: the workflow now
  only fires while that machine is on, awake, and logged in — it's no
  longer a GitHub-side schedule. If snapshots are missing during a game,
  check the scheduled task's state/log first (see README "How it runs")
  before assuming it's a Sleeper/ESPN data issue.
- **`docs/week<N>.html` only reflects what's in `data/week<N>.jsonl` as of
  the last render.** After any direct edit to the `.jsonl` history, you must
  run `python scripts/render.py` (or trigger the workflow) to regenerate the
  HTML — the committed data and the committed page can drift out of sync
  otherwise.
- **Multiple fantasy rosters can share the same real NFL game.** Don't
  assume different roster IDs means different real games — in this league's
  Week 1 data, six different fantasy rosters all had a player in the same
  single NE@SEA game.
- **Adaptive polling cadence (2026-09-14).** User-requested: poll every 3
  minutes when a full slate is live, every 6 when it's down to (at most)
  one game, rather than one fixed interval all game long. (Originally
  requested as 5 minutes; changed to 6 shortly after shipping.)
  `local_scheduler.py`'s Windows Task Scheduler trigger ticks every 3
  minutes — briefly 1 minute right after this first shipped, since 3 and
  the original 5-minute sparse target only share a GCD of 1, but once the
  sparse target changed to 6 (a clean multiple of 3) the tick moved back
  down to 3 with no precision lost. The script itself decides whether a
  given tick actually dispatches, using `common.count_live_games()` (2+
  concurrent live games anywhere on the real NFL scoreboard = dense/3min,
  else sparse/6min) and a small gitignored state file,
  `local_scheduler_last_dispatch.txt`, to remember
  when it last actually dispatched across separate process invocations
  (each tick is a fresh `python` process — nothing persists in memory
  between them). This does NOT poll Sleeper any more often than before;
  it only wakes up more often to check whether it's time to. A
  live-game-count fetch failure defaults to dense (3min) rather than
  risking under-polling on bad information, same fail-safe posture as
  `team_game_progress`'s empty-dict fallback.

## Workflow for any change to the scoring/render logic

1. Make the change.
2. Run `python3 selftest.py` from the repo root — it must print
   `ALL SELFTESTS PASSED`. Extend its fake-data scenarios to cover whatever
   you just changed before considering it verified (see the scoreless-roster
   test case already in there as a template).
3. Only then commit/push.
