# College Football Elo Rankings

A margin-adjusted Elo rating for every FBS team, computed from game results
alone — no polls, no preseason expectations, no eye test. Runs on your machine,
and rebuilds itself every morning on GitHub Actions and publishes to GitHub Pages.

```
 1. Ohio State        12-1   1833   SOS 1507   ▲ 1
 2. Georgia           12-1   1815   SOS 1466   ▼ 1
 3. Oregon            10-3   1765   SOS 1385   ▲ 2
```

## What you get

- **A rankings dashboard** — sortable, searchable, filterable by conference,
  with week-over-week movement, biggest risers and fallers, and the season's
  least likely results.
- **A page for every team** — rating history chart, full game log with the Elo
  points gained or lost per game, strength of schedule, best win, worst loss.
- **Projections** for the next slate, with win probabilities and implied spreads.
- **`rankings.csv` and `rankings.json`** published alongside the site, so the
  ratings are usable outside the page.
- **A CLI** for poking at it locally without building the site.

---

## 1. Get an API key

Game data comes from [CollegeFootballData.com](https://collegefootballdata.com).
Grab a free key at **https://collegefootballdata.com/key** — it arrives by email.

```bash
copy .env.example .env      # Windows
# cp .env.example .env      # macOS / Linux
```

Then paste your key into `.env`:

```
CFBD_API_KEY=your_key_here
```

`.env` is gitignored. Never commit the key.

## 2. Run it locally

```bash
python -m venv .venv
.venv\Scripts\activate           # Windows
# source .venv/bin/activate      # macOS / Linux

pip install -r requirements.txt

python -m cfbrank check          # verify the key and connection (one small request)
python -m cfbrank top -n 25      # print the top 25
python -m cfbrank team "Ohio State"   # one team's rating and game log
python -m cfbrank build          # build the full site into docs/
python -m cfbrank --season 2025 compare   # Elo vs the AP poll, week by week
```

Then open `docs/index.html` in a browser.

The first run fetches every season from `history_start` forward, which takes a
minute. After that everything is cached in `data/raw/`, and only the current
season is re-fetched. Force a full refresh with `--refresh`.

Useful flags:

| Flag | What it does |
|---|---|
| `--season 2024` | Rank a different year |
| `--refresh` | Ignore the cache and refetch from the API |
| `--verbose` | Log every step |
| `--timeout 180` | Seconds per API request (default 90; each retry gets 50% more) |
| `--output somewhere/` | Write the site somewhere other than `docs/` |

## 3. Publish it

```bash
git init
git add .
git commit -m "Elo rankings"
git branch -M main
git remote add origin https://github.com/<you>/<repo>.git
git push -u origin main
```

Then, in the repository on GitHub:

1. **Settings → Secrets and variables → Actions → New repository secret.**
   Name it `CFBD_API_KEY`, paste your key.
2. **Settings → Pages → Source: GitHub Actions.**
3. **Actions tab → "Update rankings" → Run workflow** to kick off the first
   build without waiting for the schedule.

The site lands at `https://<you>.github.io/<repo>/`. After that it rebuilds
every morning at 12:00 UTC (8am ET) and on every push to `main`.

To change the schedule, edit the `cron` line in
`.github/workflows/update-rankings.yml`. Cron there is always UTC.

---

## Comparing against the AP poll

```bash
python -m cfbrank --season 2025 compare
python -m cfbrank --season 2025 compare --json ap-2025.json   # full detail
```

For each AP release it reports how many of the same 25 teams appear, how far
apart the two systems place the teams they share, and the single biggest
disagreement. It closes with the teams the two disagreed about all season and a
side-by-side of the most recent poll.

**On the timing.** The AP poll published during week N reflects games through
week N-1, so it's compared against the Elo ratings as they stood after week
N-1 — the same evidence the voters had. That's `--offset 1`, the default. Use
`--offset 0` to compare each poll against the ratings that *followed* it
instead, which asks a different question: how well did the voters anticipate
the next week?

## Tuning the model

Everything lives in `config.yaml`. The settings that actually change the
rankings:

| Setting | Default | What moving it does |
|---|---|---|
| `k` | 45 | How far one game moves a rating. Higher = more reactive to last Saturday, noisier early. |
| `home_field` | 62 | Elo points for playing at home, ~2.5 points of spread. |
| `preseason_regression` | 0.75 | How much of last year a team keeps. 0.0 wipes the slate every season. |
| `margin_of_victory` | true | Turn off to rate wins and losses only, ignoring scores. |
| `fcs_rating` | 1000 | Strength of the pooled non-FBS opponent. Raise it and cupcake wins pay better. |
| `history_start` | 2015 | Earlier = better-seeded preseason ratings, slower first run. |

Change one, rerun `python -m cfbrank top`, and see what moves. That's the whole
point of having it in a file.

## How the math works

Each team carries one number, starting at 1500.

1. **Before the game**, the rating gap (plus home field) implies a win
   probability: `1 / (1 + 10^(-diff/400))`. A 400-point edge is 10-to-1 odds.
2. **After the game**, the winner takes points from the loser, scaled by how
   surprising the result was: `K × (actual − expected)`.
3. **Margin of victory** multiplies that by `log(margin + 1)`, so a three-score
   win counts for more than a walk-off field goal, with diminishing returns.
4. **Favorite damping** shrinks that multiplier when a heavy favorite wins —
   without it, good teams inflate their ratings by beating up on bad ones.

Every non-FBS opponent is pooled into a single fixed-strength team, so
scheduling an FCS opponent can cost you rating but can't earn you much. Between
seasons, ratings are pulled 25% of the way back toward average.

Full write-up on the site's "How it works" page.

## Project layout

```
cfbrank/
  config.py       config.yaml loading and validation
  data.py         CFBD API client + on-disk cache
  elo.py          the rating engine
  polls.py        AP poll fetching + the week-by-week comparison
  rankings.py     records, SOS, movement, resumes, projections
  site.py         static site rendering + chart geometry
  cli.py          the command line interface
  templates/      Jinja2 templates
  static/         CSS and JS, copied into the build
tests/
  synthetic.py    a fake season, so tests run without an API key
  test_elo.py     properties the rating math must satisfy
  test_pipeline.py end-to-end: season in, rendered site out
  test_polls.py   poll parsing, comparison math, week alignment
data/raw/         cached API responses
docs/             the generated site (what Pages serves)
```

## Tests

```bash
pip install pytest
python -m pytest tests -q
```

The suite runs entirely on a simulated season, so it needs no API key and no
network. `test_model_recovers_true_strength_ordering` is the interesting one: it
generates teams with a hidden true strength, simulates a season from it, and
checks that the ratings recover that ordering.

## When the API is slow

`Read timed out` means the request outran its budget, not that anything is
wrong with your key. Each request is retried three times with a larger budget
each time, and everything that already succeeded stays cached — so rerunning
picks up where it left off rather than starting over.

If it keeps timing out:

```bash
python -m cfbrank check                 # is it the connection, or just this endpoint?
python -m cfbrank --timeout 180 top     # give it three minutes per request
```

Still stuck? Shorten the backfill — set `history_start: 2022` in `config.yaml`
to pull four seasons instead of eleven. You can raise it again later; the cache
keeps what you've already fetched.

## Caveats worth knowing

- **Elo is backward-looking.** It measures what a team has done, not what it
  would do on a neutral field tomorrow.
- **Early-season ratings are mostly last year.** Through three or four games,
  carry-over dominates.
- **Strength of schedule moves all year**, since it's the mean *final* rating of
  the opponents you played.
- **Undocumented API changes happen.** The client reads both snake_case and
  camelCase field names, and falls back to the cache when the API is down, but a
  breaking change upstream is always possible.
