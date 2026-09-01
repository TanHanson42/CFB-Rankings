"""BUILDING THE WEBSITE.

Takes the finished rankings and writes out the actual web pages that GitHub
Pages serves. The output is completely static - plain HTML, CSS and JavaScript
files with no server behind them - which is why hosting it is free and it can
never go down under load.

Nine files come out of here:

    index.html          the rankings table, movers, and projections
    team.html           one page that renders ANY team, filled in from...
    data/teams.json     ...this, every team's history, game log and chart
    methodology.html    the plain-English explanation of the model
    assets/style.css    all the styling
    assets/app.js       sorting, filtering, theme toggle, team rendering
    data/rankings.csv   the ratings as a spreadsheet
    data/rankings.json  the ratings for other programs to read
    .nojekyll           tells GitHub not to reprocess our HTML

The one non-obvious piece is line_chart() below: the rating-history chart's
geometry is calculated here, in Python, and shipped to the browser as a list of
coordinates. The browser only draws the shape it's handed. Keeping the math on
this side means it can be tested, which chart code in JavaScript usually isn't.
"""

from __future__ import annotations

import json
import logging
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, select_autoescape

from .config import Config, REPO_ROOT
from .rankings import RankingTable, TeamRanking

__all__ = ["render_site", "line_chart", "Chart", "DEFAULT_OUTPUT"]

log = logging.getLogger(__name__)

TEMPLATE_DIR = Path(__file__).parent / "templates"
STATIC_DIR = Path(__file__).parent / "static"
DEFAULT_OUTPUT = REPO_ROOT / "docs"


@dataclass
class Chart:
    """Geometry for a single-series line chart, computed server-side."""

    width: int
    height: int
    path: str
    area: str
    points: list[dict[str, Any]]
    y_ticks: list[dict[str, Any]]
    baseline_y: float | None


def line_chart(
    history: list[tuple[str, float]],
    width: int = 720,
    height: int = 240,
    pad_left: int = 48,
    pad_right: int = 16,
    pad_top: int = 16,
    pad_bottom: int = 28,
    baseline: float | None = 1500.0,
) -> Chart | None:
    """Work out where to draw each point of a team's rating-history line.

    Converts a list of (week label, rating) pairs into screen coordinates.

    The core idea is two conversions. Ratings might run from 1450 to 1850,
    while the drawing is 240 pixels tall - so x_at() spreads the weeks evenly
    across the width, and y_at() maps a rating onto a height. Note that y_at
    flips the direction: on a screen, y counts DOWNWARD from the top, so a
    higher rating needs a smaller y.
    """
    # A single point isn't a line; there's nothing to draw until week 1 is done.
    if len(history) < 2:
        return None

    # Find the range the line has to cover, and always include the 1500
    # average line so "above/below average" is visible on every chart.
    values = [v for _, v in history]
    lo, hi = min(values), max(values)
    if baseline is not None:
        lo, hi = min(lo, baseline), max(hi, baseline)
    # A team whose rating barely moved would otherwise get a wildly zoomed-in
    # chart where a 3-point wobble looks like a collapse. This floor keeps the
    # scale honest.
    span = max(hi - lo, 40.0)
    # Pad 12% above and below so the line never touches the edges.
    lo, hi = lo - span * 0.12, hi + span * 0.12
    span = hi - lo

    plot_w = width - pad_left - pad_right
    plot_h = height - pad_top - pad_bottom

    def x_at(i: int) -> float:
        return pad_left + (plot_w * i / (len(history) - 1))

    def y_at(v: float) -> float:
        return pad_top + plot_h * (1 - (v - lo) / span)

    points = [
        {
            "x": round(x_at(i), 2),
            "y": round(y_at(v), 2),
            "label": label,
            "value": round(v, 1),
        }
        for i, (label, v) in enumerate(history)
    ]

    path = "M " + " L ".join(f"{p['x']} {p['y']}" for p in points)
    area = (
        f"{path} L {points[-1]['x']} {pad_top + plot_h} "
        f"L {points[0]['x']} {pad_top + plot_h} Z"
    )

    # Four evenly spaced gridlines, rounded to friendly numbers.
    y_ticks = []
    for frac in (0.0, 0.25, 0.5, 0.75, 1.0):
        value = lo + span * frac
        y_ticks.append(
            {"y": round(y_at(value), 2), "label": f"{value:.0f}"}
        )

    return Chart(
        width=width,
        height=height,
        path=path,
        area=area,
        points=points,
        y_ticks=y_ticks,
        baseline_y=round(y_at(baseline), 2) if baseline is not None else None,
    )


def _environment() -> Environment:
    env = Environment(
        loader=FileSystemLoader(TEMPLATE_DIR),
        autoescape=select_autoescape(["html"]),
        trim_blocks=True,
        lstrip_blocks=True,
    )
    env.filters["pct"] = lambda v: f"{v * 100:.0f}%"
    env.filters["signed"] = lambda v: f"{v:+.1f}"
    env.filters["signed_int"] = lambda v: f"{v:+d}"
    env.filters["rating"] = lambda v: f"{v:,.0f}"
    return env


def _team_payload(team: TeamRanking, chart: Chart | None) -> dict[str, Any]:
    """Everything the team page needs, as plain JSON.

    The chart geometry is computed here rather than in the browser, so the
    layout math stays in one place (and stays testable) even though the page
    that draws it is rendered client-side.
    """

    def game(g) -> dict[str, Any]:
        return {
            "weekLabel": g.week_label,
            "date": g.date,
            "opponent": g.opponent,
            "opponentSlug": g.opponent_slug,
            "prefix": g.prefix,
            "neutral": g.location == "n",
            "result": g.result_letter,
            "won": g.won,
            "tied": g.tied,
            "score": g.score,
            "winProbability": round(g.win_probability, 4),
            "eloChange": round(g.rating_change, 1),
            "opponentRating": round(g.opponent_rating, 1),
            "upset": g.upset,
        }

    return {
        "slug": team.slug,
        "school": team.school,
        "conference": team.conference,
        "logo": team.logo,
        "rank": team.rank,
        "rankChange": team.rank_change,
        "rating": round(team.rating, 1),
        "ratingChange": round(team.rating_change, 1),
        "record": team.record,
        "conferenceRecord": team.conference_record,
        "sos": round(team.sos, 1),
        "sosRank": team.sos_rank,
        "chart": (
            {
                "width": chart.width,
                "height": chart.height,
                "path": chart.path,
                "area": chart.area,
                "points": chart.points,
                "yTicks": chart.y_ticks,
                "baselineY": chart.baseline_y,
            }
            if chart
            else None
        ),
        "bestWin": game(team.best_win) if team.best_win else None,
        "worstLoss": game(team.worst_loss) if team.worst_loss else None,
        "games": [game(g) for g in team.games],
    }


def render_site(
    table: RankingTable, config: Config, output_dir: Path = DEFAULT_OUTPUT
) -> Path:
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "assets").mkdir(parents=True, exist_ok=True)
    (output_dir / "data").mkdir(parents=True, exist_ok=True)

    # An earlier build wrote one HTML file per team. Clear that out so a
    # rebuild doesn't leave 138 orphaned pages behind.
    legacy = output_dir / "team"
    if legacy.is_dir():
        shutil.rmtree(legacy)

    env = _environment()
    # Links are page-relative ("" at the root, "../" one level down) so the
    # same output works opened from disk, on a user site, and on a project
    # site at /repo-name/ - with nothing to configure.
    shared = {"site": config.site, "config": config, "table": table}

    index_html = env.get_template("index.html").render(rel="", **shared)
    (output_dir / "index.html").write_text(index_html, encoding="utf-8")

    methodology_html = env.get_template("methodology.html").render(rel="", **shared)
    (output_dir / "methodology.html").write_text(methodology_html, encoding="utf-8")

    # One shell page for every team, filled in from teams.json by slug.
    team_html = env.get_template("team.html").render(rel="", **shared)
    (output_dir / "team.html").write_text(team_html, encoding="utf-8")

    teams_payload = {
        "season": table.season,
        "generatedAt": table.generated_at,
        "teams": {t.slug: _team_payload(t, line_chart(t.history)) for t in table.teams},
    }
    (output_dir / "data" / "teams.json").write_text(
        json.dumps(teams_payload, separators=(",", ":")), encoding="utf-8"
    )

    # Machine-readable exports, so the ratings are usable outside the page.
    (output_dir / "data" / "rankings.json").write_text(
        json.dumps(table.to_dict(), indent=2), encoding="utf-8"
    )
    (output_dir / "data" / "rankings.csv").write_text(_to_csv(table.teams), encoding="utf-8")

    for asset in STATIC_DIR.iterdir():
        if asset.is_file():
            shutil.copy2(asset, output_dir / "assets" / asset.name)

    # Tell GitHub Pages not to run the output through Jekyll.
    (output_dir / ".nojekyll").write_text("", encoding="utf-8")

    files = sum(1 for p in output_dir.rglob("*") if p.is_file())
    log.info("wrote %d files to %s (%d teams)", files, output_dir, len(table.teams))
    return output_dir


def _to_csv(teams: list[TeamRanking]) -> str:
    header = "rank,team,conference,rating,record,conference_record,sos,sos_rank"
    rows = [header]
    for t in teams:
        name = f'"{t.school}"' if "," in t.school else t.school
        rows.append(
            f"{t.rank},{name},{t.conference},{t.rating:.1f},{t.record},"
            f"{t.conference_record},{t.sos:.1f},{t.sos_rank}"
        )
    return "\n".join(rows) + "\n"
