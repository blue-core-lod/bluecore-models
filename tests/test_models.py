import pathlib
from datetime import datetime, UTC

import pytest
import rdflib

from pytest_mock_resources import create_sqlite_fixture, Rows

from sqlalchemy.orm import sessionmaker

from bluecore_models.models import (
    Base,
    BibframeClass,
    ResourceBibframeClass,
    Instance,
    OtherResource,
    Version,
    Work,
    BibframeOtherResources,
)

from bluecore_models.utils.graph import BF, init_graph


def create_test_rows():
    time_now = datetime.now(UTC)  # Use for Instance and Work for now

    return Rows(
        # BibframeClass
        BibframeClass(
            id=1,
            name="Instance",
            uri="http://id.loc.gov/ontologies/bibframe/Instance",
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        ),
        BibframeClass(
            id=2,
            name="Work",
            uri="http://id.loc.gov/ontologies/bibframe/Work",
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        ),
        # Work
        Work(
            id=1,
            uri="https://bluecore.info/works/23db8603-1932-4c3f-968c-ae584ef1b4bb",
            created_at=time_now,
            updated_at=time_now,
            data=pathlib.Path("tests/blue-core-work.jsonld").read_text(),
            type="works",
        ),
        # Instance
        Instance(
            id=2,
            uri="https://bluecore.info/instances/75d831b9-e0d6-40f0-abb3-e9130622eb8a",
            created_at=time_now,
            updated_at=time_now,
            data=pathlib.Path("tests/blue-core-instance.jsonld").read_text(),
            type="instances",
            work_id=1,
        ),
        # OtherResource
        OtherResource(
            id=3,
            uri="https://bluecore.info/other-resource/sample",
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
            data='{"description": "Sample Other Resource"}',
            type="other_resources",
            is_profile=False,
        ),
        # BibframeOtherResources
        BibframeOtherResources(
            id=1,
            other_resource_id=3,
            bibframe_resource_id=1,
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        ),
    )


engine = create_sqlite_fixture(create_test_rows())


@pytest.fixture()
def pg_session(engine):
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)


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


def test_versions(pg_session):
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


def test_bibframe_other_resources(pg_session):
    with pg_session() as session:
        bibframe_other_resource = (
            session.query(BibframeOtherResources)
            .where(BibframeOtherResources.id == 1)
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
        instance_graph = init_graph()
        instance_graph.parse(data=instance.data, format="json-ld")
        instance_uri = rdflib.URIRef(instance.uri)
        instance_graph.add((instance_uri, rdflib.RDF.type, BF.Electronic))
        instance.data = instance_graph.serialize(format="json-ld")
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
        work_graph = init_graph()
        work_graph.parse(data=work.data, format="json-ld")
        work_uri = rdflib.URIRef(work.uri)
        work_graph.remove((work_uri, rdflib.RDF.type, BF.Text))
        work.data = work_graph.serialize(format="json-ld")
        session.add(work)
        session.commit()
        # Assert new version was created, classes & timestamps aligned
        assert len(work.versions) == 2
        assert len(work.classes) == 2
        latest_version = max(work.versions, key=lambda version: version.id)
        assert latest_version.created_at == work.updated_at
