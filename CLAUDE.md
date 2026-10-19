# Khay Trader

SPEC.md is the source of truth. Implement one phase at a time; stop for review after each.

## Standing rules
- Never add a hard limit, threshold or order side effect to model-driven code.
- Never commit API keys; use env vars and `.env` (gitignored).
- Use UTC everywhere.
- Prefer small pure functions; no global state in risk code.

## Commands
- Setup: `python -m venv .venv && .venv/Scripts/python -m pip install -e ".[dev]"` (or `uv sync --extra dev`)
- Secret scan: `.venv/Scripts/pre-commit install` once; runs detect-secrets on every commit
- Tests: `.venv/Scripts/python -m pytest`
- Lint/types: `.venv/Scripts/python -m ruff check . && .venv/Scripts/python -m mypy`
- Backfill candles: `.venv/Scripts/trader-backfill --years 2`
