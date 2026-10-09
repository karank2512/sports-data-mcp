.PHONY: test record-fixtures

# make test                              runs everything
# make test TESTS=tests/test_players.py  runs one file
# make test PYTEST_ARGS="-x -k stats"    passes extra pytest options
TESTS ?=
PYTEST_ARGS ?=

test:
	.venv/bin/python -m pytest -q $(PYTEST_ARGS) $(TESTS)

# Fetches real responses from statsapi.mlb.com into tests/fixtures/. Needs network.
record-fixtures:
	.venv/bin/python scripts/record_fixtures.py
