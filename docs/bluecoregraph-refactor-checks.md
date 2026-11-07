# Checking a BluecoreGraph refactor phase

Read this before you open the PR for a phase of the `BluecoreGraph` refactor
([#213](https://github.com/blue-core-lod/bluecore-models/issues/213)). It
assumes you have never seen the harness.

## What the checks are for

A refactor phase must not change what `save_graph` writes. Three checks show it:

1. **The policy tests**, `tests/test_save_policy.py` (Phase 0b). They pin the
   behaviour case by case and run in CI with the rest of the suite.
2. **The differential harness**, [`scripts/diff_save.py`](../scripts/diff_save.py).
   It loads the same records with the baseline code and with your branch,
   snapshots each database, and compares the snapshots.
3. **The benchmark**, [`benchmarks/save_graph_bench.py`](../benchmarks/save_graph_bench.py).
   Your branch must be no slower than the baseline.

The policy tests run on their own. The other two are manual, and this page says
how to run them.

## The baseline

The baseline is commit `7f0064c`, which is `main` just before the refactor. It
is **not** the previous phase. If each phase were compared with the one before,
small changes could add up across phases without any check seeing them. Every
phase is compared with the same fixed code.

Put the baseline next to your work as a worktree:

```
git worktree add ../bluecore-models-7f0064c 7f0064c
```

Remove it when you are done (`git worktree remove ../bluecore-models-7f0064c`).

## Setup

- Use Python 3.12: `UV_PYTHON=3.12 uv run ...`. CI uses 3.12, and other versions
  skew the benchmark.
- Docker must be running. The harness starts and removes its own databases.
- Snapshots go in `harness-out/` (`mkdir harness-out` once). They are about 60 MB
  each for a full run, and are never committed; the directory is in `.gitignore`.

## While you work: the quick check

It loads the first 25 records of each batch and takes a few minutes per side.
It catches most mistakes, but it is not the full check. Both sides need the same `--per-batch`, because `compare`
refuses snapshots with different selections.

```
PYTHONPATH=../bluecore-models-7f0064c/src UV_PYTHON=3.12 uv run python scripts/diff_save.py run \
    --docker --per-batch 25 --corpus tests/data/harness-sample.tar.gz \
    --sequences --out harness-out/quick-a.json
UV_PYTHON=3.12 uv run python scripts/diff_save.py run \
    --docker --per-batch 25 --corpus tests/data/harness-sample.tar.gz \
    --sequences --out harness-out/quick.json
UV_PYTHON=3.12 uv run python scripts/diff_save.py compare \
    harness-out/quick-a.json harness-out/quick.json
```

Each `run` prints which `bluecore_models` it imported. Check that the first run
shows the `7f0064c` worktree and the second shows yours.

## Before you open the PR

### The full check

Both sides over the whole sample. Each side takes about 45-50 minutes. The two
sides can run in parallel and in the background; each gets its own container.

```
PYTHONPATH=../bluecore-models-7f0064c/src UV_PYTHON=3.12 uv run python scripts/diff_save.py run \
    --docker --corpus tests/data/harness-sample.tar.gz --sequences \
    --out harness-out/a.json
UV_PYTHON=3.12 uv run python scripts/diff_save.py run \
    --docker --corpus tests/data/harness-sample.tar.gz --sequences \
    --out harness-out/branch.json
UV_PYTHON=3.12 uv run python scripts/diff_save.py compare \
    harness-out/a.json harness-out/branch.json
```

`compare` exits 0 and prints `no differences` when the sides match.

### The benchmark

Run the baseline and the branch on the same machine, one after the other, with
nothing else heavy running. Each run needs an empty Postgres; any throwaway one
will do, for example:

```
docker run -d --rm --name bench -p 55440:5432 -e POSTGRES_PASSWORD=diff postgres:16
```

Use the commands from the benchmark's docstring, with `<url>` set to
`postgresql+psycopg://postgres:diff@localhost:55440/postgres`:

```
PYTHONPATH=../bluecore-models-7f0064c/src UV_PYTHON=3.12 uv run python \
    benchmarks/save_graph_bench.py --database-url <url> --reset \
    --no-warmup --no-update-other-resources \
    --tarball tests/data/harness-sample.tar.gz --limit 300 --count 300
```

and the same command without `PYTHONPATH` for your branch. Run each three times,
and compare the medians of the ms/record figures. The branch must be no slower.
The Phase 0a figure, 98.7 ms/record, is a reference and not a threshold: it
depends on the machine, so only the two sides measured together are comparable.
Stop the container with `docker stop bench` when you are done.

### Paste the results into the PR

Put the `compare` output (or `no differences`) and the six benchmark figures
with their medians in the PR description.

## When `compare` reports differences

A difference means behaviour changed. In a refactor phase that is never
acceptable silently.

- **A regression**, where something that worked now fails or writes something
  different, is fixed in the phase.
- **A change that looks like an improvement**, such as a save that used to fail
  and now works, is still a behaviour change. Either undo it, or make it
  deliberate: change the policy case that pins the old behaviour, mark that row
  *chosen*, and say so in the PR.

The harness includes steps that are meant to be refused, so the comparison also
covers error behaviour. A refactor that began accepting bad saves would show up
there as a difference.

## What the harness can't see

The list is in the [harness docstring](../scripts/diff_save.py) under "What the
harness can't see". Read it before you trust a clean comparison. In particular,
the default ingest path that re-describes Other Resources isn't exercised.

## If you need to regenerate the sample

You shouldn't. It is committed as `tests/data/harness-sample.tar.gz`. If you
must, it needs the 1.4 GB `uploads/` corpus; the command and the SHA-256 it
should print are in the harness docstring.
