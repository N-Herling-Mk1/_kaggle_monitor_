# HELIX :: Kaggle Leaderboard Monitor (GitHub Actions)

Polls a Kaggle competition leaderboard every 15 minutes from the cloud.
Emails you when someone passes you. Records every snapshot as a git commit.

```
.github/workflows/kaggle-monitor.yml   <- cron + checkout + run + commit
monitor.py                             <- the worker
state.json                             <- last full board (auto-written)
snapshots/<UTC timestamp>.json         <- one per poll, audit trail
```

----------------------------------------------------------------
SETUP  (one-time, ~5 minutes)
----------------------------------------------------------------

1. Create a NEW PRIVATE GitHub repo (call it whatever — `kaggle-monitor` works).
   Private matters: state.json contains the full board.

2. Copy these four things into the repo and push:
       .github/workflows/kaggle-monitor.yml
       monitor.py
       README.md   (this file, optional)
       .gitignore  (optional)

3. Get a free Resend API key:
   - Sign up at https://resend.com  (no credit card; 100 emails/day free)
   - API Keys -> Create -> copy the `re_...` string
   - You can use the default sender `onboarding@resend.dev` immediately;
     verifying your own domain is optional.

4. In your repo, go to:
       Settings -> Secrets and variables -> Actions

   Add these REPOSITORY SECRETS:
       KAGGLE_USERNAME    your kaggle username  (from kaggle.json)
       KAGGLE_KEY         your kaggle api key   (from kaggle.json)
       MY_TEAM            your team-name fragment, e.g. "Nathan Herling"
       RESEND_API_KEY     re_xxxxxxxxxxxxxxxxxxxx
       NOTIFY_TO          email address to alert

   And these REPOSITORY VARIABLES:
       COMP               ling-539-competition-2026
       NOTIFY_FROM        HELIX <onboarding@resend.dev>

5. Trigger it once manually:
       Actions tab -> "HELIX Kaggle Leaderboard Monitor" -> Run workflow

   First run takes ~30 seconds. Watch the log; you should see something like:

       [poll] 2026-04-30T... competition=ling-539-...
       [poll] 87 teams
       [poll] no rank change  (prev=None, curr=12)
       [done]  rank=12  score=0.93121  alerted=False

   The first run never emails (no previous state to compare against). It just
   establishes the baseline.

6. Done. The cron takes over from here. Every 15 minutes, forever, until you
   disable it. If anyone passes you, you'll get an email within 15 min.

----------------------------------------------------------------
ALERT TRIGGER
----------------------------------------------------------------

Email fires when:    your_rank_now  >  your_rank_last_poll
                     (i.e. you dropped — someone scored higher than you
                     OR a new team entered above you)

Email contains:
  - your previous rank/score and current rank/score
  - the list of teams that newly outrank you (with their scores)
  - the current top 10
  - a link straight to the leaderboard

Climbing the board is logged but does not email (you don't need that ping).

----------------------------------------------------------------
THE RECORD
----------------------------------------------------------------

Every poll commits to the repo. So you get, for free:

  - state.json                    most recent full board
  - snapshots/2026-04-30T1500Z.json
    snapshots/2026-04-30T1515Z.json
    ...                           one compact snapshot per poll

`git log -- state.json` is your timeline. Every commit message is a UTC
timestamp. You can `git diff` between any two and see exactly what moved.

If you want a single CSV of your rank over time after the competition:

    grep -hE '"my_rank"|"at"' snapshots/*.json | paste - - | sed ...

(or just write a 10-line Python script — the per-snapshot JSON is structured.)

----------------------------------------------------------------
COSTS
----------------------------------------------------------------

GitHub Actions:  free for public repos; 2,000 min/month free for private.
                 This job uses ~30 sec * 96 polls/day = ~48 min/day = ~24 hr/mo.
                 Well under the free tier.

Resend:          100 emails/day free. You will not hit this on a class kaggle.

Kaggle API:      no rate limit issue at 15-min intervals.

----------------------------------------------------------------
KNOBS
----------------------------------------------------------------

Change poll frequency: edit the cron line in
  .github/workflows/kaggle-monitor.yml
        - cron: '*/15 * * * *'    every 15 min  (default)
        - cron: '*/5  * * * *'    every 5  min
        - cron: '0 */1 * * *'     every hour

Change competition: update the COMP repository variable. No code edit needed.

Stop polling:  Actions tab -> select the workflow -> "..." menu -> Disable.

----------------------------------------------------------------
TROUBLESHOOTING
----------------------------------------------------------------

"403 Forbidden" from kaggle CLI:
   You haven't accepted the competition rules. Go to the competition page
   on kaggle.com and click Join / Accept Rules.

"team 'X' not found on board":
   MY_TEAM secret doesn't match. It's a case-insensitive substring match
   against the TeamName column. Use any unique fragment of your team name.

Email not arriving:
   - check the workflow log; the [email] line shows the Resend response code
   - 422 usually means NOTIFY_FROM is invalid; use the default sender
   - check your spam folder once
   - test Resend manually: https://resend.com/docs/send-with-curl

Workflow not running on schedule:
   GitHub silently disables scheduled workflows on inactive repos after
   60 days. Push any commit to wake it up. Or just open the repo in the
   browser once a week.
