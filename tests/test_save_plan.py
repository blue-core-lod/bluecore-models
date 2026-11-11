import uuid

from rdflib import BNode, Graph, Namespace, URIRef
from sqlalchemy import text

from bluecore_models import bluecore_graph
from bluecore_models.bluecore_graph import (
    Action,
    BluecoreGraph,
    Ingest,
    Kind,
    Resolution,
    Role,
    SavePlan,
    _copy_graph,
    save_graph,
)
from bluecore_models.namespaces import BF, RDF

NS = Namespace("https://bcld.info/")
INGEST = Ingest(update_others=True)


def _lc(kind: str) -> URIRef:
    return URIRef(f"http://id.loc.gov/resources/{kind}/{uuid.uuid4()}")


def _described_with_stub(
    described: URIRef | None = None,
) -> tuple[Graph, URIRef, URIRef]:
    """An LC Work with adminMetadata of its own, pointing at one it doesn't describe."""
    described, stub = described or _lc("works"), _lc("works")
    admin_metadata = BNode()
    g = Graph()
    g.add((described, RDF.type, BF.Work))
    g.add((described, BF.adminMetadata, admin_metadata))
    g.add((admin_metadata, RDF.type, BF.AdminMetadata))
    g.add((described, BF.relatedTo, stub))
    g.add((stub, RDF.type, BF.Work))
    return g, described, stub


def _record_with_stub(work: URIRef | None = None) -> Graph:
    """The record of _described_with_stub, for work, citing a fresh stub."""
    return _described_with_stub(work)[0]


def test_save_graph_leaves_the_callers_graph_alone(pg_session):
    g = _record_with_stub()
    before = set(g)

    out = save_graph(pg_session, g)

    assert set(g) == before
    assert out is not g


def test_bluecore_graph_works_on_a_copy():
    g = _record_with_stub()
    bg = BluecoreGraph(g)
    assert bg.graph is not g
    # _infer may add triples, but only to the copy
    assert len(bg.graph) >= len(g)


def test_a_plan_decides_roles_without_a_database():
    g, described, stub = _described_with_stub()

    plan = SavePlan.from_graph(g, NS, INGEST)

    assert plan.roles[(stub, Kind.WORK)] == Role.STUB
    assert plan.roles[(described, Kind.WORK)] == Role.PRIMARY


def test_a_copy_keeps_the_order_the_graph_gives_its_resources():
    # works(), minting and inserts follow the graph's order, which a copy made
    # triple by triple would reshuffle by hash
    g = Graph()
    g.parse("tests/data/23807141.ttl")

    copy = _copy_graph(g)

    assert list(copy.subjects(RDF.type, BF.Work)) == list(g.subjects(RDF.type, BF.Work))
    for s in set(g.subjects()):
        assert list(copy.predicate_objects(s)) == list(g.predicate_objects(s))


def test_a_copy_and_its_original_change_on_their_own():
    g = Graph()
    g.parse("tests/data/23807141.ttl")
    copy = _copy_graph(g)
    first = next(iter(g))
    extra = (URIRef("http://example.org/s"), RDF.type, BF.Work)

    copy.add(extra)
    g.remove(first)

    assert extra not in g and first in copy
    assert len(copy) == len(g) + 2
    assert set(copy.namespaces()) == set(g.namespaces())


def test_a_store_laid_out_differently_is_deep_copied_in_order(monkeypatch):
    g = Graph()
    g.parse("tests/data/23807141.ttl")
    monkeypatch.setattr(bluecore_graph, "_MEMORY_FIELDS", frozenset())

    copy = _copy_graph(g)

    assert list(copy.subjects(RDF.type, BF.Work)) == list(g.subjects(RDF.type, BF.Work))
    for s in set(g.subjects()):
        assert list(copy.predicate_objects(s)) == list(g.predicate_objects(s))


TABLES = (
    "resource_base",
    "bibframe_other_resources",
    "versions",
    "resource_bibframe_classes",
)


def _table_counts(pg_session) -> dict[str, int]:
    with pg_session() as session:
        return {
            table: session.execute(text(f"SELECT count(*) FROM {table}")).scalar_one()
            for table in TABLES
        }


def _shape(resolution: Resolution) -> list[tuple[str, str, str]]:
    return sorted(
        (entry.kind.value, str(entry.original), entry.role.value)
        for entries in resolution.entries.values()
        for entry in entries
    )


def test_resolve_writes_nothing(pg_session):
    work = _lc("works")
    save_graph(pg_session, _record_with_stub(work))
    # the same LC Work again (held: KEEP), now citing a new stub (INSERT)
    plan = SavePlan.from_graph(_record_with_stub(work), NS, INGEST)
    before = _table_counts(pg_session)

    with pg_session() as session:
        resolution = plan.resolve(session)
        # nothing added, changed or deleted, flushed or not
        assert not (session.new or session.dirty or session.deleted)
        session.rollback()

    assert _table_counts(pg_session) == before
    assert set(resolution.actions.values()) >= {Action.INSERT, Action.KEEP}


def test_resolve_leaves_the_plan_alone(pg_session):
    plan = SavePlan.from_graph(_record_with_stub(), NS, INGEST)
    triples, roles = set(plan.graph), dict(plan.roles)

    with pg_session() as session:
        first = plan.resolve(session)
        second = plan.resolve(session)

    assert set(plan.graph) == triples
    assert plan.roles == roles
    assert _shape(first) == _shape(second)
    assert first.graph is not second.graph
