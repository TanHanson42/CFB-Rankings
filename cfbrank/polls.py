"""AP poll data, and week-by-week comparison against the Elo ratings.

The interesting question about any rating system isn't whether it agrees with
the humans - it's *where* it disagrees, and whether those disagreements were
right. This module lines the two up week by week so you can look.

Timing matters and is easy to get wrong. The AP poll released during week N
reflects games played through week N-1, so it is compared against the Elo
ratings as they stood after week N-1. That's the ``offset`` below.
"""

from __future__ import annotations

import logging
import statistics
from dataclasses import dataclass, field
from typing import Any, Iterable

from .data import CFBDClient, Game, Team, _pick
from .elo import EloModel, Snapshot

log = logging.getLogger(__name__)

AP_POLL_NAMES = ("ap top 25", "ap")


@dataclass(frozen=True)
class PollEntry:
    rank: int
    school: str
    conference: str | None
    first_place_votes: int
    points: int


@dataclass(frozen=True)
class Poll:
    season: int
    season_type: str
    week: int
    name: str
    ranks: tuple[PollEntry, ...]

    @property
    def schools(self) -> list[str]:
        return [entry.school for entry in sorted(self.ranks, key=lambda e: e.rank)]

    @property
    def rank_of(self) -> dict[str, int]:
        return {entry.school: entry.rank for entry in self.ranks}


def fetch_polls(
    client: CFBDClient, season: int, refresh: bool = False
) -> list[Poll]:
    """Every published poll for a season, in week order."""
    raw: list[dict[str, Any]] = []
    for season_type in ("regular", "postseason"):
        raw += client._cached(
            f"rankings_{season}_{season_type}",
            season,
            lambda st=season_type: client._request(
                "/rankings", {"year": season, "seasonType": st}
            ),
            refresh=refresh,
        )

    polls: list[Poll] = []
    for week_block in raw:
        week = int(_pick(week_block, "week", default=0))
        season_type = str(
            _pick(week_block, "seasonType", "season_type", default="regular")
        )
        for poll in _pick(week_block, "polls", default=[]) or []:
            entries = []
            for row in _pick(poll, "ranks", default=[]) or []:
                school = _pick(row, "school", "team")
                if not school:
                    continue
                entries.append(
                    PollEntry(
                        rank=int(_pick(row, "rank", default=0)),
                        school=school,
                        conference=_pick(row, "conference"),
                        first_place_votes=int(
                            _pick(row, "firstPlaceVotes", "first_place_votes", default=0)
                        ),
                        points=int(_pick(row, "points", default=0)),
                    )
                )
            polls.append(
                Poll(
                    season=int(_pick(week_block, "season", default=season)),
                    season_type=season_type,
                    week=week,
                    name=str(_pick(poll, "poll", default="?")),
                    ranks=tuple(sorted(entries, key=lambda e: e.rank)),
                )
            )

    polls.sort(key=lambda p: (0 if p.season_type == "regular" else 1, p.week))
    return polls


def ap_polls(polls: Iterable[Poll]) -> list[Poll]:
    """Just the AP Top 25, in week order, skipping any empty releases."""
    return [
        p
        for p in polls
        if p.name.strip().lower() in AP_POLL_NAMES and p.ranks
    ]


# --------------------------------------------------------------- comparison


@dataclass
class Disagreement:
    school: str
    ap_rank: int | None
    elo_rank: int
    gap: int  # positive = Elo likes the team more than the AP does

    @property
    def direction(self) -> str:
        return "Elo higher" if self.gap > 0 else "AP higher"


@dataclass
class WeekComparison:
    poll_week: int
    poll_label: str
    elo_label: str
    ap_order: list[str]
    elo_order: list[str]
    overlap: int
    mean_abs_gap: float
    max_gap: Disagreement | None
    elo_higher: list[Disagreement]
    elo_lower: list[Disagreement]
    ap_only: list[str] = field(default_factory=list)
    elo_only: list[str] = field(default_factory=list)

    @property
    def agreement_pct(self) -> float:
        return 100.0 * self.overlap / max(len(self.ap_order), 1)

    def to_dict(self) -> dict[str, Any]:
        return {
            "poll_week": self.poll_week,
            "poll_label": self.poll_label,
            "elo_through": self.elo_label,
            "overlap": self.overlap,
            "agreement_pct": round(self.agreement_pct, 1),
            "mean_abs_rank_gap": round(self.mean_abs_gap, 2),
            "ap_top25": self.ap_order,
            "elo_top25": self.elo_order,
            "in_elo_not_ap": self.elo_only,
            "in_ap_not_elo": self.ap_only,
            "elo_higher": [
                {"team": d.school, "ap": d.ap_rank, "elo": d.elo_rank, "gap": d.gap}
                for d in self.elo_higher
            ],
            "elo_lower": [
                {"team": d.school, "ap": d.ap_rank, "elo": d.elo_rank, "gap": d.gap}
                for d in self.elo_lower
            ],
        }


def elo_top_n(snapshot: Snapshot, fbs: set[str], n: int = 25) -> list[str]:
    """The Elo top N as of one weekly snapshot."""
    rated = [(school, rating) for school, rating in snapshot.ratings.items() if school in fbs]
    rated.sort(key=lambda pair: -pair[1])
    return [school for school, _ in rated[:n]]


def _snapshot_index(model: EloModel, season: int) -> dict[tuple[str, int], Snapshot]:
    return {
        (snap.season_type, snap.week): snap
        for snap in model.snapshots
        if snap.season == season
    }


def compare_week(
    poll: Poll, snapshot: Snapshot, fbs: set[str], top_n: int = 25, show: int = 4
) -> WeekComparison:
    ap_order = poll.schools[:top_n]
    elo_order = elo_top_n(snapshot, fbs, top_n)
    ap_rank = {school: i + 1 for i, school in enumerate(ap_order)}
    elo_rank = {school: i + 1 for i, school in enumerate(elo_order)}

    common = [s for s in elo_order if s in ap_rank]
    gaps = [ap_rank[s] - elo_rank[s] for s in common]

    disagreements = [
        Disagreement(school=s, ap_rank=ap_rank[s], elo_rank=elo_rank[s], gap=ap_rank[s] - elo_rank[s])
        for s in common
    ]
    # Unranked by the AP but inside the Elo top N: treat as a gap off the bottom
    # of the poll, which is the honest floor rather than a made-up number.
    for s in elo_order:
        if s not in ap_rank:
            disagreements.append(
                Disagreement(
                    school=s, ap_rank=None, elo_rank=elo_rank[s],
                    gap=(top_n + 1) - elo_rank[s],
                )
            )

    ranked_by_gap = sorted(disagreements, key=lambda d: -d.gap)
    elo_higher = [d for d in ranked_by_gap if d.gap > 0][:show]
    elo_lower = sorted([d for d in disagreements if d.gap < 0], key=lambda d: d.gap)[:show]

    return WeekComparison(
        poll_week=poll.week,
        poll_label=(
            f"AP week {poll.week}" if poll.season_type == "regular" else "AP final"
        ),
        elo_label=snapshot.label,
        ap_order=ap_order,
        elo_order=elo_order,
        overlap=len(common),
        mean_abs_gap=statistics.fmean(abs(g) for g in gaps) if gaps else 0.0,
        max_gap=max(disagreements, key=lambda d: abs(d.gap)) if disagreements else None,
        elo_higher=elo_higher,
        elo_lower=elo_lower,
        ap_only=[s for s in ap_order if s not in elo_rank],
        elo_only=[s for s in elo_order if s not in ap_rank],
    )


def compare_season(
    model: EloModel,
    teams: dict[str, Team],
    polls: list[Poll],
    season: int,
    offset: int = 1,
    top_n: int = 25,
) -> list[WeekComparison]:
    """Line each AP release up against the Elo ratings that preceded it."""
    fbs = set(teams)
    snapshots = _snapshot_index(model, season)
    comparisons: list[WeekComparison] = []

    for poll in ap_polls(polls):
        if poll.season_type == "regular":
            target_week = poll.week - offset
            if target_week < 0:
                continue
            snapshot = snapshots.get(("regular", target_week)) or snapshots.get(
                ("preseason", 0)
            )
        else:
            # The final AP poll is published after the bowls and the title game,
            # so it belongs against the last ratings of the year - bowls included.
            #
            # Take the chronologically last snapshot rather than the highest week
            # number: postseason weeks restart at 1, so "max by week" quietly
            # picked the final *regular season* snapshot and compared the final
            # poll against pre-bowl ratings.
            season_snaps = [s for s in model.snapshots if s.season == season]
            snapshot = season_snaps[-1] if season_snaps else None
        if snapshot is None:
            log.warning("no Elo snapshot to match %s week %d", poll.name, poll.week)
            continue
        comparisons.append(compare_week(poll, snapshot, fbs, top_n=top_n))

    return comparisons


@dataclass
class SeasonBias:
    """How a team was rated by the AP across the year, relative to Elo."""

    school: str
    weeks: int
    mean_gap: float  # positive = Elo consistently liked them more

    @property
    def direction(self) -> str:
        return "Elo higher" if self.mean_gap > 0 else "AP higher"


def season_biases(
    comparisons: list[WeekComparison], min_weeks: int = 3
) -> list[SeasonBias]:
    """Teams the two systems persistently disagreed about, worst first."""
    per_team: dict[str, list[int]] = {}
    for week in comparisons:
        ap_rank = {s: i + 1 for i, s in enumerate(week.ap_order)}
        elo_rank = {s: i + 1 for i, s in enumerate(week.elo_order)}
        for school in set(ap_rank) | set(elo_rank):
            a = ap_rank.get(school, len(week.ap_order) + 1)
            e = elo_rank.get(school, len(week.elo_order) + 1)
            if school in ap_rank or school in elo_rank:
                per_team.setdefault(school, []).append(a - e)

    biases = [
        SeasonBias(school=school, weeks=len(gaps), mean_gap=statistics.fmean(gaps))
        for school, gaps in per_team.items()
        if len(gaps) >= min_weeks
    ]
    biases.sort(key=lambda b: -abs(b.mean_gap))
    return biases
