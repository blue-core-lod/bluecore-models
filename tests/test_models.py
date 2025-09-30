import json
from pathlib import Path
from uuid import UUID, uuid1

import pytest  # noqa
import rdflib


from bluecore_models.models import (
    Base,  # noqa
    BibframeClass,
    ResourceBibframeClass,
    Instance,
    OtherResource,
    Version,
    Work,
    BibframeOtherResources,
)

from bluecore_models.utils.graph import BF, load_jsonld


def test_bibframe_class(pg_session):
    with pg_session() as session:
        bf_instance = (
            session.query(BibframeClass).where(BibframeClass.name == "Instance").first()
        )
        assert bf_instance is not None
        assert bf_instance.uri.startswith(
            "http://id.loc.gov/ontologies/bibframe/Instance"
        )
        assert bf_instance.created_at
        assert bf_instance.updated_at


def test_resource_bibframe_class(pg_session):
    with pg_session() as session:
        resource_bf_class = (
            session.query(ResourceBibframeClass)
            .where(ResourceBibframeClass.id == 1)
            .first()
        )
        assert resource_bf_class.resource.uri.startswith("https://bluecore.info/works")
        assert resource_bf_class.bf_class.name == "Monograph"


def test_instance(pg_session):
    with pg_session() as session:
        instance = session.query(Instance).where(Instance.id == 2).first()
        version = session.query(Version).filter_by(resource_id=instance.id).first()
        assert instance.created_at == instance.updated_at
        assert version.created_at == instance.updated_at
        assert instance.uri.startswith("https://bluecore.info/instance")
        assert instance.uuid == UUID("9bd652f3-9e92-4aee-ba6c-cd33dcb43ffa")
        assert instance.data
        assert instance.created_at
        assert instance.updated_at
        assert instance.work is not None
        assert instance.work.uri.startswith("https://bluecore.info/work")
        assert len(instance.versions) == 1
        assert len(instance.classes) == 1


def test_work(pg_session):
    with pg_session() as session:
        work = session.query(Work).where(Work.id == 1).first()
        version = session.query(Version).filter_by(resource_id=work.id).first()
        assert work.created_at == work.updated_at
        assert version.created_at == work.updated_at
        assert work.uri.startswith("https://bluecore.info/work")
        assert work.uuid == UUID("629e9a53-7d5b-439c-a227-5efdbeb102e4")
        assert work.data
        assert work.created_at
        assert work.updated_at
        assert len(work.instances) > 0
        assert len(work.versions) == 1
        assert len(work.classes) == 3


def test_other_resource(pg_session):
    with pg_session() as session:
        other_resource = (
            session.query(OtherResource).where(OtherResource.id == 3).first()
        )
        assert other_resource.uri.startswith("https://bluecore.info/other-resource")
        assert other_resource.data
        assert other_resource.created_at
        assert other_resource.updated_at
        assert other_resource.is_profile is False


def test_versions(pg_session, user_context):
    with pg_session() as session:
        version = session.query(Version).where(Version.id == 1).first()
        work = session.query(Work).where(Work.id == 1).first()
        assert version.resource is not None
        assert version.resource == work
        assert version.data
        assert version.created_at
        version2 = session.query(Version).where(Version.id == 2).first()
        instance = session.query(Instance).where(Instance.id == 2).first()
        assert version2.resource == instance

        with user_context("55875c9a-c67c-4c60-9c91-bc260f71f92a"):
            work.data = {**work.data, "_test_bump": uuid1().hex}
            session.add(work)
            session.commit()
        latest_version = (
            session.query(Version)
            .filter(Version.resource_id == work.id)
            .order_by(Version.id.desc())
            .first()
        )
        assert latest_version is not None
        assert latest_version.keycloak_user_id == "55875c9a-c67c-4c60-9c91-bc260f71f92a"


def test_bibframe_other_resources(pg_session):
    with pg_session() as session:
        bibframe_other_resource = (
            session.query(BibframeOtherResources)
            .where(BibframeOtherResources.id == 4)
            .first()
        )
        assert bibframe_other_resource.other_resource is not None
        assert bibframe_other_resource.bibframe_resource is not None
        assert bibframe_other_resource.created_at
        assert bibframe_other_resource.updated_at


def test_updated_instance(pg_session):
    with pg_session() as session:
        instance = session.query(Instance).where(Instance.id == 2).first()
        # Before updates: assert instance has 1 version and 1 class
        assert len(instance.versions) == 1
        assert len(instance.classes) == 1
        # Assert instance 'updated_at' & version 'created_at' are the same
        version_before_update = instance.versions[0]
        assert version_before_update.created_at == instance.updated_at
        # Update the instance
        instance_graph = load_jsonld(instance.data)
        instance_uri = rdflib.URIRef(instance.uri)
        instance_graph.add((instance_uri, rdflib.RDF.type, BF.Electronic))
        instance.data = json.loads(instance_graph.serialize(format="json-ld"))
        session.add(instance)
        session.commit()
        # Assert new version was created, classes & timestamps aligned
        assert len(instance.versions) == 2
        assert len(instance.classes) == 2
        latest_version = max(instance.versions, key=lambda version: version.id)
        assert latest_version.created_at == instance.updated_at


def test_updated_work(pg_session):
    with pg_session() as session:
        work = session.query(Work).where(Work.id == 1).first()
        # Before updates: assert work has 1 version and 3 classes
        assert len(work.versions) == 1
        assert len(work.classes) == 3
        # Assert work 'updated_at' & version 'created_at' are the same
        version_before_update = work.versions[0]
        assert version_before_update.created_at == work.updated_at
        # Update the work
        work_graph = load_jsonld(work.data)

        work_uri = rdflib.URIRef(work.uri)
        work_graph.remove((work_uri, rdflib.RDF.type, BF.Text))
        work.data = json.loads(work_graph.serialize(format="json-ld"))
        session.add(work)
        session.commit()
        # Assert new version was created, classes & timestamps aligned
        assert len(work.versions) == 2
        assert len(work.classes) == 2
        latest_version = max(work.versions, key=lambda version: version.id)
        assert latest_version.created_at == work.updated_at


def test_work_with_other_resources(pg_session):
    """
    Tests replicates logic in the resource_loader DAG when ingesting a
    CBD json-ld file.
    """
    with pg_session() as session:
        new_work = Work(
            id=4,
            uri="https://bcld.info/works/4d579ca1-ab41-443b-a225-a35bc6a54281",
            data={
                "@id": "https://bcld.info/works/4d579ca1-ab41-443b-a225-a35bc6a54281",
                "@context": {
                    "bflc": "http://id.loc.gov/ontologies/bflc/",
                    "mads": "http://www.loc.gov/mads/rdf/v1#",
                    "@vocab": "http://id.loc.gov/ontologies/bibframe/",
                },
                "title": [{"@type": "Title", "mainTitle": "Hannah Arendt and the law"}],
                "language": {"@id": "http://id.loc.gov/vocabulary/languages/eng"},
                "contribution": [
                    {
                        "role": {"@id": "http://id.loc.gov/vocabulary/relators/ctb"},
                        "@type": "Contribution",
                        "agent": {"@id": "http://id.loc.gov/rwo/agents/no2012077908"},
                    }
                ],
            },
        )
        session.add(new_work)

        # it's important to commit the work here or else the test of version counts below will always succeed.
        session.commit()

        language = OtherResource(
            id=5,
            uri="http://id.loc.gov/vocabulary/languages/eng",
            data=[
                {
                    "@id": "http://id.loc.gov/vocabulary/languages/eng",
                    "@type": ["http://id.loc.gov/ontologies/bibframe/Language"],
                    "http://id.loc.gov/ontologies/bibframe/code": [
                        {
                            "@type": "http://www.w3.org/2001/XMLSchema#string",
                            "@value": "eng",
                        }
                    ],
                    "http://www.w3.org/2000/01/rdf-schema#label": [
                        {"@value": "English", "@language": "en"}
                    ],
                }
            ],
        )
        session.add(language)
        language_resource = BibframeOtherResources(
            other_resource=language, bibframe_resource=new_work
        )
        session.add(
            language_resource
        )  # Before this would add an duplicate Work version
        ctb_relator = OtherResource(
            id=6,
            uri="http://id.loc.gov/vocabulary/relators/ctb",
            data={
                "@id": "http://id.loc.gov/vocabulary/relators/ctb",
                "@type": ["http://id.loc.gov/ontologies/bibframe/Role"],
                "http://id.loc.gov/ontologies/bibframe/code": [{"@value": "ctb"}],
            },
        )
        session.add(ctb_relator)
        ctb_relator_resource = BibframeOtherResources(
            other_resource=ctb_relator, bibframe_resource=new_work
        )
        session.add(ctb_relator_resource)
        person = OtherResource(
            id=7,
            uri="http://id.loc.gov/rwo/agents/no2012077908",
            data={
                "@id": "http://id.loc.gov/rwo/agents/no2012077908",
                "@type": [
                    "http://id.loc.gov/ontologies/bibframe/Agent",
                    "http://id.loc.gov/ontologies/bibframe/Person",
                ],
                "http://id.loc.gov/ontologies/bflc/marcKey": [
                    {"@value": "1001 $aMcCorkindale, Christopher"}
                ],
                "http://www.w3.org/2000/01/rdf-schema#label": [
                    {"@value": "McCorkindale, Christopher"}
                ],
            },
        )
        session.add(person)
        person_resource = BibframeOtherResources(
            other_resource=person, bibframe_resource=new_work
        )
        session.add(person_resource)
        session.commit()

        assert len(new_work.versions) == 1, "Ensure only 1 version for work"


def test_work_jsonld_framing():
    work_json = json.load(Path("tests/blue-core-work.jsonld").open())
    work = Work(
        uri="https://bluecore.info/works/23db8603-1932-4c3f-968c-ae584ef1b4bb",
        data=work_json,
    )
    assert work_json != work.data, "setting Work.data frames json-ld"
    assert (
        work.data["@id"]
        == "https://bluecore.info/works/23db8603-1932-4c3f-968c-ae584ef1b4bb"
    ), "framing set @uri"
    assert work.data["@type"] == ["Monograph", "Text", "Work"], "framing set @type"
    assert (
        work.data["title"][0]["mainTitle"][0]
        == "Chaesaeng en\u014fji kumae chedo mit chiw\u014fn ch\u014fngch'aek kaes\u014fn kwaje"
    )
    assert isinstance(work.data["@context"], dict), "framing added @context"


def test_instance_jsonld_framing():
    instance_json = json.load(Path("tests/blue-core-instance.jsonld").open())
    instance = Instance(
        uri="https://bluecore.info/instances/75d831b9-e0d6-40f0-abb3-e9130622eb8a",
        data=instance_json,
    )
    assert instance_json != instance.data, "setting Instance.data frames json-ld"
    assert (
        instance.data["@id"]
        == "https://bluecore.info/instances/75d831b9-e0d6-40f0-abb3-e9130622eb8a"
    ), "framing set @uri"
    assert instance.data["@type"] == "Instance"
    assert (
        instance.data["title"]["mainTitle"][0]
        == "Chaesaeng en\u014fji kumae chedo mit chiw\u014fn ch\u014fngch'aek kaes\u014fn kwaje"
    )
    assert isinstance(instance.data["@context"], dict), "framing added @context"


def test_other_resource_jsonld_framing():
    other_json = [{"foo": "bar"}]
    other = OtherResource(
        uri="https://bluecore.info/instances/75d831b9-e0d6-40f0-abb3-e9130622eb8a",
        data=other_json,
    )
    assert other_json == other.data, "setting OtherResource.data DOES NOT frame json-ld"


def test_property_order():
    """
    The JSON-LD stored in the data property is automatically "framed" when it is
    set. But framing requires that a uri be defined already. These tests ensure
    that a descriptive exception is thrown when the ordering is incorrect.
    """
    with pytest.raises(ValueError) as e:
        Instance(
            data={"foo": "bar"},
            uri="https://bluecore.info/instances/75d831b9-e0d6-40f0-abb3-e9130622eb8a",
        )
    assert (
        str(e.value)
        == "For automatic jsonld framing to work you must ensure the uri property is set before the data property, even when constructing an object."
    )

    with pytest.raises(ValueError) as e:
        Work(
            data={"foo": "bar"},
            uri="https://bluecore.info/instances/75d831b9-e0d6-40f0-abb3-e9130622eb8a",
        )
    assert (
        str(e.value)
        == "For automatic jsonld framing to work you must ensure the uri property is set before the data property, even when constructing an object."
    )


def test_works_search(pg_session):
    with pg_session() as session:
        # pg_sessions loads a work with subject "Renewable energy sources--Government policy--Korea (South)"
        w = (
            session.query(Work).where(
                Work.data_vector.match("Renewable energy sources")
            )
        ).first()
        assert w, "known item search"

        w = (
            session.query(Work).where(Work.data_vector.match("Renewable energy source"))
        ).first()
        assert w, "stemming 'sources' to 'source'"

        assert (
            len(
                session.query(Work)
                .where(Work.data_vector.match("Nope nada never"))
                .all()
            )
            == 0
        ), "missing text"

        # TODO: should we be using websearch_to_tsquery() instead of the default
        # which SQLAlchemy performs here which I believe is plainto_tsquery()?
        # I think this would allow us to support boolean searches and phrase
        # searches?
        #
        # see: https://www.postgresql.org/docs/current/textsearch-controls.html


def test_data_vector_update(pg_session):
    """
    Ensure that updating the Work.data causes the Work.data_vector to get
    updated.
    """
    with pg_session() as session:
        # a random string
        random_str = uuid1().hex

        # ensure that the random string isn't in the db
        results = (session.query(Work).where(Work.data_vector.match(random_str))).all()
        assert len(results) == 0, "random string not present"

        # add the random string to the Work JSON-LD
        work = session.query(Work).where(Work.id == 1).first()
        work.data = {**work.data, "title": random_str}
        session.add(work)
        session.commit()

        # ensure that now we can find the random string
        results = (session.query(Work).where(Work.data_vector.match(random_str))).all()
        assert len(results) == 1
