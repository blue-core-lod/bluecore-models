import uuid

from rdflib import RDFS, Graph, Literal, URIRef

from bluecore_models import bluecore_graph
from bluecore_models.bluecore_graph import BluecoreGraph, Kind, save_graph
from bluecore_models.namespaces import BF, RDF


def _lc(kind: str) -> URIRef:
    """A non-Bluecore URI no other test uses, since pg_session keeps rows."""
    return URIRef(f"http://id.loc.gov/resources/{kind}/{uuid.uuid4()}")


def _hub_work_instance_other() -> Graph:
    hub, work, instance = _lc("hubs"), _lc("works"), _lc("instances")
    other = URIRef(f"http://example.org/phase2/subject/{uuid.uuid4()}")
    g = Graph()
    g.add((hub, RDF.type, BF.Hub))
    g.add((work, RDF.type, BF.Work))
    g.add((work, BF.expressionOf, hub))
    g.add((work, BF.subject, other))
    g.add((other, RDFS.label, Literal("Subject")))
    g.add((instance, RDF.type, BF.Instance))
    g.add((instance, BF.instanceOf, work))
    return g


def test_save_extracts_each_resource_once(pg_session, monkeypatch):
    """
    The graph stops changing once minting is done, so a save extracts each
    resource's subgraph once rather than again whenever minting has moved on.
    """
    extracted: list[str] = []
    original = bluecore_graph.generate_entity_graph

    def counting(graph, entity):
        extracted.append(str(entity))
        return original(graph, entity)

    monkeypatch.setattr(bluecore_graph, "generate_entity_graph", counting)

    save_graph(pg_session, _hub_work_instance_other())

    # the Hub, Work, Instance and the Other Resource, once each
    assert len(extracted) == 4
    assert len(set(extracted)) == 4


def test_entries_keep_the_subject_and_where_it_came_from(pg_session):
    work = _lc("works")
    g = Graph()
    g.add((work, RDF.type, BF.Work))
    bg = BluecoreGraph(g)

    bg.save(pg_session)

    [entry] = bg._entries[Kind.WORK]
    assert entry.kind is Kind.WORK
    assert entry.original == work
    assert str(entry.subject).startswith("https://bcld.info/works/")
    assert (entry.subject, RDF.type, BF.Work) in entry.graph


def test_entries_are_empty_before_save():
    assert BluecoreGraph(_hub_work_instance_other())._entries == {}
