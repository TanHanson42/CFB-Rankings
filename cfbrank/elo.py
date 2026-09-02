"""THE RATING ENGINE - this is the heart of the whole project.

=============================================================================
WHAT THIS FILE DOES, IN PLAIN ENGLISH
=============================================================================

Every team carries a single number, called its rating. Everyone starts at 1500.

Before a game, the gap between the two teams' numbers is a prediction. After the
game, the winner takes points from the loser. How many points depends entirely
on how *surprising* the result was:

    Beat a team you were supposed to beat  ->  you gain almost nothing.
                                               The system already knew.
    Beat a team nobody expected you to     ->  you take a lot.
                                               The system just learned something.

Losing works the same way in reverse. Losing to a great team costs you very
little; losing to a bad one is brutal.

Nothing else feeds in. No preseason polls, no recruiting rankings, no conference
reputation, no eye test. Only who played whom, where, and what the score was.

On top of that basic idea, this file adds two refinements:

  1. MARGIN OF VICTORY - a three-score win counts for more than a last-second
     field goal, but with sharply diminishing returns (a 49-point win is barely
     worth more than a 35-point win).

  2. FAVORITE DAMPING - that margin bonus is shrunk when a heavy favorite wins,
     so good teams can't inflate their rating by running up the score on bad
     ones.

=============================================================================
THE ONE FORMULA
=============================================================================

    rating change  =  K  x  margin multiplier  x  (what happened - what we expected)

    K ................... the size dial. Set in config.yaml, currently 45.
    margin multiplier ... 0.69 to 3.91, based on the final score.
    what happened ....... 1 if you won, 0 if you lost, 0.5 for a tie.
    what we expected .... your win probability before kickoff, 0.0 to 1.0.

That last term is where nearly all the action is. Winning as a 10% underdog is
worth about 34x as much as winning as a 96% favorite.
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

# Teams below the FBS level (FCS, Division II) don't get their own rating.
# They're all lumped together into one imaginary opponent stored under this
# name. See key_for() below for why.
FCS_POOL = "__FCS__"

# The constant that defines what a rating gap *means*. With SCALE = 400, a team
# rated 400 points higher than its opponent is expected to win about 91% of the
# time (10-to-1 odds). This is the standard value chess Elo has used since the
# 1960s; changing it would rescale every number on the site.
SCALE = 400.0


def win_probability(rating_diff: float) -> float:
    """Turn a rating gap into a win probability.

    Pass in (your rating - their rating) and get back your chance of winning,
    as a number between 0 and 1.

        win_probability(0)    -> 0.50   dead even, a coin flip
        win_probability(100)  -> 0.64   you're a solid favorite
        win_probability(400)  -> 0.91   you're a heavy favorite

    The S-curve shape matters: the difference between being 0 and 100 points
    better is large, while the difference between 700 and 800 barely registers,
    because you were already nearly certain to win.
    """
    return 1.0 / (1.0 + 10 ** (-rating_diff / SCALE))


def mov_multiplier(margin: int, winner_edge: float) -> float:
    """Decide how much a result counts, based on how lopsided the score was.

    Returns a number that the rating change gets multiplied by. Two separate
    ideas are packed into this one line, so here they are one at a time.

    PART ONE - log(margin + 1)
        Bigger wins count for more, but with hard diminishing returns:

            win by 1  -> 0.69      win by 21 -> 3.09
            win by 7  -> 2.08      win by 35 -> 3.58
            win by 14 -> 2.71      win by 49 -> 3.91

        Going from a 1-point win to a 7-point win triples the value. Going from
        35 to 49 - two more touchdowns - adds about 9%. This is deliberate:
        running up the score should be nearly worthless.

    PART TWO - the (2.2 / (winner_edge * 0.001 + 2.2)) fraction
        `winner_edge` is how much better the WINNER was rated before kickoff
        (a negative number if the underdog won). This fraction shrinks the
        multiplier when a big favorite wins, and grows it when an underdog does:

            underdog by 400 -> 1.22x the normal amount
            evenly matched  -> 1.00x
            favorite by 600 -> 0.79x

        Without this correction, strong teams would climb forever simply by
        blowing out weak opponents, and the ratings would drift apart with no
        connection to reality. Statisticians call this an autocorrelation
        correction; in practice it's the anti-score-running rule.
    """
    # A tie has no margin to scale, so it just counts once, normally.
    if margin == 0:
        return 1.0
    return math.log(abs(margin) + 1.0) * (2.2 / (winner_edge * 0.001 + 2.2))


@dataclass
class GameResult:
    """One played game, with a record of what it did to both teams' ratings.

    Kept so the website can show a game log with "this game was worth +12.5 to
    you" next to every result, instead of only the final number.
    """

    game: Game
    home_key: str          # who the home team was, for rating lookup
    away_key: str
    home_pre: float        # home team's rating BEFORE this game
    away_pre: float
    home_post: float       # ...and AFTER
    away_post: float
    home_win_prob: float   # what we thought would happen, 0.0 to 1.0
    shift: float           # rating points the home team gained (negative = lost)

    @property
    def upset(self) -> bool:
        """True if the team that was expected to lose actually won."""
        margin = self.game.margin
        if margin is None or margin == 0:
            return False
        home_won = margin > 0
        # An upset is: the winner had less than a 50% chance beforehand.
        return (self.home_win_prob < 0.5) if home_won else (self.home_win_prob > 0.5)


@dataclass
class Snapshot:
    """A photograph of every team's rating at one moment in time.

    One of these is saved after every week of games. That's what makes the
    rating-history chart on each team page possible, and what lets the site say
    "up 3 spots since last week" - you can't compute movement without a record
    of where things stood before.
    """

    season: int
    season_type: str       # "preseason", "regular", or "postseason"
    week: int
    label: str             # human-readable: "Preseason", "Week 7", "Bowls"
    ratings: dict[str, float]


@dataclass
class EloModel:
    """Holds every team's rating and applies games to it, one at a time.

    Typical use is not to build this by hand but to call build_model() at the
    bottom of this file, which creates one and feeds it a decade of games.
    """

    config: EloConfig                                   # the dials from config.yaml
    fbs_teams: set[str] = field(default_factory=set)    # who counts as FBS this year
    ratings: dict[str, float] = field(default_factory=dict)      # team name -> rating
    results: list[GameResult] = field(default_factory=list)      # every game applied
    snapshots: list[Snapshot] = field(default_factory=list)      # weekly photographs
    games_played: dict[str, int] = field(default_factory=lambda: defaultdict(int))

    # ---------------------------------------------------------------- lookups

    def key_for(self, team: str, classification: str | None) -> str:
        """Decide which rating bucket a team's name belongs to.

        FBS teams get their own rating. Everyone below that level - all several
        hundred FCS and Division II schools - shares a single pooled rating.

        Why pool them? Two reasons. There are far too many of them to rate
        meaningfully off one or two games a year against FBS opponents, and
        pooling stops a team from farming rating by scheduling a string of
        cupcakes: they're all treated as the same fixed-strength opponent.
        """
        if classification:
            return team if classification.lower() == "fbs" else FCS_POOL
        # No classification in the data? Fall back to the FBS roster we loaded.
        return team if team in self.fbs_teams else FCS_POOL

    def rating(self, key: str) -> float:
        """Look up a rating, creating it at the starting value if it's new.

        A team appearing for the first time (a new program, or the first season
        we have data for) starts at config.initial_rating, normally 1500.
        """
        if key == FCS_POOL:
            return self.ratings.setdefault(key, self.config.fcs_rating)
        return self.ratings.setdefault(key, self.config.initial_rating)

    def regress_to_mean(self) -> None:
        """Between seasons, pull every rating partway back toward average.

        This is the only place the model ever forgets anything.

        Rosters turn over by roughly a quarter every year - seniors graduate,
        players transfer, coaches leave - so last season's rating is real
        evidence but not the whole story. With the default setting of 0.75, a
        team finishing 300 points above average starts the next season 225
        above it. Set it to 0.0 in config.yaml and every team resets to 1500
        each August; set it to 1.0 and the model never forgets a thing.
        """
        keep = self.config.preseason_regression
        mean = self.config.initial_rating
        for key in list(self.ratings):
            # The pooled FCS opponent is a fixed reference point, not a real
            # team, so it goes back to its configured value rather than drifting.
            if key == FCS_POOL:
                self.ratings[key] = self.config.fcs_rating
                continue
            self.ratings[key] = mean + keep * (self.ratings[key] - mean)

    # ------------------------------------------------------------------- core

    def play(self, game: Game) -> GameResult | None:
        """Apply ONE game and move both teams' ratings.

        This is the function everything else exists to support. Returns None if
        the game can't be rated (not played yet, or two non-FBS teams).
        """
        # Step 0: skip anything that hasn't actually been played.
        if not game.completed or game.home_points is None or game.away_points is None:
            return None

        home_key = self.key_for(game.home_team, game.home_classification)
        away_key = self.key_for(game.away_team, game.away_classification)
        # Two non-FBS teams playing each other tells us nothing about FBS.
        if home_key == FCS_POOL and away_key == FCS_POOL:
            return None

        # Step 1: what were both teams rated before kickoff?
        home_pre = self.rating(home_key)
        away_pre = self.rating(away_key)

        # Step 2: give the home team its home-field bonus (nobody gets it at a
        # neutral site like a bowl game), then work out the expected result.
        hfa = 0.0 if game.neutral_site else self.config.home_field
        home_edge = (home_pre + hfa) - away_pre
        expected_home = win_probability(home_edge)

        # Step 3: what actually happened? 1.0 = home won, 0.0 = home lost,
        # 0.5 = tie. We also note the WINNER's pregame edge, which the margin
        # multiplier needs in order to damp blowouts by big favorites.
        margin = game.home_points - game.away_points
        if margin > 0:
            actual_home, winner_edge = 1.0, home_edge
        elif margin < 0:
            actual_home, winner_edge = 0.0, -home_edge
        else:
            actual_home, winner_edge = 0.5, 0.0

        # Step 4: the formula from the top of this file.
        # Turn margin_of_victory off in config.yaml and every win counts the
        # same regardless of score.
        multiplier = (
            mov_multiplier(margin, winner_edge) if self.config.margin_of_victory else 1.0
        )
        shift = self.config.k * multiplier * (actual_home - expected_home)

        # Step 5: move both ratings. This is zero-sum - exactly what one team
        # gains, the other loses - so the total amount of rating in college
        # football never inflates over time.
        home_post = home_pre + shift
        away_post = away_pre - shift

        # The pooled FCS rating is normally frozen. Otherwise FBS teams would
        # beat it ~95% of the time all season and grind it into the floor,
        # which would slowly make cupcake wins look impressive again.
        if self.config.freeze_fcs:
            if home_key == FCS_POOL:
                home_post = home_pre
            if away_key == FCS_POOL:
                away_post = away_pre

        self.ratings[home_key] = home_post
        self.ratings[away_key] = away_post
        self.games_played[home_key] += 1
        self.games_played[away_key] += 1

        # Keep a record of what this game did, for the team-page game logs.
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
        """Save a copy of every rating right now, labelled for display."""
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
                # dict(...) makes a COPY. Without it, every snapshot would point
                # at the same live dictionary and they'd all show today's
                # numbers - the chart would be a flat line.
                ratings=dict(self.ratings),
            )
        )

    def run(self, games: Iterable[Game]) -> None:
        """Play an entire history of games in order, from oldest to newest.

        This is the main loop. Hand it every game from 2015 to today and it
        walks through them chronologically, applying each one, saving a snapshot
        at the end of each week, and regressing ratings toward average whenever
        it crosses from one season into the next.
        """
        # Only completed games. Scheduled-but-unplayed games are filtered out
        # HERE rather than inside play(), because a future week would otherwise
        # still trigger a snapshot - producing a duplicate of the last real week
        # and flattening every team's week-over-week movement to zero. (That was
        # a real bug; there's a test for it now.)
        games = sorted(
            (g for g in games if g.completed and g.margin is not None),
            key=lambda g: g.sort_key,
        )

        current_season: int | None = None
        current_week: tuple | None = None

        for game in games:
            # --- Did we just cross into a new season? ---
            if current_season is not None and game.season != current_season:
                self.regress_to_mean()
                log.info("season %d -> %d: regressed ratings to mean", current_season, game.season)
            if current_season != game.season:
                current_season = game.season
                current_week = None
                # Save where everyone stood before a snap was played this year.
                # This is the "Preseason" point on every rating chart.
                self.snapshot(current_season, "preseason", 0)

            # --- Did we just cross into a new week? ---
            # If so, photograph the ratings as the previous week left them.
            week_key = (game.season_type, game.week)
            if current_week is not None and week_key != current_week:
                self.snapshot(current_season, current_week[0], current_week[1])
            current_week = week_key

            self.play(game)

        # The loop above only snapshots when a week ENDS, so the final week
        # needs one on the way out.
        if current_season is not None and current_week is not None:
            self.snapshot(current_season, current_week[0], current_week[1])


def build_model(
    games: Iterable[Game], teams: dict[str, Team], config: EloConfig
) -> EloModel:
    """Create a model and run every game through it. The usual entry point."""
    model = EloModel(config=config, fbs_teams=set(teams))
    model.run(games)
    return model


def predict(model: EloModel, game: Game) -> float:
    """Chance the home team wins a game that hasn't been played yet.

    Same math as play() uses to set expectations - just stopped before anyone's
    rating moves. This is what fills the "This week" column on the rankings
    table and the projections list at the bottom of the page.
    """
    home_key = model.key_for(game.home_team, game.home_classification)
    away_key = model.key_for(game.away_team, game.away_classification)
    hfa = 0.0 if game.neutral_site else model.config.home_field
    return win_probability((model.rating(home_key) + hfa) - model.rating(away_key))


def rating_to_spread(rating_diff: float) -> float:
    """Convert a rating gap into a betting-style point spread.

    Roughly 25 Elo points per point of spread is the long-standing rule of
    thumb. It also squares with the home field setting: 62 Elo points of home
    advantage works out to about 2.5 points, which is close to what
    sportsbooks have historically used for college football.
    """
    return rating_diff / 25.0
