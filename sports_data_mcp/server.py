"""``sports-data-mcp``: an MCP stdio server over the public MLB Stats API."""

from __future__ import annotations

import argparse
import os
import sys
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from sports_data_mcp import __version__
from sports_data_mcp.mlb import games, players, teams
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


@mcp.tool()
def mlb_team_schedule(team: str | int, start: str, end: str) -> dict[str, Any]:
    """Games for one team over a date range of at most 31 days.

    ``team`` is a team id (``147``) or any name the team goes by: full name
    (``"New York Yankees"``), nickname (``"Yankees"``), abbreviation
    (``"NYY"``), location or short name, matched case-insensitively against
    the cached ``/teams`` list. A name matching several teams (``"New York"``)
    is a tool error listing the candidates. ``start`` and ``end`` are
    inclusive ``YYYY-MM-DD`` dates spanning at most 31 days. Returns
    ``{team_id, team, start, end, games, cached, source_url}`` where each
    game is ``{game_pk, date, home, away, status, home_score, away_score,
    venue}``; scores are null until the API publishes them.
    """
    try:
        games, cached, url, record = teams.team_schedule(get_client(), team, start, end)
    except teams.TeamError as exc:
        raise ToolError(str(exc)) from exc
    except APIError as exc:
        raise ToolError(exc.message) from exc
    return {
        "team_id": record.get("id"),
        "team": record.get("name"),
        "start": start,
        "end": end,
        "games": games,
        "cached": cached,
        "source_url": url,
    }


@mcp.tool()
def mlb_standings(season: int, league: teams.League = "all") -> dict[str, Any]:
    """Division standings for a season.

    Fetches ``/standings`` for both leagues and returns
    ``{season, league, divisions, cached, source_url}``. ``league`` keeps only
    ``"AL"`` or ``"NL"`` divisions; ``"all"`` (the default) returns all six.
    Each division is ``{division_id, division, league_id, league, teams}``
    with teams in rank order as ``{team_id, team, w, l, pct, gb, streak}``.
    ``pct`` and ``gb`` are the API's strings verbatim (``".580"``, ``"-"`` for
    the leader); ``streak`` is a code such as ``"W4"``.
    """
    try:
        divisions, cached, url = teams.standings(get_client(), season, league=league)
    except teams.TeamError as exc:
        raise ToolError(str(exc)) from exc
    except APIError as exc:
        raise ToolError(exc.message) from exc
    return {
        "season": season,
        "league": league,
        "divisions": divisions,
        "cached": cached,
        "source_url": url,
    }


@mcp.tool()
def mlb_game_boxscore(game_pk: int) -> dict[str, Any]:
    """Trimmed box score for one game.

    Fetches ``/game/{game_pk}/boxscore`` (``game_pk`` comes from
    ``mlb_team_schedule``) and returns ``{game_pk, home, away, officials,
    weather, info, cached, source_url}``. ``home`` and ``away`` are each
    ``{team, totals, batting, pitching}``: ``team`` is ``{id, name,
    abbreviation}``, ``totals`` is ``{runs, hits, errors, left_on_base}``,
    ``batting`` has one row per lineup slot holder in lineup order
    (``{player_id, name, position, slot, substitute, ab, r, h, doubles,
    triples, hr, rbi, bb, so, sb, lob, summary}``) and ``pitching`` one row
    per pitcher in order of appearance (``{player_id, name, ip, h, r, er, bb,
    so, hr, pitches, strikes, note, summary}``; ``note`` is the decision such
    as ``"(W, 2-0)"``). ``officials`` is ``[{position, name, id}]``,
    ``weather`` is ``{conditions, wind}`` and ``info`` is ``{venue,
    attendance, first_pitch, duration}``, all null when the API omits them.
    Unknown game ids fail with the API's error message.
    """
    try:
        box, cached, url = games.game_boxscore(get_client(), game_pk)
    except games.GameError as exc:
        raise ToolError(str(exc)) from exc
    except APIError as exc:
        raise ToolError(exc.message) from exc
    return {"game_pk": game_pk, **box, "cached": cached, "source_url": url}


@mcp.tool()
def mlb_live_linescore(game_pk: int) -> dict[str, Any]:
    """Current linescore of one game, suitable for polling.

    Fetches ``/game/{game_pk}/linescore`` with a 30 second cache lifetime
    (instead of the server-wide TTL) and returns ``{game_pk, status, inning,
    half, balls, strikes, outs, innings, totals, cached, source_url}``.
    ``status`` is ``scheduled``, ``in_progress`` or ``final``, derived from
    the count and score because the endpoint has no status field. ``inning``
    is the current inning (null before first pitch), ``half`` is ``top``,
    ``middle``, ``bottom`` or ``end``. ``innings`` is ``[{n, home, away}]``
    with runs per half-inning (null for a half not yet played); ``totals``
    is ``{home: {runs, hits, errors, left_on_base}, away: {...}}``.
    Unknown game ids fail with the API's error message.
    """
    try:
        line, cached, url = games.live_linescore(get_client(), game_pk)
    except games.GameError as exc:
        raise ToolError(str(exc)) from exc
    except APIError as exc:
        raise ToolError(exc.message) from exc
    return {"game_pk": game_pk, **line, "cached": cached, "source_url": url}


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
