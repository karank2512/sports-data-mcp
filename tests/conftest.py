"""Shared test setup: fixtures served through ``httpx.MockTransport``.

``tests/fixtures/index.json`` maps fixture names to the exact URL that was
(or will be) requested. The ``fixture_transport`` answers those URLs from
the matching JSON file and fails loudly for anything else, so no test can
reach the real MLB Stats API.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from sports_data_mcp.mlb.client import MLBClient

FIXTURES_DIR = Path(__file__).parent / "fixtures"

UrlKey = tuple[str, str, tuple[tuple[str, str], ...]]


def url_key(url: str | httpx.URL) -> UrlKey:
    """Normalize a URL so query encoding/order differences do not matter."""
    u = httpx.URL(str(url))
    return (u.host, u.path.rstrip("/"), tuple(sorted(u.params.multi_items())))


def load_fixtures() -> dict[UrlKey, tuple[str, Any]]:
    index = json.loads((FIXTURES_DIR / "index.json").read_text(encoding="utf-8"))
    table: dict[UrlKey, tuple[str, Any]] = {}
    for name, entry in index["fixtures"].items():
        data = json.loads((FIXTURES_DIR / entry["file"]).read_text(encoding="utf-8"))
        table[url_key(entry["url"])] = (name, data)
    return table


class FakeClock:
    """Deterministic time source for TTL tests."""

    def __init__(self, start: float = 1_700_000_000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class RequestLog:
    """Records every request a mock transport sees."""

    def __init__(self) -> None:
        self.urls: list[str] = []

    def __len__(self) -> int:
        return len(self.urls)


@pytest.fixture(autouse=True)
def _no_real_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Hard guarantee: any attempt to use a real httpx transport fails."""

    def _blocked(self: Any, request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"real network access attempted: {request.url}")

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", _blocked)


@pytest.fixture(scope="session")
def fixtures() -> dict[UrlKey, tuple[str, Any]]:
    return load_fixtures()


@pytest.fixture
def fixture_by_name(fixtures: dict[UrlKey, tuple[str, Any]]):
    def _get(name: str) -> Any:
        for fixture_name, data in fixtures.values():
            if fixture_name == name:
                return data
        raise KeyError(name)

    return _get


@pytest.fixture
def request_log() -> RequestLog:
    return RequestLog()


@pytest.fixture
def fixture_transport(fixtures: dict[UrlKey, tuple[str, Any]], request_log: RequestLog) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        request_log.urls.append(str(request.url))
        hit = fixtures.get(url_key(request.url))
        if hit is None:
            raise AssertionError(f"no fixture for {request.url}; add it to tests/fixtures/index.json")
        _name, data = hit
        return httpx.Response(200, json=data, request=request)

    return httpx.MockTransport(handler)


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def sleeps() -> list[float]:
    return []


@pytest.fixture
def cache_dir(tmp_path: Path) -> Path:
    return tmp_path / "cache"


@pytest.fixture
def client(fixture_transport: httpx.MockTransport, cache_dir: Path, clock: FakeClock, sleeps: list[float]) -> MLBClient:
    c = MLBClient(cache_dir=cache_dir, ttl_s=3600, transport=fixture_transport, clock=clock, sleep=sleeps.append)
    yield c
    c.close()
