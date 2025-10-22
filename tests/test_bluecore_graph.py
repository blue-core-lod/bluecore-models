import uuid

import pytest
from rdflib import Graph

from bluecore_models import bluecore_graph
from bluecore_models.bluecore_graph import BluecoreGraph, save_graph
from bluecore_models.models import Work, Instance, OtherResource, BibframeOtherResources
from bluecore_models.namespaces import BF, MADS, RDF
from bluecore_models.utils.graph import load_jsonld


jsonld_context = {
    "@vocab": "http://id.loc.gov/ontologies/bibframe/",
    "bflc": "http://id.loc.gov/ontologies/bflc/",
    "mads": "http://www.loc.gov/mads/rdf/v1#",
    "rdfs": "http://www.w3.org/2000/01/rdf-schema#",
}


def test_bluecore_graph():
    """
    Test that we can instantiate a BluecoreGraph and find works, instances and
    other resources.
    """
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


def test_save(pg_session):
    """
    Load a real CBD graph from disk, persist to the database, and check that the
    right number of Work, Instance and Other Resource objects are there.
    """

    # it is easier to evaluate if the database is empty of fixture data
    with pg_session() as session:
        session.query(Instance).delete()
        session.query(Work).delete()
        session.query(BibframeOtherResources).delete()
        session.query(OtherResource).delete()
        session.commit()

    g = Graph()
    g.parse("tests/23807141.ttl")
    bg = BluecoreGraph(g)
    bg.save(pg_session)

    with pg_session() as session:
        # two works are there, and they are linked to Other Resources
        works = session.query(Work).order_by(Work.id).all()
        assert len(works) == 2
        assert len(works[0].other_resources) == 3
        assert len(works[1].other_resources) == 25

        # two instances are there, and they are linked to Other Resources
        instances = session.query(Instance).order_by(Instance.id).all()
        assert len(instances) == 2
        assert len(instances[0].other_resources) == 2
        assert len(instances[1].other_resources) == 14

        # other resources are there and linked to works and instances
        others = session.query(OtherResource).all()
        assert len(others) == 32
        for other in others:
            print(other)
            bfs = (
                session.query(BibframeOtherResources)
                .filter(BibframeOtherResources.other_resource == other)
                .all()
            )
            assert len(bfs) != 0

    # loading the same graph again should overlay on the existing database
    # resources instead of creating new Work, Instance or Other Resource rows
    g = Graph()
    g.parse("tests/23807141.ttl")
    bg = BluecoreGraph(g)
    bg.save(pg_session)

    with pg_session() as session:
        assert len(session.query(Work).order_by(Work.id).all()) == 2
        assert len(session.query(Instance).order_by(Instance.id).all()) == 2
        assert len(session.query(OtherResource).all()) == 32


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


def test_non_bluecore_work(pg_session, monkeypatch, mocker):
    """
    Test that a Work graph from a non bluecore URI can be persisted to the database,
    with the original URI presered in a derivedFrom assertion.
    """
    monkeypatch.setattr(
        bluecore_graph,
        "uuid4",
        lambda *args, **kwargs: "7dbb7674-7373-473f-9014-b9a993a2dd03",
    )

    # spy on uuid generation which happens when new bluecore URIs are minted
    uuid_spy = mocker.spy(bluecore_graph, "uuid4")

    jsonld_object = {
        "@context": jsonld_context,
        "@id": "https://example.com/1234",
        "@type": BF.Work,
        "title": {"mainTitle": "Gravity's Rainbow", "@type": "Title"},
    }

    assert uuid_spy.call_count == 0
    save_graph(pg_session, load_jsonld(jsonld_object))
    assert uuid_spy.call_count == 1

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

    # saving the same JSON-LD again shouldn't cause a new bluecore URI to be
    # minted since the existing Work will be found using the derivedFrom
    save_graph(pg_session, load_jsonld(jsonld_object))

    assert uuid_spy.call_count == 1


def test_namespace(pg_session, monkeypatch, mocker):
    """
    Ensure that the default bluecore namespace can be changed.
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

    save_graph(pg_session, load_jsonld(jsonld_object), namespace="https://example.edu/")

    with pg_session() as session:
        work = (
            session.query(Work)
            .where(
                Work.uri
                == "https://example.edu/works/7dbb7674-7373-473f-9014-b9a993a2dd03"
            )
            .first()
        )
        assert work is not None


def test_invalid_namespace(pg_session):
    """
    Namespace must be a string that stars with http.
    """

    jsonld_object = {
        "@context": jsonld_context,
        "@id": "https://example.com/1234",
        "@type": BF.Work,
        "title": {"mainTitle": "Gravity's Rainbow", "@type": "Title"},
    }

    with pytest.raises(Exception) as e:
        save_graph(pg_session, load_jsonld(jsonld_object), namespace=None)
    assert str(e.value) == "default namespace cannot be None"

    with pytest.raises(Exception) as e:
        save_graph(pg_session, load_jsonld(jsonld_object), namespace="not-a-url")
    assert str(e.value) == "default namespace must be a URL, got not-a-url"


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


def test_non_bluecore_instance(pg_session, monkeypatch):
    """
    Test that a non bluecore Instance graph can be persisted to the database
    which will add a derivedFrom assertion for the original URI.
    """

    # patch the UUID function to return a known value during URI minting
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


def test_instance_update(pg_session):
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
    jsonld_object = {
        "@context": jsonld_context,
        "@id": "https://bcld.info/works/7dbb7674-7373-473f-9014-b9a993a2dd03",
        "@type": BF.Work,
        "title": {"@type": "Title", "mainTitle": "Gravity's Rainbow"},
        "hasInstance": [
            {
                "@id": "https://bcld.info/instances/B1380F26-55CA-4B89-B577-2E353665AC95",
                "@type": "Instance",
                "publicationStatement": "New York: Penguin Books, 1995",
                "title": {"@type": "Title", "mainTitle": "Gravity's rainbow"},
            },
            {
                "@id": "https://bcld.info/instances/79D5F91F-F4A9-4461-B3E0-CEA7ED470989",
                "@type": "Instance",
                "publicationStatement": "New Jersey: Penguin Books, 1987",
                "title": {"@type": "Title", "mainTitle": "Gravity's rainbow"},
            },
        ],
    }

    save_graph(pg_session, load_jsonld(jsonld_object))

    with pg_session() as session:
        # the work is there
        work = (
            session.query(Work)
            .where(
                Work.uri
                == "https://bcld.info/works/7dbb7674-7373-473f-9014-b9a993a2dd03"
            )
            .first()
        )
        assert work is not None
        assert work.data["title"]["mainTitle"] == "Gravity's Rainbow"

        # the first instance is there
        instance = (
            session.query(Instance)
            .where(
                Instance.uri
                == "https://bcld.info/instances/B1380F26-55CA-4B89-B577-2E353665AC95"
            )
            .first()
        )
        assert instance is not None
        assert instance.data["publicationStatement"] == "New York: Penguin Books, 1995"
        assert instance.work is not None, "instance got linked to the work in the db"

        # the second instance is there
        instance = (
            session.query(Instance)
            .where(
                Instance.uri
                == "https://bcld.info/instances/79D5F91F-F4A9-4461-B3E0-CEA7ED470989"
            )
            .first()
        )
        assert instance is not None
        assert (
            instance.data["publicationStatement"] == "New Jersey: Penguin Books, 1987"
        )
        assert instance.work is not None, "instance got linked to the work in the db"

        # and they are both available on the work
        assert len(work.instances) == 2


def test_work_instance_bnode(pg_session, monkeypatch):
    # patch the UUID function to return a known value during URI minting
    monkeypatch.setattr(
        bluecore_graph,
        "uuid4",
        lambda *args, **kwargs: "2B8CE2D3-BBD2-4F30-94BB-F382F39E5320",
    )

    jsonld_object = {
        "@context": jsonld_context,
        "@id": "https://bcld.info/works/7dbb7674-7373-473f-9014-b9a993a2dd03",
        "@type": BF.Work,
        "title": {"@type": "Title", "mainTitle": "Gravity's Rainbow"},
        "hasInstance": [
            {
                "@id": "_:123",
                "@type": "Instance",
                "publicationStatement": "New York: Penguin Books, 1995",
                "title": {"@type": "Title", "mainTitle": "Gravity's rainbow"},
            },
        ],
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
        assert len(work.instances) == 1, "work has instance"
        assert (
            work.instances[0].uri
            == "https://bcld.info/instances/2B8CE2D3-BBD2-4F30-94BB-F382F39E5320"
        ), "bnode turned into URI"


def test_other_resources(pg_session):
    """
    Ensure Work and Instance can be persisted with Other Resources attached.
    """

    jsonld_object = {
        "@context": jsonld_context,
        "@id": "https://bcld.info/works/7dbb7674-7373-473f-9014-b9a993a2dd03",
        "@type": BF.Work,
        "title": {"@type": "Title", "mainTitle": "Gravity's Rainbow"},
        "contribution": {
            "@type": ["Contribution", "PrimaryContribution"],
            # the Work has its first Other Resource here
            "agent": {
                "@id": "http://id.loc.gov/rwo/agents/n79099184",
                "@type": ["Agent", "Person"],
                "bflc:marcKey": "1001 $aPynchon, Thomas",
                "rdfs:label": "Pynchon, Thomas",
            },
            # the Work has a second Other Resource Here
            "role": {
                "@id": "http://id.loc.gov/vocabulary/relators/aut",
                "@type": "Role",
                "code": "aut",
                "rdfs:label": "author",
            },
        },
        # the Work has an Instance
        "hasInstance": [
            {
                "@id": "https://bcld.info/instances/B1380F26-55CA-4B89-B577-2E353665AC95",
                "@type": "Instance",
                "publicationStatement": "New York: Penguin Books, 1995",
                "title": {"@type": "Title", "mainTitle": "Gravity's rainbow"},
                # the Instance has an Other Resource here
                "carrier": {
                    "@id": "http://id.loc.gov/vocabulary/carriers/cd",
                    "@type": "Carrier",
                    "rdfs:label": "computer disc",
                },
            }
        ],
    }

    save_graph(pg_session, load_jsonld(jsonld_object))

    with pg_session() as session:
        # the Person other resource is there
        other = (
            session.query(OtherResource)
            .where(OtherResource.uri == "http://id.loc.gov/rwo/agents/n79099184")
            .first()
        )
        assert other is not None
        assert other.data["rdfs:label"] == "Pynchon, Thomas"

        # the Role other resource is there
        other = (
            session.query(OtherResource)
            .where(OtherResource.uri == "http://id.loc.gov/vocabulary/relators/aut")
            .first()
        )
        assert other is not None
        assert other.data["rdfs:label"] == "author"

        # and so is the work
        work = (
            session.query(Work)
            .where(
                Work.uri
                == "https://bcld.info/works/7dbb7674-7373-473f-9014-b9a993a2dd03"
            )
            .first()
        )
        assert work is not None
        assert work.data["title"]["mainTitle"] == "Gravity's Rainbow"

        # the work should be attached to two other resources
        assert len(work.other_resources) == 2

        # sort the BibframeOtherResource objects by the label of their Other Resource
        # so that they are in a predictable order that can be tested
        others = sorted(
            work.other_resources,
            key=lambda o: o.other_resource.data["rdfs:label"].lower(),
        )
        assert (
            others[0].bibframe_resource.data["title"]["mainTitle"]
            == "Gravity's Rainbow"
        )
        assert others[0].other_resource.data["rdfs:label"] == "author"
        assert (
            others[1].bibframe_resource.data["title"]["mainTitle"]
            == "Gravity's Rainbow"
        )
        assert others[1].other_resource.data["rdfs:label"] == "Pynchon, Thomas"

        # the instance is there
        instance = (
            session.query(Instance)
            .where(
                Instance.uri
                == "https://bcld.info/instances/B1380F26-55CA-4B89-B577-2E353665AC95"
            )
            .first()
        )
        assert instance is not None
        assert instance.data["publicationStatement"] == "New York: Penguin Books, 1995"

        # the instance should be attached to one other resource
        assert len(instance.other_resources) == 1
        assert (
            instance.other_resources[0].other_resource.data["rdfs:label"]
            == "computer disc"
        )


def test_other_resource_update(pg_session):
    """
    When saving a Work or Instance's Other Instances the previously linked ones
    will be removed before new ones are added. This allows Other Resources to be removed from a
    description when being updated.
    """

    jsonld_object = {
        "@context": jsonld_context,
        "@id": "https://bcld.info/instances/7dbb7674-7373-473f-9014-b9a993a2dd03",
        "@type": "Instance",
        "title": {"@type": "Title", "mainTitle": "Gravity's Rainbow"},
        "carrier": {
            "@id": "http://id.loc.gov/vocabulary/carriers/cd",
            "@type": "Carrier",
            "rdfs:label": "computer disc",
        },
    }

    save_graph(pg_session, load_jsonld(jsonld_object))

    jsonld_object = {
        "@context": jsonld_context,
        "@id": "https://bcld.info/instances/7dbb7674-7373-473f-9014-b9a993a2dd03",
        "@type": "Instance",
        "title": {"@type": "Title", "mainTitle": "Gravity's Rainbow"},
        "carrier": {
            "@id": "http://id.loc.gov/vocabulary/carriers/cb",
            "@type": "Carrier",
            "rdfs:label": "computer chip cartridge",
        },
    }

    save_graph(pg_session, load_jsonld(jsonld_object))

    with pg_session() as session:
        instance = (
            session.query(Instance)
            .where(
                Work.uri
                == "https://bcld.info/instances/7dbb7674-7373-473f-9014-b9a993a2dd03"
            )
            .first()
        )
        assert instance is not None
        assert len(instance.other_resources) == 1
