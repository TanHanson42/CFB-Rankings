"""End-to-end: synthetic season in, rendered site out."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from cfbrank.config import Config, EloConfig, SiteConfig
from cfbrank.elo import build_model
from cfbrank.rankings import build_rankings, slugify
from cfbrank.site import line_chart, render_site
from tests.synthetic import make_teams, simulate_season


@pytest.fixture(scope="module")
def built():
    teams, strength = make_teams(48)
    games = simulate_season(teams, strength, season=2025, weeks=12)
    config = Config(
        season=2025,
        history_start=2025,
        elo=EloConfig(),
        site=SiteConfig(title="Test Rankings", subtitle="synthetic", author="tests"),
    )
    model = build_model(games, teams, config.elo)
    table = build_rankings(model, games, teams, config)
    return table, config


def test_every_fbs_team_is_ranked_exactly_once(built):
    table, _ = built
    assert len(table.teams) == 48
    assert [t.rank for t in table.teams] == list(range(1, 49))


def test_ranks_follow_ratings(built):
    table, _ = built
    ratings = [t.rating for t in table.teams]
    assert ratings == sorted(ratings, reverse=True)


def test_records_add_up(built):
    table, _ = built
    for team in table.teams:
        played = team.wins + team.losses + team.ties
        assert played == len(team.games)
        assert played >= 11  # 11 played weeks + the FCS game, minus byes


def test_fcs_opponents_do_not_get_team_pages(built):
    table, _ = built
    for team in table.teams:
        for g in team.games:
            if g.opponent.startswith("Directional State"):
                assert g.opponent_slug is None


def test_history_starts_at_the_preseason(built):
    table, _ = built
    team = table.teams[0]
    assert team.history[0][0] == "Preseason"
    assert len(team.history) >= 10


def test_projections_exist_for_the_unplayed_week(built):
    table, _ = built
    assert table.projections, "week 12 was left unplayed and should be projected"
    assert table.projection_week == "Week 12"
    for p in table.projections:
        assert 0 < p.home_win_probability < 1
        assert p.favorite_probability >= 0.5


def test_strength_of_schedule_is_ranked(built):
    table, _ = built
    sos_ranks = sorted(t.sos_rank for t in table.teams)
    assert sos_ranks == list(range(1, 49))


def test_slugify_handles_punctuation_and_accents():
    assert slugify("Texas A&M") == "texas-am"
    assert slugify("Hawai'i") == "hawaii"
    assert slugify("San José State") == "san-jose-state"


def test_line_chart_geometry():
    chart = line_chart([("Preseason", 1500), ("Week 1", 1530), ("Week 2", 1490)])
    assert chart is not None
    assert len(chart.points) == 3
    assert chart.path.startswith("M ")
    assert chart.area.endswith("Z")
    # Higher rating must sit higher on screen (smaller y).
    assert chart.points[1]["y"] < chart.points[0]["y"] < chart.points[2]["y"]


def test_line_chart_needs_two_points():
    assert line_chart([("Preseason", 1500)]) is None


def test_render_writes_a_complete_site(built, tmp_path):
    table, config = built
    out = render_site(table, config, tmp_path / "site")

    index = (out / "index.html").read_text(encoding="utf-8")
    assert "Full rankings" in index
    assert table.teams[0].school in index
    assert "Week 12 projections" in index

    assert (out / "methodology.html").exists()
    assert (out / ".nojekyll").exists()
    assert (out / "assets" / "style.css").exists()
    assert (out / "assets" / "app.js").exists()

    # One shell page for every team, not one file per team.
    assert (out / "team.html").exists()
    assert not (out / "team").exists()
    team_html = (out / "team.html").read_text(encoding="utf-8")
    assert "Game log" in team_html
    assert "rating-chart" in team_html

    teams = json.loads((out / "data" / "teams.json").read_text(encoding="utf-8"))
    assert len(teams["teams"]) == 48
    first = teams["teams"][table.teams[0].slug]
    assert first["rank"] == 1
    assert first["games"] and first["chart"]["path"].startswith("M ")


def test_published_site_stays_a_handful_of_files(built, tmp_path):
    """The whole point of the JSON-driven team page: a flat, small output."""
    table, config = built
    out = render_site(table, config, tmp_path / "count")
    files = [p for p in out.rglob("*") if p.is_file()]
    assert len(files) < 20, f"{len(files)} files: {[p.name for p in files][:30]}"


def test_rebuild_clears_old_per_team_pages(built, tmp_path):
    """Upgrading an existing checkout must not leave 138 orphans behind."""
    table, config = built
    out = tmp_path / "legacy"
    (out / "team").mkdir(parents=True)
    (out / "team" / "stale-team.html").write_text("old", encoding="utf-8")
    render_site(table, config, out)
    assert not (out / "team").exists()


def test_every_team_link_resolves_to_a_real_slug(built, tmp_path):
    import re

    table, config = built
    out = render_site(table, config, tmp_path / "links")
    teams = json.loads((out / "data" / "teams.json").read_text(encoding="utf-8"))["teams"]
    index = (out / "index.html").read_text(encoding="utf-8")
    linked = set(re.findall(r'team\.html\?t=([a-z0-9\-]+)', index))
    assert linked, "the index should link to team pages"
    assert linked <= set(teams), f"dead links: {sorted(linked - set(teams))}"

    data = json.loads((out / "data" / "rankings.json").read_text(encoding="utf-8"))
    assert len(data["rankings"]) == 48
    assert data["rankings"][0]["rank"] == 1

    csv = (out / "data" / "rankings.csv").read_text(encoding="utf-8")
    assert csv.splitlines()[0].startswith("rank,team")
    assert len(csv.strip().splitlines()) == 49


def test_no_unrendered_template_syntax(built, tmp_path):
    table, config = built
    out = render_site(table, config, tmp_path / "site2")
    for path in out.rglob("*.html"):
        text = path.read_text(encoding="utf-8")
        assert "{{" not in text and "{%" not in text, f"unrendered Jinja in {path.name}"
