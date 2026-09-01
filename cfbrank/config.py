"""Loading and validating config.yaml."""

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
    initial_rating: float = 1500.0
    fcs_rating: float = 1000.0
    freeze_fcs: bool = True
    k: float = 45.0
    home_field: float = 62.0
    margin_of_victory: bool = True
    preseason_regression: float = 0.75

    def validate(self) -> None:
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
