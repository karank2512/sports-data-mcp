"""Team lookups over the MLB Stats API: name resolution, schedules, standings.

Pure functions over an :class:`MLBClient`. They raise :class:`TeamError`
for caller mistakes and let :class:`APIError` propagate so the server layer
can turn both into MCP tool errors.

Team names are resolved against the ``/teams?sportId=1`` list, which the
client caches on disk, so repeated lookups cost no requests.
"""

from __future__ import annotations

from datetime import date
from typing import Any, Literal

from sports_data_mcp.mlb.client import MLBClient

MAX_RANGE_DAYS = 31

League = Literal["AL", "NL", "all"]
LEAGUES: tuple[str, ...] = ("AL", "NL", "all")
LEAGUE_IDS: dict[str, int] = {"AL": 103, "NL": 104}

TEAMS_PATH, TEAMS_PARAMS = "/teams", {"sportId": 1}

# ``/teams`` fields a caller might type for a team, in the order they are shown back.
NAME_FIELDS: tuple[str, ...] = (
    "name",
    "abbreviation",
    "teamName",
    "shortName",
    "locationName",
    "franchiseName",
    "clubName",
)


class TeamError(ValueError):
    """A team request that cannot be made (unknown or ambiguous team, bad dates, bad league)."""


# -- /teams --------------------------------------------------------------------


def list_teams(client: MLBClient) -> tuple[list[dict[str, Any]], bool]:
    """Return ``(teams, cached)`` from ``/teams?sportId=1``."""
    data, cached = client.fetch(TEAMS_PATH, TEAMS_PARAMS)
    teams = data.get("teams", []) if isinstance(data, dict) else []
    return [t for t in teams if isinstance(t, dict)], cached


def resolve_team(client: MLBClient, team: str | int) -> tuple[dict[str, Any], bool]:
    """Find one ``/teams`` record for an id, a name or an abbreviation.

    ``team`` may be an integer id (or a string of digits), or any of the
    team's names: full name (``"New York Yankees"``), nickname
    (``"Yankees"``), abbreviation (``"NYY"``), short name, location,
    franchise or club name. Matching is case-insensitive. An exact match on
    any of those fields wins; failing that, a substring match is tried.

    Returns ``(team_record, cached)``.

    Raises:
        TeamError: if nothing matches, or if more than one team matches
            (the message lists the candidates so the caller can pick one).
    """
    teams, cached = list_teams(client)

    if isinstance(team, bool):  # bool is an int subclass; never a team id
        raise TeamError(f"team must be an id or a name, got {team!r}")
    if isinstance(team, int) or (isinstance(team, str) and team.strip().isdigit()):
        team_id = int(team)
        for record in teams:
            if record.get("id") == team_id:
                return record, cached
        raise TeamError(f"no MLB team with id {team_id}")

    query = " ".join(str(team).split()).casefold()
    if not query:
        raise TeamError("team must be a non-empty name, abbreviation or id")

    exact = [t for t in teams if query in _aliases(t)]
    if len(exact) == 1:
        return exact[0], cached
    if not exact:
        exact = [t for t in teams if any(query in alias for alias in _aliases(t))]
    if len(exact) == 1:
        return exact[0], cached
    if not exact:
        raise TeamError(f"no MLB team matches {team!r}; try the full name, nickname or abbreviation")
    raise TeamError(f"ambiguous team {team!r}; candidates: {', '.join(_describe(t) for t in exact)}")


def _aliases(team: dict[str, Any]) -> set[str]:
    return {" ".join(str(team[f]).split()).casefold() for f in NAME_FIELDS if team.get(f)}


def _describe(team: dict[str, Any]) -> str:
    return f"{team.get('name')} ({team.get('abbreviation')}, id {team.get('id')})"


# -- /schedule -----------------------------------------------------------------


def validate_range(start: str, end: str) -> tuple[date, date]:
    """Parse ``YYYY-MM-DD`` bounds and enforce the :data:`MAX_RANGE_DAYS` cap (inclusive)."""
    try:
        first, last = date.fromisoformat(start), date.fromisoformat(end)
    except (TypeError, ValueError) as exc:
        raise TeamError(f"dates must be YYYY-MM-DD, got start={start!r} end={end!r}") from exc
    if last < first:
        raise TeamError(f"end {end} is before start {start}")
    days = (last - first).days + 1
    if days > MAX_RANGE_DAYS:
        raise TeamError(f"date range is {days} days; at most {MAX_RANGE_DAYS} days per call")
    return first, last


def team_schedule(
    client: MLBClient, team: str | int, start: str, end: str
) -> tuple[list[dict[str, Any]], bool, str, dict[str, Any]]:
    """Games for one team between ``start`` and ``end`` (inclusive, at most 31 days).

    Returns ``(games, cached, source_url, team_record)``. Each game is
    ``{game_pk, date, home, away, status, home_score, away_score, venue}``.
    Scores are ``None`` until the API publishes them.
    """
    first, last = validate_range(start, end)
    record, _teams_cached = resolve_team(client, team)
    path = "/schedule"
    params: dict[str, Any] = {
        "sportId": 1,
        "teamId": int(record["id"]),
        "startDate": first.isoformat(),
        "endDate": last.isoformat(),
    }
    data, cached = client.fetch(path, params)
    return flatten_schedule(data), cached, client.url_for(path, params), record


def flatten_schedule(data: Any) -> list[dict[str, Any]]:
    """Flatten every ``dates[*].games[*]`` of a schedule response into one list."""
    out: list[dict[str, Any]] = []
    if not isinstance(data, dict):
        return out
    for day in data.get("dates", []) or []:
        if not isinstance(day, dict):
            continue
        for game in day.get("games", []) or []:
            if isinstance(game, dict):
                out.append(summarize_game(game, default_date=day.get("date")))
    return out


def summarize_game(game: dict[str, Any], default_date: str | None = None) -> dict[str, Any]:
    teams = game.get("teams") or {}
    home = teams.get("home") or {}
    away = teams.get("away") or {}
    return {
        "game_pk": game.get("gamePk"),
        "date": game.get("officialDate") or default_date,
        "home": (home.get("team") or {}).get("name"),
        "away": (away.get("team") or {}).get("name"),
        "status": (game.get("status") or {}).get("detailedState"),
        "home_score": home.get("score"),
        "away_score": away.get("score"),
        "venue": (game.get("venue") or {}).get("name"),
    }


# -- /standings ----------------------------------------------------------------


def standings(client: MLBClient, season: int, league: str = "all") -> tuple[list[dict[str, Any]], bool, str]:
    """Division standings for ``season``.

    Both leagues are always fetched (one cache entry) and ``league`` filters
    the result to ``"AL"``, ``"NL"`` or ``"all"``. Returns
    ``(divisions, cached, source_url)``; each division is
    ``{division_id, division, league, teams}`` with teams as
    ``{team_id, team, w, l, pct, gb, streak}`` in division-rank order.
    Division and league names come from the cached ``/teams`` list.
    """
    if league not in LEAGUES:
        raise TeamError(f"league must be one of {', '.join(LEAGUES)}, got {league!r}")
    path = "/standings"
    params: dict[str, Any] = {"leagueId": "103,104", "season": int(season)}
    data, cached = client.fetch(path, params)
    teams, _teams_cached = list_teams(client)
    divisions = flatten_standings(data, teams)
    if league != "all":
        divisions = [d for d in divisions if d["league_id"] == LEAGUE_IDS[league]]
    return divisions, cached, client.url_for(path, params)


def flatten_standings(data: Any, teams: list[dict[str, Any]]) -> list[dict[str, Any]]:
    division_names: dict[int, str] = {}
    league_names: dict[int, str] = {}
    for t in teams:
        division, league = t.get("division") or {}, t.get("league") or {}
        if division.get("id") is not None and division.get("name"):
            division_names[division["id"]] = division["name"]
        if league.get("id") is not None and league.get("name"):
            league_names[league["id"]] = league["name"]

    out: list[dict[str, Any]] = []
    if not isinstance(data, dict):
        return out
    for record in data.get("records", []) or []:
        if not isinstance(record, dict):
            continue
        division_id = (record.get("division") or {}).get("id")
        league_id = (record.get("league") or {}).get("id")
        out.append(
            {
                "division_id": division_id,
                "division": division_names.get(division_id, f"Division {division_id}"),
                "league_id": league_id,
                "league": league_names.get(league_id, f"League {league_id}"),
                "teams": [summarize_team_record(r) for r in record.get("teamRecords", []) or [] if isinstance(r, dict)],
            }
        )
    return out


def summarize_team_record(record: dict[str, Any]) -> dict[str, Any]:
    """Reduce one ``teamRecords`` entry to ``{team_id, team, w, l, pct, gb, streak}``.

    ``pct`` and ``gb`` are the API's strings verbatim (``".580"``, ``"-"`` for
    the division leader, ``"3.5"``); ``streak`` is the code such as ``"W4"``.
    """
    team = record.get("team") or {}
    return {
        "team_id": team.get("id"),
        "team": team.get("name"),
        "w": record.get("wins"),
        "l": record.get("losses"),
        "pct": record.get("winningPercentage"),
        "gb": record.get("gamesBack"),
        "streak": (record.get("streak") or {}).get("streakCode"),
    }
