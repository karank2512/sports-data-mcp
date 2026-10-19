"""Single-game lookups over the MLB Stats API: box scores and live linescores.

Pure functions over an :class:`MLBClient`. They raise :class:`GameError`
for caller mistakes and let :class:`APIError` propagate so the server layer
can turn both into MCP tool errors.

The box score endpoint returns several hundred kilobytes per game; the
functions here trim it to what a game summary needs. The kept fields are
listed on :func:`summarize_boxscore`.
"""

from __future__ import annotations

from typing import Any

from sports_data_mcp.mlb.client import MLBClient

# A linescore changes pitch by pitch, so it is cached far more briefly than
# the client's default (an hour). Thirty seconds keeps a polling agent from
# hammering the API without serving stale counts for long.
LIVE_TTL_S = 30.0

DEFAULT_SCHEDULED_INNINGS = 9

# ``info`` labels in a box score that describe the game rather than a team.
WEATHER_LABEL, WIND_LABEL = "Weather", "Wind"
INFO_LABELS: dict[str, str] = {
    "Venue": "venue",
    "Att": "attendance",
    "First pitch": "first_pitch",
    "T": "duration",
}


class GameError(ValueError):
    """A game request that cannot be made (bad game id)."""


def validate_game_pk(game_pk: Any) -> int:
    """Return ``game_pk`` as a positive int or raise :class:`GameError`."""
    if isinstance(game_pk, bool) or not isinstance(game_pk, int) or game_pk <= 0:
        raise GameError(f"game_pk must be a positive integer game id, got {game_pk!r}")
    return game_pk


# -- /game/{pk}/boxscore -----------------------------------------------------------


def game_boxscore(client: MLBClient, game_pk: int) -> tuple[dict[str, Any], bool, str]:
    """Trimmed box score for one game.

    Returns ``(boxscore, cached, source_url)``; see :func:`summarize_boxscore`
    for the shape.
    """
    pk = validate_game_pk(game_pk)
    path = f"/game/{pk}/boxscore"
    data, cached = client.fetch(path)
    return summarize_boxscore(data), cached, client.url_for(path)


def summarize_boxscore(data: Any) -> dict[str, Any]:
    """Reduce a ``/game/{pk}/boxscore`` document to the fields a summary needs.

    Kept fields::

        home / away:
          team:    {id, name, abbreviation}
          totals:  {runs, hits, errors, left_on_base}   (team batting/fielding totals)
          batting: one row per player who had a lineup slot, in lineup order:
                   {player_id, name, position, slot, substitute,
                    ab, r, h, doubles, triples, hr, rbi, bb, so, sb, lob, summary}
          pitching: one row per pitcher used, in order of appearance:
                   {player_id, name, ip, h, r, er, bb, so, hr, pitches, strikes,
                    note, summary}
        officials: [{position, name, id}] in the API's order (home plate first)
        weather:   {conditions, wind}  (strings from the box score ``info`` notes,
                   null when absent)
        info:      {venue, attendance, first_pitch, duration}  (same source)

    ``position`` is every position the player appeared at, joined with ``-``
    (``"PH-3B"``); ``slot`` is the lineup slot 1-9 and ``substitute`` is true
    for a player who took over that slot. ``note`` is the pitcher's decision
    verbatim (``"(W, 2-0)"``, ``"(L, 4-6)"``, ``"(H, 4)"``) or null.
    ``summary`` is the API's one-line stat summary for the player.
    Everything else in the box score (season stats, play-by-play notes,
    bench and bullpen lists, per-team notes, top performers) is dropped.
    """
    doc = data if isinstance(data, dict) else {}
    teams = doc.get("teams") or {}
    info = _info_map(doc.get("info"))
    return {
        "home": summarize_team(teams.get("home")),
        "away": summarize_team(teams.get("away")),
        "officials": [
            {
                "position": o.get("officialType"),
                "name": (o.get("official") or {}).get("fullName"),
                "id": (o.get("official") or {}).get("id"),
            }
            for o in doc.get("officials") or []
            if isinstance(o, dict)
        ],
        "weather": {"conditions": info.get(WEATHER_LABEL), "wind": info.get(WIND_LABEL)},
        "info": {field: info.get(label) for label, field in INFO_LABELS.items()},
    }


def summarize_team(side: Any) -> dict[str, Any]:
    """One side of :func:`summarize_boxscore` (``home`` or ``away``)."""
    team_doc = side if isinstance(side, dict) else {}
    team = team_doc.get("team") or {}
    stats = team_doc.get("teamStats") or {}
    batting_totals = stats.get("batting") or {}
    fielding_totals = stats.get("fielding") or {}
    players = {k: v for k, v in (team_doc.get("players") or {}).items() if isinstance(v, dict)}

    lineup = [p for p in players.values() if _order(p) is not None]
    lineup.sort(key=lambda p: _order(p) or 0)

    by_id = {(p.get("person") or {}).get("id"): p for p in players.values()}
    pitchers = [by_id[pid] for pid in team_doc.get("pitchers") or [] if pid in by_id]

    return {
        "team": {"id": team.get("id"), "name": team.get("name"), "abbreviation": team.get("abbreviation")},
        "totals": {
            "runs": batting_totals.get("runs"),
            "hits": batting_totals.get("hits"),
            "errors": fielding_totals.get("errors"),
            "left_on_base": batting_totals.get("leftOnBase"),
        },
        "batting": [summarize_batter(p) for p in lineup],
        "pitching": [summarize_pitcher(p) for p in pitchers],
    }


def summarize_batter(player: dict[str, Any]) -> dict[str, Any]:
    person = player.get("person") or {}
    batting = (player.get("stats") or {}).get("batting") or {}
    order = _order(player) or 0
    return {
        "player_id": person.get("id"),
        "name": person.get("fullName"),
        "position": _positions(player),
        "slot": order // 100,
        "substitute": order % 100 != 0,
        "ab": batting.get("atBats"),
        "r": batting.get("runs"),
        "h": batting.get("hits"),
        "doubles": batting.get("doubles"),
        "triples": batting.get("triples"),
        "hr": batting.get("homeRuns"),
        "rbi": batting.get("rbi"),
        "bb": batting.get("baseOnBalls"),
        "so": batting.get("strikeOuts"),
        "sb": batting.get("stolenBases"),
        "lob": batting.get("leftOnBase"),
        "summary": batting.get("summary"),
    }


def summarize_pitcher(player: dict[str, Any]) -> dict[str, Any]:
    person = player.get("person") or {}
    pitching = (player.get("stats") or {}).get("pitching") or {}
    return {
        "player_id": person.get("id"),
        "name": person.get("fullName"),
        "ip": pitching.get("inningsPitched"),
        "h": pitching.get("hits"),
        "r": pitching.get("runs"),
        "er": pitching.get("earnedRuns"),
        "bb": pitching.get("baseOnBalls"),
        "so": pitching.get("strikeOuts"),
        "hr": pitching.get("homeRuns"),
        "pitches": pitching.get("numberOfPitches"),
        "strikes": pitching.get("strikes"),
        "note": pitching.get("note"),
        "summary": pitching.get("summary"),
    }


def _order(player: dict[str, Any]) -> int | None:
    """The API's ``battingOrder`` (``"100"``..``"901"``) as an int, or None for non-batters."""
    raw = player.get("battingOrder")
    if raw in (None, ""):
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def _positions(player: dict[str, Any]) -> str | None:
    codes = [p.get("abbreviation") for p in player.get("allPositions") or [] if isinstance(p, dict)]
    codes = [c for c in codes if c]
    if not codes:
        primary = (player.get("position") or {}).get("abbreviation")
        return primary or None
    return "-".join(codes)


def _info_map(info: Any) -> dict[str, str]:
    """``info: [{label, value}]`` as a dict, with the API's trailing periods removed."""
    out: dict[str, str] = {}
    for item in info or []:
        if not isinstance(item, dict) or "label" not in item or "value" not in item:
            continue
        value = str(item["value"]).strip()
        out[str(item["label"])] = value[:-1].rstrip() if value.endswith(".") else value
    return out


# -- /game/{pk}/linescore ----------------------------------------------------------


def live_linescore(client: MLBClient, game_pk: int) -> tuple[dict[str, Any], bool, str]:
    """Current linescore for one game, cached for only :data:`LIVE_TTL_S` seconds.

    Returns ``(linescore, cached, source_url)``; see :func:`summarize_linescore`.
    """
    pk = validate_game_pk(game_pk)
    path = f"/game/{pk}/linescore"
    data, cached = client.fetch(path, ttl_s=LIVE_TTL_S)
    return summarize_linescore(data), cached, client.url_for(path)


def summarize_linescore(data: Any) -> dict[str, Any]:
    """Reduce a ``/game/{pk}/linescore`` document to
    ``{status, inning, half, balls, strikes, outs, innings, totals}``.

    ``inning`` is the current inning number (null before first pitch) and
    ``half`` is the API's ``inningState`` lowercased: ``top``, ``middle``,
    ``bottom`` or ``end``. ``innings`` is ``[{n, home, away}]`` with runs per
    half-inning (null for a half not yet played). ``totals`` is
    ``{home: {runs, hits, errors, left_on_base}, away: {...}}``.
    ``status`` is derived by :func:`linescore_status`.
    """
    doc = data if isinstance(data, dict) else {}
    teams = doc.get("teams") or {}
    half = doc.get("inningState")
    return {
        "status": linescore_status(doc),
        "inning": doc.get("currentInning"),
        "half": str(half).lower() if half else None,
        "balls": doc.get("balls"),
        "strikes": doc.get("strikes"),
        "outs": doc.get("outs"),
        "innings": [
            {
                "n": inning.get("num"),
                "home": (inning.get("home") or {}).get("runs"),
                "away": (inning.get("away") or {}).get("runs"),
            }
            for inning in doc.get("innings") or []
            if isinstance(inning, dict)
        ],
        "totals": {
            "home": _side_totals(teams.get("home")),
            "away": _side_totals(teams.get("away")),
        },
    }


def linescore_status(doc: dict[str, Any]) -> str:
    """``scheduled``, ``in_progress`` or ``final``, derived from the linescore itself.

    The linescore endpoint carries no status field, so the state is read from
    the count and score: no current inning means the game has not started;
    otherwise the game is over once a regulation (or later) inning is
    complete with one side ahead, or the home side leads in the bottom of
    such an inning (a walk-off). Anything else is in progress. Shortened or
    suspended games cannot be told apart from live ones here.
    """
    inning = doc.get("currentInning")
    if inning is None:
        return "scheduled"
    scheduled = doc.get("scheduledInnings") or DEFAULT_SCHEDULED_INNINGS
    if inning < scheduled:
        return "in_progress"
    teams = doc.get("teams") or {}
    home = (teams.get("home") or {}).get("runs")
    away = (teams.get("away") or {}).get("runs")
    if home is None or away is None or home == away:
        return "in_progress"
    outs = doc.get("outs")
    is_top = doc.get("isTopInning")
    if is_top is None:
        is_top = str(doc.get("inningState", "")).lower() in ("top", "middle")
    if is_top:
        return "final" if outs == 3 and home > away else "in_progress"
    return "final" if home > away or outs == 3 else "in_progress"


def _side_totals(side: Any) -> dict[str, Any]:
    totals = side if isinstance(side, dict) else {}
    return {
        "runs": totals.get("runs"),
        "hits": totals.get("hits"),
        "errors": totals.get("errors"),
        "left_on_base": totals.get("leftOnBase"),
    }
