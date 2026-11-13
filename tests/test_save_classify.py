import types
import uuid

import pytest
from rdflib import RDFS, BNode, Graph, Literal, URIRef

from bluecore_models.bluecore_graph import (
    STUB_STATUS,
    Action,
    BluecoreGraph,
    BluecoreGraphError,
    Citation,
    Entry,
    Ingest,
    Kind,
    Role,
    Write,
    save_graph,
)
from bluecore_models.bluecore_graph.policy import (
    _action,
    _admin_nodes,
    _citation,
    _classify,
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


def _entry(role: Role, kind: Kind = Kind.WORK, original: URIRef | None = None) -> Entry:
    subject = _bc_work() if kind != Kind.OTHER else _example("agent")
    return Entry(
        subject=subject,
        kind=kind,
        role=role,
        graph=Graph(),
        original=original if original is not None else subject,
        minted=original is not None,
        cited=frozenset(),
    )


def _minted_entry() -> Entry:
    """A Work minted from an LC URI this save brought."""
    return _entry(Role.PRIMARY, original=_lc("works"))


def _stored_work(stub: bool = False):
    """
    The stored row of a Work, holding only the data _action reads. Not a Work:
    the model rewrites data it is constructed with.
    """
    status = str(STUB_STATUS) if stub else "http://id.loc.gov/vocabulary/mstatus/c"
    return types.SimpleNamespace(data={"adminMetadata": [{"status": {"@id": status}}]})


def test_action_inserts_when_nothing_is_stored():
    assert _action(_entry(Role.REFERENCE), None, Write(Kind.WORK)) == Action.INSERT


def test_action_keeps_a_stored_reference_or_stub():
    work = _stored_work()
    assert _action(_entry(Role.REFERENCE), work, Write(Kind.INSTANCE)) == Action.KEEP
    assert _action(_entry(Role.STUB), work, INGEST) == Action.KEEP


def test_action_keeps_a_full_description_on_reingest():
    assert _action(_minted_entry(), _stored_work(), INGEST) == Action.KEEP


def test_action_replaces_a_stored_stub_on_reingest():
    assert _action(_minted_entry(), _stored_work(stub=True), INGEST) == Action.UPDATE


def test_action_updates_a_resource_that_arrives_with_its_bluecore_uri():
    assert _action(_entry(Role.PRIMARY), _stored_work(), INGEST) == Action.UPDATE


def test_action_updates_an_other_resource_when_primary():
    stored = types.SimpleNamespace(data={})
    entry = _entry(Role.PRIMARY, kind=Kind.OTHER)
    assert _action(entry, stored, INGEST) == Action.UPDATE


def test_actions_are_recorded_afresh_on_a_retry(pg_session):
    from test_save_graph_deadlocks import _FlakySessionMaker

    # an Agent we hold, cited by uri alone from a new stub
    agent = URIRef(f"http://id.loc.gov/rwo/agents/{uuid.uuid4()}")
    holder = _bc_work()
    g = Graph()
    g.add((holder, RDF.type, BF.Work))
    g.add((holder, BF.contributor, agent))
    g.add((agent, RDF.type, BF.Agent))
    g.add((agent, RDFS.label, Literal("Held Agent")))
    save_graph(pg_session, g, primary_class=BF.Work)

    g, _, stub = _described_with_stub()
    g.add((stub, BF.contributor, agent))
    bg = BluecoreGraph(g)
    flaky = _FlakySessionMaker(pg_session, fail_times=1)
    bg.save(flaky)

    assert flaky.calls == 2
    works = bg._entries[Kind.WORK]
    assert {bg._actions[str(e.subject)] for e in works} == {Action.INSERT}
    stub_entry = next(e for e in works if e.role == Role.STUB)
    # (it is also linked to the stub status it carries, which the save describes)
    assert str(agent) in _linked(pg_session, stub_entry.subject)


WORK = URIRef("https://bcld.info/works/7dbb7674-7373-473f-9014-b9a993a2dd03")


def _cites(p: URIRef, o: URIRef, *extra: tuple) -> Citation:
    """What o is, cited from WORK through p, in a graph with the extra triples."""
    g = Graph()
    g.add((WORK, RDF.type, BF.Work))
    g.add((WORK, p, o))
    for triple in extra:
        g.add(triple)
    return _citation(g, WORK, p, o, _admin_nodes(g))


def test_citation_vocabulary_term():
    assert _cites(BF.content, BF.Text) == Citation.VOCABULARY


def test_citation_record_typed_in_payload():
    other = _example("work")
    assert _cites(BF.subject, other, (other, RDF.type, BF.Work)) == Citation.RECORD


def test_citation_lc_item():
    item = URIRef("http://id.loc.gov/resources/items/1")
    assert _cites(BF.hasItem, item) == Citation.ITEM


def test_citation_typed_item_under_record_predicate():
    item = _example("item")
    assert _cites(BF.itemOf, item, (item, RDF.type, BF.Item)) == Citation.ITEM


def test_citation_excluded_predicate():
    assert _cites(BF.relatedTo, _example("x")) == Citation.EXCLUDED


def test_citation_lc_resources_prefix():
    hub = URIRef("http://id.loc.gov/resources/hubs/1")
    assert _cites(BF.subject, hub) == Citation.EXCLUDED


def _in_admin_metadata(o: URIRef) -> Citation:
    g = Graph()
    admin_metadata = BNode()
    g.add((WORK, RDF.type, BF.Work))
    g.add((WORK, BF.adminMetadata, admin_metadata))
    g.add((admin_metadata, RDF.type, BF.AdminMetadata))
    g.add((admin_metadata, BF.status, o))
    return _citation(g, admin_metadata, BF.status, o, _admin_nodes(g))


def test_citation_admin_metadata():
    status = URIRef("http://id.loc.gov/vocabulary/mstatus/n")
    assert _in_admin_metadata(status) == Citation.ADMIN_ONLY


def test_citation_other_resource():
    assert _cites(BF.subject, _example("topic")) == Citation.OTHER


def test_citation_item_through_plain_predicate_is_other():
    item = _example("item")
    assert _cites(BF.subject, item, (item, RDF.type, BF.Item)) == Citation.OTHER


def test_citation_vocabulary_wins_over_admin_metadata():
    assert _in_admin_metadata(BF.Text) == Citation.VOCABULARY


def test_only_vocabulary_terms_and_records_are_never_stored():
    assert {c for c in Citation if not c.stored} == {
        Citation.VOCABULARY,
        Citation.RECORD,
    }


def _derived_from_itself(work: URIRef, label: str) -> Graph:
    """A Work whose adminMetadata says it is derived from itself."""
    admin_metadata = BNode()
    g = Graph()
    g.add((work, RDF.type, BF.Work))
    g.add((work, RDFS.label, Literal(label)))
    g.add((work, BF.adminMetadata, admin_metadata))
    g.add((admin_metadata, RDF.type, BF.AdminMetadata))
    g.add((admin_metadata, BF.derivedFrom, work))
    return g


def test_accessors_still_work_after_a_save_that_cannot_find_its_root(pg_session):
    a, b, work = BNode(), BNode(), _bc_work()
    g = Graph()
    for node in (a, b, work):
        g.add((node, RDF.type, BF.Work))
    bg = BluecoreGraph(g)

    with pytest.raises(BluecoreGraphError, match="Unable to tell which resource"):
        bg.save(pg_session, primary_class=BF.Work)

    assert len(bg.works()) == 1


def test_reingest_of_a_resource_derived_from_itself_keeps_it(pg_session):
    # The derivedFrom lookup finds the resource under its own uri, so it is
    # switched onto itself: it counts as minted, and an ingest keeps it.
    work = _example("selfderived")
    save_graph(pg_session, _derived_from_itself(work, "v1"))
    save_graph(pg_session, _derived_from_itself(work, "v2"))

    with pg_session() as session:
        stored = session.query(Work).where(Work.uri == str(work)).one()
        assert "v1" in str(stored.data)
        assert "v2" not in str(stored.data)
