# deploy-tracker

How long do launched side projects stay online? A one-page dashboard that follows the 100 most-upvoted websites launched each month on Show HN, r/SideProject, r/InternetIsBeautiful, r/IMadeThis, r/ClaudeAI and r/SaaS, and checks every one of them once a day.

- `scripts/track.py` collects posts, ranks each launch month's sites by votes, checks every tracked site and writes `site/data/tracker.json`. Posts come from [deploy-list](https://github.com/PigeonFlare/deploy-list)'s published rankings (last 30 days, live Reddit counts), Show HN via hn.algolia.com, and the Arctic Shift Reddit archive, which is read back to October 2025 a little each run. `scripts/rules.py` holds deploy-list's rules for what counts as a standalone website and its game/app categories.
- Each check records one character per site per day: `U` up, `B` up behind a bot wall, `P` parked or for sale, `D` down (DNS, timeout, TLS, 404/410, 5xx, or the host says the deployment was removed), `.` not tracked yet. Sites that look down are retried once.
- `site/` is the static dashboard (D3, vendored in `site/vendor/`).
- `.github/workflows/track.yml` runs daily at 06:41 UTC, commits the results to `main`, and deploys `site/` to GitHub Pages. Pushes redeploy without checking; a manual run checks immediately.

## Setup

Settings → Pages → Source: **GitHub Actions**. Then run the workflow once from the Actions tab to collect and check the first batch. The Reddit archive backfill takes a couple of weeks of daily runs to reach the present, so older months fill in with Reddit posts gradually.

## Local

```sh
python3 scripts/track.py
python3 -m http.server -d site 8000
python3 -B -m unittest discover -s scripts -p 'test_*.py'
```
