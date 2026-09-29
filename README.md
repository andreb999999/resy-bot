# resy-bot

A small CLI client for Resy's (unofficial) web API - see the docstring at the
top of [resy.py](resy.py) for all commands (`find`, `book`, `scan`, `snipe`,
`watch`, ...). Weekly hours, party size, and money rules (max prepay / max
cancellation fee) live in [resy_config.toml](resy_config.toml).

## Running it yourself, locally

```
pip install -r requirements.txt
python resy.py login          # paste your Resy auth token, or use --email
python resy.py scan --days 30 --book --yes
```

## Running it in GitHub Actions (so it works even when your computer is off)

[.github/workflows/resy-autobook.yml](.github/workflows/resy-autobook.yml)
runs `python resy.py scan --book --yes` on a schedule, using the restaurants,
hours, and money rules in `resy_config.toml`. It keeps re-checking (the
script's own `--every` loop) and stops as soon as it books one table that
fits your rules, same as running `scan --book` locally.

### One-time setup

1. In this repo on GitHub: **Settings -> Secrets and variables -> Actions ->
   New repository secret**.
2. Name it `RESY_TOKEN`, and paste your Resy auth token as the value (the
   same token `python resy.py login` asks you to paste - grab a fresh one
   from resy.com if you don't have it handy).
3. That's it. The scheduled workflow will start running automatically. You
   can also trigger it manually from the **Actions** tab
   (`Resy auto-book` -> **Run workflow**).

### How it stays running "all the time"

GitHub Actions kills any job after 6 hours, so a single run can't loop
forever. Instead, the workflow restarts a fresh ~4h50m run every 5 hours
(cron), so it's continuously polling except for a short gap around each
restart. Booking state (`resy_booked.json`, the no-double-booking lock) and
the opening log (`resy_openings.csv`) are committed back to this repo after
each run so they survive between runs on the ephemeral GitHub-hosted
runners.

### Stopping it

Disable the workflow from the **Actions** tab (**...** -> **Disable
workflow**), or delete the `schedule:` block in the workflow file.

### Notes / limitations

- This repo is **public**. `resy_config.toml` (your restaurant list, hours,
  and money limits) and the booking/opening logs will be visible to anyone.
  Nothing secret is committed - the auth token only ever lives in an
  encrypted Actions secret and the ephemeral runner's `~/.resy_auth.json`.
- GitHub disables scheduled workflows automatically after 60 days with no
  repository activity (commits/pushes). As long as the bot books something
  or logs an opening periodically, that resets the clock - but if it goes
  quiet for 2 months with nothing to commit, re-enable it from the Actions
  tab or push any commit.
- Scheduled workflow runs are best-effort timed by GitHub and can be delayed
  by a few minutes, especially during high load.
