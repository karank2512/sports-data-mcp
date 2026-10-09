from __future__ import annotations

import hashlib
import json
from pathlib import Path

import httpx
import pytest

from sports_data_mcp.mlb.client import APIError, MLBClient

TEAMS = ("/teams", {"sportId": 1})


def make_client(handler, cache_dir: Path, clock, sleeps: list[float], **kw) -> MLBClient:
    return MLBClient(
        cache_dir=cache_dir,
        transport=httpx.MockTransport(handler),
        clock=clock,
        sleep=sleeps.append,
        **kw,
    )


def test_cache_miss_then_hit(client: MLBClient, request_log, fixture_by_name) -> None:
    data, cached = client.fetch(*TEAMS)
    assert cached is False
    assert data == fixture_by_name("teams")
    assert len(request_log) == 1

    data2, cached2 = client.fetch(*TEAMS)
    assert cached2 is True
    assert data2 == data
    assert len(request_log) == 1  # served from disk, no second request


def test_cache_file_is_sha256_of_full_url(client: MLBClient, cache_dir: Path) -> None:
    client.get(*TEAMS)
    url = client.url_for(*TEAMS)
    assert url == "https://statsapi.mlb.com/api/v1/teams?sportId=1"
    expected = cache_dir / (hashlib.sha256(url.encode()).hexdigest() + ".json")
    assert expected.is_file()
    entry = json.loads(expected.read_text())
    assert entry["url"] == url
    assert "teams" in entry["data"]


def test_ttl_expiry_refetches(client: MLBClient, request_log, clock) -> None:
    client.get(*TEAMS)
    clock.advance(3599)
    _, cached = client.fetch(*TEAMS)
    assert cached is True
    clock.advance(2)  # now past the 3600 s default
    _, cached = client.fetch(*TEAMS)
    assert cached is False
    assert len(request_log) == 2


def test_per_call_ttl_override(client: MLBClient, request_log, clock) -> None:
    client.get(*TEAMS)
    clock.advance(10)
    _, cached = client.fetch(*TEAMS, ttl_s=5)
    assert cached is False  # 10 s old is stale under a 5 s TTL
    _, cached = client.fetch(*TEAMS, ttl_s=0)
    assert cached is False  # ttl 0 always refetches
    assert len(request_log) == 3


def test_offline_serves_stale_cache(client: MLBClient, request_log, clock) -> None:
    client.get(*TEAMS)
    clock.advance(10 * 24 * 3600)  # far past TTL
    data, cached = client.fetch(*TEAMS, offline=True)
    assert cached is True
    assert "teams" in data
    assert len(request_log) == 1


def test_offline_miss_raises_without_network(fixture_transport, cache_dir, request_log) -> None:
    client = MLBClient(cache_dir=cache_dir, transport=fixture_transport, offline=True)
    with pytest.raises(APIError) as excinfo:
        client.get(*TEAMS)
    assert excinfo.value.status is None
    assert "offline" in excinfo.value.message
    assert len(request_log) == 0


def test_429_retries_once_then_succeeds(cache_dir, clock, sleeps) -> None:
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(429, headers={"Retry-After": "2"}, text="slow down")
        return httpx.Response(200, json={"teams": []})

    client = make_client(handler, cache_dir, clock, sleeps)
    data, cached = client.fetch(*TEAMS)
    assert data == {"teams": []}
    assert cached is False
    assert len(calls) == 2
    assert sleeps == [2.0]  # honoured Retry-After


def test_429_twice_raises_api_error(cache_dir, clock, sleeps) -> None:
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(429, text="rate limited")

    client = make_client(handler, cache_dir, clock, sleeps, backoff_s=0.5)
    with pytest.raises(APIError) as excinfo:
        client.get(*TEAMS)
    err = excinfo.value
    assert err.status == 429
    assert "429" in err.message and "rate limited" in err.message
    assert str(err) == err.message
    assert len(calls) == 2  # exactly one retry
    assert sleeps == [0.5]
    assert not any(cache_dir.glob("*.json"))  # errors are never cached


def test_5xx_retries_once_then_raises(cache_dir, clock, sleeps) -> None:
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(503, text="upstream down")

    client = make_client(handler, cache_dir, clock, sleeps)
    with pytest.raises(APIError) as excinfo:
        client.get(*TEAMS)
    assert excinfo.value.status == 503
    assert len(calls) == 2
    assert len(sleeps) == 1


def test_4xx_does_not_retry(cache_dir, clock, sleeps) -> None:
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(404, json={"message": "no such thing"})

    client = make_client(handler, cache_dir, clock, sleeps)
    with pytest.raises(APIError) as excinfo:
        client.get("/nope")
    assert excinfo.value.status == 404
    assert len(calls) == 1
    assert sleeps == []


def test_transport_error_retries_then_raises(cache_dir, clock, sleeps) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out", request=request)

    client = make_client(handler, cache_dir, clock, sleeps)
    with pytest.raises(APIError) as excinfo:
        client.get(*TEAMS)
    assert excinfo.value.status is None
    assert "ReadTimeout" in excinfo.value.message
    assert len(sleeps) == 1


def test_timeout_default_is_10s(client: MLBClient) -> None:
    assert client.timeout_s == 10.0
    assert client._http.timeout == httpx.Timeout(10.0)
