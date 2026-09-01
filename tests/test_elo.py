"""Property tests for the rating engine."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from cfbrank.config import EloConfig
from cfbrank.data import Game
from cfbrank.elo import FCS_POOL, EloModel, mov_multiplier, win_probability
from tests.synthetic import make_teams, simulate_season


def game(home_pts, away_pts, neutral=False, home="A", away="B", classification="fbs"):
    return Game(
        id=1, season=2025, week=1, season_type="regular",
        start_date="2025-09-06T18:00:00Z", completed=True, neutral_site=neutral,
        conference_game=False, home_team=home, home_conference="X",
        home_classification="fbs", home_points=home_pts, away_team=away,
        away_conference="Y", away_classification=classification,
        away_points=away_pts, venue="Stadium",
    )


def model(**overrides):
    cfg = EloConfig(**overrides)
    return EloModel(config=cfg, fbs_teams={"A", "B", "C"})


# ------------------------------------------------------------- probability

def test_even_ratings_are_a_coin_flip():
    assert win_probability(0) == pytest.approx(0.5)


def test_four_hundred_points_is_ten_to_one():
    assert win_probability(400) == pytest.approx(10 / 11, abs=1e-6)


def test_probabilities_are_symmetric():
    for diff in (25, 100, 380):
        assert win_probability(diff) + win_probability(-diff) == pytest.approx(1.0)


# ------------------------------------------------------------------- core

def test_ratings_are_zero_sum():
    m = model()
    result = m.play(game(35, 14))
    total_before = 1500 * 2
    assert m.ratings["A"] + m.ratings["B"] == pytest.approx(total_before)
    assert result.shift > 0


def test_winner_gains_and_loser_loses():
    m = model()
    m.play(game(21, 17))
    assert m.ratings["A"] > 1500
    assert m.ratings["B"] < 1500


def test_home_field_makes_a_home_win_worth_less():
    home_win = model()
    home_win.play(game(28, 21, neutral=False))
    neutral_win = model()
    neutral_win.play(game(28, 21, neutral=True))
    # Winning at home was more expected, so it moves the rating less.
    assert home_win.ratings["A"] < neutral_win.ratings["A"]


def test_bigger_margin_moves_the_rating_more():
    close = model()
    close.play(game(24, 21))
    blowout = model()
    blowout.play(game(56, 3))
    assert blowout.ratings["A"] > close.ratings["A"]


def test_margin_has_diminishing_returns():
    def gain(margin):
        m = model()
        m.play(game(20 + margin, 20))
        return m.ratings["A"] - 1500

    first_14 = gain(14) - gain(7)
    second_14 = gain(28) - gain(21)
    assert second_14 < first_14


def test_upset_moves_more_than_an_expected_win():
    expected = model()
    expected.ratings.update({"A": 1800, "B": 1400})
    expected.play(game(31, 10))

    upset = model()
    upset.ratings.update({"A": 1400, "B": 1800})
    upset.play(game(31, 10))

    assert (upset.ratings["A"] - 1400) > (expected.ratings["A"] - 1800)


def test_mov_multiplier_damps_favorites():
    assert mov_multiplier(21, winner_edge=600) < mov_multiplier(21, winner_edge=-600)


def test_tie_between_equals_on_a_neutral_field_changes_nothing():
    m = model()
    m.play(game(21, 21, neutral=True))
    assert m.ratings["A"] == pytest.approx(1500)
    assert m.ratings["B"] == pytest.approx(1500)


def test_tie_at_home_costs_the_favorite():
    """A home team is favored by construction, so a draw is a bad result."""
    m = model()
    m.play(game(21, 21, neutral=False))
    assert m.ratings["A"] < 1500
    assert m.ratings["B"] > 1500


# -------------------------------------------------------------- FCS pooling

def test_fcs_opponents_are_pooled_and_frozen():
    m = model(freeze_fcs=True)
    m.play(game(49, 7, away="Directional State", classification="fcs"))
    assert "Directional State" not in m.ratings
    assert m.ratings[FCS_POOL] == pytest.approx(1000)


def test_beating_fcs_is_worth_little():
    """A 42-point win over an FCS team must not outweigh a real FBS win."""
    cupcake = model(freeze_fcs=True)
    cupcake.play(game(49, 7, away="Directional State", classification="fcs"))
    cupcake_gain = cupcake.ratings["A"] - 1500

    real = model()
    real.play(game(24, 21))
    real_gain = real.ratings["A"] - 1500

    assert 0 < cupcake_gain < 8
    assert cupcake_gain < real_gain


def test_losing_to_fcs_is_costly():
    m = model(freeze_fcs=True)
    m.play(game(10, 24, away="Directional State", classification="fcs"))
    assert m.ratings["A"] < 1440


# ------------------------------------------------------------- season flow

def test_preseason_regression_pulls_toward_the_mean():
    m = model(preseason_regression=0.5)
    m.ratings["A"] = 1700
    m.regress_to_mean()
    assert m.ratings["A"] == pytest.approx(1600)


def test_full_carryover_keeps_ratings():
    m = model(preseason_regression=1.0)
    m.ratings["A"] = 1700
    m.regress_to_mean()
    assert m.ratings["A"] == pytest.approx(1700)


def test_snapshots_cover_every_week_plus_preseason():
    teams, strength = make_teams(16)
    games = simulate_season(teams, strength, weeks=6, include_future_week=False)
    m = EloModel(config=EloConfig(), fbs_teams=set(teams))
    m.run(games)
    labels = [s.label for s in m.snapshots]
    assert labels[0] == "Preseason"
    assert labels.count("Preseason") == 1
    assert "Week 6" in labels


def test_model_recovers_true_strength_ordering():
    """The whole point: ratings should correlate with real underlying quality."""
    teams, strength = make_teams(48)
    games = simulate_season(teams, strength, weeks=14, include_future_week=False)
    m = EloModel(config=EloConfig(), fbs_teams=set(teams))
    m.run(games)

    ranked = sorted(teams, key=lambda t: -m.ratings[t])
    true_ranked = sorted(teams, key=lambda t: -strength[t])

    top_ten_overlap = len(set(ranked[:10]) & set(true_ranked[:10]))
    assert top_ten_overlap >= 5, f"only {top_ten_overlap}/10 of the real top ten"

    # Rank correlation across the whole field.
    positions = {name: i for i, name in enumerate(ranked)}
    true_positions = {name: i for i, name in enumerate(true_ranked)}
    n = len(teams)
    d_squared = sum((positions[t] - true_positions[t]) ** 2 for t in teams)
    spearman = 1 - (6 * d_squared) / (n * (n * n - 1))
    assert spearman > 0.7, f"rank correlation only {spearman:.2f}"


def test_total_rating_is_conserved_across_a_season():
    teams, strength = make_teams(24)
    # No FCS games and no freezing: the pool must be exactly zero-sum.
    games = [
        g
        for g in simulate_season(teams, strength, weeks=8, include_future_week=False)
        if g.away_classification == "fbs"
    ]
    m = EloModel(config=EloConfig(freeze_fcs=False), fbs_teams=set(teams))
    m.run(games)
    assert sum(m.ratings.values()) == pytest.approx(1500 * len(teams), abs=1e-6)
