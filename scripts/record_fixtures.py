#!/usr/bin/env python3
"""Record real MLB Stats API responses into ``tests/fixtures/``.

Run by a human with network access (``make record-fixtures``), never by
tests. Each endpoint is written to ``tests/fixtures/<name>.json`` and
``tests/fixtures/index.json`` maps names to the exact URL requested so
``tests/conftest.py`` can serve them through an ``httpx.MockTransport``.

The boxscore endpoint needs a game id; it is taken from the first game in
the recorded schedule so the set stays self-consistent.
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from sports_data_mcp.mlb.client import DEFAULT_BASE_URL, MLBClient  # noqa: E402

FIXTURES_DIR = REPO_ROOT / "tests" / "fixtures"

# Fixed choices so the recording is reproducible.
TEAM_ID = 147  # New York Yankees
SEASON = 2025
WEEK_START = "2025-07-01"
WEEK_END = "2025-07-07"
PLAYER_QUERY = "Aaron Judge"

ENDPOINTS: list[tuple[str, str, dict[str, Any]]] = [
    ("teams", "/teams", {"sportId": 1}),
    (
        "schedule_week",
        "/schedule",
        {"sportId": 1, "teamId": TEAM_ID, "startDate": WEEK_START, "endDate": WEEK_END},
    ),
    ("player_search", "/people/search", {"names": PLAYER_QUERY, "sportIds": 1}),
    ("standings", "/standings", {"leagueId": "103,104", "season": SEASON}),
]


def first_game_pk(schedule: dict[str, Any]) -> int:
    for date in schedule.get("dates", []):
        for game in date.get("games", []):
            if "gamePk" in game:
                return int(game["gamePk"])
    raise SystemExit("schedule fixture has no games; cannot pick a boxscore game")


def record(base_url: str, out_dir: Path) -> dict[str, dict[str, str]]:
    out_dir.mkdir(parents=True, exist_ok=True)
    index: dict[str, dict[str, str]] = {}
    with tempfile.TemporaryDirectory(prefix="record-fixtures-") as tmp:
        client = MLBClient(base_url=base_url, cache_dir=tmp, ttl_s=0)
        try:
            endpoints = list(ENDPOINTS)
            recorded: dict[str, Any] = {}
            for name, path, params in endpoints:
                url = client.url_for(path, params)
                print(f"GET {url}", file=sys.stderr)
                data = client.get(path, params)
                recorded[name] = data
                (out_dir / f"{name}.json").write_text(json.dumps(data, indent=1) + "\n", encoding="utf-8")
                index[name] = {"url": url, "file": f"{name}.json"}

            game_pk = first_game_pk(recorded["schedule_week"])
            path = f"/game/{game_pk}/boxscore"
            url = client.url_for(path)
            print(f"GET {url}", file=sys.stderr)
            data = client.get(path)
            (out_dir / "boxscore.json").write_text(json.dumps(data, indent=1) + "\n", encoding="utf-8")
            index["boxscore"] = {"url": url, "file": "boxscore.json"}
        finally:
            client.close()

    index_doc = {
        "recorded_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "base_url": base_url,
        "fixtures": index,
    }
    (out_dir / "index.json").write_text(json.dumps(index_doc, indent=1) + "\n", encoding="utf-8")
    return index


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--out", type=Path, default=FIXTURES_DIR)
    args = parser.parse_args(argv)
    index = record(args.base_url, args.out)
    print(f"recorded {len(index)} fixtures into {args.out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
