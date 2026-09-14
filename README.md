# Race for Weekly Top Scorer

A live, week-by-week time-lapse of your Sleeper league's scoring, rebuilt from
real API snapshots every few minutes during game windows. Built for league
`1393377829990727680`.

Each bar shows two things: the solid portion is actual points scored so far,
and the pale extension past it is that roster's live-projected final (actual
+ each remaining starter's pre-game projection, scored with *your* league's
own custom scoring settings — not Sleeper's generic PPR/half-PPR totals).

No API key, no secrets, and no server to keep running — it's a GitHub Actions
schedule and a static page.

## One-time setup

1. **Create the repo.** On github.com, click "New repository," name it
   whatever you like (e.g. `race-for-wts`), and create it empty — don't
   add a README or .gitignore there, this folder already has one.

2. **Push this folder to it.** From inside this folder:

   ```
   git init
   git add .
   git commit -m "Race for Weekly Top Scorer"
   git branch -M main
   git remote add origin https://github.com/bhu248/<your-repo-name>.git
   git push -u origin main
   ```

3. **Let the workflow write back to the repo.** On GitHub: your repo →
   Settings → Actions → General → scroll to "Workflow permissions" → select
   "Read and write permissions" → Save. Without this, the scheduled job can
   poll but can't commit the results.

4. **Turn on GitHub Pages.** Your repo → Settings → Pages → under "Build and
   deployment," set Source to "Deploy from a branch," branch `main`, folder
   `/docs` → Save. Your scoreboard will live at:

   ```
   https://bhu248.github.io/<your-repo-name>/
   ```

   It'll say "No weeks played yet" until the first snapshot lands.

That's it — no secrets to add. Sleeper's API is public and read-only, and the
workflow's built-in `GITHUB_TOKEN` is enough to commit its own results.

## Testing before Week 1

Three ways to check it's wired up correctly without waiting for a real game,
from quickest to most realistic:

- **Logic-only, no network:** `pip install -r requirements.txt && python
  selftest.py` runs the actual scoring and rendering code against fake data
  and asserts the output is correct. Safe to run anytime — it works in a
  throwaway temp directory and never touches your real `data/` or `docs/`.

- **See the real page, with dummy data, entirely from the browser:** repo →
  Actions tab → "Sunday Scoreboard" → "Run workflow" → check the "Seed fake
  week-0 data" box → Run workflow. This fetches your real 14 team names from
  Sleeper, fabricates a full fake game day of scoring for them (using
  `scripts/seed_dummy_data.py`), and publishes it as **week 0** — a week
  number that can never collide with a real week, and that will always sort
  to the bottom of the index once real weeks exist. Give Pages a minute,
  then visit `https://bhu248.github.io/<your-repo-name>/week0.html` to see
  the actual time-lapse: drag the scrubber to replay the fake day, or hit
  play. Re-running it regenerates week 0 from scratch (same fixed random
  seed, so it's the same fake day each time) — nothing about it ever touches
  real week data. Delete `data/week0.jsonl` and `docs/week0.html` whenever
  you're done with it; nothing references them automatically.

- **Real network, real repo, no fake data:** the same "Run workflow" button
  with the seed box left unchecked runs the real poller instead. Outside the
  season this will log "not in-season — skipping" and exit cleanly, which
  just confirms the plumbing works; you won't see real bars move until your
  league's actually live.

The Wednesday opener (Sept 9 — this year's season kickoff game was moved off
its usual Thursday slot) is the natural first real test: one game, low
stakes, easy to watch and compare against the Sleeper app directly before
trusting it with a full Sunday and an actual prize on the line.

## How it runs

`.github/workflows/scoreboard.yml` no longer has a `schedule:` trigger —
GitHub's own cron scheduler was observed missing runs by 15-20+ minutes or
more during the Week 1 opener, with nothing diagnosable from outside GitHub.
Timing now lives on your local machine instead: `scripts/local_scheduler.py`
ticks every 1 minute via a Windows Task Scheduler job named
`SundayScoreboardLocalTrigger`, checks whether it's currently inside a game
window (the same Sunday/Monday/Wednesday/Thursday-night + specific
Friday/Saturday windows the old cron comments described — now living as
`WEEKLY_WINDOWS`/`DATE_WINDOWS` in that script), and if so — and if it's
actually due, per its own adaptive cadence: every 3 minutes while 2+ NFL
games are concurrently live anywhere, every 5 when it's down to one (or
zero) — calls `gh workflow run scoreboard.yml` (a `workflow_dispatch`, the
only trigger left in the YAML). The 1-minute tick is just how often it
*checks*; it doesn't poll Sleeper any more often than the 3/5-minute
cadence actually calls for. **This means the workflow only fires while your machine
is on, awake, and logged in during game windows** — it's not a
GitHub-side schedule anymore. Check the task with:

```
Get-ScheduledTask -TaskName SundayScoreboardLocalTrigger
```

and see `local_scheduler.log` (gitignored, in the repo root) for a run-by-run
record of when it dispatched vs. skipped.

Each firing:

1. `scripts/poll.py` — fetches this week's matchups + projections from
   Sleeper, scores everything through your league's own `scoring_settings`,
   and appends one line to `data/week<N>.jsonl`.
2. `scripts/render.py` — rebuilds `docs/week<N>.html` from every snapshot
   logged so far this week, plus `docs/index.html` linking to whatever weeks
   exist.
3. If anything changed, it's committed and pushed — Pages picks up the new
   file within about a minute.

Polling runs every 3 minutes during a full slate and every 5 once it's down
to one game, both within (or close to) the 1–3 minutes mentioned in the
original feasibility check, and reads as smooth, continuous motion once
it's animated.

## Known limitations (carried over from the feasibility check)

- **It's a time-lapse, not a live feed.** Bars move every 3-5 minutes
  (depending on how many games are concurrently live), not on every
  literal snap.
- **The live-projection number is a simplification.** A starter's remaining
  projection drops to zero the moment they register *any* stat, not at their
  actual kickoff time (the public API doesn't expose per-player kickoff
  times directly) — so it can undersell a player who starts slow and
  explodes late in their game. Good enough to be genuinely useful; a
  schedule-aware version is the natural next improvement if the gap bothers
  you.
- **The page doesn't auto-refresh itself.** Reload to see the latest — it's
  a static file, not a live socket.
- Nothing here posts into Sleeper's own in-app league chat — that's a
  read-only API. Share the Pages link yourself wherever your league actually
  talks.

## Files

```
scripts/common.py           shared Sleeper API + scoring helpers
scripts/poll.py              one poll → one snapshot
scripts/render.py            snapshots → the published HTML pages
scripts/seed_dummy_data.py   fabricates a fake "week 0" for testing
scripts/local_scheduler.py   local timer that dispatches the workflow (see "How it runs")
.github/workflows/scoreboard.yml   the workflow_dispatch job the above triggers
selftest.py            offline sanity check, safe to run anytime
data/                  one .jsonl file per week (created automatically)
docs/                  the published pages (served by GitHub Pages)
```
