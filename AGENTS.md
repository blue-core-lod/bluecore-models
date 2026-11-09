# Agent guide

This repository holds the Blue Core BIBFRAME data models (SQLAlchemy), the
`BluecoreGraph` save logic that persists BIBFRAME graphs, and the Alembic
migrations for the database.

## Setup and checks

Use [uv](https://docs.astral.sh/uv/) with Python 3.12, which is what CI uses.
The tests need Docker running. CI (`.github/workflows/test.yml`) runs these
four commands, and all must pass:

```
uv run ruff format --diff
uv run ruff check
uv run mypy .
uv run pytest
```

The README has more on running tests, migrations and the benchmark.

## The BluecoreGraph refactor

While the `BluecoreGraph` refactor
([#213](https://github.com/blue-core-lod/bluecore-models/issues/213)) is in
progress, a refactor phase must not change behaviour: `save_graph` has to write
exactly what it wrote before. Before you open a PR for a phase, follow
[docs/bluecoregraph-refactor-checks.md](docs/bluecoregraph-refactor-checks.md).

## Where to find things

- `docs/bluecoregraph.md`: the analysis the refactor started from. Where it
  disagrees with #213, #213 is right.
- `docs/bluecoregraph-refactor-checks.md`: the runbook for checking a phase.
- `tests/policy_cases.py`: the policy cases, once Phase 0b lands.
- #213: the plan and the phases.
