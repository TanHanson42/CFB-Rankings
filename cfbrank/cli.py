"""THE COMMANDS YOU TYPE.

Everything you can ask this program to do, run as:

    python -m cfbrank check              is my API key working?
    python -m cfbrank top -n 25          print the top 25 in the terminal
    python -m cfbrank team "Ohio State"  one team's rating and game log
    python -m cfbrank build              generate the website into docs/
    python -m cfbrank compare            Elo vs the AP poll, week by week

Options that apply to any command go before it (they affect how data is
loaded), while options specific to one command go after it:

    python -m cfbrank --season 2024 --refresh top -n 10
                      \\_______ global _______/  \\_ command _/

Each command is a cmd_* function below. They all start by calling load(),
which does the same three things every time: read config.yaml, download or
un-cache the games, and run them through the rating engine.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import statistics
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

from .config import Config, REPO_ROOT
from .data import API_BASE, DEFAULT_TIMEOUT, CFBDClient, CFBDError, load_seasons
from .elo import build_model
from .polls import compare_season, fetch_polls, season_biases
from .rankings import build_rankings, RankingTable
from .site import DEFAULT_OUTPUT, render_site

log = logging.getLogger("cfbrank")


def load(args: argparse.Namespace):
    """Fetch the games and run the ratings. Every command starts here.

    Three steps, in order:
      1. read config.yaml (and let --season override the year)
      2. download the games, or read them from data/raw/ if already cached
      3. feed them all through the rating engine

    Returns the pieces every command needs. The first run of the day does real
    downloading; after that it's reading files off your disk and takes seconds.
    """
    config = Config.load(args.config)
    if args.season:
        config.season = args.season
        if config.history_start > config.season:
            config.history_start = config.season

    log.info(
        "rating %d season using history from %d, K=%s, HFA=%s",
        config.season,
        config.history_start,
        config.elo.k,
        config.elo.home_field,
    )

    client = CFBDClient(timeout=args.timeout, strict=args.strict)
    games, teams = load_seasons(
        client,
        config.seasons,
        refresh=args.refresh,
        on_progress=lambda msg: print(msg, file=sys.stderr, flush=True),
    )
    log.info("loaded %d games and %d FBS teams", len(games), len(teams))

    for note in client.warnings:
        print(f"  Note: {note}", file=sys.stderr, flush=True)

    model = build_model(games, teams, config.elo)
    return client, model, games, teams, config


def compute(args: argparse.Namespace) -> tuple[RankingTable, Config]:
    _, model, games, teams, config = load(args)
    return build_rankings(model, games, teams, config), config


def cmd_build(args: argparse.Namespace) -> int:
    table, config = compute(args)
    out = render_site(table, config, Path(args.output))
    print(f"Built {len(table.teams) + 2} pages in {out}")
    print(f"Open {out / 'index.html'} in a browser to see it.")
    return 0


def cmd_top(args: argparse.Namespace) -> int:
    table, _ = compute(args)
    width = max((len(t.school) for t in table.teams[: args.number]), default=10)
    print(f"\n{table.season} Elo rankings - through {table.through}\n")
    print(f"{'':>4}  {'Team':<{width}}  {'Rec':>6}  {'Elo':>6}  {'SOS':>6}  Move")
    print("-" * (width + 36))
    for team in table.teams[: args.number]:
        change = team.rank_change
        move = "  -" if not change else (f" +{change}" if change > 0 else f" {change}")
        print(
            f"{team.rank:>3}.  {team.school:<{width}}  {team.record:>6}  "
            f"{team.rating:>6.0f}  {team.sos:>6.0f}  {move}"
        )
    print()
    return 0


def cmd_team(args: argparse.Namespace) -> int:
    table, _ = compute(args)
    needle = args.name.lower()
    matches = [t for t in table.teams if needle in t.school.lower()]
    if not matches:
        print(f"No FBS team matching {args.name!r}.", file=sys.stderr)
        return 1

    for team in matches[:3]:
        print(f"\n#{team.rank} {team.school} ({team.conference})")
        print(
            f"  {team.record} overall, {team.conference_record} in conference | "
            f"Elo {team.rating:.0f} ({team.rating_change:+.1f} on the season) | "
            f"SOS #{team.sos_rank}"
        )
        if team.games:
            print(f"\n  {'Week':<12} {'Opponent':<28} {'Result':<12} {'Elo':>8}")
            for g in team.games:
                loc = {"vs": "vs ", "at": "at ", "n": "vs "}[g.location]
                print(
                    f"  {g.week_label:<12} {loc + g.opponent:<28} "
                    f"{g.result_letter + ' ' + g.score:<12} {g.rating_change:>+8.1f}"
                )
        if team.best_win:
            print(f"\n  Best win:   {team.best_win.opponent} ({team.best_win.score})")
        if team.worst_loss:
            print(f"  Worst loss: {team.worst_loss.opponent} ({team.worst_loss.score})")
    print()
    return 0


def cmd_check(args: argparse.Namespace) -> int:
    """Fast connectivity and key check - one small request, no season pull."""
    config = Config.load(args.config)
    season = args.season or config.season

    key = os.environ.get("CFBD_API_KEY", "").strip()
    env_file = REPO_ROOT / ".env"
    print()
    print(f"  .env file      {'found' if env_file.exists() else 'MISSING'} ({env_file})")
    if not env_file.exists():
        stray = sorted(REPO_ROOT.glob(".env.*"))
        stray = [p for p in stray if p.name != ".env.example"]
        if stray:
            print(f"                 found {stray[0].name} instead - rename it to .env")
    print(f"  API key        {'loaded, ' + str(len(key)) + ' chars' if key else 'NOT SET'}")
    if not key:
        print("\n  Get one free at https://collegefootballdata.com/key\n")
        return 2

    client = CFBDClient(timeout=args.timeout)
    print(f"  Endpoint       {API_BASE}")
    print(f"  Timeout        {args.timeout}s per try, 3 tries\n")
    print(f"  Requesting the {season} FBS team list (small)...")

    started = time.monotonic()
    teams = client.teams(season, refresh=True)
    elapsed = time.monotonic() - started

    print(f"  OK: {len(teams)} teams in {elapsed:.1f}s.")
    if elapsed > 20:
        print("  That's slow for a small request - expect the full pull to take a while.")
    print(f"\n  Connection and key are good. Run: python -m cfbrank top -n 25\n")
    return 0


def cmd_compare(args: argparse.Namespace) -> int:
    client, model, _games, teams, config = load(args)
    polls = fetch_polls(client, config.season, refresh=args.refresh)
    weeks = compare_season(
        model, teams, polls, config.season, offset=args.offset, top_n=args.top
    )

    if not weeks:
        print(
            f"No AP polls found for {config.season}. "
            "The season may not have started yet.",
            file=sys.stderr,
        )
        return 1

    print(f"\n{config.season}: Elo vs the AP Top {args.top}, week by week")
    print(
        "  Each AP release is compared against the Elo ratings as they stood "
        f"{args.offset} week(s) earlier,\n  which is the data the voters had.\n"
    )
    print(f"  {'Poll':<14} {'Elo through':<12} {'Same teams':>11} {'Agree':>7} {'Avg gap':>8}   Biggest disagreement")
    print("  " + "-" * 96)
    for w in weeks:
        gap = w.max_gap
        note = ""
        if gap:
            ap = f"AP #{gap.ap_rank}" if gap.ap_rank else "AP unranked"
            note = f"{gap.school} ({ap}, Elo #{gap.elo_rank})"
        # No shared teams means no gap to average - don't print 0.0, which
        # would read as perfect agreement.
        gap_col = f"{w.mean_abs_gap:>8.1f}" if w.overlap else f"{'-':>8}"
        print(
            f"  {w.poll_label:<14} {w.elo_label:<12} {w.overlap:>7}/{args.top:<3} "
            f"{w.agreement_pct:>6.0f}% {gap_col}   {note}"
        )

    overlaps = [w.overlap for w in weeks]
    gaps = [w.mean_abs_gap for w in weeks if w.overlap]
    print(
        f"\n  Season average: {statistics.fmean(overlaps):.1f}/{args.top} teams shared, "
        f"{statistics.fmean(gaps):.1f} places apart on the ones both ranked."
    )

    print("\n  Teams the two systems disagreed about all year:")
    print(f"    {'Team':<26} {'Weeks':>5}  {'Avg gap':>8}  Who liked them more")
    print("    " + "-" * 68)
    for bias in season_biases(weeks)[: args.show]:
        print(
            f"    {bias.school:<26} {bias.weeks:>5}  {bias.mean_gap:>+8.1f}  "
            f"{bias.direction}"
        )

    last = weeks[-1]
    print(f"\n  {last.poll_label} vs Elo ({last.elo_label}):")
    print(f"    {'#':>3}  {'AP':<26} {'Elo':<26}")
    print("    " + "-" * 58)
    for i in range(args.top):
        ap = last.ap_order[i] if i < len(last.ap_order) else ""
        elo = last.elo_order[i] if i < len(last.elo_order) else ""
        flag = "" if ap == elo else "  <"
        print(f"    {i + 1:>3}  {ap:<26} {elo:<26}{flag}")
    if last.elo_only:
        print(f"\n    In the Elo top {args.top} but unranked by the AP: "
              f"{', '.join(last.elo_only)}")
    if last.ap_only:
        print(f"    Ranked by the AP but outside the Elo top {args.top}: "
              f"{', '.join(last.ap_only)}")

    if args.json:
        payload = {
            "season": config.season,
            "offset": args.offset,
            "weeks": [w.to_dict() for w in weeks],
            "season_biases": [
                {"team": b.school, "weeks": b.weeks, "mean_gap": round(b.mean_gap, 2)}
                for b in season_biases(weeks)
            ],
        }
        Path(args.json).write_text(json.dumps(payload, indent=2), encoding="utf-8")
        print(f"\n  Wrote {args.json}")

    print()
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cfbrank",
        description="Elo ratings for college football.",
    )
    parser.add_argument("-c", "--config", default=None, help="path to config.yaml")
    parser.add_argument("-s", "--season", type=int, default=None, help="season to rank")
    parser.add_argument(
        "-r", "--refresh", action="store_true", help="ignore the cache and refetch"
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="log every step")
    parser.add_argument(
        "--strict",
        action="store_true",
        help="fail on any missing data instead of continuing without it",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=DEFAULT_TIMEOUT,
        help=f"seconds to wait on each API request (default {DEFAULT_TIMEOUT}; "
        "retries get 50%% more each time)",
    )

    sub = parser.add_subparsers(dest="command", required=True)

    build = sub.add_parser("build", help="generate the full static site")
    build.add_argument("-o", "--output", default=str(DEFAULT_OUTPUT))
    build.set_defaults(func=cmd_build)

    top = sub.add_parser("top", help="print the top N teams to the terminal")
    top.add_argument("-n", "--number", type=int, default=25)
    top.set_defaults(func=cmd_top)

    team = sub.add_parser("team", help="print one team's rating and game log")
    team.add_argument("name")
    team.set_defaults(func=cmd_team)

    check = sub.add_parser(
        "check", help="verify the API key and connection with one small request"
    )
    check.set_defaults(func=cmd_check)

    compare = sub.add_parser(
        "compare", help="compare the Elo ratings against the AP poll, week by week"
    )
    compare.add_argument("-t", "--top", type=int, default=25, help="poll size to compare")
    compare.add_argument(
        "--offset",
        type=int,
        default=1,
        help="weeks to shift the Elo snapshot back (default 1: the AP poll "
        "released in week N reflects games through week N-1)",
    )
    compare.add_argument("--show", type=int, default=10, help="season-long disagreements to list")
    compare.add_argument("--json", default=None, help="also write the full comparison here")
    compare.set_defaults(func=cmd_compare)

    return parser


def main(argv: list[str] | None = None) -> int:
    load_dotenv(REPO_ROOT / ".env")
    parser = build_parser()
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )

    try:
        return args.func(args)
    except CFBDError as exc:
        print(f"\nData error: {exc}\n", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
