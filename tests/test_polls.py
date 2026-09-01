"""Tests for AP poll parsing and the Elo-vs-AP comparison."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from cfbrank.config import EloConfig
from cfbrank.data import CFBDClient
from cfbrank.elo import EloModel
from cfbrank.polls import (
    Poll,
    PollEntry,
    ap_polls,
    compare_season,
    compare_week,
    elo_top_n,
    season_biases,
)
from tests.synthetic import make_teams, simulate_season


def poll(week, schools, season_type="regular", name="AP Top 25"):
    return Poll(
        season=2025,
        season_type=season_type,
        week=week,
        name=name,
        ranks=tuple(
            PollEntry(rank=i + 1, school=s, conference="X", first_place_votes=0, points=0)
            for i, s in enumerate(schools)
        ),
    )


@pytest.fixture(scope="module")
def season():
    teams, strength = make_teams(48)
    games = simulate_season(teams, strength, season=2025, weeks=12, include_future_week=False)
    model = EloModel(config=EloConfig(), fbs_teams=set(teams))
    model.run(games)
    return model, teams


# ------------------------------------------------------------------ parsing

def test_parses_camelcase_and_snakecase_poll_rows():
    """CFBD has shipped both spellings; the reader must not care."""
    camel = CFBDClient._parse_team({"school": "A", "logos": []})
    assert camel.school == "A"

    from cfbrank.data import _pick
    assert _pick({"firstPlaceVotes": 12}, "firstPlaceVotes", "first_place_votes") == 12
    assert _pick({"first_place_votes": 9}, "firstPlaceVotes", "first_place_votes") == 9


def test_ap_filter_ignores_other_polls():
    polls = [
        poll(3, ["A", "B"]),
        poll(3, ["B", "A"], name="Coaches Poll"),
        poll(4, [], name="AP Top 25"),  # empty release
    ]
    kept = ap_polls(polls)
    assert len(kept) == 1
    assert kept[0].name == "AP Top 25"


def test_poll_exposes_order_and_lookup():
    p = poll(5, ["Georgia", "Ohio State", "Texas"])
    assert p.schools == ["Georgia", "Ohio State", "Texas"]
    assert p.rank_of["Ohio State"] == 2


# --------------------------------------------------------------- comparison

def test_elo_top_n_reads_a_snapshot(season):
    model, teams = season
    top = elo_top_n(model.snapshots[-1], set(teams), n=10)
    assert len(top) == 10
    ratings = [model.snapshots[-1].ratings[s] for s in top]
    assert ratings == sorted(ratings, reverse=True)


def test_identical_rankings_agree_completely(season):
    model, teams = season
    snap = model.snapshots[-1]
    top25 = elo_top_n(snap, set(teams), 25)
    result = compare_week(poll(13, top25), snap, set(teams))
    assert result.overlap == 25
    assert result.mean_abs_gap == 0
    assert result.elo_only == [] and result.ap_only == []


def test_reversed_rankings_disagree_maximally(season):
    model, teams = season
    snap = model.snapshots[-1]
    top25 = elo_top_n(snap, set(teams), 25)
    result = compare_week(poll(13, list(reversed(top25))), snap, set(teams))
    assert result.overlap == 25
    assert result.mean_abs_gap > 10


def test_gap_sign_says_who_liked_the_team_more(season):
    model, teams = season
    snap = model.snapshots[-1]
    top25 = elo_top_n(snap, set(teams), 25)
    # Move the Elo #1 down to last in the AP poll.
    reordered = top25[1:] + [top25[0]]
    result = compare_week(poll(13, reordered), snap, set(teams))
    top_team = next(d for d in result.elo_higher if d.school == top25[0])
    assert top_team.gap > 0
    assert top_team.direction == "Elo higher"


def test_teams_outside_the_poll_get_a_floor_not_a_guess(season):
    model, teams = season
    snap = model.snapshots[-1]
    top25 = elo_top_n(snap, set(teams), 25)
    # AP ranks 25 teams that exclude the Elo #1 entirely.
    others = [s for s in teams if s not in top25[:1]][:25]
    result = compare_week(poll(13, others), snap, set(teams))
    unranked = [d for d in result.elo_higher if d.ap_rank is None]
    assert unranked, "a team in the Elo top 25 but unranked by the AP should surface"
    assert all(d.gap <= 25 for d in unranked), "gaps must not be invented past the poll size"


def test_offset_lines_the_poll_up_with_the_prior_week(season):
    model, teams = season
    week_5 = next(s for s in model.snapshots if s.label == "Week 5")
    polls = [poll(6, elo_top_n(week_5, set(teams), 25))]
    result = compare_season(model, teams, polls, 2025, offset=1)[0]
    assert result.elo_label == "Week 5"
    assert result.overlap == 25, "offset=1 should line week 6's poll up with week 5's Elo"


def test_offset_zero_uses_the_same_week(season):
    model, teams = season
    week_6 = next(s for s in model.snapshots if s.label == "Week 6")
    polls = [poll(6, elo_top_n(week_6, set(teams), 25))]
    result = compare_season(model, teams, polls, 2025, offset=0)[0]
    assert result.elo_label == "Week 6"


def test_preseason_poll_maps_to_preseason_ratings(season):
    model, teams = season
    polls = [poll(1, ["Team 00", "Team 01"])]
    result = compare_season(model, teams, polls, 2025, offset=1)[0]
    assert result.elo_label == "Preseason"


def test_final_poll_uses_the_last_snapshot(season):
    model, teams = season
    polls = [poll(1, ["Team 00"], season_type="postseason")]
    result = compare_season(model, teams, polls, 2025, offset=1)[0]
    assert result.poll_label == "AP final"
    assert result.elo_label == model.snapshots[-1].label


@pytest.fixture(scope="module")
def season_with_bowls():
    teams, strength = make_teams(48)
    games = simulate_season(
        teams, strength, season=2025, weeks=12, include_future_week=False, bowls=True
    )
    model = EloModel(config=EloConfig(), fbs_teams=set(teams))
    model.run(games)
    return model, teams


def test_bowl_games_produce_their_own_snapshot(season_with_bowls):
    model, _teams = season_with_bowls
    assert model.snapshots[-1].label == "Bowls"
    assert model.snapshots[-1].season_type == "postseason"


def test_final_poll_is_compared_against_post_bowl_ratings(season_with_bowls):
    """Postseason weeks restart at 1, so picking the snapshot by highest week
    number silently compared the final poll against pre-bowl ratings."""
    model, teams = season_with_bowls
    polls = [poll(1, ["Team 00"], season_type="postseason")]
    result = compare_season(model, teams, polls, 2025, offset=1)[0]

    assert result.elo_label == "Bowls", "the final poll must see bowl results"
    last_regular = [s for s in model.snapshots if s.season_type == "regular"][-1]
    assert result.elo_label != last_regular.label


def test_bowls_actually_move_the_final_top_25(season_with_bowls):
    """If the bowl snapshot were identical to the regular season one, the fix
    above would be untestable - confirm the postseason really changes things."""
    model, teams = season_with_bowls
    last_regular = [s for s in model.snapshots if s.season_type == "regular"][-1]
    bowls = model.snapshots[-1]
    assert elo_top_n(bowls, set(teams), 25) != elo_top_n(last_regular, set(teams), 25)


def test_season_biases_find_persistent_disagreement(season):
    model, teams = season
    snap = model.snapshots[-1]
    top25 = elo_top_n(snap, set(teams), 25)
    demoted = top25[0]
    weeks = [
        compare_week(poll(w, top25[1:] + [demoted]), snap, set(teams))
        for w in range(5, 12)
    ]
    biases = season_biases(weeks)
    worst = biases[0]
    assert worst.school == demoted
    assert worst.mean_gap > 0
    assert worst.direction == "Elo higher"


def test_season_biases_ignore_one_week_wonders(season):
    model, teams = season
    snap = model.snapshots[-1]
    top25 = elo_top_n(snap, set(teams), 25)
    weeks = [compare_week(poll(5, top25), snap, set(teams))]
    assert season_biases(weeks, min_weeks=3) == []
