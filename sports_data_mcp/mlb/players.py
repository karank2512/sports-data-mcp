"""Player lookups over the MLB Stats API: name search and stat splits.

Pure functions over an :class:`MLBClient`. They raise :class:`PlayerError`
for caller mistakes and let :class:`APIError` propagate so the server layer
can turn both into MCP tool errors.
"""

from __future__ import annotations

from typing import Any, Literal

from sports_data_mcp.mlb.client import MLBClient

SEARCH_LIMIT = 10
MIN_NAME_LENGTH = 2

StatGroup = Literal["hitting", "pitching", "fielding"]
StatType = Literal["season", "career"]

STAT_GROUPS: tuple[str, ...] = ("hitting", "pitching", "fielding")
STAT_TYPES: tuple[str, ...] = ("season", "career")


class PlayerError(ValueError):
    """A player request that cannot be made (bad name, group or type)."""


def search_player(client: MLBClient, name: str, active_only: bool = True) -> tuple[list[dict[str, Any]], bool, str]:
    """Search people by name via ``/people/search?names=``.

    Returns ``(players, cached, source_url)`` with at most :data:`SEARCH_LIMIT`
    players. ``active_only`` keeps only people whose ``active`` flag is true.
    """
    query = name.strip()
    if len(query) < MIN_NAME_LENGTH:
        raise PlayerError(f"name must be at least {MIN_NAME_LENGTH} characters, got {name!r}")
    path, params = "/people/search", {"names": query, "sportIds": 1}
    data, cached = client.fetch(path, params)
    people = data.get("people", []) if isinstance(data, dict) else []
    players = [summarize_person(p) for p in people if isinstance(p, dict)]
    if active_only:
        players = [p for p in players if p["active"]]
    return players[:SEARCH_LIMIT], cached, client.url_for(path, params)


def summarize_person(person: dict[str, Any]) -> dict[str, Any]:
    """Reduce a ``/people`` record to ``{id, full_name, team, position, bats, throws, active}``."""
    team = person.get("currentTeam") or {}
    position = person.get("primaryPosition") or {}
    return {
        "id": person.get("id"),
        "full_name": person.get("fullName"),
        "team": team.get("name"),
        "position": position.get("abbreviation") or position.get("name"),
        "bats": (person.get("batSide") or {}).get("code"),
        "throws": (person.get("pitchHand") or {}).get("code"),
        "active": bool(person.get("active", False)),
    }


def player_stats(
    client: MLBClient,
    player_id: int,
    season: int,
    group: str = "hitting",
    type: str = "season",  # noqa: A002 - mirrors the API's query parameter
) -> tuple[list[dict[str, Any]], bool, str]:
    """Fetch ``/people/{id}/stats`` and flatten each split.

    Returns ``(splits, cached, source_url)``. Each split is a flat dict: the
    API's ``stat`` keys verbatim (``gamesPlayed``, ``avg``, ``era``...) plus
    ``season``, ``game_type``, ``team``, ``team_id`` and ``player_id`` context.
    """
    if group not in STAT_GROUPS:
        raise PlayerError(f"group must be one of {', '.join(STAT_GROUPS)}, got {group!r}")
    if type not in STAT_TYPES:
        raise PlayerError(f"type must be one of {', '.join(STAT_TYPES)}, got {type!r}")
    path = f"/people/{int(player_id)}/stats"
    params: dict[str, Any] = {"stats": type, "group": group}
    if type == "season":
        params["season"] = int(season)
    data, cached = client.fetch(path, params)
    return flatten_stats(data), cached, client.url_for(path, params)


def flatten_stats(data: Any) -> list[dict[str, Any]]:
    """Flatten every ``stats[*].splits[*]`` of a stats response into one list."""
    out: list[dict[str, Any]] = []
    if not isinstance(data, dict):
        return out
    for block in data.get("stats", []) or []:
        if not isinstance(block, dict):
            continue
        for split in block.get("splits", []) or []:
            if isinstance(split, dict):
                out.append(flatten_split(split))
    return out


def flatten_split(split: dict[str, Any]) -> dict[str, Any]:
    team = split.get("team") or {}
    player = split.get("player") or {}
    flat: dict[str, Any] = {
        "season": split.get("season"),
        "game_type": split.get("gameType"),
        "team": team.get("name"),
        "team_id": team.get("id"),
        "player_id": player.get("id"),
    }
    stat = split.get("stat") or {}
    if isinstance(stat, dict):
        flat.update(stat)
    return flat
