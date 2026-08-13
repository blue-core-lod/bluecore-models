import json
import uuid

import pytest
from rdflib import RDFS, BNode, Graph, Literal, URIRef
from sqlalchemy.orm import sessionmaker

from bluecore_models import bluecore_graph
from bluecore_models.bluecore_graph import BluecoreGraph, save_graph
from bluecore_models.models import (
    BibframeOtherResources,
    Hub,
    Instance,
    OtherResource,
    Work,
)
from bluecore_models.namespaces import BF, MADS, RDF
from bluecore_models.utils.graph import CONTEXT, load_jsonld


def _derived_from_ids(data: dict) -> list[str]:
    """
    Collect every bf:derivedFrom @id recorded in a resource's adminMetadata
    nodes. derivedFrom now lives on the generated AdminMetadata rather than as a
    top-level assertion on the resource.
    """
    admin_metadata = data.get("adminMetadata", [])
    if isinstance(admin_metadata, dict):
        admin_metadata = [admin_metadata]
    return [
        am["derivedFrom"]["@id"]
        for am in admin_metadata
        if isinstance(am, dict) and "derivedFrom" in am
    ]


def test_bluecore_graph():
    """
    Test that we can instantiate a BluecoreGraph and find works, instances and
    other resources.
    """
    g = Graph()
    g.parse("tests/data/23807141.ttl")
    bg = BluecoreGraph(g)

    works = bg.works()
    assert len(works) == 2, "found two Works"
    assert len(works[0]) == 14, "found expected number of assertions for Work 1"
    assert len(works[1]) == 118, "found expected number of assertions for Work 2"

    instances = bg.instances()
    assert len(instances) == 2, "found two Instances"
    assert len(instances[0]) == 12, "found expected number of assertions for Instance 1"
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


def test_remove_oclc_number_identifiers():
    """
    bf:identifiedBy blank nodes typed bf:OclcNumber are stripped out, while
    other identifier types (e.g. bf:Lccn) are left in place.
    """
    g = Graph()
    g.parse(
        data="""
        @prefix bf: <http://id.loc.gov/ontologies/bibframe/> .
        @prefix rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#> .

        <https://dev.bcld.info/instances/0ca0f64e-d086-4309-a3e7-22ed061ad7e8>
            a bf:Instance ;
            bf:identifiedBy [ a bf:Lccn ; rdf:value "  2022508031" ] ;
            bf:identifiedBy [ a bf:OclcNumber ; rdf:value "on1267753195" ] .
        """,
        format="turtle",
    )
    bg = BluecoreGraph(g)
    instances = bg.instances()
    assert len(instances) == 1
    instance_graph = instances[0]

    bg._remove_triples_by_type(instance_graph, BF.identifiedBy, BF.OclcNumber)

    identifier_types = {
        identifier_type
        for _, identifier in instance_graph.subject_objects(BF.identifiedBy)
        for identifier_type in instance_graph.objects(identifier, RDF.type)
    }
    assert BF.OclcNumber not in identifier_types
    assert BF.Lccn in identifier_types


def test_save(pg_session):
    """
    Load a real CBD graph from disk, persist to the database, and check that the
    right number of Work, Instance and Other Resource objects are there.
    """

    # it is easier to evaluate if the database is empty of fixture data
    _remove_fixtures(pg_session)

    g = Graph()
    g.parse("tests/data/23807141.ttl")
    bg = BluecoreGraph(g)
    bg.save(pg_session)

    with pg_session() as session:
        # two works are there, and they are linked to Other Resources. The counts
        # reflect that deriving these (non-bluecore) resources regenerates their
        # AdminMetadata: the original AdminMetadata's Other Resources are removed
        # and the generated AdminMetadata's are added (descriptionAuthentication,
        # descriptionLanguage, descriptionLevel and status).
        # works[0] arrived without AdminMetadata of its own, so it is only a stub
        # of the resource the record points at, and its generated status is
        # mstatus/incmp instead of the mstatus/c works[1] gets. The count is the
        # same either way -- it is one status term or the other.
        works = session.query(Work).order_by(Work.id).all()
        assert len(works) == 2
        assert len(works[0].other_resources) == 6
        assert len(works[1].other_resources) == 20

        # two instances are there, and they are linked to Other Resources.
        # instances[0] is a stub for the same reason works[0] is.
        instances = session.query(Instance).order_by(Instance.id).all()
        assert len(instances) == 2
        assert len(instances[0].other_resources) == 5
        assert len(instances[1].other_resources) == 8

        # other resources are there and linked to works and instances. The extra
        # one over the record's own vocabulary terms is mstatus/incmp, which the
        # record never mentions, so we describe it ourselves for the stubs to
        # resolve against like any other term.
        others = session.query(OtherResource).all()
        assert len(others) == 28
        for other in others:
            bfs = (
                session.query(BibframeOtherResources)
                .filter(BibframeOtherResources.other_resource == other)
                .all()
            )
            assert len(bfs) != 0

    # loading the same graph again should overlay on the existing database
    # resources instead of creating new Work, Instance or Other Resource rows
    g = Graph()
    g.parse("tests/data/23807141.ttl")
    bg = BluecoreGraph(g)
    bg.save(pg_session)

    with pg_session() as session:
        assert len(session.query(Work).order_by(Work.id).all()) == 2
        assert len(session.query(Instance).order_by(Instance.id).all()) == 2
        assert len(session.query(OtherResource).all()) == 28


def test_work(pg_session):
    """
    Test that a bluecore Work graph can be persisted to the database.
    """
    jsonld_object = {
        # "@context": CONTEXT, # it should parse with or without @context
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
        "@context": CONTEXT,
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
        assert "https://example.com/1234" in _derived_from_ids(work.data)

    # saving the same JSON-LD again shouldn't cause a new bluecore URI to be
    # minted since the existing Work will be found using the derivedFrom
    save_graph(pg_session, load_jsonld(jsonld_object))

    assert uuid_spy.call_count == 1


@pytest.mark.parametrize(
    "bf_class, sqla_class, collection",
    [
        (BF.Work, Work, "works"),
        (BF.Instance, Instance, "instances"),
        (BF.Hub, Hub, "hubs"),
    ],
)
def test_blank_node_resource(
    pg_session, monkeypatch, mocker, bf_class, sqla_class, collection
):
    """
    A graph whose subject is a blank node (i.e. has no URI yet, as when a
    brand-new resource is created in an editor) should be persisted with a freshly
    minted bluecore URI and NO derivedFrom assertion, since there is no original
    URI to derive from.
    """
    minted_uuid = "7dbb7674-7373-473f-9014-b9a993a2dd03"
    monkeypatch.setattr(bluecore_graph, "uuid4", lambda *args, **kwargs: minted_uuid)

    uuid_spy = mocker.spy(bluecore_graph, "uuid4")

    graph = Graph()
    resource = BNode()
    title = BNode()
    graph.add((resource, RDF.type, bf_class))
    graph.add((resource, BF.title, title))
    graph.add((title, RDF.type, BF.Title))
    graph.add((title, BF.mainTitle, Literal("Gravity's Rainbow")))

    assert uuid_spy.call_count == 0
    save_graph(pg_session, graph)
    assert uuid_spy.call_count == 1

    with pg_session() as session:
        db_resource = (
            session.query(sqla_class)
            .where(sqla_class.uri == f"https://bcld.info/{collection}/{minted_uuid}")
            .first()
        )
        assert db_resource is not None
        assert db_resource.uuid == uuid.UUID(minted_uuid)
        assert db_resource.data["title"]["mainTitle"] == "Gravity's Rainbow"
        # no derivedFrom should be recorded for a brand-new (blank node) resource
        assert _derived_from_ids(db_resource.data) == []


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
        "@context": CONTEXT,
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
        "@context": CONTEXT,
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
        "@context": CONTEXT,
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
        "@context": CONTEXT,
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
        "@context": CONTEXT,
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
        assert "https://example.com/1234" in _derived_from_ids(instance.data)


def test_instance_update(pg_session):
    """
    Test that a bluecore Instance graph can be updated in the database.
    """

    # save an initial instance
    jsonld_object = {
        "@context": CONTEXT,
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
        "@context": CONTEXT,
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
        "@context": CONTEXT,
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


def test_other_resources_autoflush_disabled(engine):
    """
    save_graph must link Other Resources even when the session has autoflush
    disabled (as the bluecore_api session does). Without an explicit flush
    before _link, its uri lookups don't see the just-added rows, so the link
    rows are built with null foreign keys and commit raises NotNullViolation.
    """
    pg_session = sessionmaker(bind=engine, autoflush=False)

    jsonld_object = {
        "@context": CONTEXT,
        "@id": "https://bcld.info/works/9999aaaa-0000-1111-2222-333344445555",
        "@type": BF.Work,
        "title": {"@type": "Title", "mainTitle": "Gravity's Rainbow"},
        "contribution": {
            "@type": ["Contribution", "PrimaryContribution"],
            "agent": {
                "@id": "http://id.loc.gov/rwo/agents/n79099184",
                "@type": ["Agent", "Person"],
                "rdfs:label": "Pynchon, Thomas",
            },
            "role": {
                "@id": "http://id.loc.gov/vocabulary/relators/aut",
                "@type": "Role",
                "rdfs:label": "author",
            },
        },
    }

    save_graph(pg_session, load_jsonld(jsonld_object))

    with pg_session() as session:
        work = (
            session.query(Work)
            .where(
                Work.uri
                == "https://bcld.info/works/9999aaaa-0000-1111-2222-333344445555"
            )
            .first()
        )
        assert work is not None
        # the two Other Resources are linked, with non-null foreign keys
        assert len(work.other_resources) == 2


def test_other_resources(pg_session):
    """
    Ensure Work and Instance can be persisted with Other Resources attached.
    """

    jsonld_object = {
        "@context": CONTEXT,
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
        "@context": CONTEXT,
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
        "@context": CONTEXT,
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


def test_inference(pg_session):
    """
    If rdf:type assertions are missing from the graph they should be inferred
    for resources involved in hasInstance and instanceOf assertions.
    """
    cbd_jsonld = {
        "@context": CONTEXT,
        "@id": "https://bcld.info/works/4e2496b4-2c5b-491e-8369-a837138234de",
        "@type": BF.Work,
        "title": {"mainTitle": "Gravity's Rainbow"},
        "hasInstance": {
            "@id": "https://bcld.info/instances/500da8ca-2a06-4c35-a028-15e37e0e0ddd",
            "provisionActivity": {"date": "1993"},
        },
    }

    save_graph(pg_session, load_jsonld(cbd_jsonld))

    with pg_session() as session:
        work = (
            session.query(Work)
            .where(
                Work.uri
                == "https://bcld.info/works/4e2496b4-2c5b-491e-8369-a837138234de"
            )
            .first()
        )
        assert work is not None

        instance = (
            session.query(Instance)
            .where(
                Instance.uri
                == "https://bcld.info/instances/500da8ca-2a06-4c35-a028-15e37e0e0ddd",
            )
            .first()
        )
        assert instance is not None


def test_instance_linking(pg_session):
    """
    Ensure that works and instances are linked correctly when external related
    reources are used. In this case the hasInstance and instanceOf assertions
    point at id.loc.gov resources. This test exercises the logic that looks in
    the graph that is being saved for derivedFrom links.
    """
    # Start with empty database
    _remove_fixtures(pg_session)

    cbd_jsonld = [
        {
            "@context": CONTEXT,
            "@id": "https://bcld.info/works/4e2496b4-2c5b-491e-8369-a837138234de",
            "@type": BF.Work,
            "adminMetadata": {
                "@type": "AdminMetadata",
                "derivedFrom": {"@id": "http://id.loc.gov/resources/works/24021036"},
            },
            "hasInstance": {"@id": "http://id.loc.gov/resources/instances/24021036"},
        },
        {
            "@context": CONTEXT,
            "@id": "https://bcld.info/instances/500da8ca-2a06-4c35-a028-15e37e0e0ddd",
            "@type": BF.Instance,
            "adminMetadata": {
                "@type": "AdminMetadata",
                "derivedFrom": {
                    "@id": "http://id.loc.gov/resources/instances/24021036"
                },
            },
            "instanceOf": {"@id": "http://id.loc.gov/resources/works/24021036"},
        },
    ]

    save_graph(pg_session, load_jsonld(cbd_jsonld))

    with pg_session() as session:
        works = session.query(Work).all()
        assert len(works) == 1
        assert (
            works[0].uri
            == "https://bcld.info/works/4e2496b4-2c5b-491e-8369-a837138234de"
        )

        instances = session.query(Instance).all()
        assert len(instances) == 1
        assert (
            instances[0].uri
            == "https://bcld.info/instances/500da8ca-2a06-4c35-a028-15e37e0e0ddd"
        )


def test_hub_not_in_works():
    """
    A resource typed as both bf:Hub and bf:Work should appear in hubs() but not
    works(). This verifies the Hub exclusion added to works().
    """
    g = load_jsonld(
        {
            "@context": CONTEXT,
            "@id": "https://bcld.info/hubs/7dbb7674-7373-473f-9014-b9a993a2dd03",
            "@type": [BF.Hub, BF.Work],
            "title": {"mainTitle": "Hub Record", "@type": "Title"},
        }
    )
    bg = BluecoreGraph(g)

    assert len(bg.hubs()) == 1
    assert len(bg.works()) == 0


def test_hub(pg_session):
    """
    Test that a bluecore Hub graph can be persisted to the database.
    """
    jsonld_object = {
        "@context": CONTEXT,
        "@id": "https://bcld.info/hubs/7dbb7674-7373-473f-9014-b9a993a2dd03",
        "@type": [BF.Hub, BF.Work],
        "title": {"mainTitle": "Hub Record", "@type": "Title"},
    }

    save_graph(pg_session, load_jsonld(jsonld_object))

    with pg_session() as session:
        hub = (
            session.query(Hub)
            .where(
                Hub.uri == "https://bcld.info/hubs/7dbb7674-7373-473f-9014-b9a993a2dd03"
            )
            .first()
        )
        assert hub is not None
        assert hub.uuid == uuid.UUID("7dbb7674-7373-473f-9014-b9a993a2dd03")
        assert "Hub" in hub.data["@type"]
        assert hub.data["title"]["mainTitle"] == "Hub Record"


def test_non_bluecore_hub(pg_session, monkeypatch, mocker):
    """
    Test that a Hub graph from a non-bluecore URI can be persisted to the database,
    with the original URI preserved in a derivedFrom assertion.
    """
    monkeypatch.setattr(
        bluecore_graph,
        "uuid4",
        lambda *args, **kwargs: "7dbb7674-7373-473f-9014-b9a993a2dd03",
    )

    uuid_spy = mocker.spy(bluecore_graph, "uuid4")

    jsonld_object = {
        "@context": CONTEXT,
        "@id": "https://example.com/hubs/1234",
        "@type": [BF.Hub, BF.Work],
        "title": {"mainTitle": "Hub Record", "@type": "Title"},
    }

    assert uuid_spy.call_count == 0
    save_graph(pg_session, load_jsonld(jsonld_object))
    assert uuid_spy.call_count == 1

    with pg_session() as session:
        hub = (
            session.query(Hub)
            .where(
                Hub.uri == "https://bcld.info/hubs/7dbb7674-7373-473f-9014-b9a993a2dd03"
            )
            .first()
        )
        assert hub is not None
        assert hub.uuid == uuid.UUID("7dbb7674-7373-473f-9014-b9a993a2dd03")
        assert "Hub" in hub.data["@type"]
        assert hub.data["title"]["mainTitle"] == "Hub Record"
        assert "https://example.com/hubs/1234" in _derived_from_ids(hub.data)

    # saving the same JSON-LD again shouldn't mint a new URI
    save_graph(pg_session, load_jsonld(jsonld_object))
    assert uuid_spy.call_count == 1


def test_hubs_are_not_other_resources(pg_session):
    """
    This JSON-LD contains a Hub along with Works, Instances and Other Resources.
    If the Hub isn't distinguished from Other Resources it could result in it
    being added to the database twice, which will cause a unique constraint
    violation.
    """
    with open("tests/data/19167709.cbd.jsonld") as fo:
        g = load_jsonld(json.load(fo))
    save_graph(pg_session, g)


def test_hub_update(pg_session):
    """
    Test that a bluecore Hub graph can be updated in the database.
    """
    jsonld_object = {
        "@context": CONTEXT,
        "@id": "https://bcld.info/hubs/7dbb7674-7373-473f-9014-b9a993a2dd03",
        "@type": [BF.Hub, BF.Work],
        "title": {"@type": "Title", "mainTitle": "Hub Record"},
    }

    save_graph(pg_session, load_jsonld(jsonld_object))

    jsonld_object["note"] = {"@type": "Note", "rdfs:label": "Updated note"}
    save_graph(pg_session, load_jsonld(jsonld_object))

    with pg_session() as session:
        hub = (
            session.query(Hub)
            .where(
                Hub.uri == "https://bcld.info/hubs/7dbb7674-7373-473f-9014-b9a993a2dd03"
            )
            .first()
        )
        assert hub.data["note"]["rdfs:label"] == "Updated note"


def test_admin_metadata(pg_session):
    """
    Tests removal of old adminMetadata and the generation of BC
    specific adminMetadata.

    The generated adminMetadata is split across two AdminMetadata blank nodes per
    resource: bf:derivedFrom lives on the first node and bf:descriptionAuthentication
    on the second. So for each Instance we collect those predicates across *all* of
    its adminMetadata nodes rather than assuming they sit on a single node.
    """
    g = Graph()
    g.parse("tests/data/23807141.ttl")

    existing_admin_metadata = [
        r for r in g.subjects(predicate=RDF.type, object=BF.AdminMetadata)
    ]

    assert len(existing_admin_metadata) == 8

    # the original Instance URIs are preserved as bf:derivedFrom assertions once
    # the resources are minted Bluecore URIs
    original_instance_uris = set(g.subjects(predicate=RDF.type, object=BF.Instance))
    assert original_instance_uris

    updated_graph = save_graph(pg_session, g)

    instances = list(updated_graph.subjects(predicate=RDF.type, object=BF.Instance))
    # the original Instance URIs have been replaced by minted Bluecore URIs
    assert instances

    for instance in instances:
        admin_metadata = list(
            updated_graph.objects(subject=instance, predicate=BF.adminMetadata)
        )
        assert len(admin_metadata) == 2

        # gather the predicates we care about across every adminMetadata node
        description_authentications = set()
        derived_from = set()
        for node in admin_metadata:
            description_authentications.update(
                updated_graph.objects(
                    subject=node, predicate=BF.descriptionAuthentication
                )
            )
            derived_from.update(
                updated_graph.objects(subject=node, predicate=BF.derivedFrom)
            )

        assert description_authentications == {
            URIRef("http://id.loc.gov/vocabulary/marcauthen/pcc")
        }
        # each Instance records exactly one derivedFrom, pointing at its original URI
        assert len(derived_from) == 1
        assert derived_from <= original_instance_uris


def _remove_fixtures(pg_session):
    with pg_session() as session:
        session.query(Instance).delete()
        session.query(Work).delete()
        session.query(BibframeOtherResources).delete()
        session.query(OtherResource).delete()
        session.commit()


# ---------------------------------------------------------------------------
# primary_class: references must not clobber existing full descriptions
# ---------------------------------------------------------------------------


def _new_uri(kind: str) -> str:
    return f"https://bcld.info/{kind}/{uuid.uuid4()}"


def test_reference_does_not_clobber_existing_instance(pg_session):
    """
    A Work saved as the primary that only *references* an existing Instance (even
    with a display label) must leave the Instance's full description untouched,
    and still create the link.
    """
    inst_uri = _new_uri("instances")
    save_graph(
        pg_session,
        load_jsonld(
            {
                "@id": inst_uri,
                "@type": BF.Instance,
                "title": {"mainTitle": "Full Instance", "@type": "Title"},
            }
        ),
    )
    with pg_session() as session:
        inst = session.query(Instance).where(Instance.uri == inst_uri).first()
        before = json.dumps(inst.data, sort_keys=True)

    work_uri = _new_uri("works")
    g = Graph()
    g.add((URIRef(work_uri), RDF.type, BF.Work))
    g.add((URIRef(work_uri), BF.hasInstance, URIRef(inst_uri)))
    g.add((URIRef(inst_uri), RDFS.label, Literal("display label")))
    save_graph(pg_session, g, primary_class=BF.Work)

    with pg_session() as session:
        inst = session.query(Instance).where(Instance.uri == inst_uri).first()
        # full description preserved -- the sparse reference did not overwrite it
        assert inst.data["title"]["mainTitle"] == "Full Instance"
        assert json.dumps(inst.data, sort_keys=True) == before
        # link was still created
        work = session.query(Work).where(Work.uri == work_uri).first()
        assert work is not None
        assert inst.work_id == work.id


def test_reference_does_not_clobber_existing_work(pg_session):
    """
    Symmetric case: a new Instance saved as the primary that references an existing
    Work (with only a label) must not overwrite the Work's full description.
    """
    work_uri = _new_uri("works")
    save_graph(
        pg_session,
        load_jsonld(
            {
                "@id": work_uri,
                "@type": BF.Work,
                "title": {"mainTitle": "Full Work", "@type": "Title"},
            }
        ),
    )
    with pg_session() as session:
        work = session.query(Work).where(Work.uri == work_uri).first()
        before = json.dumps(work.data, sort_keys=True)

    inst_uri = _new_uri("instances")
    g = Graph()
    g.add((URIRef(inst_uri), RDF.type, BF.Instance))
    g.add((URIRef(inst_uri), BF.instanceOf, URIRef(work_uri)))
    g.add((URIRef(work_uri), RDFS.label, Literal("display label")))
    save_graph(pg_session, g, primary_class=BF.Instance)

    with pg_session() as session:
        work = session.query(Work).where(Work.uri == work_uri).first()
        assert work.data["title"]["mainTitle"] == "Full Work"
        assert json.dumps(work.data, sort_keys=True) == before
        inst = session.query(Instance).where(Instance.uri == inst_uri).first()
        assert inst is not None
        assert inst.work_id == work.id


def test_reference_to_absent_resource_is_created_with_known_triples(pg_session):
    """
    A reference to a resource that isn't in the db yet is created (so the link can
    be made) storing whatever the payload knows about it -- not just the id.
    """
    work_uri = _new_uri("works")
    inst_uri = _new_uri("instances")  # not yet in the db
    g = Graph()
    g.add((URIRef(work_uri), RDF.type, BF.Work))
    g.add((URIRef(work_uri), BF.hasInstance, URIRef(inst_uri)))
    g.add((URIRef(inst_uri), RDFS.label, Literal("just a label")))
    save_graph(pg_session, g, primary_class=BF.Work)

    with pg_session() as session:
        inst = session.query(Instance).where(Instance.uri == inst_uri).first()
        assert inst is not None  # created
        assert "just a label" in json.dumps(inst.data)  # known triples captured
        work = session.query(Work).where(Work.uri == work_uri).first()
        assert inst.work_id == work.id  # linked


def test_reference_does_not_clobber_existing_other_resource(pg_session):
    """
    Other Resources are shared and variably-described: a Work that references one
    with only a label must not shrink an existing fuller description.
    """
    agent_uri = _new_uri("other_resources")
    # a first Work creates the Agent with a fuller description (create-if-absent)
    g1 = Graph()
    w1 = _new_uri("works")
    g1.add((URIRef(w1), RDF.type, BF.Work))
    g1.add((URIRef(w1), BF.contribution, URIRef(agent_uri)))
    g1.add((URIRef(agent_uri), RDF.type, BF.Agent))
    g1.add((URIRef(agent_uri), RDFS.label, Literal("Ursula K. Le Guin")))
    g1.add((URIRef(agent_uri), BF.note, Literal("author")))
    save_graph(pg_session, g1, primary_class=BF.Work)
    with pg_session() as session:
        agent = (
            session.query(OtherResource).where(OtherResource.uri == agent_uri).first()
        )
        assert agent is not None
        before = json.dumps(agent.data, sort_keys=True)

    # a second Work references the same Agent with only a sparse label
    g2 = Graph()
    w2 = _new_uri("works")
    g2.add((URIRef(w2), RDF.type, BF.Work))
    g2.add((URIRef(w2), BF.contribution, URIRef(agent_uri)))
    g2.add((URIRef(agent_uri), RDF.type, BF.Agent))
    g2.add((URIRef(agent_uri), RDFS.label, Literal("Le Guin")))
    save_graph(pg_session, g2, primary_class=BF.Work)

    with pg_session() as session:
        agent = (
            session.query(OtherResource).where(OtherResource.uri == agent_uri).first()
        )
        assert json.dumps(agent.data, sort_keys=True) == before  # unchanged


def test_other_resource_as_primary_updates(pg_session):
    """
    With primary_class=OtherResource, the Other Resource is authoritative and IS
    updated, while a Work it references stays create-if-absent.
    """
    agent_uri = _new_uri("other_resources")
    w = _new_uri("works")
    g1 = Graph()
    g1.add((URIRef(w), RDF.type, BF.Work))
    g1.add((URIRef(w), BF.contribution, URIRef(agent_uri)))
    g1.add((URIRef(agent_uri), RDF.type, BF.Agent))
    g1.add((URIRef(agent_uri), RDFS.label, Literal("Old Name")))
    save_graph(pg_session, g1, primary_class=BF.Work)

    g2 = Graph()
    g2.add((URIRef(w), RDF.type, BF.Work))
    g2.add((URIRef(w), BF.contribution, URIRef(agent_uri)))
    g2.add((URIRef(agent_uri), RDF.type, BF.Agent))
    g2.add((URIRef(agent_uri), RDFS.label, Literal("New Name")))
    save_graph(pg_session, g2, primary_class=OtherResource)

    with pg_session() as session:
        agent = (
            session.query(OtherResource).where(OtherResource.uri == agent_uri).first()
        )
        assert "New Name" in json.dumps(agent.data)  # authoritatively updated


def test_primary_class_none_still_overwrites(pg_session):
    """
    The bulk-loader path (no primary_class) keeps the original upsert behavior:
    every subject is authoritative and is overwritten.
    """
    inst_uri = _new_uri("instances")
    save_graph(
        pg_session,
        load_jsonld(
            {
                "@id": inst_uri,
                "@type": BF.Instance,
                "title": {"mainTitle": "First", "@type": "Title"},
            }
        ),
    )
    save_graph(
        pg_session,
        load_jsonld(
            {
                "@id": inst_uri,
                "@type": BF.Instance,
                "title": {"mainTitle": "Second", "@type": "Title"},
            }
        ),
    )
    with pg_session() as session:
        inst = session.query(Instance).where(Instance.uri == inst_uri).first()
        assert inst.data["title"]["mainTitle"] == "Second"


def test_update_other_resources_flag(pg_session):
    """
    On the bulk path (primary_class=None), update_other_resources=False treats
    Other Resources as references (create-if-absent, never overwrite) so batch
    loads don't needlessly re-describe shared Other Resources; the default True
    still updates them.
    """
    agent_uri = _new_uri("other_resources")

    def save_work_with_agent(label: str, **kwargs):
        g = Graph()
        w = _new_uri("works")
        g.add((URIRef(w), RDF.type, BF.Work))
        g.add((URIRef(w), BF.contribution, URIRef(agent_uri)))
        g.add((URIRef(agent_uri), RDF.type, BF.Agent))
        g.add((URIRef(agent_uri), RDFS.label, Literal(label)))
        save_graph(pg_session, g, **kwargs)
        return w

    save_work_with_agent("Original")
    with pg_session() as session:
        before = json.dumps(
            session.query(OtherResource)
            .where(OtherResource.uri == agent_uri)
            .first()
            .data,
            sort_keys=True,
        )

    # bulk save with update_other_resources=False must not re-describe the Agent
    w2 = save_work_with_agent("Changed", update_other_resources=False)
    with pg_session() as session:
        agent = (
            session.query(OtherResource).where(OtherResource.uri == agent_uri).first()
        )
        assert json.dumps(agent.data, sort_keys=True) == before  # unchanged
        # but the new Work is still linked to the existing Agent
        work2 = session.query(Work).where(Work.uri == w2).first()
        link = (
            session.query(BibframeOtherResources)
            .where(BibframeOtherResources.bibframe_resource_id == work2.id)
            .first()
        )
        assert link is not None and link.other_resource.uri == agent_uri

    # default (update_other_resources=True) still updates the Agent
    save_work_with_agent("Updated")
    with pg_session() as session:
        agent = (
            session.query(OtherResource).where(OtherResource.uri == agent_uri).first()
        )
        assert "Updated" in json.dumps(agent.data)


# --- stubbed resources -------------------------------------------------------
#
# A record describes a few resources and only points at the others. We keep a
# stub of those, marked bf:status mstatus/incmp on the AdminMetadata we generate,
# until a record that really describes them is ingested. See _arrived_as_stub.


def _ext_uri(kind: str) -> str:
    """A non-Bluecore URI, the way resources arrive in an ingested record."""
    return f"http://id.loc.gov/resources/{kind}/{uuid.uuid4()}"


def _record(described: str, title: str, stub: str | None = None) -> Graph:
    """
    Build a record the way an ingest sees one: a Work the record describes (it
    carries AdminMetadata of its own) and, optionally, a Work it only points at
    with no AdminMetadata -- the stub.
    """
    g = Graph()
    work = URIRef(described)
    g.add((work, RDF.type, BF.Work))
    title_node = BNode()
    g.add((work, BF.title, title_node))
    g.add((title_node, RDF.type, BF.Title))
    g.add((title_node, BF.mainTitle, Literal(title)))
    admin_metadata = BNode()
    g.add((work, BF.adminMetadata, admin_metadata))
    g.add((admin_metadata, RDF.type, BF.AdminMetadata))
    g.add((admin_metadata, BF.status, URIRef("http://id.loc.gov/vocabulary/mstatus/n")))
    if stub is not None:
        stub_work = URIRef(stub)
        g.add((work, BF.relatedTo, stub_work))
        g.add((stub_work, RDF.type, BF.Work))
        g.add((stub_work, RDFS.label, Literal("stub label")))
    return g


def _minted(graph: Graph, source_uri: str) -> str:
    """The Bluecore URI a save minted for an original URI, read back from the graph."""
    for admin_metadata in graph.subjects(BF.derivedFrom, URIRef(source_uri)):
        subject = graph.value(predicate=BF.adminMetadata, object=admin_metadata)
        if subject is not None:
            return str(subject)
    raise AssertionError(f"no bluecore uri was minted for {source_uri}")


def _statuses(data: dict) -> list[str]:
    """Every bf:status @id recorded in a resource's AdminMetadata nodes."""
    admin_metadata = data.get("adminMetadata", [])
    if isinstance(admin_metadata, dict):
        admin_metadata = [admin_metadata]
    found = []
    for block in admin_metadata:
        if not isinstance(block, dict):
            continue
        status = block.get("status")
        for item in status if isinstance(status, list) else [status]:
            if isinstance(item, dict) and "@id" in item:
                found.append(item["@id"])
    return found


def _linked_others(session, resource) -> list[str]:
    return [
        link.other_resource.uri
        for link in session.query(BibframeOtherResources).where(
            BibframeOtherResources.bibframe_resource_id == resource.id
        )
    ]


def _fetch(session, uri: str) -> Work:
    return session.query(Work).where(Work.uri == uri).first()


def test_stub_is_marked_incomplete(pg_session):
    """
    The resource a record describes gets the normal status; the one it only
    points at is recorded as incomplete so a placeholder can be told from a
    real description.
    """
    described, stub = _ext_uri("works"), _ext_uri("works")
    out = save_graph(pg_session, _record(described, "Described Work", stub=stub))

    with pg_session() as session:
        full = _fetch(session, _minted(out, described))
        stubbed = _fetch(session, _minted(out, stub))

        assert str(bluecore_graph.DEFAULT_STATUS) in _statuses(full.data)
        assert str(bluecore_graph.STUB_STATUS) not in _statuses(full.data)

        assert str(bluecore_graph.STUB_STATUS) in _statuses(stubbed.data)
        assert str(bluecore_graph.DEFAULT_STATUS) not in _statuses(stubbed.data)


def test_stub_status_resolves_as_an_other_resource(pg_session):
    """
    Nothing in an incoming record describes mstatus/incmp, and _extract_others
    only promotes uris that appear as subjects, so we describe it ourselves --
    otherwise the marker would be an unresolvable uri with nothing to display.
    """
    described, stub = _ext_uri("works"), _ext_uri("works")
    out = save_graph(pg_session, _record(described, "Described Work", stub=stub))

    with pg_session() as session:
        marker = (
            session.query(OtherResource)
            .where(OtherResource.uri == str(bluecore_graph.STUB_STATUS))
            .first()
        )
        assert marker is not None
        assert "incomplete" in json.dumps(marker.data)

        stubbed = _fetch(session, _minted(out, stub))
        assert str(bluecore_graph.STUB_STATUS) in _linked_others(session, stubbed)


def test_full_description_replaces_a_stub(pg_session):
    """
    A stub is only a placeholder, so a record that really describes the resource
    always replaces it.
    """
    described, stub = _ext_uri("works"), _ext_uri("works")
    first = save_graph(pg_session, _record(described, "Described Work", stub=stub))
    stub_uri = _minted(first, stub)

    # a later record describes what was only a stub before, and resolves to the
    # same Bluecore resource through its derivedFrom assertion
    second = save_graph(pg_session, _record(stub, "Now Fully Described"))
    assert _minted(second, stub) == stub_uri

    with pg_session() as session:
        work = _fetch(session, stub_uri)
        assert "Now Fully Described" in json.dumps(work.data)
        assert str(bluecore_graph.STUB_STATUS) not in _statuses(work.data)


def test_stub_does_not_clobber_a_full_description(pg_session):
    """
    Once we hold a real description, a later record that only points at the
    resource must not overwrite it with its stub.
    """
    source = _ext_uri("works")
    out = save_graph(pg_session, _record(source, "Full Description"))
    uri = _minted(out, source)

    with pg_session() as session:
        before = json.dumps(_fetch(session, uri).data, sort_keys=True)

    # another record arrives that only points at it
    save_graph(pg_session, _record(_ext_uri("works"), "Other Record", stub=source))

    with pg_session() as session:
        work = _fetch(session, uri)
        assert json.dumps(work.data, sort_keys=True) == before
        assert "Full Description" in json.dumps(work.data)
        assert "stub label" not in json.dumps(work.data)
        assert str(bluecore_graph.STUB_STATUS) not in _statuses(work.data)


def test_stub_keeps_the_links_its_description_gave_it(pg_session):
    """
    A stub isn't a description of the resource, so it must not strip the Other
    Resource links the real description established.
    """
    source = _ext_uri("works")
    agent = f"http://id.loc.gov/rwo/agents/{uuid.uuid4()}"
    g = _record(source, "Described With Agent")
    g.add((URIRef(source), BF.contributor, URIRef(agent)))
    g.add((URIRef(agent), RDF.type, BF.Agent))
    g.add((URIRef(agent), RDFS.label, Literal("Some Agent")))
    out = save_graph(pg_session, g)
    uri = _minted(out, source)

    with pg_session() as session:
        assert agent in _linked_others(session, _fetch(session, uri))

    # a record that only stubs it must leave those links alone
    save_graph(pg_session, _record(_ext_uri("works"), "Other Record", stub=source))

    with pg_session() as session:
        assert agent in _linked_others(session, _fetch(session, uri))


def test_reingesting_a_record_does_not_overwrite_edits(pg_session):
    """
    Once we hold a full description, re-ingesting the very same record must not
    undo whatever has been edited since.
    """
    source = _ext_uri("works")
    out = save_graph(pg_session, _record(source, "REINGESTED 1"))
    uri = _minted(out, source)

    # a cataloger edits it through the API, which round-trips the stored jsonld
    with pg_session() as session:
        edited = dict(_fetch(session, uri).data)
    edited["title"] = {"@type": "Title", "mainTitle": "Cataloger Edit"}
    save_graph(pg_session, load_jsonld(edited), primary_class=BF.Work)

    with pg_session() as session:
        assert "Cataloger Edit" in json.dumps(_fetch(session, uri).data)

    # ingesting the same record again must leave the edit in place
    save_graph(pg_session, _record(source, "REINGESTED 1"))

    with pg_session() as session:
        work = _fetch(session, uri)
        assert "Cataloger Edit" in json.dumps(work.data)
        assert "REINGESTED 1" not in json.dumps(work.data)


def test_api_save_clears_the_stub_marker(pg_session):
    """
    An explicit write from the API is a real description, so the resource isn't
    a stub any more.
    """
    described, stub = _ext_uri("works"), _ext_uri("works")
    out = save_graph(pg_session, _record(described, "Described Work", stub=stub))
    uri = _minted(out, stub)

    with pg_session() as session:
        assert str(bluecore_graph.STUB_STATUS) in _statuses(_fetch(session, uri).data)
        edited = dict(_fetch(session, uri).data)

    edited["title"] = {"@type": "Title", "mainTitle": "Now Described By Hand"}
    save_graph(pg_session, load_jsonld(edited), primary_class=BF.Work)

    with pg_session() as session:
        work = _fetch(session, uri)
        assert "Now Described By Hand" in json.dumps(work.data)
        assert str(bluecore_graph.STUB_STATUS) not in _statuses(work.data)
        assert str(bluecore_graph.DEFAULT_STATUS) in _statuses(work.data)


def test_clearing_the_stub_marker_leaves_a_contents_note_alone(pg_session):
    """
    mstatus/incmp is also what marc2bibframe2 emits from MARC 505 ind1=1, onto a
    bf:TableOfContents, to mean the contents note lists only some of the parts.
    Clearing our stub marker must not touch it -- the term only means "this is a
    stub" on AdminMetadata.
    """
    uri = _new_uri("works")
    g = Graph()
    work = URIRef(uri)
    g.add((work, RDF.type, BF.Work))
    contents = BNode()
    g.add((work, BF.tableOfContents, contents))
    g.add((contents, RDF.type, BF.TableOfContents))
    g.add(
        (contents, RDFS.label, Literal("v. 1. Beginnings -- v. 2. The middle years."))
    )
    g.add((contents, BF.status, bluecore_graph.STUB_STATUS))

    save_graph(pg_session, g, primary_class=BF.Work)

    with pg_session() as session:
        data = json.dumps(_fetch(session, uri).data)
        assert str(bluecore_graph.STUB_STATUS) in data  # the note survives
        assert str(bluecore_graph.DEFAULT_STATUS) not in data  # not rewritten


# ---------------------------------------------------------------------------
# relation stubs: nested mentions must not become records
# ---------------------------------------------------------------------------


def _relation_work(uri: str) -> Graph:
    """
    A Work that mentions another Work (with its own Instance) as the other end
    of a bf:relation, the way LC describes the print version of a serial.
    """
    g = Graph()
    work = URIRef(uri)
    g.add((work, RDF.type, BF.Work))
    g.add((work, BF.adminMetadata, BNode()))

    relation, other_work, other_instance = BNode(), BNode(), BNode()
    g.add((work, BF.relation, relation))
    g.add((relation, RDF.type, BF.Relation))
    g.add((relation, BF.associatedResource, other_work))
    g.add((other_work, RDF.type, BF.Work))
    g.add((other_work, RDFS.label, Literal("AI and society")))
    g.add((other_work, BF.hasInstance, other_instance))
    g.add((other_instance, RDF.type, BF.Instance))
    return g


def test_relation_stub_is_not_promoted(pg_session):
    """
    The nameless Work and Instance behind a bf:relation are mentions, not
    records, so they are never extracted or saved.
    """
    graph = _relation_work("http://id.loc.gov/resources/works/20133027")
    bluecore_graph_ = BluecoreGraph(graph)

    assert len(bluecore_graph_.works()) == 1
    assert len(bluecore_graph_.instances()) == 0


def test_relation_stub_does_not_accumulate(pg_session):
    """
    A nameless mention has no uri to recognize it by next time, so promoting it
    used to add a fresh Work and Instance on every load. Reloading must not
    change the counts.
    """
    _remove_fixtures(pg_session)
    uri = "http://id.loc.gov/resources/works/20133027"

    for _ in range(3):
        save_graph(pg_session, _relation_work(uri))

    with pg_session() as session:
        assert session.query(Work).count() == 1
        assert session.query(Instance).count() == 0


def test_new_nested_resource_is_still_promoted(pg_session):
    """
    An untyped, uri-less Instance nested under a Work is a new resource being
    written (the API's POST /works/ does this), not a mention, so it is still
    given a type and saved.
    """
    cbd_jsonld = {
        "@context": CONTEXT,
        "@type": BF.Work,
        "title": {"mainTitle": "Gravity's Rainbow"},
        "hasInstance": {"provisionActivity": {"date": "1973"}},
    }
    bluecore_graph_ = BluecoreGraph(load_jsonld(cbd_jsonld))

    assert len(bluecore_graph_.works()) == 1
    assert len(bluecore_graph_.instances()) == 1
