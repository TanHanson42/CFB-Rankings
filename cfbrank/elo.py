"""The Elo engine.

Elo in one paragraph: every team carries a single number. Before a game, the gap
between the two numbers (plus a home field bump) implies a win probability. After
the game, the winner takes points from the loser - a lot if the result was a
surprise, very few if it was expected. Nothing else feeds in: no preseason polls,
no recruiting rankings, no human opinion. A team's rating is the accumulated
residue of who it played and what happened.

Two refinements on top of textbook Elo:

* **Margin of victory** scales the swing by log(margin + 1), so a three-score win
  counts for more than a walk-off field goal, with diminishing returns.
* That multiplier is **damped for favorites**, which stops good teams from
  farming rating against bad ones and keeps the system from rewarding
  running up the score.
"""

from __future__ import annotations

import logging
import math
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Iterable

from .config import EloConfig
from .data import Game, Team

log = logging.getLogger(__name__)

#: Every non-FBS opponent is pooled into this one virtual team.
FCS_POOL = "__FCS__"

#: Elo's logistic scale: a 400-point edge is 10:1 odds.
SCALE = 400.0


def win_probability(rating_diff: float) -> float:
    """Probability the higher-rated side wins, given a rating difference."""
    return 1.0 / (1.0 + 10 ** (-rating_diff / SCALE))


def mov_multiplier(margin: int, winner_edge: float) -> float:
    """How much to amplify a result based on how lopsided it was.

    ``winner_edge`` is the winner's pregame rating advantage (negative for an
    upset). The denominator is the autocorrelation correction: without it,
    already-strong teams gain rating simply by blowing out weak opponents, and
    ratings run away from each other.
    """
    if margin == 0:
        return 1.0
    return math.log(abs(margin) + 1.0) * (2.2 / (winner_edge * 0.001 + 2.2))


@dataclass
class GameResult:
    """A played game, annotated with what it did to both ratings."""

    game: Game
    home_key: str
    away_key: str
    home_pre: float
    away_pre: float
    home_post: float
    away_post: float
    home_win_prob: float
    shift: float

    @property
    def upset(self) -> bool:
        """Did the pregame underdog win?"""
        margin = self.game.margin
        if margin is None or margin == 0:
            return False
        home_won = margin > 0
        return (self.home_win_prob < 0.5) if home_won else (self.home_win_prob > 0.5)


@dataclass
class Snapshot:
    """Every team's rating at the end of one week."""

    season: int
    season_type: str
    week: int
    label: str
    ratings: dict[str, float]


@dataclass
class EloModel:
    config: EloConfig
    fbs_teams: set[str] = field(default_factory=set)
    ratings: dict[str, float] = field(default_factory=dict)
    results: list[GameResult] = field(default_factory=list)
    snapshots: list[Snapshot] = field(default_factory=list)
    games_played: dict[str, int] = field(default_factory=lambda: defaultdict(int))

    # ------------------------------------------------------------------ setup

    def key_for(self, team: str, classification: str | None) -> str:
        """Map a team name to a rating key, pooling everyone below FBS."""
        if classification:
            return team if classification.lower() == "fbs" else FCS_POOL
        return team if team in self.fbs_teams else FCS_POOL

    def rating(self, key: str) -> float:
        if key == FCS_POOL:
            return self.ratings.setdefault(key, self.config.fcs_rating)
        return self.ratings.setdefault(key, self.config.initial_rating)

    def regress_to_mean(self) -> None:
        """Between seasons, pull every rating partway back toward average.

        Rosters turn over ~25% a year, so last season's rating is informative
        but not the whole story. This is the only place the model "forgets."
        """
        keep = self.config.preseason_regression
        mean = self.config.initial_rating
        for key in list(self.ratings):
            if key == FCS_POOL:
                self.ratings[key] = self.config.fcs_rating
                continue
            self.ratings[key] = mean + keep * (self.ratings[key] - mean)

    # ------------------------------------------------------------------- core

    def play(self, game: Game) -> GameResult | None:
        """Apply one completed game. Returns None for anything unplayed."""
        if not game.completed or game.home_points is None or game.away_points is None:
            return None

        home_key = self.key_for(game.home_team, game.home_classification)
        away_key = self.key_for(game.away_team, game.away_classification)
        if home_key == FCS_POOL and away_key == FCS_POOL:
            return None  # two non-FBS teams: not our business

        home_pre = self.rating(home_key)
        away_pre = self.rating(away_key)

        hfa = 0.0 if game.neutral_site else self.config.home_field
        home_edge = (home_pre + hfa) - away_pre
        expected_home = win_probability(home_edge)

        margin = game.home_points - game.away_points
        if margin > 0:
            actual_home, winner_edge = 1.0, home_edge
        elif margin < 0:
            actual_home, winner_edge = 0.0, -home_edge
        else:
            actual_home, winner_edge = 0.5, 0.0

        multiplier = (
            mov_multiplier(margin, winner_edge) if self.config.margin_of_victory else 1.0
        )
        shift = self.config.k * multiplier * (actual_home - expected_home)

        # Zero-sum: what one side gains, the other loses. The pooled FCS rating
        # is optionally held fixed so it can't be ground down over a season.
        home_post = home_pre + shift
        away_post = away_pre - shift
        if self.config.freeze_fcs:
            if home_key == FCS_POOL:
                home_post = home_pre
            if away_key == FCS_POOL:
                away_post = away_pre

        self.ratings[home_key] = home_post
        self.ratings[away_key] = away_post
        self.games_played[home_key] += 1
        self.games_played[away_key] += 1

        result = GameResult(
            game=game,
            home_key=home_key,
            away_key=away_key,
            home_pre=home_pre,
            away_pre=away_pre,
            home_post=home_post,
            away_post=away_post,
            home_win_prob=expected_home,
            shift=shift,
        )
        self.results.append(result)
        return result

    def snapshot(self, season: int, season_type: str, week: int) -> None:
        if season_type == "preseason":
            label = "Preseason"
        elif season_type == "regular":
            label = f"Week {week}"
        else:
            label = "Bowls"
        self.snapshots.append(
            Snapshot(
                season=season,
                season_type=season_type,
                week=week,
                label=label,
                ratings=dict(self.ratings),
            )
        )

    def run(self, games: Iterable[Game]) -> None:
        """Walk every played game in chronological order, snapshotting each week.

        Scheduled-but-unplayed games are dropped here rather than inside
        :meth:`play`, so a future week can't produce a duplicate snapshot that
        would flatten every team's week-over-week movement to zero.
        """
        games = sorted(
            (g for g in games if g.completed and g.margin is not None),
            key=lambda g: g.sort_key,
        )
        current_season: int | None = None
        current_week: tuple | None = None

        for game in games:
            if current_season is not None and game.season != current_season:
                self.regress_to_mean()
                log.info("season %d -> %d: regressed ratings to mean", current_season, game.season)
            if current_season != game.season:
                current_season = game.season
                current_week = None
                # Ratings as they stood before a snap was played this year.
                self.snapshot(current_season, "preseason", 0)

            week_key = (game.season_type, game.week)
            if current_week is not None and week_key != current_week:
                self.snapshot(current_season, current_week[0], current_week[1])
            current_week = week_key

            self.play(game)

        if current_season is not None and current_week is not None:
            self.snapshot(current_season, current_week[0], current_week[1])


def build_model(
    games: Iterable[Game], teams: dict[str, Team], config: EloConfig
) -> EloModel:
    model = EloModel(config=config, fbs_teams=set(teams))
    model.run(games)
    return model


def predict(model: EloModel, game: Game) -> float:
    """Home win probability for a game that hasn't been played yet."""
    home_key = model.key_for(game.home_team, game.home_classification)
    away_key = model.key_for(game.away_team, game.away_classification)
    hfa = 0.0 if game.neutral_site else model.config.home_field
    return win_probability((model.rating(home_key) + hfa) - model.rating(away_key))


def rating_to_spread(rating_diff: float) -> float:
    """Convert an Elo edge to a point spread.

    ~25 Elo points per point of spread is the standard rule of thumb, and it
    lines up well with the home field value in config.yaml.
    """
    return rating_diff / 25.0
