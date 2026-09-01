"""Tests for the API client's failure handling - the paths you only hit on a
bad day, which is exactly when you don't want to be debugging them.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest
import requests

from cfbrank.data import CFBDClient, CFBDError


def client(tmp_path, **kwargs):
    return CFBDClient(api_key="test-key", cache_dir=tmp_path, **kwargs)


def test_missing_key_says_where_to_get_one(tmp_path, monkeypatch):
    monkeypatch.delenv("CFBD_API_KEY", raising=False)
    c = CFBDClient(api_key="", cache_dir=tmp_path)
    with pytest.raises(CFBDError, match="collegefootballdata.com/key"):
        c._request("/teams/fbs", {"year": 2025})


def test_timeout_is_retried_then_explained(tmp_path, monkeypatch):
    c = client(tmp_path, timeout=10)
    calls = []

    def always_timeout(url, **kwargs):
        calls.append(kwargs["timeout"])
        raise requests.Timeout("read timed out")

    monkeypatch.setattr(c._session, "get", always_timeout)
    monkeypatch.setattr("time.sleep", lambda _s: None)

    with pytest.raises(CFBDError) as excinfo:
        c._request("/games", {"year": 2025})

    from cfbrank.data import MAX_ATTEMPTS

    assert len(calls) == MAX_ATTEMPTS
    assert calls == sorted(calls) and calls[0] < calls[-1], "each retry gets more time"
    message = str(excinfo.value)
    assert "Timed out" in message
    assert "--timeout" in message, "the error must name the flag that fixes it"


def test_bad_key_fails_immediately(tmp_path, monkeypatch):
    c = client(tmp_path)
    attempts = []

    class Unauthorized:
        status_code = 401

    def unauthorized(url, **kwargs):
        attempts.append(1)
        return Unauthorized()

    monkeypatch.setattr(c._session, "get", unauthorized)
    with pytest.raises(CFBDError, match="401"):
        c._request("/games", {"year": 2025})
    assert len(attempts) == 1, "a rejected key should not be retried"


class FakeResponse:
    def __init__(self, status_code, reason="Bad Gateway", payload=None):
        self.status_code = status_code
        self.reason = reason
        self._payload = payload if payload is not None else []

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code} {self.reason}")


def test_server_errors_are_retried_with_real_backoff(tmp_path, monkeypatch):
    """A 502 clears on its own - but not in the 2s a doubling backoff waits."""
    from cfbrank.data import MAX_ATTEMPTS, SERVER_ERROR_BACKOFF

    c = client(tmp_path)
    attempts, waits = [], []
    monkeypatch.setattr(c._session, "get", lambda url, **kw: (attempts.append(1), FakeResponse(502))[1])
    monkeypatch.setattr("time.sleep", lambda s: waits.append(s))

    with pytest.raises(CFBDError) as excinfo:
        c._request("/games", {"year": 2024, "seasonType": "postseason"})

    assert len(attempts) == MAX_ATTEMPTS
    assert waits == list(SERVER_ERROR_BACKOFF), "backoff should escalate, not double from 1s"
    assert sum(waits) >= 60, "a server error deserves a full minute of patience"
    message = str(excinfo.value)
    assert "server error" in message.lower()
    assert "cached" in message, "must tell the user rerunning resumes"


def test_a_502_that_clears_on_retry_succeeds(tmp_path, monkeypatch):
    c = client(tmp_path)
    responses = [FakeResponse(502), FakeResponse(502), FakeResponse(200, "OK", [{"id": 1}])]
    monkeypatch.setattr(c._session, "get", lambda url, **kw: responses.pop(0))
    monkeypatch.setattr("time.sleep", lambda _s: None)
    assert c._request("/games", {"year": 2024}) == [{"id": 1}]


def test_lost_postseason_degrades_instead_of_aborting(tmp_path, monkeypatch):
    """Losing a few bowl games shouldn't throw away eleven cached seasons."""
    c = client(tmp_path)

    def cached(name, season, fetch, refresh=False):
        if "postseason" in name:
            raise CFBDError("502 Bad Gateway")
        return [{"id": 1, "homeTeam": "A", "awayTeam": "B",
                 "homePoints": 21, "awayPoints": 17, "week": 1}]

    monkeypatch.setattr(c, "_cached", cached)
    games = c.games(2024)
    assert len(games) == 1, "the regular season should still come through"
    assert c.warnings and "postseason" in c.warnings[0]


def test_lost_regular_season_still_aborts(tmp_path, monkeypatch):
    c = client(tmp_path)
    monkeypatch.setattr(
        c, "_cached",
        lambda name, season, fetch, refresh=False: (_ for _ in ()).throw(CFBDError("down")),
    )
    with pytest.raises(CFBDError):
        c.games(2024)


def test_strict_mode_refuses_to_degrade(tmp_path, monkeypatch):
    c = client(tmp_path, strict=True)

    def cached(name, season, fetch, refresh=False):
        if "postseason" in name:
            raise CFBDError("502 Bad Gateway")
        return []

    monkeypatch.setattr(c, "_cached", cached)
    with pytest.raises(CFBDError):
        c.games(2024)


def test_stale_cache_is_used_when_the_api_is_down(tmp_path, monkeypatch):
    c = client(tmp_path)
    cached = [{"school": "Cached State", "logos": []}]
    (tmp_path / "teams_2025.json").write_text(json.dumps(cached), encoding="utf-8")

    def down():
        raise CFBDError("api is down")

    result = c._cached("teams_2025", 2099, down, refresh=True)
    assert result == cached, "a dead API should fall back to whatever is on disk"


def test_no_cache_and_a_dead_api_raises(tmp_path):
    c = client(tmp_path)

    def down():
        raise CFBDError("api is down")

    with pytest.raises(CFBDError):
        c._cached("teams_2025", 2099, down, refresh=True)


def test_finished_seasons_are_cached_forever(tmp_path):
    c = client(tmp_path)
    path = tmp_path / "games_2015_regular.json"
    path.write_text("[]", encoding="utf-8")
    assert c._is_fresh(path, 2015) is True


def test_progress_callback_reports_each_season(tmp_path, monkeypatch):
    from cfbrank.data import load_seasons

    c = client(tmp_path)
    monkeypatch.setattr(c, "teams", lambda season, refresh=False: [])
    monkeypatch.setattr(c, "games", lambda season, refresh=False: [])

    messages = []
    load_seasons(c, [2023, 2024, 2025], on_progress=messages.append)
    assert any("2023" in m for m in messages)
    assert any("2025" in m for m in messages)
