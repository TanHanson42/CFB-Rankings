"""WHERE THE GAME RESULTS COME FROM.

This file downloads college football schedules and scores from
CollegeFootballData.com (a free public API) and hands them to the rest of the
program in a tidy, predictable shape. Nothing here does any rating math.

Three jobs, in order of how much code they take up:

1. ASKING FOR DATA. Send an HTTP request with your API key attached, and be
   patient about it - the API is occasionally slow or briefly broken, so
   requests are retried with escalating waits rather than failing on the first
   hiccup.

2. REMEMBERING IT. Every response is saved to data/raw/ as a JSON file. A
   finished season never changes, so once 2019 is on disk it's never fetched
   again. Only the current season is re-downloaded, and only if the saved copy
   is more than six hours old. This is why the first run takes a minute and
   every run after it takes seconds.

3. TIDYING IT UP. The API's raw output becomes simple Game and Team records,
   defined at the bottom of this file, so no other part of the program has to
   know anything about JSON or HTTP.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable

import requests

from .config import REPO_ROOT, current_season

log = logging.getLogger(__name__)

API_BASE = "https://api.collegefootballdata.com"
CACHE_DIR = REPO_ROOT / "data" / "raw"

# A full season of games is a megabyte of JSON, and CFBD is sometimes slow to
# produce the first byte. 30s was too tight on real connections.
DEFAULT_TIMEOUT = int(os.environ.get("CFBD_TIMEOUT", "90"))

# 5xx responses are the API having a bad minute. They clear on their own, but
# not within the couple of seconds a naive doubling backoff waits.
RETRYABLE_STATUS = {500, 502, 503, 504}
SERVER_ERROR_BACKOFF = (5, 15, 45)
MAX_ATTEMPTS = 4

# How long a cached *current-season* file stays fresh.
CURRENT_SEASON_TTL = dt.timedelta(hours=6)


class CFBDError(RuntimeError):
    """Raised when the API can't be reached or returns something unusable."""


def _pick(row: dict[str, Any], *keys: str, default: Any = None) -> Any:
    """Read whichever of several possible field names actually exists.

    The API has changed how it spells its fields over the years - the home
    team's score has been both "homePoints" and "home_points" - so instead of
    betting on one spelling, every read asks for all the ones we've seen:

        _pick(row, "homePoints", "home_points")

    It returns the first one present. This is why the program keeps working
    when the API changes underneath it.
    """
    for key in keys:
        if key in row and row[key] is not None:
            return row[key]
    return default


@dataclass(frozen=True)
class Game:
    """One game - either already played, or still on the schedule.

    "frozen" means these can't be changed after they're created, which is a
    deliberate safety net: game results are facts, and a bug that quietly
    rewrote a score would be very hard to notice in the final rankings.
    """

    id: int
    season: int
    week: int
    season_type: str
    start_date: str
    completed: bool
    neutral_site: bool
    conference_game: bool
    home_team: str
    home_conference: str | None
    home_classification: str | None
    home_points: int | None
    away_team: str
    away_conference: str | None
    away_classification: str | None
    away_points: int | None
    venue: str | None

    @property
    def sort_key(self) -> tuple:
        """Used to put games in the order they were actually played.

        The subtlety: bowl games are labelled week 1, 2, 3 of the postseason,
        so sorting by week number alone would file the national championship
        before September. The `phase` value forces all postseason games after
        all regular season ones, no matter what week they claim to be.
        """
        phase = 0 if self.season_type == "regular" else 1
        return (self.season, phase, self.week, self.start_date or "", self.id)

    @property
    def margin(self) -> int | None:
        """Home score minus away score. Negative means the home team lost."""
        if self.home_points is None or self.away_points is None:
            return None
        return self.home_points - self.away_points


@dataclass(frozen=True)
class Team:
    school: str
    mascot: str | None
    abbreviation: str | None
    conference: str | None
    division: str | None
    color: str | None
    alt_color: str | None
    logo: str | None


class CFBDClient:
    def __init__(
        self,
        api_key: str | None = None,
        cache_dir: Path = CACHE_DIR,
        timeout: int = DEFAULT_TIMEOUT,
        strict: bool = False,
    ) -> None:
        self.api_key = api_key or os.environ.get("CFBD_API_KEY", "").strip()
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.timeout = timeout
        #: When true, any failed fetch aborts instead of degrading.
        self.strict = strict
        #: Non-fatal problems worth telling the user about at the end.
        self.warnings: list[str] = []
        self._session = requests.Session()

    # ---------------------------------------------------------------- transport

    def _request(self, path: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        if not self.api_key:
            raise CFBDError(
                "No CFBD API key found. Get a free one at "
                "https://collegefootballdata.com/key and set CFBD_API_KEY "
                "(in a .env file locally, or as a GitHub Actions secret)."
            )
        url = f"{API_BASE}{path}"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Accept": "application/json",
        }
        last_error: Exception | None = None
        timed_out = False
        server_error = False
        for attempt in range(MAX_ATTEMPTS):
            # Give a slow endpoint more room on each retry rather than failing
            # it the same way three times.
            attempt_timeout = int(self.timeout * (1 + 0.5 * attempt))
            try:
                resp = self._session.get(
                    url, params=params, headers=headers, timeout=attempt_timeout
                )
                if resp.status_code == 401:
                    raise CFBDError(
                        "CFBD rejected the API key (401). Check that CFBD_API_KEY "
                        "is set to a valid key."
                    )
                if resp.status_code == 429:
                    wait = 2 ** attempt * 5
                    log.warning("Rate limited by CFBD; sleeping %ss", wait)
                    time.sleep(wait)
                    continue
                if resp.status_code in RETRYABLE_STATUS:
                    # The API is having a moment. These clear on their own, but
                    # not in the two seconds a naive backoff waits.
                    server_error = True
                    wait = SERVER_ERROR_BACKOFF[min(attempt, len(SERVER_ERROR_BACKOFF) - 1)]
                    log.warning(
                        "CFBD returned %d for %s (attempt %d/%d); waiting %ds",
                        resp.status_code, path, attempt + 1, MAX_ATTEMPTS, wait,
                    )
                    last_error = requests.HTTPError(
                        f"{resp.status_code} {resp.reason} for {path}"
                    )
                    if attempt < MAX_ATTEMPTS - 1:
                        time.sleep(wait)
                    continue
                resp.raise_for_status()
                payload = resp.json()
                if not isinstance(payload, list):
                    raise CFBDError(f"Expected a list from {path}, got {type(payload)}")
                return payload
            except CFBDError:
                raise
            except requests.Timeout as exc:
                timed_out = True
                last_error = exc
                log.warning(
                    "Request to %s timed out after %ds (attempt %d/%d); retrying with more time",
                    path, attempt_timeout, attempt + 1, MAX_ATTEMPTS,
                )
                time.sleep(2 ** attempt)
            except Exception as exc:  # network blip, malformed JSON
                last_error = exc
                log.warning(
                    "Request to %s failed (attempt %d/%d): %s",
                    path, attempt + 1, MAX_ATTEMPTS, exc,
                )
                time.sleep(2 ** attempt)

        if server_error:
            raise CFBDError(
                f"CFBD kept returning a server error for {path} "
                f"({MAX_ATTEMPTS} tries over ~{sum(SERVER_ERROR_BACKOFF)}s).\n"
                "  That's their end, not yours - nothing you can configure fixes it.\n"
                "  Everything fetched before this point is cached, so rerunning\n"
                "  resumes from here rather than starting over. Try again in a few\n"
                "  minutes; check https://status.collegefootballdata.com if it persists."
            )
        if timed_out:
            raise CFBDError(
                f"Timed out fetching {path} after {MAX_ATTEMPTS} tries (up to "
                f"{int(self.timeout * (1 + 0.5 * (MAX_ATTEMPTS - 1)))}s each).\n"
                "  The API is slow or unreachable right now. Things to try:\n"
                "    - run it again; the cache keeps whatever already succeeded\n"
                "    - raise the budget:  --timeout 180\n"
                "    - shorten the backfill: set history_start closer to season "
                "in config.yaml\n"
                "    - check a VPN, proxy, or corporate firewall isn't in the way"
            )
        raise CFBDError(f"Could not fetch {path}: {last_error}")

    # ------------------------------------------------------------------- cache

    def _cache_path(self, name: str) -> Path:
        return self.cache_dir / f"{name}.json"

    def _is_fresh(self, path: Path, season: int) -> bool:
        if not path.exists():
            return False
        if season < current_season():
            return True  # a finished season never changes
        age = dt.datetime.now() - dt.datetime.fromtimestamp(path.stat().st_mtime)
        return age < CURRENT_SEASON_TTL

    def _cached(
        self,
        name: str,
        season: int,
        fetch,
        refresh: bool = False,
    ) -> list[dict[str, Any]]:
        path = self._cache_path(name)
        if not refresh and self._is_fresh(path, season):
            log.info("cache hit: %s", path.name)
            return json.loads(path.read_text(encoding="utf-8"))
        try:
            payload = fetch()
        except CFBDError:
            if path.exists():
                log.warning("API unavailable; falling back to stale cache %s", path.name)
                return json.loads(path.read_text(encoding="utf-8"))
            raise
        path.write_text(json.dumps(payload, indent=0), encoding="utf-8")
        log.info("cached %d records to %s", len(payload), path.name)
        return payload

    # -------------------------------------------------------------- public API

    def teams(self, season: int, refresh: bool = False) -> list[Team]:
        raw = self._cached(
            f"teams_{season}",
            season,
            lambda: self._request("/teams/fbs", {"year": season}),
            refresh=refresh,
        )
        return [self._parse_team(row) for row in raw]

    def games(self, season: int, refresh: bool = False) -> list[Game]:
        """All regular season + postseason games for a year, in play order.

        A failed *postseason* fetch is survivable: it's a handful of bowl games
        against a full season, and for the current year it's often empty anyway.
        Losing the regular season is not, so that still raises.
        """
        raw: list[dict[str, Any]] = []
        for season_type in ("regular", "postseason"):
            try:
                raw += self._cached(
                    f"games_{season}_{season_type}",
                    season,
                    lambda st=season_type: self._request(
                        "/games", {"year": season, "seasonType": st}
                    ),
                    refresh=refresh,
                )
            except CFBDError:
                if season_type == "regular" or self.strict:
                    raise
                note = (
                    f"{season} postseason unavailable - bowl results are missing "
                    f"from that season's ratings"
                )
                log.warning(note)
                self.warnings.append(note)
        games = [self._parse_game(row, season) for row in raw]
        games = [g for g in games if g is not None]
        return sorted(games, key=lambda g: g.sort_key)

    # --------------------------------------------------------------- parsing

    @staticmethod
    def _parse_team(row: dict[str, Any]) -> Team:
        logos = _pick(row, "logos") or []
        # Some logo URLs come back as http://; on an https Pages site those are
        # blocked as mixed content and the image silently never appears.
        logo = logos[0].replace("http://", "https://") if logos else None
        return Team(
            school=_pick(row, "school", "team", default="?"),
            mascot=_pick(row, "mascot"),
            abbreviation=_pick(row, "abbreviation"),
            conference=_pick(row, "conference"),
            division=_pick(row, "division", "classification"),
            color=_pick(row, "color"),
            alt_color=_pick(row, "alt_color", "altColor"),
            logo=logo,
        )

    @staticmethod
    def _parse_game(row: dict[str, Any], season: int) -> Game | None:
        home = _pick(row, "homeTeam", "home_team")
        away = _pick(row, "awayTeam", "away_team")
        if not home or not away:
            return None

        home_pts = _pick(row, "homePoints", "home_points")
        away_pts = _pick(row, "awayPoints", "away_points")
        completed = _pick(row, "completed")
        if completed is None:
            completed = home_pts is not None and away_pts is not None

        return Game(
            id=int(_pick(row, "id", default=0)),
            season=int(_pick(row, "season", default=season)),
            week=int(_pick(row, "week", default=0)),
            season_type=str(_pick(row, "seasonType", "season_type", default="regular")),
            start_date=str(_pick(row, "startDate", "start_date", default="")),
            completed=bool(completed),
            neutral_site=bool(_pick(row, "neutralSite", "neutral_site", default=False)),
            conference_game=bool(
                _pick(row, "conferenceGame", "conference_game", default=False)
            ),
            home_team=home,
            home_conference=_pick(row, "homeConference", "home_conference"),
            home_classification=_pick(
                row, "homeClassification", "home_division", "homeDivision"
            ),
            home_points=int(home_pts) if home_pts is not None else None,
            away_team=away,
            away_conference=_pick(row, "awayConference", "away_conference"),
            away_classification=_pick(
                row, "awayClassification", "away_division", "awayDivision"
            ),
            away_points=int(away_pts) if away_pts is not None else None,
            venue=_pick(row, "venue"),
        )


def load_seasons(
    client: CFBDClient,
    seasons: Iterable[int],
    refresh: bool = False,
    on_progress: Callable[[str], None] | None = None,
) -> tuple[list[Game], dict[str, Team]]:
    """Fetch every season's games plus the current season's team metadata."""
    seasons = list(seasons)
    say = on_progress or (lambda _msg: None)

    # Teams first: it's a small request, so a bad key or a dead connection
    # fails here in a second instead of after a minute of waiting on games.
    say(f"Checking the API and fetching {seasons[-1]} teams...")
    teams = {t.school: t for t in client.teams(seasons[-1], refresh=refresh)}
    say(f"  {len(teams)} FBS teams.")

    games: list[Game] = []
    for i, season in enumerate(seasons, start=1):
        say(f"Fetching {season} games ({i}/{len(seasons)})...")
        season_games = client.games(season, refresh=refresh)
        log.info("season %d: %d games", season, len(season_games))
        games += season_games
    say(f"  {len(games)} games across {len(seasons)} seasons.")
    return games, teams
