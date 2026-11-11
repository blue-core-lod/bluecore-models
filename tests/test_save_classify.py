import uuid

from rdflib import RDFS, BNode, Graph, Literal, URIRef

from bluecore_models.bluecore_graph import (
    BluecoreGraph,
    Ingest,
    Kind,
    Role,
    Write,
    _classify,
    save_graph,
)
from bluecore_models.models import BibframeOtherResources, Work
from bluecore_models.namespaces import BF, RDF

INGEST = Ingest(update_others=True)


def _bc_work() -> URIRef:
    """A Bluecore Work URI no other test uses, since pg_session keeps rows."""
    return URIRef(f"https://bcld.info/works/{uuid.uuid4()}")


def _example(path: str) -> URIRef:
    return URIRef(f"http://example.org/phase3/{path}/{uuid.uuid4()}")


def _linked(pg_session, work: URIRef) -> list[str]:
    with pg_session() as session:
        stored = session.query(Work).where(Work.uri == str(work)).one()
        return [
            link.other_resource.uri
            for link in session.query(BibframeOtherResources).where(
                BibframeOtherResources.bibframe_resource_id == stored.id
            )
        ]


def test_uri_cited_through_an_excluded_and_a_plain_predicate_is_linked(pg_session):
    work, topic = _bc_work(), _example("topic")
    g = Graph()
    g.add((work, RDF.type, BF.Work))
    g.add((work, BF.relatedTo, topic))
    g.add((work, BF.subject, topic))
    g.add((topic, RDFS.label, Literal("Topic")))
    save_graph(pg_session, g, primary_class=BF.Work)

    assert _linked(pg_session, work) == [str(topic)]


def test_uri_cited_inside_and_outside_admin_metadata_is_linked_when_held(pg_session):
    topic = _example("topic")
    holder = _bc_work()
    g = Graph()
    g.add((holder, RDF.type, BF.Work))
    g.add((holder, BF.subject, topic))
    g.add((topic, RDFS.label, Literal("Held Topic")))
    save_graph(pg_session, g, primary_class=BF.Work)

    # cited by uri alone, both in the body and inside adminMetadata
    work = _bc_work()
    admin_metadata = BNode()
    g = Graph()
    g.add((work, RDF.type, BF.Work))
    g.add((work, BF.subject, topic))
    g.add((work, BF.adminMetadata, admin_metadata))
    g.add((admin_metadata, RDF.type, BF.AdminMetadata))
    g.add((admin_metadata, BF.status, topic))
    save_graph(pg_session, g, primary_class=BF.Work)

    assert _linked(pg_session, work) == [str(topic)]


def test_item_cited_through_a_plain_predicate_is_linked(pg_session):
    work, item = _bc_work(), _example("item")
    g = Graph()
    g.add((work, RDF.type, BF.Work))
    g.add((work, BF.subject, item))
    g.add((item, RDF.type, BF.Item))
    g.add((item, RDFS.label, Literal("An item")))
    save_graph(pg_session, g, primary_class=BF.Work)

    assert _linked(pg_session, work) == [str(item)]


def _lc(kind: str) -> URIRef:
    return URIRef(f"http://id.loc.gov/resources/{kind}/{uuid.uuid4()}")


def _prepared(g: Graph, mode) -> BluecoreGraph:
    bg = BluecoreGraph(g)
    bg._prepare(mode)
    return bg


def _described_with_stub() -> tuple[Graph, URIRef, URIRef]:
    """An LC Work with adminMetadata of its own, pointing at one it doesn't describe."""
    described, stub = _lc("works"), _lc("works")
    admin_metadata = BNode()
    g = Graph()
    g.add((described, RDF.type, BF.Work))
    g.add((described, BF.adminMetadata, admin_metadata))
    g.add((admin_metadata, RDF.type, BF.AdminMetadata))
    g.add((described, BF.relatedTo, stub))
    g.add((stub, RDF.type, BF.Work))
    return g, described, stub


def test_ingest_blank_record_is_inline():
    instance, blank_work = URIRef("https://bcld.info/instances/1"), BNode()
    g = Graph()
    g.add((instance, RDF.type, BF.Instance))
    g.add((instance, BF.instanceOf, blank_work))
    g.add((blank_work, RDF.type, BF.Work))

    roles = _classify(g, INGEST, {blank_work}, None, {str(instance)})

    assert roles[(blank_work, Kind.WORK)] == Role.INLINE


def test_ingest_undescribed_uri_is_a_stub_when_the_record_uses_admin_metadata():
    g, described, stub = _described_with_stub()

    roles = _prepared(g, INGEST)._roles

    assert roles[(stub, Kind.WORK)] == Role.STUB
    assert roles[(described, Kind.WORK)] == Role.PRIMARY


def test_ingest_without_admin_metadata_makes_everything_primary():
    first, second = _lc("works"), _lc("works")
    g = Graph()
    g.add((first, RDF.type, BF.Work))
    g.add((second, RDF.type, BF.Work))
    g.add((first, BF.relatedTo, second))

    roles = _classify(g, INGEST, set(), None, set())

    assert {roles[(w, Kind.WORK)] for w in (first, second)} == {Role.PRIMARY}


def test_write_root_is_primary_and_other_blank_nodes_are_inline():
    root, nested = BNode(), BNode()
    g = Graph()
    g.add((root, RDF.type, BF.Work))
    g.add((root, BF.accompaniedBy, nested))
    g.add((nested, RDF.type, BF.Work))

    roles = _classify(g, Write(Kind.WORK), {root}, root, set())

    assert roles[(root, Kind.WORK)] == Role.PRIMARY
    assert roles[(nested, Kind.WORK)] == Role.INLINE


def test_write_makes_other_kinds_references():
    instance, work = _example("instance"), _example("work")
    agent = _example("agent")
    g = Graph()
    g.add((instance, RDF.type, BF.Instance))
    g.add((instance, BF.instanceOf, work))
    g.add((work, RDF.type, BF.Work))
    g.add((instance, BF.contribution, agent))
    g.add((agent, RDFS.label, Literal("Agent")))

    roles = _classify(g, Write(Kind.INSTANCE), set(), None, set())

    assert roles[(instance, Kind.INSTANCE)] == Role.PRIMARY
    assert roles[(work, Kind.WORK)] == Role.REFERENCE
    assert roles[(agent, Kind.OTHER)] == Role.REFERENCE


def _work_citing_agent() -> tuple[Graph, URIRef]:
    work, agent = _bc_work(), _example("agent")
    g = Graph()
    g.add((work, RDF.type, BF.Work))
    g.add((work, BF.contribution, agent))
    g.add((agent, RDFS.label, Literal("Agent")))
    return g, agent


def test_write_other_resource_makes_others_primary():
    g, agent = _work_citing_agent()

    roles = _classify(g, Write(Kind.OTHER), set(), None, set())

    assert roles[(agent, Kind.OTHER)] == Role.PRIMARY


def test_ingest_update_others_off_makes_others_references():
    g, agent = _work_citing_agent()

    roles = _classify(g, Ingest(update_others=False), set(), None, set())

    assert roles[(agent, Kind.OTHER)] == Role.REFERENCE


def test_a_uri_typed_work_and_instance_gets_a_role_per_kind():
    both = _example("both")
    g = Graph()
    g.add((both, RDF.type, BF.Work))
    g.add((both, RDF.type, BF.Instance))

    roles = _classify(g, Write(Kind.WORK), set(), None, set())

    assert roles[(both, Kind.WORK)] == Role.PRIMARY
    assert roles[(both, Kind.INSTANCE)] == Role.REFERENCE


def test_accessors_before_save_keep_roots_and_leave_out_nested_blank_nodes():
    a, b, c = BNode(), BNode(), BNode()
    g = Graph()
    for work in (a, b, c):
        g.add((work, RDF.type, BF.Work))
    g.add((a, BF.relatedTo, c))

    assert len(BluecoreGraph(g).works()) == 2


def test_entries_carry_their_role(pg_session):
    g, described, stub = _described_with_stub()
    bg = BluecoreGraph(g)
    bg.save(pg_session)

    assert {(e.original, e.role) for e in bg._entries[Kind.WORK]} == {
        (described, Role.PRIMARY),
        (stub, Role.STUB),
    }
