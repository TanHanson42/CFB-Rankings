"""TURNING RATINGS INTO A RANKINGS PAGE.

The rating engine (elo.py) produces one number per team and nothing else. That
number alone doesn't make a web page. This file works out everything else the
site displays:

    - the ranked order, 1 through 138
    - each team's win-loss record, overall and in conference
    - strength of schedule: the average rating of everyone they played
    - movement: how many places they've climbed or fallen since last week
    - resume: their best win and worst loss
    - a game-by-game log showing what each result was worth in rating points
    - projections for the upcoming week, with win probabilities

None of this changes any ratings. It only reads what elo.py produced and
reshapes it for display. Everything lands in a RankingTable, which is what the
website templates and the command line both read from.
"""

from __future__ import annotations

import datetime as dt
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any

from .config import Config
from .data import Game, Team
from .elo import FCS_POOL, EloModel, GameResult, predict, rating_to_spread

FCS_LABEL = "FCS opponent"


def slugify(name: str) -> str:
    """Turn a school name into something safe for a web address.

        "Texas A&M"     -> "texas-am"
        "San Jose State" -> "san-jose-state"
        "Hawai'i"        -> "hawaii"

    Accents are stripped, punctuation dropped, spaces become hyphens. This is
    the id each team is looked up by on the team page (team.html?t=texas-am).
    """
    text = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    text = re.sub(r"[^\w\s-]", "", text).strip().lower()
    return re.sub(r"[-\s]+", "-", text) or "team"


@dataclass
class GameLogEntry:
    """One row of a team's game log, as shown on their page."""

    week: int
    week_label: str          # "Week 7" or "Postseason"
    date: str
    opponent: str
    opponent_slug: str | None  # None for pooled FCS teams, which have no page
    location: str            # "vs" = home, "at" = away, "n" = neutral site
    won: bool
    tied: bool
    points_for: int
    points_against: int
    rating_change: float     # Elo gained or lost in THIS game
    win_probability: float   # what we gave them before kickoff
    opponent_rating: float   # the opponent's final rating, for context
    upset: bool

    @property
    def result_letter(self) -> str:
        return "T" if self.tied else ("W" if self.won else "L")

    @property
    def prefix(self) -> str:
        return {"vs": "vs", "at": "at", "n": "vs"}[self.location]

    @property
    def score(self) -> str:
        return f"{self.points_for}-{self.points_against}"


@dataclass
class NextGame:
    """The upcoming matchup shown in the 'This week' column."""

    opponent: str
    opponent_slug: str | None
    location: str  # "vs", "at", or "n"
    win_probability: float
    date: str

    @property
    def prefix(self) -> str:
        return {"vs": "vs", "at": "at", "n": "vs"}[self.location]


@dataclass
class TeamRanking:
    school: str
    slug: str
    conference: str
    logo: str | None
    color: str | None
    rating: float
    rank: int = 0
    previous_rank: int | None = None
    preseason_rating: float = 0.0
    wins: int = 0
    losses: int = 0
    ties: int = 0
    conference_wins: int = 0
    conference_losses: int = 0
    sos: float = 0.0
    sos_rank: int = 0
    games: list[GameLogEntry] = field(default_factory=list)
    history: list[tuple[str, float]] = field(default_factory=list)
    best_win: GameLogEntry | None = None
    worst_loss: GameLogEntry | None = None
    next_game: NextGame | None = None

    @property
    def last_game(self) -> GameLogEntry | None:
        return self.games[-1] if self.games else None

    @property
    def record(self) -> str:
        base = f"{self.wins}-{self.losses}"
        return f"{base}-{self.ties}" if self.ties else base

    @property
    def conference_record(self) -> str:
        return f"{self.conference_wins}-{self.conference_losses}"

    @property
    def rank_change(self) -> int | None:
        """How many places the team moved. Positive means UP the board.

        Subtracting this way round looks backwards but isn't: going from 12th
        to 5th is an improvement, and 12 - 5 = +7. Lower rank numbers are
        better, so the previous rank has to come first.

        Returns None when there's nothing to compare against - the very first
        rating of a season, before any movement exists.
        """
        if self.previous_rank is None:
            return None
        return self.previous_rank - self.rank

    @property
    def rating_change(self) -> float:
        return self.rating - self.preseason_rating

    def to_dict(self) -> dict[str, Any]:
        return {
            "rank": self.rank,
            "team": self.school,
            "slug": self.slug,
            "conference": self.conference,
            "rating": round(self.rating, 1),
            "record": self.record,
            "conference_record": self.conference_record,
            "previous_rank": self.previous_rank,
            "rank_change": self.rank_change,
            "sos": round(self.sos, 1),
            "sos_rank": self.sos_rank,
        }


@dataclass
class Projection:
    week_label: str
    date: str
    home: str
    home_slug: str | None
    away: str
    away_slug: str | None
    neutral: bool
    home_win_probability: float
    spread: float  # positive = home favored, in points

    @property
    def favorite(self) -> str:
        return self.home if self.home_win_probability >= 0.5 else self.away

    @property
    def underdog(self) -> str:
        return self.away if self.home_win_probability >= 0.5 else self.home

    @property
    def favorite_probability(self) -> float:
        return max(self.home_win_probability, 1 - self.home_win_probability)


@dataclass
class RankingTable:
    season: int
    generated_at: str
    through: str
    teams: list[TeamRanking]
    projections: list[Projection]
    projection_week: str | None
    biggest_upsets: list[dict[str, Any]]
    games_rated: int
    #: Label of the snapshot the rank movement is measured against. In week 1
    #: that's "Preseason", which is worth saying out loud rather than implying
    #: the movement is week-over-week.
    previous_label: str | None = None

    @property
    def risers(self) -> list[TeamRanking]:
        movers = [t for t in self.teams if t.rank_change]
        return sorted(movers, key=lambda t: -(t.rank_change or 0))[:5]

    @property
    def fallers(self) -> list[TeamRanking]:
        movers = [t for t in self.teams if t.rank_change]
        return sorted(movers, key=lambda t: (t.rank_change or 0))[:5]

    @property
    def conferences(self) -> list[str]:
        return sorted({t.conference for t in self.teams if t.conference})

    def to_dict(self) -> dict[str, Any]:
        return {
            "season": self.season,
            "generated_at": self.generated_at,
            "through": self.through,
            "games_rated": self.games_rated,
            "rankings": [t.to_dict() for t in self.teams],
        }


def _location(is_home: bool, neutral: bool) -> str:
    if neutral:
        return "n"
    return "vs" if is_home else "at"


def build_rankings(
    model: EloModel,
    games: list[Game],
    teams: dict[str, Team],
    config: Config,
) -> RankingTable:
    """Assemble everything the website and the CLI display.

    Reads the finished model and builds one TeamRanking per FBS team, in six
    passes over the data:

        1. create a row for every FBS team, with its final rating
        2. attach each team's weekly rating history, for the chart
        3. walk every game to build records and game logs
        4. average up strength of schedule, find best win / worst loss
        5. sort by rating to assign ranks, and compare to last week
        6. project the upcoming week's games

    Nothing here changes a rating - it only reads and reorganizes.
    """
    season = config.season
    final = model.ratings

    # --- Pass 1: one row per FBS team ---
    # Note this uses the CURRENT season's FBS list, so a team that moved up
    # from FCS this year appears, and one that dropped out doesn't.
    entries: dict[str, TeamRanking] = {}
    for school, meta in teams.items():
        entries[school] = TeamRanking(
            school=school,
            slug=slugify(school),
            conference=meta.conference or "Independent",
            logo=meta.logo,
            color=meta.color,
            rating=final.get(school, config.elo.initial_rating),
        )

    slug_of = {school: e.slug for school, e in entries.items()}

    # --- season snapshots, for the history chart and week-over-week movement
    season_snaps = [s for s in model.snapshots if s.season == season]
    for school, entry in entries.items():
        entry.history = [
            (snap.label, round(snap.ratings.get(school, config.elo.initial_rating), 1))
            for snap in season_snaps
        ]
        if season_snaps:
            entry.preseason_rating = season_snaps[0].ratings.get(
                school, config.elo.initial_rating
            )
        else:
            entry.preseason_rating = config.elo.initial_rating

    # --- Pass 3: game logs and records ---
    # Each game appears once in the model but needs to show up on TWO team
    # pages - once from the home team's point of view, once from the away
    # team's - which is what the `for is_home in (True, False)` loop below does.
    season_results = [r for r in model.results if r.game.season == season]
    opponent_ratings: dict[str, list[float]] = {s: [] for s in entries}

    for result in season_results:
        game = result.game
        for is_home in (True, False):
            school = game.home_team if is_home else game.away_team
            entry = entries.get(school)
            if entry is None:
                continue  # non-FBS team; it doesn't get a page

            opp_name = game.away_team if is_home else game.home_team
            opp_key = result.away_key if is_home else result.home_key
            pooled = opp_key == FCS_POOL
            opp_rating = final.get(opp_key, config.elo.fcs_rating)

            pf = game.home_points if is_home else game.away_points
            pa = game.away_points if is_home else game.home_points
            margin = pf - pa

            entry.games.append(
                GameLogEntry(
                    week=game.week,
                    week_label=(
                        f"Week {game.week}"
                        if game.season_type == "regular"
                        else "Postseason"
                    ),
                    date=game.start_date[:10],
                    opponent=opp_name,
                    opponent_slug=None if pooled else slug_of.get(opp_name),
                    location=_location(is_home, game.neutral_site),
                    won=margin > 0,
                    tied=margin == 0,
                    points_for=pf,
                    points_against=pa,
                    rating_change=result.shift if is_home else -result.shift,
                    win_probability=(
                        result.home_win_prob if is_home else 1 - result.home_win_prob
                    ),
                    opponent_rating=opp_rating,
                    upset=result.upset,
                )
            )

            if margin > 0:
                entry.wins += 1
            elif margin < 0:
                entry.losses += 1
            else:
                entry.ties += 1

            if game.conference_game:
                if margin > 0:
                    entry.conference_wins += 1
                elif margin < 0:
                    entry.conference_losses += 1

            opponent_ratings[school].append(opp_rating)

    # --- Strength of schedule ---
    # The average FINAL rating of every opponent a team played. Using final
    # ratings rather than the ratings at the time means SOS keeps moving all
    # season: beat a team in September that turns out to be excellent, and your
    # schedule retroactively gets stronger. That's the honest way to do it, but
    # it does mean this number changes even in a week your team didn't play.
    for school, entry in entries.items():
        faced = opponent_ratings[school]
        entry.sos = sum(faced) / len(faced) if faced else config.elo.initial_rating
        entry.games.sort(key=lambda g: (g.date or "", g.week))
        wins = [g for g in entry.games if g.won]
        losses = [g for g in entry.games if not g.won and not g.tied]
        entry.best_win = max(wins, key=lambda g: g.opponent_rating) if wins else None
        entry.worst_loss = (
            min(losses, key=lambda g: g.opponent_rating) if losses else None
        )

    # --- The actual ranking ---
    # Sort by rating, highest first (the minus sign reverses the order), then
    # number them 1, 2, 3... That's the whole ranking. No tiebreakers, no
    # committee, no adjustments.
    ranked = sorted(entries.values(), key=lambda t: -t.rating)
    for i, entry in enumerate(ranked, start=1):
        entry.rank = i
    # Same again, but ordered by strength of schedule, to get an SOS rank.
    for i, entry in enumerate(sorted(ranked, key=lambda t: -t.sos), start=1):
        entry.sos_rank = i

    # --- Last week's ranks, for the movement arrows ---
    # Re-rank every team using the SECOND-TO-LAST snapshot, then compare. The
    # [-2] is "the week before the most recent one". In week 1 that snapshot is
    # the preseason, which is why the site labels the movement "since preseason"
    # rather than implying it's week-over-week.
    previous_label: str | None = None
    if len(season_snaps) >= 2:
        previous_label = season_snaps[-2].label
        prev = season_snaps[-2].ratings
        prev_order = sorted(
            entries, key=lambda s: -prev.get(s, config.elo.initial_rating)
        )
        for i, school in enumerate(prev_order, start=1):
            entries[school].previous_rank = i

    # --- Projections for the next slate of unplayed games ---
    # Find the earliest week that still has unplayed games, then predict every
    # game in it. This is the only forward-looking part of the whole program.
    upcoming = [g for g in games if g.season == season and not g.completed]
    projections: list[Projection] = []
    projection_week: str | None = None
    if upcoming:
        upcoming.sort(key=lambda g: g.sort_key)
        first = upcoming[0]
        projection_week = (
            f"Week {first.week}" if first.season_type == "regular" else "Postseason"
        )
        slate = [
            g
            for g in upcoming
            if g.week == first.week and g.season_type == first.season_type
        ]
        for game in slate:
            if game.home_team not in entries and game.away_team not in entries:
                continue
            prob = predict(model, game)
            home_key = model.key_for(game.home_team, game.home_classification)
            away_key = model.key_for(game.away_team, game.away_classification)
            hfa = 0.0 if game.neutral_site else config.elo.home_field
            projections.append(
                Projection(
                    week_label=projection_week,
                    date=game.start_date[:10],
                    home=game.home_team,
                    home_slug=slug_of.get(game.home_team),
                    away=game.away_team,
                    away_slug=slug_of.get(game.away_team),
                    neutral=game.neutral_site,
                    home_win_probability=prob,
                    spread=rating_to_spread(
                        (model.rating(home_key) + hfa) - model.rating(away_key)
                    ),
                )
            )
        projections.sort(key=lambda p: (p.date, -p.favorite_probability))

        # Hang each team's upcoming opponent off its row, for the "This week"
        # column on the rankings table.
        for p in projections:
            if p.home in entries:
                entries[p.home].next_game = NextGame(
                    opponent=p.away,
                    opponent_slug=p.away_slug,
                    location="n" if p.neutral else "vs",
                    win_probability=p.home_win_probability,
                    date=p.date,
                )
            if p.away in entries:
                entries[p.away].next_game = NextGame(
                    opponent=p.home,
                    opponent_slug=p.home_slug,
                    location="n" if p.neutral else "at",
                    win_probability=1 - p.home_win_probability,
                    date=p.date,
                )

    # --- the season's most surprising results
    upsets = _biggest_upsets(season_results, slug_of)

    played = [g for g in games if g.season == season and g.completed]
    through = "no games yet"
    if played:
        last = max(played, key=lambda g: g.sort_key)
        through = (
            f"Week {last.week}" if last.season_type == "regular" else "the postseason"
        )

    return RankingTable(
        season=season,
        generated_at=dt.datetime.now(dt.timezone.utc).strftime("%b %d, %Y at %H:%M UTC"),
        through=through,
        teams=ranked,
        projections=projections,
        projection_week=projection_week,
        biggest_upsets=upsets,
        games_rated=len(season_results),
        previous_label=previous_label,
    )


def _biggest_upsets(
    results: list[GameResult], slug_of: dict[str, str], limit: int = 5
) -> list[dict[str, Any]]:
    upsets = []
    for r in results:
        if not r.upset:
            continue
        home_won = (r.game.margin or 0) > 0
        winner = r.game.home_team if home_won else r.game.away_team
        loser = r.game.away_team if home_won else r.game.home_team
        win_prob = r.home_win_prob if home_won else 1 - r.home_win_prob
        upsets.append(
            {
                "winner": winner,
                "winner_slug": slug_of.get(winner),
                "loser": loser,
                "loser_slug": slug_of.get(loser),
                "score": f"{max(r.game.home_points, r.game.away_points)}-"
                f"{min(r.game.home_points, r.game.away_points)}",
                "probability": win_prob,
                "week": r.game.week,
                "date": r.game.start_date[:10],
            }
        )
    upsets.sort(key=lambda u: u["probability"])
    return upsets[:limit]
