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
        --database-url postgresql+psycopg2://airflow:airflow@localhost:5432/bluecore \\
        --count 50 --profile

Use ``--reset`` to TRUNCATE the resource tables first (don't point that at a
database you care about).
"""

import argparse
import cProfile
import glob
import io
import os
import pstats
import time

import rdflib
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from bluecore_models.models import Base
from bluecore_models.models.pg_ext_func import PG_EXT_FUNC
from bluecore_models.bluecore_graph import save_graph

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
            except Exception as exc:  # pragma: no cover - setup convenience
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
        except Exception as exc:
            print(f"  (skipped {os.path.basename(path)}: {exc})")
            continue
        if len(g):
            graphs.append(g)
    return graphs


def run(graphs, session_maker, count):
    saved = 0
    triples = 0
    for i in range(count):
        g = graphs[i % len(graphs)]
        # copy so repeated saves don't accumulate minted URIs on the same object
        gc = rdflib.Graph()
        for triple in g:
            gc.add(triple)
        save_graph(session_maker, gc, namespace="https://bcld.info/")
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
    ap.add_argument("--profile", action="store_true", help="print cProfile hot spots")
    ap.add_argument(
        "--reset", action="store_true", help="TRUNCATE resource tables first"
    )
    args = ap.parse_args()

    if not args.database_url:
        ap.error("provide --database-url or set DATABASE_URL")

    engine = create_engine(args.database_url)
    ensure_schema(engine)
    if args.reset:
        reset(engine)
    session_maker = sessionmaker(bind=engine)

    graphs = load_graphs(args.input)
    if not graphs:
        ap.error(f"no graphs loaded from {args.input}")
    print(
        f"loaded {len(graphs)} sample graph(s); running {args.count} save_graph calls"
    )

    # warm up (schema caches, first-insert vs update path) — excluded from timing
    run(graphs, session_maker, min(len(graphs), args.count))

    profiler = cProfile.Profile() if args.profile else None
    t0 = time.time()
    if profiler:
        profiler.enable()
    saved, triples = run(graphs, session_maker, args.count)
    if profiler:
        profiler.disable()
    wall = time.time() - t0

    print(
        f"\n{saved} saves in {wall:.1f}s  =>  {saved / wall:.1f} graphs/s, "
        f"{triples / wall:.0f} triples/s"
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
