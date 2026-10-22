# sports-data-mcp

MCP server over public sports stats APIs (MLB first): players, teams, schedules, box scores and standings as tools an agent can call.

Data comes from the public MLB Stats API (`statsapi.mlb.com`). This project is not affiliated with, endorsed by, or sponsored by MLB. Check the API's terms before any commercial use.

## Status

Version 0.1.0 (alpha, see `CHANGELOG.md`): a cached MLB Stats API client, a stdio MCP server with `mlb_ping`, `mlb_search_player`, `mlb_player_stats`, `mlb_team_schedule`, `mlb_standings`, `mlb_game_boxscore` and `mlb_live_linescore`, one error shape for every tool, an offline mode, and an offline test suite run by CI on Ubuntu and macOS with Python 3.11 and 3.12.

## Install

Requires Python 3.11+.

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
.venv/bin/sports-data-mcp --help
```

The `sports-data-mcp` console script starts the server on stdio. Options:

| Flag | Meaning | Default |
| --- | --- | --- |
| `--cache-dir DIR` | where JSON responses are cached | `~/.cache/sports-data-mcp` |
| `--ttl SECONDS` | how long a cached response is reused | `3600` |
| `--offline` | serve only from the cache; never open a socket | off |

The same settings can be given as environment variables: `SPORTS_DATA_MCP_CACHE_DIR`, `SPORTS_DATA_MCP_TTL_S`, `SPORTS_DATA_MCP_OFFLINE=1` (also `true` or `yes`), and `SPORTS_DATA_MCP_BASE_URL` to point at a different API root. A flag wins over its environment variable.

### Offline mode

With `--offline` or `SPORTS_DATA_MCP_OFFLINE=1` the server answers every tool from the on-disk cache regardless of age and never opens a socket. A response that is not in the cache is an `offline` error (see below). Run the server online once to fill the cache, then switch it to offline for demos, flights and tests.

## Claude Desktop

Add to `claude_desktop_config.json` (Settings → Developer → Edit Config), using the absolute path to the console script:

```json
{
  "mcpServers": {
    "sports-data": {
      "command": "/path/to/sports-data-mcp/.venv/bin/sports-data-mcp"
    }
  }
}
```

Restart Claude Desktop. Ask "ping the MLB API" and the `mlb_ping` tool should answer with `{ok, cached, teams}`.

## Claude Code

```bash
claude mcp add sports-data -- /path/to/sports-data-mcp/.venv/bin/sports-data-mcp
```

Or add the same `mcpServers` block to `.mcp.json` in your project.

## Tools

| Tool | Returns |
| --- | --- |
| `mlb_ping()` | `{ok: true, cached: bool, teams: int}` from `/teams?sportId=1`. `cached` is true when the answer came from disk without a request. |
| `mlb_search_player(name, active_only=true)` | Up to 10 `{id, full_name, team, position, bats, throws, active}` from `/people/search?names=`. `name` must be at least 2 characters (shorter is a tool error). `active_only` drops inactive people. `team` is null when the API omits `currentTeam`. |
| `mlb_player_stats(player_id, season, group="hitting", type="season")` | `{player_id, season, group, type, splits, cached, source_url}` from `/people/{id}/stats`. `group` is `hitting`, `pitching` or `fielding`; `type` is `season` (for `season`) or `career` (ignores `season`). Each split is a flat dict: the API's stat names verbatim (`gamesPlayed`, `avg`, `era`...) plus `season`, `game_type`, `team`, `team_id`, `player_id`. An unknown player id is a tool error carrying the API's message. |
| `mlb_team_schedule(team, start, end)` | `{team_id, team, start, end, games, cached, source_url}` from `/schedule`. `team` is an id (`147`) or any name the team goes by (`"New York Yankees"`, `"Yankees"`, `"NYY"`, `"Bronx"`), matched case-insensitively against the cached `/teams` list; a name shared by several teams (`"New York"`, `"Chicago"`) is a tool error listing the candidates. `start`/`end` are inclusive `YYYY-MM-DD` dates covering at most 31 days (more is a tool error). Each game is `{game_pk, date, home, away, status, home_score, away_score, venue}`; scores are null until published. |
| `mlb_standings(season, league="all")` | `{season, league, divisions, cached, source_url}` from `/standings?leagueId=103,104`. Both leagues are fetched in one request and `league` (`AL`, `NL` or `all`) filters the divisions. Each division is `{division_id, division, league_id, league, teams}` with teams in rank order as `{team_id, team, w, l, pct, gb, streak}`; `pct` and `gb` are the API's strings verbatim (`".580"`, `"-"` for the leader) and `streak` is a code such as `"W4"`. Division and league names come from the cached `/teams` list. |
| `mlb_game_boxscore(game_pk)` | `{game_pk, home, away, officials, weather, info, cached, source_url}` from `/game/{game_pk}/boxscore`, trimmed to what a game summary needs (`game_pk` comes from `mlb_team_schedule`). `home` and `away` are `{team, totals, batting, pitching}`: `team` is `{id, name, abbreviation}`; `totals` is `{runs, hits, errors, left_on_base}`; `batting` has one row per lineup-slot holder in lineup order, `{player_id, name, position, slot, substitute, ab, r, h, doubles, triples, hr, rbi, bb, so, sb, lob, summary}` (`position` lists every position played, `"PH-3B"`; `slot` is 1-9; `substitute` marks a player who took over the slot; pitchers who never batted are omitted); `pitching` has one row per pitcher in order of appearance, `{player_id, name, ip, h, r, er, bb, so, hr, pitches, strikes, note, summary}` (`note` is the decision verbatim, `"(W, 2-0)"`, `"(L, 4-6)"`, `"(H, 4)"`, or null). `officials` is `[{position, name, id}]`, home plate first. `weather` is `{conditions, wind}` and `info` is `{venue, attendance, first_pitch, duration}`, taken from the box score notes (null when absent). Season stats, bench and bullpen lists, play-by-play notes and top performers are dropped. An unknown game id is a tool error carrying the API's message. |
| `mlb_live_linescore(game_pk)` | `{game_pk, status, inning, half, balls, strikes, outs, innings, totals, cached, source_url}` from `/game/{game_pk}/linescore`, cached for **30 seconds** instead of the server-wide TTL so an agent can poll it. `status` is `scheduled`, `in_progress` or `final`; the endpoint has no status field, so it is derived from the count and score (no current inning: scheduled; a regulation-or-later inning complete with one side ahead, or the home side ahead in the bottom of one: final; otherwise in progress; shortened or suspended games look in progress). `inning` is the current inning (null before first pitch); `half` is `top`, `middle`, `bottom` or `end`. `innings` is `[{n, home, away}]` with runs per half-inning (null for a half not yet played); `totals` is `{home: {runs, hits, errors, left_on_base}, away: {...}}`. |

## Errors

Every tool fails the same way: an MCP tool error (`isError: true`) whose text content is one JSON document, so the calling model can read the kind and decide what to do:

```json
{"error": {"kind": "rate_limited", "message": "HTTP 429 Too Many Requests for https://statsapi.mlb.com/api/v1/teams?sportId=1", "retry_after_s": 7}}
```

| `kind` | When | `retry_after_s` |
| --- | --- | --- |
| `not_found` | the API answered 404: no such player, game, team or endpoint | null |
| `rate_limited` | the API answered 429 twice (one retry) | the `Retry-After` header in whole seconds, or null if the API sent none |
| `upstream` | the API answered 5xx or an unexpected 4xx twice, returned a body that is not JSON, or could not be reached (timeout, DNS, connection failure) | `Retry-After` if present, else null |
| `bad_input` | the arguments cannot be sent: a name under 2 characters, bad dates, a range over 31 days, an ambiguous team, a non-positive id, or HTTP 400 from the API | null |
| `offline` | offline mode and the response is not in the cache | null |

`message` is human readable and, for HTTP errors, quotes the status line and the start of the API's body. The MCP SDK prefixes the text with `Error executing tool <name>: `; `sports_data_mcp.errors.parse_tool_error(text)` returns the document from either form. Arguments that fail the tool's JSON schema (a string where an integer is required) are rejected by the SDK before the tool runs and keep the SDK's own validation message.

In Python, `sports_data_mcp.mlb.client.APIError` carries the same `kind` and `retry_after_s`, and `sports_data_mcp.errors.SportsDataError` is the `ToolError` subclass whose `str()` is the document.

## How caching works

Every GET is cached on disk as JSON, keyed by the sha256 of the full URL. A response is reused until it is older than the TTL (per client, or per call). In offline mode the cache is served regardless of age and a miss is an error rather than a network call. Responses with 429 or 5xx status and transport failures are retried once with backoff (honouring `Retry-After`, in seconds or as an HTTP date, up to 10 s), then raised as `APIError(status, message)` with `kind` and `retry_after_s` set as in the table above. Other 4xx errors are raised immediately. Errors are never cached. Each request has a 10 second timeout.

## Development

```bash
make test                                   # offline; serves tests/fixtures/ through an httpx MockTransport
make test TESTS=tests/test_players.py       # one file
make test PYTEST_ARGS="-x -k stats"         # extra pytest options
make record-fixtures                        # needs network: re-records tests/fixtures/*.json from statsapi.mlb.com
```

`.github/workflows/test.yml` runs `make test` on every push to `main` and every pull request, on Ubuntu and macOS with Python 3.11 and 3.12.

`tests/fixtures/index.json` maps each fixture name to the exact URL it answers. Most fixtures are real recordings; an entry marked `"synthetic": true` is a hand-written placeholder in the documented response shape, waiting for `make record-fixtures` to replace it with a real response. `tests/fixtures/linescore_live.json` (an in-progress game) is not in the index: it is hand-written in the recorded shape and served only by the mock transport in `tests/test_games.py`. Tests derive their expected values from the fixture contents so they keep passing after re-recording. Tests never contact the network: an autouse fixture makes any real `httpx` transport fail.

## What it does not do

- No streaming data; everything is a cached GET of a public endpoint. `mlb_live_linescore` is the only near-live tool and it can be up to 30 seconds stale.
- No other sports yet. Only the MLB Stats API is wired up, and only the tools in the table above are exposed.
- No authentication, API keys, or paid data sources. It uses only the public, unauthenticated API.
- No betting odds, projections, or any data that the MLB Stats API does not publish.
- No write operations of any kind.
- No guarantees about the upstream API, which is undocumented and may change or rate-limit without notice.

## License

MIT, see `LICENSE`. The license covers this code only, not the data returned by the MLB Stats API.
