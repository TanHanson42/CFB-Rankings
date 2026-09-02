"""THE SETTINGS FILE READER.

Reads config.yaml - the one file you edit to change how the rankings work - and
checks the values make sense before anything else runs.

Every knob that affects the ratings lives in config.yaml rather than being
buried in the code, so tuning the model is a one-line edit and a rerun rather
than a programming task. This file's job is just to load those values and
refuse obviously broken ones early, with a clear message, instead of letting
them cause strange results twenty minutes into a run.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent


def current_season(today: dt.date | None = None) -> int:
    """The season a given date belongs to.

    College football seasons are named for the calendar year they start in, so
    January bowl games belong to the previous year's season. Anything before
    July is treated as the tail of last season.
    """
    today = today or dt.date.today()
    return today.year if today.month >= 7 else today.year - 1


@dataclass
class EloConfig:
    """The dials that control the rating math. Defaults match config.yaml."""

    # Where a team starts the first time it ever appears.
    initial_rating: float = 1500.0
    # The single rating shared by every non-FBS opponent. 1000 makes an average
    # FBS home team about a 96% favorite, which is roughly what really happens.
    fcs_rating: float = 1000.0
    # Hold that pooled rating still, so FBS teams can't grind it down all season.
    freeze_fcs: bool = True
    # How far one game can move a rating. Higher = the board churns weekly.
    k: float = 45.0
    # Home-field advantage in rating points (~2.5 points of spread).
    home_field: float = 62.0
    # Whether the final score matters, or only who won.
    margin_of_victory: bool = True
    # How much of last year a team keeps. 0.75 = a quarter is forgotten.
    preseason_regression: float = 0.75

    def validate(self) -> None:
        """Catch nonsense settings now, with a readable error.

        These bounds are deliberately generous - they're here to catch typos
        (a K of 4500, a negative home field) rather than to enforce taste.
        Plenty of unusual-but-valid settings pass.
        """
        if not 0 < self.k <= 200:
            raise ValueError(f"elo.k must be in (0, 200]; got {self.k}")
        if not 0 <= self.home_field <= 300:
            raise ValueError(f"elo.home_field must be in [0, 300]; got {self.home_field}")
        if not 0 <= self.preseason_regression <= 1:
            raise ValueError(
                f"elo.preseason_regression must be in [0, 1]; got {self.preseason_regression}"
            )


@dataclass
class SiteConfig:
    title: str = "Elo Rankings"
    subtitle: str = ""
    author: str = ""


@dataclass
class Config:
    season: int
    history_start: int
    elo: EloConfig = field(default_factory=EloConfig)
    site: SiteConfig = field(default_factory=SiteConfig)

    @property
    def seasons(self) -> list[int]:
        """Every season the model walks through, oldest first."""
        return list(range(self.history_start, self.season + 1))

    @classmethod
    def load(cls, path: str | Path | None = None) -> "Config":
        path = Path(path) if path else REPO_ROOT / "config.yaml"
        raw: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8")) or {}

        season = raw.get("season", "auto")
        season = current_season() if season in (None, "auto") else int(season)

        history_start = int(raw.get("history_start", season))
        if history_start > season:
            raise ValueError(
                f"history_start ({history_start}) cannot be after season ({season})"
            )

        cfg = cls(
            season=season,
            history_start=history_start,
            elo=EloConfig(**(raw.get("elo") or {})),
            site=SiteConfig(**(raw.get("site") or {})),
        )
        cfg.elo.validate()
        return cfg
