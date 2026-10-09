from __future__ import annotations

import asyncio

import pytest

from sports_data_mcp import server
from sports_data_mcp.mlb.client import MLBClient


@pytest.fixture
def injected_client(client: MLBClient):
    server.set_client(client)
    yield client
    server.set_client(None)


def test_mlb_ping_shape_and_cache_flag(injected_client: MLBClient, request_log, fixture_by_name) -> None:
    first = server.mlb_ping()
    assert first == {"ok": True, "cached": False, "teams": len(fixture_by_name("teams")["teams"])}
    assert request_log.urls == ["https://statsapi.mlb.com/api/v1/teams?sportId=1"]

    second = server.mlb_ping()
    assert second["cached"] is True
    assert second["teams"] == first["teams"]
    assert len(request_log) == 1


def test_mlb_ping_is_registered_and_callable_via_mcp(injected_client: MLBClient, fixture_by_name) -> None:
    tools = asyncio.run(server.mcp.list_tools())
    assert [t.name for t in tools] == ["mlb_ping", "mlb_search_player", "mlb_player_stats"]
    assert tools[0].input_schema.get("properties", {}) == {}

    result = asyncio.run(server.mcp.call_tool("mlb_ping", {}))
    assert result.is_error is not True
    assert result.structured_content == {
        "ok": True, "cached": False, "teams": len(fixture_by_name("teams")["teams"]),
    }


def test_cli_help_exits_zero(capsys) -> None:
    with pytest.raises(SystemExit) as excinfo:
        server.main(["--help"])
    assert excinfo.value.code == 0
    out = capsys.readouterr().out
    assert "sports-data-mcp" in out
    assert "--offline" in out
