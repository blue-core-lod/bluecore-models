#!/usr/bin/env python3
"""Benchmark and profile ``bluecore_models.save_graph``.

Persists a set of Bibframe graphs through the real save path (URI minting,
resource save, linking, bf-class updates) and reports throughput. With
``--profile`` it also prints a cProfile hot-spot report, which is how the
``_link`` / ``get_bf_classes`` costs were originally found.

The graph content doesn't affect *which* code runs, so reusing a few sample
records many times (``--count``) is a fair, repeatable way to measure changes to
the save path.

Requires a Postgres to write to. Point it at any database via ``--database-url``
(or the ``DATABASE_URL`` env var); the local bluecore-stack DB works well:

    uv run python benchmarks/save_graph_bench.py \\
        --database-url postgresql+psycopg://airflow:airflow@localhost:5432/bluecore \\
        --count 50 --profile

Use ``--reset`` to TRUNCATE the resource tables first (don't point that at a
database you care about).

``--tarball`` loads the first ``--limit`` ``.rdf`` members of a tarball (RDF/XML,
read in place) instead of ``--input``. ``--no-warmup`` skips the warm-up pass, so
the timed run is a first-time load of every record.

Baseline for the BluecoreGraph refactor
---------------------------------------
Each refactor phase must be no slower than the recorded baseline. The baseline
run puts the 7f0064c code on PYTHONPATH, against a throwaway Postgres. Create
that worktree once with:

    git worktree add ../bluecore-models-7f0064c 7f0064c

then run:

    PYTHONPATH=../bluecore-models-7f0064c/src uv run python \\
        benchmarks/save_graph_bench.py --database-url <url> --reset \\
        --no-warmup --no-update-other-resources \\
        --tarball tests/data/harness-sample.tar.gz --limit 300 --count 300

Recorded baseline: three runs of the command above gave 99.4, 98.3 and 98.7
ms/record (median 98.7) over the first 300 records of
tests/data/harness-sample.tar.gz, measured on 2026-11-06 on a developer laptop.

That figure depends on the machine. Each phase measures the baseline and the
branch on the same machine, and compares those two, not the figure above.
"""

import argparse
import cProfile
import glob
import io
import os
import pstats
import tarfile
import time
from xml.sax import SAXParseException

import rdflib
from rdflib.exceptions import ParserError
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from bluecore_models.bluecore_graph import save_graph
from bluecore_models.models import Base
from bluecore_models.models.pg_ext_func import PG_EXT_FUNC

DEFAULT_INPUT = os.path.join(
    os.path.dirname(__file__), "..", "tests", "data", "*.jsonld"
)
RESET_TABLES = [
    "bibframe_other_resources",
    "resource_bibframe_classes",
    "versions",
    "works",
    "instances",
    "hubs",
    "other_resources",
    "resource_base",
    "bibframe_classes",
]


def ensure_schema(engine) -> None:
    """Best-effort: create the custom functions + tables if they're not present."""
    with engine.begin() as conn:
        for stmt in PG_EXT_FUNC:
            try:
                conn.execute(text(stmt))
            except Exception as exc:  # noqa: BLE001  # pragma: no cover - setup convenience
                print(f"  (skipped ext stmt: {exc})")
    Base.metadata.create_all(engine)


def reset(engine) -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                "TRUNCATE TABLE "
                + ", ".join(RESET_TABLES)
                + " RESTART IDENTITY CASCADE"
            )
        )


def load_graphs(pattern: str) -> list[rdflib.Graph]:
    graphs = []
    for path in sorted(glob.glob(pattern)):
        fmt = rdflib.util.guess_format(path) or "json-ld"
        g = rdflib.Graph()
        try:
            g.parse(location=path, format=fmt)
        except Exception as exc:  # noqa: BLE001
            print(f"  (skipped {os.path.basename(path)}: {exc})")
            continue
        if len(g):
            graphs.append(g)
    return graphs


def load_tarball(path: str, limit: int) -> list[rdflib.Graph]:
    """Parse the first ``limit`` ``.rdf`` members (sorted by name) as RDF/XML."""
    graphs: list[rdflib.Graph] = []
    with tarfile.open(path) as tar:
        members = sorted(
            (m for m in tar.getmembers() if m.isfile() and m.name.endswith(".rdf")),
            key=lambda m: m.name,
        )[:limit]
        for member in members:
            fileobj = tar.extractfile(member)
            if fileobj is None:
                continue
            g = rdflib.Graph()
            try:
                g.parse(data=fileobj.read(), format="xml")
            except (SAXParseException, ParserError) as exc:
                print(f"  (skipped {os.path.basename(member.name)}: {exc})")
                continue
            if len(g):
                graphs.append(g)
    return graphs


def run(graphs, session_maker, count, update_other_resources=True):
    saved = 0
    triples = 0
    for i in range(count):
        g = graphs[i % len(graphs)]
        # copy so repeated saves don't accumulate minted URIs on the same object
        gc = rdflib.Graph()
        for triple in g:
            gc.add(triple)
        save_graph(
            session_maker,
            gc,
            namespace="https://bcld.info/",
            update_other_resources=update_other_resources,
        )
        saved += 1
        triples += len(g)
    return saved, triples


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--database-url", default=os.environ.get("DATABASE_URL"))
    ap.add_argument("--count", type=int, default=25, help="number of save_graph calls")
    ap.add_argument("--input", default=DEFAULT_INPUT, help="glob of RDF files to load")
    ap.add_argument("--tarball", help="tarball of .rdf members to load, not --input")
    ap.add_argument(
        "--limit", type=int, default=300, help="members to read from --tarball"
    )
    ap.add_argument(
        "--no-warmup",
        dest="warmup",
        action="store_false",
        help="skip the warm-up pass, so the run times first-time loads",
    )
    ap.add_argument("--profile", action="store_true", help="print cProfile hot spots")
    ap.add_argument(
        "--reset", action="store_true", help="TRUNCATE resource tables first"
    )
    ap.add_argument(
        "--no-update-other-resources",
        dest="update_other_resources",
        action="store_false",
        help="treat Other Resources as references (create-if-absent, never overwrite)",
    )
    args = ap.parse_args()

    if not args.database_url:
        ap.error("provide --database-url or set DATABASE_URL")

    engine = create_engine(args.database_url)
    ensure_schema(engine)
    if args.reset:
        reset(engine)
    session_maker = sessionmaker(bind=engine)

    source = args.tarball or args.input
    if args.tarball:
        graphs = load_tarball(args.tarball, args.limit)
    else:
        graphs = load_graphs(args.input)
    if not graphs:
        ap.error(f"no graphs loaded from {source}")
    print(
        f"loaded {len(graphs)} sample graph(s); running {args.count} save_graph calls"
    )

    # warm up (schema caches, first-insert vs update path) — excluded from timing
    if args.warmup:
        run(
            graphs,
            session_maker,
            min(len(graphs), args.count),
            args.update_other_resources,
        )

    profiler = cProfile.Profile() if args.profile else None
    t0 = time.time()
    if profiler:
        profiler.enable()
    saved, triples = run(graphs, session_maker, args.count, args.update_other_resources)
    if profiler:
        profiler.disable()
    wall = time.time() - t0

    print(
        f"\n{saved} saves in {wall:.1f}s  =>  {saved / wall:.1f} graphs/s, "
        f"{triples / wall:.0f} triples/s, {1000 * wall / saved:.1f} ms/record"
    )

    if profiler:
        for sort, label, n in (
            ("cumulative", "CUMULATIVE", 25),
            ("tottime", "SELF", 20),
        ):
            s = io.StringIO()
            pstats.Stats(profiler, stream=s).sort_stats(sort).print_stats(n)
            print(f"\n{'=' * 60}\nTOP {n} by {label} time\n{'=' * 60}\n{s.getvalue()}")


if __name__ == "__main__":
    main()
