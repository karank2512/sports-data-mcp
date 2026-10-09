.PHONY: test record-fixtures
test:
	.venv/bin/python -m pytest -q

# Fetches real responses from statsapi.mlb.com into tests/fixtures/. Needs network.
record-fixtures:
	.venv/bin/python scripts/record_fixtures.py
