# data-analysis-agent

HR data-analysis agent — Phase 0 walking skeleton (see `docs/decisions/DECISIONS.md` D68).

## Getting started

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/).

```bash
# Install dependencies (including dev/test tools)
uv sync --extra dev

# Run tests
uv run pytest

# Lint
uv run ruff check
```

## Project structure

```
src/data_agent/
  catalog/       — Semantic Catalog loader (D42, D53): reads databaseSchemaDocs/*.yaml
  sqlparse/      — ClickHouse-dialect SQL parser / column-provenance extractor (D52, D62)

tests/sqlparse/
  test_column_provenance.py — 36 unit tests (D44, D52, D57, D62, D63, D64, D69)

databaseSchemaDocs/
  *.yaml         — Semantic Catalog YAML files (one per table)

docs/
  decisions/DECISIONS.md  — Locked architecture decisions
  decisions/OPEN-QUESTIONS.md
```

## Key decisions

- D52/D62: ClickHouse-dialect parser uses `sqlglot` (pure-Python, in-process).
- D63: parse failure on live query -> fail-closed + alert (never fail-open).
- D69: column-provenance contract — fully-qualified (database.table, column) USES triples.
