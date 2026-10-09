"""Tests for ``mlb_search_player`` and ``mlb_player_stats``.

Expected values are derived from the fixture files, never hardcoded, so the
tests keep passing when the synthetic placeholders are replaced by real
recordings (``make record-fixtures``).
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from mcp.server.mcpserver.exceptions import ToolError, UnexpectedToolError

from sports_data_mcp import server
from sports_data_mcp.mlb import players
from sports_data_mcp.mlb.client import MLBClient

FIXTURES_DIR = Path(__file__).parent / "fixtures"
INDEX = json.loads((FIXTURES_DIR / "index.json").read_text(encoding="utf-8"))["fixtures"]


def call_tool(name: str, arguments: dict[str, Any]):
    return asyncio.run(server.mcp.call_tool(name, arguments))


def expected_summary(person: dict[str, Any]) -> dict[str, Any]:
    """What the tool should produce for one ``/people`` record, spelled out independently."""
    return {
        "id": person["id"],
        "full_name": person["fullName"],
        "team": person.get("currentTeam", {}).get("name"),
        "position": person["primaryPosition"]["abbreviation"],
        "bats": person["batSide"]["code"],
        "throws": person["pitchHand"]["code"],
        "active": person["active"],
    }


def expected_splits(stats_doc: dict[str, Any]) -> list[dict[str, Any]]:
    out = []
    for block in stats_doc["stats"]:
        for split in block["splits"]:
            flat = {
                "season": split.get("season"),
                "game_type": split.get("gameType"),
                "team": split.get("team", {}).get("name"),
                "team_id": split.get("team", {}).get("id"),
                "player_id": split["player"]["id"],
            }
            flat.update(split["stat"])
            out.append(flat)
    return out


@pytest.fixture
def injected_client(client: MLBClient):
    server.set_client(client)
    yield client
    server.set_client(None)


@pytest.fixture
def search_fixture(fixture_by_name) -> dict[str, Any]:
    return fixture_by_name("player_search")


@pytest.fixture
def searched_player_id(search_fixture) -> int:
    """The id the stats fixtures were (or will be) recorded for."""
    return int(search_fixture["people"][0]["id"])


# -- mlb_search_player ---------------------------------------------------------


def test_search_player_returns_fixture_people(injected_client, request_log, search_fixture) -> None:
    result = server.mlb_search_player("Aaron Judge")

    people = search_fixture["people"]
    assert result == [expected_summary(p) for p in people if p["active"]][:10]
    assert 0 < len(result) <= 10
    assert set(result[0]) == {"id", "full_name", "team", "position", "bats", "throws", "active"}
    assert request_log.urls == [INDEX["player_search"]["url"]]


def test_search_player_active_only_false_is_superset(injected_client, search_fixture) -> None:
    everyone = server.mlb_search_player("Aaron Judge", active_only=False)
    active = server.mlb_search_player("Aaron Judge")
    assert everyone == [expected_summary(p) for p in search_fixture["people"]][:10]
    assert active == [p for p in everyone if p["active"]]


def test_search_player_strips_whitespace_and_hits_cache(injected_client, request_log) -> None:
    first = server.mlb_search_player("  Aaron Judge  ")
    second = server.mlb_search_player("Aaron Judge")
    assert first == second
    assert len(request_log) == 1  # same URL, second call served from disk


def test_search_player_filters_inactive_and_caps_at_ten(cache_dir, clock, sleeps) -> None:
    def person(i: int, active: bool) -> dict[str, Any]:
        return {
            "id": i,
            "fullName": f"Player {i}",
            "active": active,
            "primaryPosition": {"abbreviation": "SS", "name": "Shortstop"},
            "batSide": {"code": "L"},
            "pitchHand": {"code": "R"},
            "currentTeam": {"id": 1, "name": f"Team {i}"},
        }

    people = [person(i, active=(i % 2 == 0)) for i in range(30)]

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/people/search"
        assert request.url.params["names"] == "Smith"
        return httpx.Response(200, json={"people": people}, request=request)

    client = MLBClient(cache_dir=cache_dir, transport=httpx.MockTransport(handler), clock=clock, sleep=sleeps.append)
    server.set_client(client)
    try:
        active = server.mlb_search_player("Smith")
        everyone = server.mlb_search_player("Smith", active_only=False)
    finally:
        server.set_client(None)
        client.close()

    assert len(active) == 10 and all(p["active"] for p in active)
    assert [p["id"] for p in active] == [0, 2, 4, 6, 8, 10, 12, 14, 16, 18]
    assert len(everyone) == 10 and [p["id"] for p in everyone] == list(range(10))
    assert everyone[1] == {
        "id": 1, "full_name": "Player 1", "team": "Team 1", "position": "SS",
        "bats": "L", "throws": "R", "active": False,
    }


@pytest.mark.parametrize("bad", ["", "a", " a ", "\t"])
def test_search_player_short_name_is_tool_error(injected_client, request_log, bad: str) -> None:
    with pytest.raises(ToolError) as excinfo:
        call_tool("mlb_search_player", {"name": bad})
    assert not isinstance(excinfo.value, UnexpectedToolError)  # deliberate, not a crash
    assert "2 characters" in str(excinfo.value)
    assert len(request_log) == 0  # rejected before any request


def test_search_player_via_mcp_matches_direct_call(injected_client) -> None:
    direct = server.mlb_search_player("Aaron Judge")
    result = call_tool("mlb_search_player", {"name": "Aaron Judge"})
    assert result.is_error is not True
    assert result.structured_content == {"result": direct}


# -- mlb_player_stats ----------------------------------------------------------


def test_player_stats_season_flattens_fixture_splits(
    injected_client, request_log, fixture_by_name, searched_player_id
) -> None:
    doc = fixture_by_name("player_stats_season")
    result = server.mlb_player_stats(searched_player_id, 2025)

    assert result["source_url"] == INDEX["player_stats_season"]["url"]
    assert request_log.urls == [result["source_url"]]
    assert result["cached"] is False
    assert result["player_id"] == searched_player_id
    assert result["season"] == 2025
    assert result["group"] == "hitting"
    assert result["type"] == "season"
    assert result["splits"] == expected_splits(doc)
    assert len(result["splits"]) == sum(len(b["splits"]) for b in doc["stats"])

    # The API's own stat names survive verbatim and each split is flat.
    first_stat = doc["stats"][0]["splits"][0]["stat"]
    for key, value in first_stat.items():
        assert result["splits"][0][key] == value
    assert all(not isinstance(v, (dict, list)) for v in result["splits"][0].values())


def test_player_stats_career_ignores_season(injected_client, request_log, fixture_by_name, searched_player_id) -> None:
    doc = fixture_by_name("player_stats_career")
    a = server.mlb_player_stats(searched_player_id, 2025, type="career")
    b = server.mlb_player_stats(searched_player_id, 1999, type="career")

    assert a["cached"] is False
    assert b["cached"] is True  # same URL, so the second call came from disk
    assert {k: v for k, v in a.items() if k != "cached"} == {k: v for k, v in b.items() if k != "cached"}
    assert a["source_url"] == INDEX["player_stats_career"]["url"]
    assert "season=" not in a["source_url"]
    assert request_log.urls == [a["source_url"]]
    assert a["season"] is None and a["type"] == "career"
    assert a["splits"] == expected_splits(doc)


def test_player_stats_via_mcp_matches_direct_call(injected_client, searched_player_id) -> None:
    direct = server.mlb_player_stats(searched_player_id, 2025, group="hitting", type="season")
    result = call_tool(
        "mlb_player_stats",
        {"player_id": searched_player_id, "season": 2025, "group": "hitting", "type": "season"},
    )
    assert result.is_error is not True
    assert result.structured_content == {**direct, "cached": True}  # second call, same URL


def test_player_stats_unknown_id_is_tool_error(injected_client, request_log) -> None:
    # No fixture is indexed for this id; whatever the fake transport does for
    # an unindexed URL must surface as a tool error, never a silent result.
    unknown_id = 1
    assert not any(f"/people/{unknown_id}/" in entry["url"] for entry in INDEX.values())
    with pytest.raises(ToolError):
        call_tool("mlb_player_stats", {"player_id": unknown_id, "season": 2025})
    assert len(request_log) == 1


def test_player_stats_api_error_message_reaches_the_tool_error(cache_dir, clock, sleeps) -> None:
    api_message = "Object not found"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"messageNumber": 10, "message": api_message}, request=request)

    client = MLBClient(cache_dir=cache_dir, transport=httpx.MockTransport(handler), clock=clock, sleep=sleeps.append)
    server.set_client(client)
    try:
        with pytest.raises(ToolError) as excinfo:
            call_tool("mlb_player_stats", {"player_id": 123, "season": 2025})
    finally:
        server.set_client(None)
        client.close()
    assert not isinstance(excinfo.value, UnexpectedToolError)  # deliberate, not a crash
    assert api_message in str(excinfo.value)
    assert sleeps == []  # 4xx is not retried


@pytest.mark.parametrize("arguments", [{"group": "batting"}, {"type": "playoffs"}])
def test_player_stats_rejects_unknown_group_or_type(injected_client, request_log, searched_player_id, arguments) -> None:
    with pytest.raises(ToolError):
        call_tool("mlb_player_stats", {"player_id": searched_player_id, "season": 2025, **arguments})
    assert len(request_log) == 0


def test_players_module_rejects_bad_group_without_a_request(client: MLBClient, request_log) -> None:
    with pytest.raises(players.PlayerError):
        players.player_stats(client, 1, 2025, group="batting")
    with pytest.raises(players.PlayerError):
        players.player_stats(client, 1, 2025, type="playoffs")
    assert len(request_log) == 0


# -- registration --------------------------------------------------------------


def test_tools_list_includes_player_tools(injected_client) -> None:
    tools = {t.name: t for t in asyncio.run(server.mcp.list_tools())}
    assert {"mlb_ping", "mlb_search_player", "mlb_player_stats"} <= set(tools)

    search = tools["mlb_search_player"].input_schema
    assert search["required"] == ["name"]
    assert search["properties"]["active_only"]["default"] is True

    stats = tools["mlb_player_stats"].input_schema
    assert set(stats["required"]) == {"player_id", "season"}
    assert stats["properties"]["group"]["enum"] == ["hitting", "pitching", "fielding"]
    assert stats["properties"]["group"]["default"] == "hitting"
    assert stats["properties"]["type"]["enum"] == ["season", "career"]
    assert stats["properties"]["type"]["default"] == "season"


def test_record_fixtures_script_lists_new_fixture_names() -> None:
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "record_fixtures", Path(__file__).parent.parent / "scripts" / "record_fixtures.py"
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)

    assert "player_stats_season" in module.FIXTURE_NAMES
    assert "player_stats_career" in module.FIXTURE_NAMES
    assert set(module.FIXTURE_NAMES) == set(INDEX)

    # The URLs the script would fetch are exactly the ones the index serves.
    recorded = {
        "schedule_week": {"dates": [{"games": [{"gamePk": 777283}]}]},
        "player_search": json.loads((FIXTURES_DIR / "player_search.json").read_text(encoding="utf-8")),
    }
    client = MLBClient(cache_dir=Path("unused"), transport=httpx.MockTransport(lambda r: httpx.Response(500)))
    try:
        urls = {name: client.url_for(path, params) for name, path, params in module.dependent_endpoints(recorded)}
    finally:
        client.close()
    assert urls["player_stats_season"] == INDEX["player_stats_season"]["url"]
    assert urls["player_stats_career"] == INDEX["player_stats_career"]["url"]
    assert urls["boxscore"] == INDEX["boxscore"]["url"]
