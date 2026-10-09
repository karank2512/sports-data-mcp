"""``sports-data-mcp``: an MCP stdio server over the public MLB Stats API."""

from __future__ import annotations

import argparse
import os
import sys
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from sports_data_mcp import __version__
from sports_data_mcp.mlb import players
from sports_data_mcp.mlb.client import DEFAULT_BASE_URL, DEFAULT_CACHE_DIR, DEFAULT_TTL_S, APIError, MLBClient

mcp = MCPServer(
    name="sports-data-mcp",
    version=__version__,
    instructions=(
        "Tools over the public MLB Stats API (statsapi.mlb.com). "
        "Responses are cached on disk; `cached` in a result means no request was made."
    ),
)

_client: MLBClient | None = None


def get_client() -> MLBClient:
    """Return the process-wide :class:`MLBClient`, creating it from env/CLI settings."""
    global _client
    if _client is None:
        _client = MLBClient(
            base_url=os.environ.get("SPORTS_DATA_MCP_BASE_URL", DEFAULT_BASE_URL),
            cache_dir=os.environ.get("SPORTS_DATA_MCP_CACHE_DIR") or DEFAULT_CACHE_DIR,
            ttl_s=float(os.environ.get("SPORTS_DATA_MCP_TTL_S", DEFAULT_TTL_S)),
            offline=os.environ.get("SPORTS_DATA_MCP_OFFLINE", "") in ("1", "true", "yes"),
        )
    return _client


def set_client(client: MLBClient | None) -> None:
    """Replace the process-wide client (tests inject a mock-transport client)."""
    global _client
    _client = client


@mcp.tool()
def mlb_ping() -> dict[str, Any]:
    """Check that the MLB Stats API is reachable.

    Fetches the MLB team list and returns ``{ok, cached, teams}`` where
    ``cached`` says whether the answer came from the on-disk cache and
    ``teams`` is the number of MLB teams returned.
    """
    data, cached = get_client().fetch("/teams", {"sportId": 1})
    teams = data.get("teams", []) if isinstance(data, dict) else []
    return {"ok": True, "cached": cached, "teams": len(teams)}


@mcp.tool()
def mlb_search_player(name: str, active_only: bool = True) -> list[dict[str, Any]]:
    """Find MLB players by name.

    Searches ``/people/search?names=`` and returns up to 10 players as
    ``{id, full_name, team, position, bats, throws, active}``. ``name`` must
    be at least 2 characters. With ``active_only`` (the default) retired and
    inactive people are dropped. Use the ``id`` with ``mlb_player_stats``.
    """
    try:
        found, _cached, _url = players.search_player(get_client(), name, active_only=active_only)
    except players.PlayerError as exc:
        raise ToolError(str(exc)) from exc
    except APIError as exc:
        raise ToolError(exc.message) from exc
    return found


@mcp.tool()
def mlb_player_stats(
    player_id: int,
    season: int,
    group: players.StatGroup = "hitting",
    type: players.StatType = "season",  # noqa: A002 - mirrors the API's query parameter
) -> dict[str, Any]:
    """Season or career stats for one player.

    Fetches ``/people/{player_id}/stats`` for ``group`` (hitting, pitching or
    fielding). ``type="season"`` returns the splits for ``season``;
    ``type="career"`` ignores ``season`` and returns career totals. Each split
    is a flat dict with the API's stat names verbatim (``gamesPlayed``,
    ``avg``, ``era``...) plus ``season``, ``game_type``, ``team``, ``team_id``
    and ``player_id``. The result also carries ``source_url`` and ``cached``.
    Unknown player ids fail with the API's error message.
    """
    try:
        splits, cached, url = players.player_stats(get_client(), player_id, season, group=group, type=type)
    except players.PlayerError as exc:
        raise ToolError(str(exc)) from exc
    except APIError as exc:
        raise ToolError(exc.message) from exc
    return {
        "player_id": player_id,
        "season": season if type == "season" else None,
        "group": group,
        "type": type,
        "splits": splits,
        "cached": cached,
        "source_url": url,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sports-data-mcp",
        description="MCP stdio server over the public MLB Stats API.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument(
        "--cache-dir",
        default=None,
        help=f"directory for cached API responses (default: {DEFAULT_CACHE_DIR})",
    )
    parser.add_argument(
        "--ttl",
        type=float,
        default=None,
        metavar="SECONDS",
        help=f"cache lifetime in seconds (default: {DEFAULT_TTL_S})",
    )
    parser.add_argument(
        "--offline",
        action="store_true",
        help="serve only from the cache; never contact statsapi.mlb.com",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.cache_dir is not None:
        os.environ["SPORTS_DATA_MCP_CACHE_DIR"] = args.cache_dir
    if args.ttl is not None:
        os.environ["SPORTS_DATA_MCP_TTL_S"] = str(args.ttl)
    if args.offline:
        os.environ["SPORTS_DATA_MCP_OFFLINE"] = "1"
    set_client(None)
    mcp.run("stdio")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
