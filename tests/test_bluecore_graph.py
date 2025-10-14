import uuid
from rdflib import Graph

from bluecore_models import bluecore_graph
from bluecore_models.bluecore_graph import BluecoreGraph, save_graph
from bluecore_models.models import Work, Instance
from bluecore_models.namespaces import BF, MADS, RDF
from bluecore_models.utils.graph import load_jsonld


jsonld_context = {
    "@vocab": "http://id.loc.gov/ontologies/bibframe/",
    "bflc": "http://id.loc.gov/ontologies/bflc/",
    "mads": "http://www.loc.gov/mads/rdf/v1#",
}


def test_bluecore_graph():
    g = Graph()
    g.parse("tests/23807141.ttl")
    bg = BluecoreGraph(g)

    works = bg.works()
    assert len(works) == 2, "found two Works"
    assert len(works[0]) == 14, "found expected number of assertions for Work 1"
    assert len(works[1]) == 118, "found expected number of assertions for Work 2"

    instances = bg.instances()
    assert len(instances) == 2, "found two Instances"
    assert len(instances[0]) == 11, "found expected number of assertions for Instance 1"
    assert len(instances[1]) == 68, "found expected number of assertions for Instance 2"

    others = bg.others()
    assert len(others) == 32, "found expected number of Other Resources"
    for other_graph in others:
        assert len(other_graph) > 0
        for s, o in other_graph.subject_objects(RDF.type):
            assert s not in BF, "Other resource URI not in Bibframe vocabulary"
            assert s not in MADS, "Other resource URI not in MADS vocabulary"
            assert o not in [BF.Work, BF.Instance], (
                "OtherResource is not a Work or Instance"
            )


def test_work(pg_session):
    """
    Test that a bluecore Work graph can be persisted to the database.
    """
    jsonld_object = {
        "@context": jsonld_context,
        "@id": "https://bcld.info/works/7dbb7674-7373-473f-9014-b9a993a2dd03",
        "@type": BF.Work,
        "title": {"mainTitle": "Gravity's Rainbow", "@type": "Title"},
    }

    save_graph(pg_session, load_jsonld(jsonld_object))

    with pg_session() as session:
        work = (
            session.query(Work)
            .where(
                Work.uri
                == "https://bcld.info/works/7dbb7674-7373-473f-9014-b9a993a2dd03"
            )
            .first()
        )
        assert work is not None
        assert work.uuid == uuid.UUID("7dbb7674-7373-473f-9014-b9a993a2dd03")
        assert work.data["@type"] == "Work"
        assert work.data["title"]["mainTitle"] == "Gravity's Rainbow"


def test_other_work(pg_session, monkeypatch):
    """
    Test that an other Work graph can be persisted to the database, with a
    derivedFrom assertion.
    """
    monkeypatch.setattr(
        bluecore_graph,
        "uuid4",
        lambda *args, **kwargs: "7dbb7674-7373-473f-9014-b9a993a2dd03",
    )

    jsonld_object = {
        "@context": jsonld_context,
        "@id": "https://example.com/1234",
        "@type": BF.Work,
        "title": {"mainTitle": "Gravity's Rainbow", "@type": "Title"},
    }

    save_graph(pg_session, load_jsonld(jsonld_object))

    with pg_session() as session:
        work = (
            session.query(Work)
            .where(
                Work.uri
                == "https://bcld.info/works/7dbb7674-7373-473f-9014-b9a993a2dd03"
            )
            .first()
        )
        assert work is not None
        assert work.uuid == uuid.UUID("7dbb7674-7373-473f-9014-b9a993a2dd03")
        assert work.data["@type"] == "Work"
        assert work.data["title"]["mainTitle"] == "Gravity's Rainbow"
        assert work.data["derivedFrom"]["@id"] == "https://example.com/1234"


def test_work_update(pg_session):
    """
    Test that a bluecore Work graph can be updated in the database.
    """

    # save an initial work
    jsonld_object = {
        "@context": jsonld_context,
        "@id": "https://bcld.info/works/7dbb7674-7373-473f-9014-b9a993a2dd03",
        "@type": BF.Work,
        "title": {"@type": "Title", "mainTitle": "Gravity's Rainbow"},
    }

    save_graph(pg_session, load_jsonld(jsonld_object))

    # add a note to the jsonld and save it again
    jsonld_object["note"] = {"@type": "Note", "rdfs:label": "First Edition"}
    save_graph(pg_session, load_jsonld(jsonld_object))

    # ensure the work has the note
    with pg_session() as session:
        work = (
            session.query(Work)
            .where(
                Work.uri
                == "https://bcld.info/works/7dbb7674-7373-473f-9014-b9a993a2dd03"
            )
            .first()
        )
        assert work.data["note"]["rdfs:label"] == "First Edition"


def test_two_works(pg_session):
    pass


def test_instance(pg_session):
    jsonld_object = {
        "@context": jsonld_context,
        "@id": "https://bcld.info/instances/7dbb7674-7373-473f-9014-b9a993a2dd03",
        "@type": BF.Instance,
        "title": {"@type": "Title", "mainTitle": "Gravity's Rainbow"},
    }

    save_graph(pg_session, load_jsonld(jsonld_object))

    with pg_session() as session:
        work = (
            session.query(Instance)
            .where(
                Instance.uri
                == "https://bcld.info/instances/7dbb7674-7373-473f-9014-b9a993a2dd03"
            )
            .first()
        )
        assert work is not None
        assert work.uuid == uuid.UUID("7dbb7674-7373-473f-9014-b9a993a2dd03")
        assert work.data["@type"] == "Instance"
        assert work.data["title"]["mainTitle"] == "Gravity's Rainbow"


def test_other_instance(pg_session, monkeypatch):
    """
    Test that an other Instance graph can be persisted to the database with a
    derivedFrom assertion.
    """
    monkeypatch.setattr(
        bluecore_graph,
        "uuid4",
        lambda *args, **kwargs: "7dbb7674-7373-473f-9014-b9a993a2dd03",
    )

    jsonld_object = {
        "@context": jsonld_context,
        "@id": "https://example.com/1234",
        "@type": BF.Instance,
        "title": {"mainTitle": "Gravity's Rainbow", "@type": "Title"},
    }

    save_graph(pg_session, load_jsonld(jsonld_object))

    with pg_session() as session:
        instance = (
            session.query(Instance)
            .where(
                Instance.uri
                == "https://bcld.info/instances/7dbb7674-7373-473f-9014-b9a993a2dd03"
            )
            .first()
        )
        assert instance is not None
        assert instance.uuid == uuid.UUID("7dbb7674-7373-473f-9014-b9a993a2dd03")
        assert instance.data["@type"] == "Instance"
        assert instance.data["title"]["mainTitle"] == "Gravity's Rainbow"
        assert instance.data["derivedFrom"]["@id"] == "https://example.com/1234"


def test_instance_update(jsonld_context, pg_session):
    """
    Test that a bluecore Instance graph can be updated in the database.
    """

    # save an initial instance
    jsonld_object = {
        "@context": jsonld_context,
        "@id": "https://bcld.info/instances/7dbb7674-7373-473f-9014-b9a993a2dd03",
        "@type": BF.Instance,
        "title": {"@type": "Title", "mainTitle": "Gravity's Rainbow"},
    }

    save_graph(pg_session, load_jsonld(jsonld_object))

    # add a note to the jsonld and save it again
    jsonld_object["note"] = {"@type": "Note", "rdfs:label": "First Edition"}
    save_graph(pg_session, load_jsonld(jsonld_object))

    # ensure the work has the note
    with pg_session() as session:
        instance = (
            session.query(Instance)
            .where(
                Instance.uri
                == "https://bcld.info/instances/7dbb7674-7373-473f-9014-b9a993a2dd03"
            )
            .first()
        )
        assert instance.data["note"]["rdfs:label"] == "First Edition"


def test_work_instances(pg_session):
    pass


def test_work_intances_update(pg_session):
    pass


def test_other_resource(pg_session):
    pass


def test_save_full_graph(pg_session):
    pass
