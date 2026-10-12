"""Tests for ``mlb_team_schedule`` and ``mlb_standings``.

Expected values are derived from the fixture files (``teams.json``,
``schedule_week.json``, ``standings.json``), never hardcoded, so the tests
keep passing after ``make record-fixtures``.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from mcp.server.mcpserver.exceptions import ToolError, UnexpectedToolError

from sports_data_mcp import server
from sports_data_mcp.mlb import teams
from sports_data_mcp.mlb.client import MLBClient

FIXTURES_DIR = Path(__file__).parent / "fixtures"
INDEX = json.loads((FIXTURES_DIR / "index.json").read_text(encoding="utf-8"))["fixtures"]
TEAMS_URL = INDEX["teams"]["url"]
SCHEDULE_URL = INDEX["schedule_week"]["url"]
STANDINGS_URL = INDEX["standings"]["url"]

# The team and window the schedule fixture was recorded for (see scripts/record_fixtures.py).
TEAM_ID = 147
START, END = "2025-07-01", "2025-07-07"


def call_tool(name: str, arguments: dict[str, Any]):
    return asyncio.run(server.mcp.call_tool(name, arguments))


def expected_games(schedule_doc: dict[str, Any]) -> list[dict[str, Any]]:
    """What the tool should produce for the schedule fixture, spelled out independently."""
    out = []
    for day in schedule_doc["dates"]:
        for game in day["games"]:
            out.append(
                {
                    "game_pk": game["gamePk"],
                    "date": game["officialDate"],
                    "home": game["teams"]["home"]["team"]["name"],
                    "away": game["teams"]["away"]["team"]["name"],
                    "status": game["status"]["detailedState"],
                    "home_score": game["teams"]["home"].get("score"),
                    "away_score": game["teams"]["away"].get("score"),
                    "venue": game["venue"]["name"],
                }
            )
    return out


def expected_team_rows(record: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {
            "team_id": r["team"]["id"],
            "team": r["team"]["name"],
            "w": r["wins"],
            "l": r["losses"],
            "pct": r["winningPercentage"],
            "gb": r["gamesBack"],
            "streak": r["streak"]["streakCode"],
        }
        for r in record["teamRecords"]
    ]


@pytest.fixture
def injected_client(client: MLBClient):
    server.set_client(client)
    yield client
    server.set_client(None)


@pytest.fixture
def teams_doc(fixture_by_name) -> list[dict[str, Any]]:
    return fixture_by_name("teams")["teams"]


@pytest.fixture
def yankees(teams_doc) -> dict[str, Any]:
    return next(t for t in teams_doc if t["id"] == TEAM_ID)


# -- team resolution -----------------------------------------------------------


def test_resolve_team_by_id(client: MLBClient, request_log, yankees) -> None:
    record, cached = teams.resolve_team(client, TEAM_ID)
    assert record == yankees
    assert cached is False
    assert request_log.urls == [TEAMS_URL]

    # A string of digits is an id too, and the second lookup is served from disk.
    record2, cached2 = teams.resolve_team(client, str(TEAM_ID))
    assert record2 == yankees
    assert cached2 is True
    assert len(request_log) == 1


def test_resolve_team_by_full_name_is_case_insensitive(client: MLBClient, yankees) -> None:
    assert teams.resolve_team(client, yankees["name"])[0] == yankees
    assert teams.resolve_team(client, yankees["name"].upper())[0] == yankees
    assert teams.resolve_team(client, f"  {yankees['name'].lower()}  ")[0] == yankees


def test_resolve_team_by_nickname(client: MLBClient, yankees) -> None:
    assert teams.resolve_team(client, yankees["teamName"])[0] == yankees  # "Yankees"


def test_resolve_team_by_abbreviation(client: MLBClient, yankees) -> None:
    assert teams.resolve_team(client, yankees["abbreviation"])[0] == yankees  # "NYY"
    assert teams.resolve_team(client, yankees["abbreviation"].lower())[0] == yankees


def test_every_fixture_team_resolves_by_each_of_its_unique_names(client: MLBClient, teams_doc) -> None:
    """Any name that belongs to exactly one team must resolve to that team."""
    owners: dict[str, set[int]] = {}
    for t in teams_doc:
        for field in teams.NAME_FIELDS:
            if t.get(field):
                owners.setdefault(t[field].casefold(), set()).add(t["id"])
    unique = {name: next(iter(ids)) for name, ids in owners.items() if len(ids) == 1}
    assert len(unique) >= len(teams_doc)  # at least one unique alias per team
    for name, team_id in unique.items():
        assert teams.resolve_team(client, name)[0]["id"] == team_id, name


def test_resolve_team_ambiguous_name_lists_candidates(client: MLBClient, teams_doc) -> None:
    # Pick a location shared by several teams in the fixture (e.g. "New York", "Chicago").
    by_franchise: dict[str, list[dict[str, Any]]] = {}
    for t in teams_doc:
        by_franchise.setdefault(t["franchiseName"], []).append(t)
    shared, candidates = next((k, v) for k, v in by_franchise.items() if len(v) > 1)

    with pytest.raises(teams.TeamError) as excinfo:
        teams.resolve_team(client, shared)
    message = str(excinfo.value)
    assert "ambiguous" in message and shared in message
    for t in candidates:
        assert t["name"] in message and t["abbreviation"] in message and str(t["id"]) in message


def test_resolve_team_substring_fallback_and_its_ambiguity(client: MLBClient, teams_doc) -> None:
    sox = [t for t in teams_doc if "Sox" in t["name"]]
    assert len(sox) == 2  # Red Sox and White Sox
    with pytest.raises(teams.TeamError) as excinfo:
        teams.resolve_team(client, "sox")
    assert all(t["name"] in str(excinfo.value) for t in sox)

    # A substring that is unique resolves (exact-match failed, fallback hit one team).
    assert teams.resolve_team(client, "Red So")[0]["name"] == "Boston Red Sox"


@pytest.mark.parametrize("bad", ["Springfield Isotopes", "", "   ", 0, 999999, True])
def test_resolve_team_unknown_is_team_error(client: MLBClient, bad) -> None:
    with pytest.raises(teams.TeamError):
        teams.resolve_team(client, bad)


# -- mlb_team_schedule ---------------------------------------------------------


def test_team_schedule_by_id_flattens_fixture_games(injected_client, request_log, fixture_by_name, yankees) -> None:
    doc = fixture_by_name("schedule_week")
    result = server.mlb_team_schedule(TEAM_ID, START, END)

    assert result["team_id"] == TEAM_ID
    assert result["team"] == yankees["name"]
    assert result["start"] == START and result["end"] == END
    assert result["cached"] is False
    assert result["source_url"] == SCHEDULE_URL
    assert request_log.urls == [TEAMS_URL, SCHEDULE_URL]

    games = result["games"]
    assert games == expected_games(doc)
    assert len(games) == doc["totalGames"] > 0
    assert set(games[0]) == {"game_pk", "date", "home", "away", "status", "home_score", "away_score", "venue"}
    assert all(TEAM_ID in (g["home"], g["away"]) or yankees["name"] in (g["home"], g["away"]) for g in games)
    assert all(not isinstance(v, (dict, list)) for g in games for v in g.values())


@pytest.mark.parametrize("field", ["name", "teamName", "abbreviation"])
def test_team_schedule_by_name_or_abbreviation_matches_id(injected_client, request_log, yankees, field) -> None:
    by_id = server.mlb_team_schedule(TEAM_ID, START, END)
    by_name = server.mlb_team_schedule(yankees[field].lower(), START, END)
    assert {k: v for k, v in by_name.items() if k != "cached"} == {k: v for k, v in by_id.items() if k != "cached"}
    assert by_name["cached"] is True  # same URL, so the second schedule came from disk
    assert request_log.urls == [TEAMS_URL, SCHEDULE_URL]  # /teams was cached for the second resolution


def test_team_schedule_via_mcp_accepts_int_and_string_team(injected_client) -> None:
    direct = server.mlb_team_schedule(TEAM_ID, START, END)
    as_int = call_tool("mlb_team_schedule", {"team": TEAM_ID, "start": START, "end": END})
    as_str = call_tool("mlb_team_schedule", {"team": "NYY", "start": START, "end": END})
    assert as_int.is_error is not True and as_str.is_error is not True
    assert as_int.structured_content == {**direct, "cached": True}
    assert as_str.structured_content == {**direct, "cached": True}


def test_team_schedule_ambiguous_team_is_tool_error_listing_candidates(injected_client, request_log) -> None:
    with pytest.raises(ToolError) as excinfo:
        call_tool("mlb_team_schedule", {"team": "New York", "start": START, "end": END})
    assert not isinstance(excinfo.value, UnexpectedToolError)  # deliberate, not a crash
    message = str(excinfo.value)
    assert "ambiguous" in message
    assert "New York Yankees" in message and "New York Mets" in message
    assert request_log.urls == [TEAMS_URL]  # looked up the team list, never asked for a schedule


def test_team_schedule_unknown_team_is_tool_error(injected_client, request_log) -> None:
    with pytest.raises(ToolError) as excinfo:
        call_tool("mlb_team_schedule", {"team": "Springfield Isotopes", "start": START, "end": END})
    assert not isinstance(excinfo.value, UnexpectedToolError)
    assert "no MLB team" in str(excinfo.value)
    assert request_log.urls == [TEAMS_URL]


# -- date range cap ------------------------------------------------------------


def test_validate_range_allows_exactly_31_days() -> None:
    first, last = teams.validate_range("2025-07-01", "2025-07-31")
    assert (last - first).days + 1 == 31 == teams.MAX_RANGE_DAYS
    assert teams.validate_range("2025-07-01", "2025-07-01")[0] == first  # a single day is fine


@pytest.mark.parametrize(
    ("start", "end", "fragment"),
    [
        ("2025-07-01", "2025-08-01", "32 days"),  # one over the cap
        ("2025-03-01", "2025-10-01", "at most 31 days"),
        ("2025-07-07", "2025-07-01", "before start"),
        ("07/01/2025", "2025-07-07", "YYYY-MM-DD"),
        ("2025-07-01", "tomorrow", "YYYY-MM-DD"),
    ],
)
def test_team_schedule_bad_range_is_tool_error_before_any_request(
    injected_client, request_log, start: str, end: str, fragment: str
) -> None:
    with pytest.raises(ToolError) as excinfo:
        call_tool("mlb_team_schedule", {"team": TEAM_ID, "start": start, "end": end})
    assert not isinstance(excinfo.value, UnexpectedToolError)  # deliberate, not a crash
    assert fragment in str(excinfo.value)
    assert len(request_log) == 0  # rejected before resolving the team or fetching anything


def test_teams_module_rejects_bad_range_without_a_request(client: MLBClient, request_log) -> None:
    with pytest.raises(teams.TeamError):
        teams.team_schedule(client, TEAM_ID, "2025-07-01", "2025-08-01")
    assert len(request_log) == 0


# -- mlb_standings -------------------------------------------------------------


def test_standings_shape_matches_fixture(injected_client, request_log, fixture_by_name, teams_doc) -> None:
    doc = fixture_by_name("standings")
    result = server.mlb_standings(2025)

    assert result["season"] == 2025 and result["league"] == "all"
    assert result["cached"] is False
    assert result["source_url"] == STANDINGS_URL
    assert request_log.urls == [STANDINGS_URL, TEAMS_URL]

    divisions = result["divisions"]
    assert len(divisions) == len(doc["records"]) == 6
    division_names = {t["division"]["id"]: t["division"]["name"] for t in teams_doc}
    league_names = {t["league"]["id"]: t["league"]["name"] for t in teams_doc}
    for division, record in zip(divisions, doc["records"], strict=True):
        assert set(division) == {"division_id", "division", "league_id", "league", "teams"}
        assert division["division_id"] == record["division"]["id"]
        assert division["division"] == division_names[record["division"]["id"]]
        assert division["league_id"] == record["league"]["id"]
        assert division["league"] == league_names[record["league"]["id"]]
        assert division["teams"] == expected_team_rows(record)
        assert len(division["teams"]) == 5
        assert set(division["teams"][0]) == {"team_id", "team", "w", "l", "pct", "gb", "streak"}
        leader = division["teams"][0]
        assert isinstance(leader["w"], int) and isinstance(leader["l"], int)
        assert leader["gb"] == "-" and leader["pct"].startswith(".") and leader["streak"][0] in "WL"

    assert {d["division"] for d in divisions} == set(division_names.values())


@pytest.mark.parametrize(("league", "league_id"), [("AL", 103), ("NL", 104)])
def test_standings_league_filter_uses_the_same_request(injected_client, request_log, league: str, league_id: int) -> None:
    everything = server.mlb_standings(2025)
    subset = server.mlb_standings(2025, league=league)

    assert subset["league"] == league
    assert subset["cached"] is True  # one URL for both leagues; filtered client-side
    assert request_log.urls == [STANDINGS_URL, TEAMS_URL]
    assert len(subset["divisions"]) == 3
    assert all(d["league_id"] == league_id for d in subset["divisions"])
    assert subset["divisions"] == [d for d in everything["divisions"] if d["league_id"] == league_id]


def test_standings_via_mcp_matches_direct_call(injected_client) -> None:
    direct = server.mlb_standings(2025, league="NL")
    result = call_tool("mlb_standings", {"season": 2025, "league": "NL"})
    assert result.is_error is not True
    assert result.structured_content == {**direct, "cached": True}


def test_standings_rejects_unknown_league(injected_client, request_log) -> None:
    with pytest.raises(ToolError):
        call_tool("mlb_standings", {"season": 2025, "league": "MLB"})
    with pytest.raises(teams.TeamError):
        teams.standings(injected_client, 2025, league="al")
    assert len(request_log) == 0


# -- registration --------------------------------------------------------------


def test_tools_list_includes_team_tools(injected_client) -> None:
    tools = {t.name: t for t in asyncio.run(server.mcp.list_tools())}
    assert len(tools) == 5
    assert {"mlb_team_schedule", "mlb_standings"} <= set(tools)

    schedule = tools["mlb_team_schedule"].input_schema
    assert set(schedule["required"]) == {"team", "start", "end"}
    team_types = {opt["type"] for opt in schedule["properties"]["team"]["anyOf"]}
    assert team_types == {"string", "integer"}

    standings = tools["mlb_standings"].input_schema
    assert standings["required"] == ["season"]
    assert standings["properties"]["league"]["enum"] == ["AL", "NL", "all"]
    assert standings["properties"]["league"]["default"] == "all"
