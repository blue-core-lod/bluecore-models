import uuid

import pytest
from psycopg import errors as pg_errors
from rdflib import BNode, Graph, Literal, Namespace, URIRef
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import sessionmaker

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
from bluecore_models.models import Instance
from bluecore_models.namespaces import BF, RDF, RDFS

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


def _deadlock() -> OperationalError:
    return OperationalError(
        "UPDATE resource_base SET data=%(data)s::JSONB ...",
        {},
        pg_errors.DeadlockDetected("deadlock detected"),
    )


class _CompetingSessionMaker:
    """
    Wraps a real sessionmaker. The first session it makes runs competing just
    before its commit, as another load committing meanwhile, and then fails
    the commit with a deadlock, so the save retries.
    """

    def __init__(self, real_maker, competing):
        self._real = real_maker
        self._competing = competing
        self.calls = 0

    def __call__(self, *args, **kwargs):
        self.calls += 1
        session = self._real(*args, **kwargs)
        if self.calls == 1:

            def compete_then_fail():
                self._competing()
                raise _deadlock()

            session.commit = compete_then_fail
        return session


class _FailingSessionMaker:
    """Wraps a real sessionmaker whose every commit fails with a deadlock."""

    def __init__(self, real_maker):
        self._real = real_maker

    def __call__(self, *args, **kwargs):
        session = self._real(*args, **kwargs)

        def fail():
            raise _deadlock()

        session.commit = fail
        return session


def _bare_record(work: URIRef) -> Graph:
    """
    An LC Work described by its record, citing nothing that would be stored as
    an Other Resource, so a competing save of it takes no lock the first
    attempt holds.
    """
    admin_metadata = BNode()
    g = Graph()
    g.add((work, RDF.type, BF.Work))
    g.add((work, RDFS.label, Literal("A Work")))
    g.add((work, BF.adminMetadata, admin_metadata))
    g.add((admin_metadata, RDF.type, BF.AdminMetadata))
    return g


def _works_derived_from(pg_session, source: URIRef) -> int:
    with pg_session() as session:
        return session.execute(
            text(
                "SELECT count(*) FROM resource_base "
                "WHERE type = 'works' AND data::text LIKE :source"
            ),
            {"source": f'%"{source}"%'},
        ).scalar_one()


def test_a_retry_finds_what_another_load_committed_meanwhile(pg_session):
    source = _lc("works")
    racing = _CompetingSessionMaker(
        pg_session, lambda: save_graph(pg_session, _bare_record(source))
    )

    save_graph(racing, _bare_record(source))

    assert racing.calls == 2
    assert _works_derived_from(pg_session, source) == 1


def _new_work() -> Graph:
    work = BNode()
    g = Graph()
    g.add((work, RDF.type, BF.Work))
    g.add((work, RDFS.label, Literal("A New Work")))
    return g


def test_a_failed_save_shows_the_attempt_that_failed(pg_session):
    bg = BluecoreGraph(_new_work())

    with pytest.raises(OperationalError):
        bg.save(_FailingSessionMaker(pg_session), primary_class=BF.Work)

    assert bg.created_uri is not None
    assert (bg.created_uri, RDF.type, BF.Work) in bg.graph
    assert [e.subject for e in bg._entries[Kind.WORK]] == [bg.created_uri]


def _shape_of(bg: BluecoreGraph) -> list[tuple[str, str, str]]:
    return sorted(
        (entry.kind.value, str(entry.original), entry.role.value)
        for entries in bg._entries.values()
        for entry in entries
    )


def test_a_second_save_classifies_as_the_first(pg_session):
    bg = BluecoreGraph(_record_with_stub())
    bg.save(pg_session)
    first = _shape_of(bg)

    bg.save(pg_session)

    assert _shape_of(bg) == first


def _new_instance_of_a_new_work() -> tuple[Graph, URIRef]:
    instance, work = BNode(), URIRef(f"https://bcld.info/works/{uuid.uuid4()}")
    g = Graph()
    g.add((instance, RDF.type, BF.Instance))
    g.add((instance, RDFS.label, Literal("A New Instance")))
    g.add((instance, BF.instanceOf, work))
    g.add((work, RDF.type, BF.Work))
    g.add((work, RDFS.label, Literal("Its Work")))
    return g, work


def test_save_links_with_autoflush_disabled(engine, pg_session):
    g, work = _new_instance_of_a_new_work()
    bg = BluecoreGraph(g)

    bg.save(sessionmaker(bind=engine, autoflush=False), primary_class=BF.Instance)

    with pg_session() as session:
        instance = (
            session.query(Instance).where(Instance.uri == str(bg.created_uri)).one()
        )
        assert instance.work is not None
        assert instance.work.uri == str(work)


def test_uri_typed_both_fails_with_autoflush_disabled(engine, pg_session):
    both = URIRef(f"http://example.org/phase4/both/{uuid.uuid4()}")
    g = Graph()
    g.add((both, RDF.type, BF.Work))
    g.add((both, RDF.type, BF.Instance))

    with pytest.raises(IntegrityError):
        save_graph(sessionmaker(bind=engine, autoflush=False), g)

    assert _works_derived_from(pg_session, both) == 0
