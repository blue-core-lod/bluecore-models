# Checking a change to what a save writes

Read this before you open a PR that changes how `save_graph` and
`BluecoreGraph` save a graph. It assumes you have never seen the harness.

## The checks

1. **The test suite**, including the policy tests (`tests/test_save_policy.py`,
   which check every case in `tests/policy_cases.py`). They run in CI.
2. **The differential harness**, [`scripts/diff_save.py`](../scripts/diff_save.py).
   It loads the same records with the baseline code and with your branch,
   snapshots each database, and compares the snapshots.
3. **The benchmark**, [`benchmarks/save_graph_bench.py`](../benchmarks/save_graph_bench.py).

The harness and the benchmark are manual, and this page says how to run them.

## Which checks a change needs

| The change | Harness | Benchmark |
| --- | --- | --- |
| Code every save goes through: `bluecore_graph/`, `utils/graph.py`, the models and columns a save writes | full check | if it touches the save's hot path |
| A deliberate change to what a save writes | full check | if it touches the save's hot path |
| A small edit near the save path | quick check | no |
| Something the harness can't reach (docs, migrations that don't touch what a save writes, a path no record takes, such as saving one `BluecoreGraph` twice) | no | no |

When in doubt, run the quick check: it takes a few minutes. The harness only
sees what loading its sample of LC records does, so for a path it can't reach,
the tests are the check.

## The baseline

The baseline is the commit your branch started from, usually
`git merge-base origin/main HEAD`. Put it next to your work as a worktree,
replacing `<base>` with that commit:

```
git worktree add ../bluecore-models-base <base>
```

Remove it when you are done (`git worktree remove ../bluecore-models-base`).

Both sides run the harness script from your branch: the baseline only puts its
own `src` first on `PYTHONPATH`. So the script has to work against both, using
only the public API that exists at the baseline.

## Setup

- Use Python 3.12: `UV_PYTHON=3.12 uv run ...`. CI uses 3.12, and other versions
  skew the benchmark.
- Docker must be running. The harness starts and removes its own databases.
- Snapshots go in `harness-out/` (`mkdir harness-out` once). They are about 60 MB
  each for a full run, and are never committed; the directory is in `.gitignore`.

## The quick check

It loads the first 25 records of each batch and takes a few minutes per side.
It is not the full check. Both sides need the same `--per-batch`, because
`compare` refuses snapshots with different selections.

```
PYTHONPATH=../bluecore-models-base/src UV_PYTHON=3.12 uv run python scripts/diff_save.py run \
    --docker --per-batch 25 --corpus tests/data/harness-sample.tar.gz \
    --sequences --out harness-out/quick-base.json
UV_PYTHON=3.12 uv run python scripts/diff_save.py run \
    --docker --per-batch 25 --corpus tests/data/harness-sample.tar.gz \
    --sequences --out harness-out/quick.json
UV_PYTHON=3.12 uv run python scripts/diff_save.py compare \
    harness-out/quick-base.json harness-out/quick.json
```

Each `run` prints which `bluecore_models` it imported. Check that the first run
shows the baseline worktree and the second shows yours.

## The full check

Both sides over the whole sample. Each side takes about 45-50 minutes. The two
sides can run in parallel and in the background; each gets its own container.

```
PYTHONPATH=../bluecore-models-base/src UV_PYTHON=3.12 uv run python scripts/diff_save.py run \
    --docker --corpus tests/data/harness-sample.tar.gz --sequences \
    --out harness-out/base.json
UV_PYTHON=3.12 uv run python scripts/diff_save.py run \
    --docker --corpus tests/data/harness-sample.tar.gz --sequences \
    --out harness-out/branch.json
UV_PYTHON=3.12 uv run python scripts/diff_save.py compare \
    harness-out/base.json harness-out/branch.json
```

`compare` exits 0 and prints `no differences` when the sides match.

**Reusing a baseline snapshot.** A snapshot from an earlier run can stand in for
the baseline side only if it was made from the same baseline code (the same
tree, not just the same branch name: `git diff <old> <base>` shows nothing),
and by the same harness code as your branch's (`git diff <old> HEAD --
scripts/diff_save.py` shows nothing, or only its docstring). If either
differs, run the baseline again.

## The benchmark

Run the baseline and the branch on the same machine, one after the other, with
nothing else heavy running. Use one throwaway Postgres for all the runs; `--reset`
empties it before each run. For example:

```
docker run -d --rm --name bench -p 55440:5432 -e POSTGRES_PASSWORD=diff postgres:16
until docker exec bench pg_isready -h 127.0.0.1 -q; do sleep 1; done
```

Use the commands from the benchmark's docstring, with `<url>` set to
`postgresql+psycopg://postgres:diff@127.0.0.1:55440/postgres`:

```
PYTHONPATH=../bluecore-models-base/src UV_PYTHON=3.12 uv run python \
    benchmarks/save_graph_bench.py --database-url <url> --reset \
    --no-warmup --no-update-other-resources \
    --tarball tests/data/harness-sample.tar.gz --limit 300 --count 300
```

and the same command without `PYTHONPATH` for your branch. Run each three times,
alternating, and compare the medians of the ms/record figures. The figures vary
by about 5 ms/record between runs on one machine, so only the two sides measured
together are comparable. The branch should be no slower; if it is, say why in
the PR. Stop the container with `docker stop bench` when you are done.

## Put the results in the PR

Put the `compare` output (or `no differences`), and the six benchmark figures
with their medians if you ran it, in the PR description. If you didn't run the
harness, say why.

## When `compare` reports differences

A difference means behaviour changed.

- **If you didn't mean to change behaviour**, it's a regression: fix it.
- **If you did**, every difference should be one you meant, and you should be
  able to say which. Change the policy cases that pin the old behaviour as
  [Changing the policy](save-policy.md#changing-the-policy) says (mark them
  *chosen* and regenerate `docs/save-policy.md` with
  `uv run python tests/policy_cases.py > docs/save-policy.md`), and summarise
  the differences in the PR. A difference you can't explain is a regression,
  even in a PR that changes behaviour on purpose.

The harness includes a step that is meant to be refused, so the comparison also
covers error behaviour. A change that began accepting bad saves would show up
there as a difference.

## What the harness can't see

The list is in the [harness docstring](../scripts/diff_save.py) under "What the
harness can't see". Read it before you trust a clean comparison. In particular,
the default ingest path that re-describes Other Resources isn't exercised, and
each record is saved by a new `BluecoreGraph`, once.

## If you need to regenerate the sample

You shouldn't. It is committed as `tests/data/harness-sample.tar.gz`. If you
must, it needs the 1.4 GB `uploads/` corpus; the command and the SHA-256 it
should print are in the harness docstring.

## History

The harness and this page were written for the `BluecoreGraph` refactor
([#213](https://github.com/blue-core-lod/bluecore-models/issues/213)), whose
phases had to leave what `save_graph` writes unchanged. Every phase was compared
with the same fixed baseline, `7f0064c` (main just before the refactor), so
that small changes couldn't add up across phases unseen. The refactor finished
with Phase 4 (#220), and changes are now compared with the commit they start
from.
