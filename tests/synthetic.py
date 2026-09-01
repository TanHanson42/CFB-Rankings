"""A fake season, so the pipeline can be exercised without an API key.

Teams are given a hidden "true strength"; games are simulated from it. A working
rating system should recover roughly that ordering, which makes this useful as
both a test fixture and a smoke test of the whole build.
"""

from __future__ import annotations

import random

from cfbrank.data import Game, Team

CONFERENCES = ["Big Ten", "SEC", "Big 12", "ACC", "Pac-12", "Mountain West"]


def make_teams(count: int = 48, seed: int = 7) -> tuple[dict[str, Team], dict[str, float]]:
    rng = random.Random(seed)
    teams: dict[str, Team] = {}
    strength: dict[str, float] = {}
    for i in range(count):
        name = f"Team {i:02d}"
        conf = CONFERENCES[i % len(CONFERENCES)]
        teams[name] = Team(
            school=name,
            mascot="Testers",
            abbreviation=f"T{i:02d}",
            conference=conf,
            division="fbs",
            color="#2a78d6",
            alt_color="#14140f",
            logo=None,
        )
        # True strength in points-per-game above average.
        strength[name] = rng.gauss(0, 9)
    return teams, strength


def simulate_season(
    teams: dict[str, Team],
    strength: dict[str, float],
    season: int = 2025,
    weeks: int = 12,
    seed: int = 11,
    include_future_week: bool = True,
    bowls: bool = False,
) -> list[Game]:
    """Round-robin-ish schedule with scores drawn from the true strengths."""
    rng = random.Random(seed)
    names = list(teams)
    games: list[Game] = []
    game_id = 1

    for week in range(1, weeks + 1):
        rng.shuffle(names)
        for home, away in zip(names[::2], names[1::2]):
            future = include_future_week and week == weeks
            edge = strength[home] - strength[away] + 2.5  # home field, in points
            margin = round(rng.gauss(edge, 13))
            base = rng.randint(17, 38)
            home_pts = max(0, base + margin // 2)
            away_pts = max(0, base - (margin - margin // 2))
            if home_pts == away_pts:
                home_pts += 3  # college football has no ties

            games.append(
                Game(
                    id=game_id,
                    season=season,
                    week=week,
                    season_type="regular",
                    start_date=f"{season}-09-{min(week, 28):02d}T18:00:00.000Z",
                    completed=not future,
                    neutral_site=False,
                    conference_game=teams[home].conference == teams[away].conference,
                    home_team=home,
                    home_conference=teams[home].conference,
                    home_classification="fbs",
                    home_points=None if future else home_pts,
                    away_team=away,
                    away_conference=teams[away].conference,
                    away_classification="fbs",
                    away_points=None if future else away_pts,
                    venue=f"{home} Stadium",
                )
            )
            game_id += 1

    if bowls:
        # A neutral-site postseason slate, so the bowl-week logic gets exercised.
        rng.shuffle(names)
        for i, (home, away) in enumerate(zip(names[::2], names[1::2])):
            edge = strength[home] - strength[away]
            margin = round(rng.gauss(edge, 13)) or 3
            base = rng.randint(17, 35)
            games.append(
                Game(
                    id=90_000 + i,
                    season=season,
                    week=1,
                    season_type="postseason",
                    start_date=f"{season}-12-28T18:00:00.000Z",
                    completed=True,
                    neutral_site=True,
                    conference_game=False,
                    home_team=home,
                    home_conference=teams[home].conference,
                    home_classification="fbs",
                    home_points=max(0, base + margin // 2),
                    away_team=away,
                    away_conference=teams[away].conference,
                    away_classification="fbs",
                    away_points=max(0, base - (margin - margin // 2)),
                    venue="Neutral Bowl",
                )
            )

    # One FCS tune-up apiece in week 1, so the pooling logic gets exercised.
    for i, name in enumerate(teams):
        games.append(
            Game(
                id=10_000 + i,
                season=season,
                week=1,
                season_type="regular",
                start_date=f"{season}-09-01T18:00:00.000Z",
                completed=True,
                neutral_site=False,
                conference_game=False,
                home_team=name,
                home_conference=teams[name].conference,
                home_classification="fbs",
                home_points=45,
                away_team=f"Directional State {i}",
                away_conference="Big Sky",
                away_classification="fcs",
                away_points=10,
                venue=f"{name} Stadium",
            )
        )
    return games
