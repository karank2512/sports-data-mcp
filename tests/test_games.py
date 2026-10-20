"""Tests for ``mlb_game_boxscore`` and ``mlb_live_linescore``.

The box score comes from the recorded ``boxscore.json`` fixture (a final
game). The linescore endpoint has no recording yet, so this module serves
``tests/fixtures/linescore_live.json`` (hand-written in the recorded shape,
an in-progress game) from its own mock transport and falls back to the
shared fixtures for every other URL. Expected values are derived from the
fixture contents, never hardcoded.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from conftest import url_key
from mcp.server.mcpserver.exceptions import ToolError, UnexpectedToolError

from sports_data_mcp import server
from sports_data_mcp.mlb import games
from sports_data_mcp.mlb.client import MLBClient

FIXTURES_DIR = Path(__file__).parent / "fixtures"
INDEX = json.loads((FIXTURES_DIR / "index.json").read_text(encoding="utf-8"))["fixtures"]
BOXSCORE_URL = INDEX["boxscore"]["url"]
BOXSCORE_PK = int(BOXSCORE_URL.rsplit("/game/", 1)[1].split("/", 1)[0])

LIVE_DOC = json.loads((FIXTURES_DIR / "linescore_live.json").read_text(encoding="utf-8"))
LIVE_PK = 777300
LIVE_URL = f"https://statsapi.mlb.com/api/v1/game/{LIVE_PK}/linescore"
UNKNOWN_PK = 999999999

NOT_FOUND = {"messageNumber": 11, "message": "Game not found", "timestamp": "2025-07-01T19:00:00Z"}


def call_tool(name: str, arguments: dict[str, Any]):
    return asyncio.run(server.mcp.call_tool(name, arguments))


@pytest.fixture
def fixture_transport(fixtures, request_log) -> httpx.MockTransport:
    """Linescore fixture plus 404s for unknown games, falling back to the shared table."""

    def handler(request: httpx.Request) -> httpx.Response:
        request_log.urls.append(str(request.url))
        if request.url.path == f"/api/v1/game/{LIVE_PK}/linescore":
            return httpx.Response(200, json=LIVE_DOC, request=request)
        if request.url.path.startswith(f"/api/v1/game/{UNKNOWN_PK}/"):
            return httpx.Response(404, json=NOT_FOUND, request=request)
        hit = fixtures.get(url_key(request.url))
        if hit is None:
            raise AssertionError(f"no fixture for {request.url}")
        _name, data = hit
        return httpx.Response(200, json=data, request=request)

    return httpx.MockTransport(handler)


@pytest.fixture
def injected_client(client: MLBClient):
    server.set_client(client)
    yield client
    server.set_client(None)


@pytest.fixture
def box_doc(fixture_by_name) -> dict[str, Any]:
    return fixture_by_name("boxscore")


# -- expected values spelled out independently of the implementation -----------------


def expected_lineup(side: dict[str, Any]) -> list[dict[str, Any]]:
    rows = [p for p in side["players"].values() if p.get("battingOrder")]
    rows.sort(key=lambda p: int(p["battingOrder"]))
    out = []
    for p in rows:
        b = p["stats"]["batting"]
        order = int(p["battingOrder"])
        out.append(
            {
                "player_id": p["person"]["id"],
                "name": p["person"]["fullName"],
                "position": "-".join(pos["abbreviation"] for pos in p["allPositions"]),
                "slot": order // 100,
                "substitute": order % 100 != 0,
                "ab": b["atBats"],
                "r": b["runs"],
                "h": b["hits"],
                "doubles": b["doubles"],
                "triples": b["triples"],
                "hr": b["homeRuns"],
                "rbi": b["rbi"],
                "bb": b["baseOnBalls"],
                "so": b["strikeOuts"],
                "sb": b["stolenBases"],
                "lob": b["leftOnBase"],
                "summary": b["summary"],
            }
        )
    return out


def expected_pitching(side: dict[str, Any]) -> list[dict[str, Any]]:
    out = []
    for pid in side["pitchers"]:
        p = side["players"][f"ID{pid}"]
        s = p["stats"]["pitching"]
        out.append(
            {
                "player_id": pid,
                "name": p["person"]["fullName"],
                "ip": s["inningsPitched"],
                "h": s["hits"],
                "r": s["runs"],
                "er": s["earnedRuns"],
                "bb": s["baseOnBalls"],
                "so": s["strikeOuts"],
                "hr": s["homeRuns"],
                "pitches": s["numberOfPitches"],
                "strikes": s["strikes"],
                "note": s.get("note"),
                "summary": s["summary"],
            }
        )
    return out


def info_value(doc: dict[str, Any], label: str) -> str:
    value = next(item["value"] for item in doc["info"] if item.get("label") == label)
    return value.rstrip(".").rstrip()


# -- mlb_game_boxscore ---------------------------------------------------------------


def test_boxscore_shape_matches_fixture(injected_client, request_log, box_doc) -> None:
    result = server.mlb_game_boxscore(BOXSCORE_PK)

    assert set(result) == {"game_pk", "home", "away", "officials", "weather", "info", "cached", "source_url"}
    assert result["game_pk"] == BOXSCORE_PK
    assert result["cached"] is False
    assert result["source_url"] == BOXSCORE_URL
    assert request_log.urls == [BOXSCORE_URL]

    for key in ("home", "away"):
        side, doc_side = result[key], box_doc["teams"][key]
        assert set(side) == {"team", "totals", "batting", "pitching"}
        assert side["team"] == {
            "id": doc_side["team"]["id"],
            "name": doc_side["team"]["name"],
            "abbreviation": doc_side["team"]["abbreviation"],
        }
        assert side["totals"] == {
            "runs": doc_side["teamStats"]["batting"]["runs"],
            "hits": doc_side["teamStats"]["batting"]["hits"],
            "errors": doc_side["teamStats"]["fielding"]["errors"],
            "left_on_base": doc_side["teamStats"]["batting"]["leftOnBase"],
        }
        assert side["batting"] == expected_lineup(doc_side)
        assert side["pitching"] == expected_pitching(doc_side)
        assert len(side["pitching"]) == len(doc_side["pitchers"]) > 0
        assert 9 <= len(side["batting"]) < len(doc_side["batters"])  # pitchers who never batted are dropped

    assert result["officials"] == [
        {"position": o["officialType"], "name": o["official"]["fullName"], "id": o["official"]["id"]}
        for o in box_doc["officials"]
    ]
    assert result["officials"][0]["position"] == "Home Plate"
    assert result["weather"] == {"conditions": info_value(box_doc, "Weather"), "wind": info_value(box_doc, "Wind")}
    assert result["weather"]["conditions"].endswith("Sunny")  # trailing period stripped
    assert result["info"] == {
        "venue": info_value(box_doc, "Venue"),
        "attendance": info_value(box_doc, "Att"),
        "first_pitch": info_value(box_doc, "First pitch"),
        "duration": info_value(box_doc, "T"),
    }


def test_boxscore_rows_are_flat_and_add_up(injected_client) -> None:
    result = server.mlb_game_boxscore(BOXSCORE_PK)
    for key in ("home", "away"):
        side = result[key]
        for row in side["batting"] + side["pitching"]:
            assert all(not isinstance(v, (dict, list)) for v in row.values())
        # Lineup order: slots 1..9 non-decreasing, substitutes flagged, starters not.
        slots = [row["slot"] for row in side["batting"]]
        assert slots == sorted(slots) and slots[0] == 1 and slots[-1] == 9
        assert {row["slot"] for row in side["batting"]} == set(range(1, 10))
        starters = [row for row in side["batting"] if not row["substitute"]]
        assert len(starters) == 9
        # Individual lines add up to the team totals.
        assert sum(row["r"] for row in side["batting"]) == side["totals"]["runs"]
        assert sum(row["h"] for row in side["batting"]) == side["totals"]["hits"]
        assert all(row["ip"] and row["pitches"] > 0 for row in side["pitching"])

    assert any(row["substitute"] for row in result["home"]["batting"] + result["away"]["batting"])


def test_boxscore_pitcher_decisions(injected_client) -> None:
    result = server.mlb_game_boxscore(BOXSCORE_PK)
    home, away = result["home"], result["away"]
    assert home["totals"]["runs"] != away["totals"]["runs"]  # the fixture is a final game with a winner
    winner, loser = (home, away) if home["totals"]["runs"] > away["totals"]["runs"] else (away, home)
    assert sum(1 for p in winner["pitching"] if (p["note"] or "").startswith("(W")) == 1
    assert sum(1 for p in loser["pitching"] if (p["note"] or "").startswith("(L")) == 1
    assert any(p["note"] is None for p in winner["pitching"] + loser["pitching"])


def test_boxscore_via_mcp_matches_direct_call(injected_client) -> None:
    direct = server.mlb_game_boxscore(BOXSCORE_PK)
    result = call_tool("mlb_game_boxscore", {"game_pk": BOXSCORE_PK})
    assert result.is_error is not True
    assert result.structured_content == {**direct, "cached": True}


@pytest.mark.parametrize("bad", [0, -5])
def test_boxscore_rejects_bad_game_pk_before_any_request(injected_client, request_log, bad: int) -> None:
    with pytest.raises(ToolError) as excinfo:
        call_tool("mlb_game_boxscore", {"game_pk": bad})
    assert not isinstance(excinfo.value, UnexpectedToolError)
    assert "game_pk" in str(excinfo.value)
    assert len(request_log) == 0


def test_boxscore_unknown_game_is_tool_error_with_api_message(injected_client, request_log) -> None:
    with pytest.raises(ToolError) as excinfo:
        call_tool("mlb_game_boxscore", {"game_pk": UNKNOWN_PK})
    assert not isinstance(excinfo.value, UnexpectedToolError)
    assert "HTTP 404" in str(excinfo.value) and "Game not found" in str(excinfo.value)
    assert len(request_log) == 1  # 4xx is not retried


def test_summarize_boxscore_tolerates_empty_document() -> None:
    empty = games.summarize_boxscore({})
    assert empty["home"]["batting"] == [] and empty["away"]["pitching"] == []
    assert empty["home"]["team"] == {"id": None, "name": None, "abbreviation": None}
    assert empty["officials"] == []
    assert empty["weather"] == {"conditions": None, "wind": None}
    assert set(empty["info"]) == {"venue", "attendance", "first_pitch", "duration"}
    assert games.summarize_boxscore(None)["home"]["totals"]["runs"] is None


# -- mlb_live_linescore --------------------------------------------------------------


def test_live_linescore_shape_matches_fixture(injected_client, request_log) -> None:
    result = server.mlb_live_linescore(LIVE_PK)

    assert set(result) == {
        "game_pk", "status", "inning", "half", "balls", "strikes", "outs", "innings", "totals", "cached", "source_url",
    }
    assert result["game_pk"] == LIVE_PK
    assert result["cached"] is False
    assert result["source_url"] == LIVE_URL
    assert request_log.urls == [LIVE_URL]

    assert result["status"] == "in_progress"
    assert result["inning"] == LIVE_DOC["currentInning"]
    assert result["half"] == LIVE_DOC["inningState"].lower() == "bottom"
    assert (result["balls"], result["strikes"], result["outs"]) == (LIVE_DOC["balls"], LIVE_DOC["strikes"], LIVE_DOC["outs"])

    assert result["innings"] == [
        {"n": i["num"], "home": i["home"].get("runs"), "away": i["away"].get("runs")} for i in LIVE_DOC["innings"]
    ]
    assert len(result["innings"]) == LIVE_DOC["currentInning"]
    assert result["innings"][-1]["home"] is None  # the half being played has no runs entry yet
    assert all(isinstance(i["away"], int) for i in result["innings"])

    for key in ("home", "away"):
        side = LIVE_DOC["teams"][key]
        assert result["totals"][key] == {
            "runs": side["runs"], "hits": side["hits"], "errors": side["errors"], "left_on_base": side["leftOnBase"],
        }
        assert sum(i[key] or 0 for i in result["innings"]) == result["totals"][key]["runs"]


def test_live_linescore_uses_30s_ttl_through_the_client(injected_client, request_log, clock) -> None:
    assert games.LIVE_TTL_S == 30

    first = server.mlb_live_linescore(LIVE_PK)
    assert first["cached"] is False and request_log.urls == [LIVE_URL]

    second = server.mlb_live_linescore(LIVE_PK)
    assert second["cached"] is True and len(request_log) == 1

    clock.advance(29)
    assert server.mlb_live_linescore(LIVE_PK)["cached"] is True and len(request_log) == 1

    clock.advance(1)  # age is now 30 s: the entry has expired even though the client TTL is 3600 s
    assert injected_client.ttl_s == 3600
    third = server.mlb_live_linescore(LIVE_PK)
    assert third["cached"] is False and request_log.urls == [LIVE_URL, LIVE_URL]
    assert {k: v for k, v in third.items() if k != "cached"} == {k: v for k, v in first.items() if k != "cached"}


def test_boxscore_keeps_the_default_ttl(injected_client, request_log, clock) -> None:
    server.mlb_game_boxscore(BOXSCORE_PK)
    clock.advance(31)
    assert server.mlb_game_boxscore(BOXSCORE_PK)["cached"] is True
    assert request_log.urls == [BOXSCORE_URL]


def test_live_linescore_via_mcp_matches_direct_call(injected_client) -> None:
    direct = server.mlb_live_linescore(LIVE_PK)
    result = call_tool("mlb_live_linescore", {"game_pk": LIVE_PK})
    assert result.is_error is not True
    assert result.structured_content == {**direct, "cached": True}


def test_live_linescore_offline_cache_miss_is_tool_error(injected_client, request_log) -> None:
    injected_client.offline = True
    with pytest.raises(ToolError) as excinfo:
        call_tool("mlb_live_linescore", {"game_pk": LIVE_PK})
    assert not isinstance(excinfo.value, UnexpectedToolError)
    assert "offline" in str(excinfo.value)
    assert len(request_log) == 0


@pytest.mark.parametrize("bad", [0, -1])
def test_live_linescore_rejects_bad_game_pk(injected_client, request_log, bad: int) -> None:
    with pytest.raises(ToolError) as excinfo:
        call_tool("mlb_live_linescore", {"game_pk": bad})
    assert not isinstance(excinfo.value, UnexpectedToolError)
    assert len(request_log) == 0
    with pytest.raises(games.GameError):
        games.live_linescore(injected_client, bad)


def test_live_linescore_unknown_game_is_tool_error(injected_client, request_log) -> None:
    with pytest.raises(ToolError) as excinfo:
        call_tool("mlb_live_linescore", {"game_pk": UNKNOWN_PK})
    assert "HTTP 404" in str(excinfo.value)
    assert request_log.urls == [f"https://statsapi.mlb.com/api/v1/game/{UNKNOWN_PK}/linescore"]


# -- status derivation ---------------------------------------------------------------


def linescore(inning: int | None, half: str, outs: int, home: int | None, away: int | None, scheduled: int = 9):
    doc: dict[str, Any] = {"scheduledInnings": scheduled, "teams": {"home": {}, "away": {}}, "innings": []}
    if inning is None:
        return doc
    doc.update(
        {
            "currentInning": inning,
            "inningState": half,
            "isTopInning": half in ("Top", "Middle"),
            "balls": 0,
            "strikes": 0,
            "outs": outs,
            "teams": {"home": {"runs": home}, "away": {"runs": away}},
        }
    )
    return doc


def test_status_of_the_live_fixture_and_a_final_game_from_the_boxscore_fixture(box_doc) -> None:
    assert games.linescore_status(LIVE_DOC) == "in_progress"
    # The recorded box score is a final game: replay its final score as a 9th-inning linescore.
    home_runs = box_doc["teams"]["home"]["teamStats"]["batting"]["runs"]
    away_runs = box_doc["teams"]["away"]["teamStats"]["batting"]["runs"]
    assert home_runs != away_runs
    assert games.linescore_status(linescore(9, "Bottom", 3, home_runs, away_runs)) == "final"
    assert games.summarize_linescore(linescore(9, "Bottom", 3, home_runs, away_runs))["status"] == "final"


@pytest.mark.parametrize(
    ("doc", "status"),
    [
        (linescore(None, "", 0, None, None), "scheduled"),
        ({}, "scheduled"),
        (linescore(1, "Top", 0, 0, 0), "in_progress"),
        (linescore(8, "Bottom", 3, 5, 1), "in_progress"),  # regulation not yet complete
        (linescore(9, "Top", 3, 5, 1), "final"),  # home leads after the visitors' 9th; no bottom half
        (linescore(9, "Middle", 3, 5, 1), "final"),
        (linescore(9, "Top", 3, 1, 5), "in_progress"),  # home still bats
        (linescore(9, "Top", 2, 5, 1), "in_progress"),
        (linescore(9, "Bottom", 3, 1, 5), "final"),
        (linescore(9, "End", 3, 1, 5), "final"),
        (linescore(9, "Bottom", 3, 4, 4), "in_progress"),  # tied: extra innings
        (linescore(9, "Bottom", 1, 6, 5), "final"),  # walk-off
        (linescore(9, "Bottom", 1, 5, 6), "in_progress"),
        (linescore(11, "Bottom", 3, 7, 8), "final"),
        (linescore(7, "Bottom", 3, 2, 4, scheduled=7), "final"),  # doubleheader of seven
        (linescore(7, "Bottom", 3, 2, 4), "in_progress"),
    ],
)
def test_linescore_status_derivation(doc: dict[str, Any], status: str) -> None:
    assert games.linescore_status(doc) == status


def test_summarize_linescore_tolerates_empty_document() -> None:
    empty = games.summarize_linescore({})
    assert empty["status"] == "scheduled"
    assert empty["inning"] is None and empty["half"] is None and empty["outs"] is None
    assert empty["innings"] == []
    assert empty["totals"]["home"] == {"runs": None, "hits": None, "errors": None, "left_on_base": None}
    assert games.summarize_linescore(None) == empty


# -- registration --------------------------------------------------------------------


def test_tools_list_includes_game_tools(injected_client) -> None:
    tools = {t.name: t for t in asyncio.run(server.mcp.list_tools())}
    assert len(tools) == 7
    assert {"mlb_game_boxscore", "mlb_live_linescore"} <= set(tools)
    for name in ("mlb_game_boxscore", "mlb_live_linescore"):
        schema = tools[name].input_schema
        assert schema["required"] == ["game_pk"]
        assert schema["properties"]["game_pk"]["type"] == "integer"
    assert "30 second" in tools["mlb_live_linescore"].description
