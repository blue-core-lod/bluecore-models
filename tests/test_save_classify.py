import uuid

from rdflib import RDFS, BNode, Graph, Literal, URIRef

from bluecore_models.bluecore_graph import save_graph
from bluecore_models.models import BibframeOtherResources, Work
from bluecore_models.namespaces import BF, RDF


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
