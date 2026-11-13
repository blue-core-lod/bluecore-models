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

## Changing what a save writes

Before you open a PR that changes how `save_graph` and `BluecoreGraph` save a
graph, follow [docs/checking-save-changes.md](docs/checking-save-changes.md). It
says which changes need the differential harness and the benchmark, and how to
run them against the commit your branch started from.

A change to what a save writes is a change to the policy: see
[docs/save-policy.md](docs/save-policy.md#changing-the-policy).

## Where to find things

- `docs/save-policy.md`: what a save does with each resource, generated from
  `tests/policy_cases.py`.
- `docs/checking-save-changes.md`: how to check a change to the save.
- `docs/bluecoregraph.md`: the analysis the `BluecoreGraph` refactor started
  from. Where it disagrees with #213, #213 is right.
- #213: the refactor's plan and phases, now finished.
