import pathlib
from datetime import datetime, UTC

import pytest
from pytest_mock_resources import create_sqlite_fixture, Rows

from sqlalchemy.orm import sessionmaker

from bluecore.models import (
    Base,
    BibframeClass,
    ResourceBibframeClass,
    Instance,
    OtherResource,
    Version,
    Work,
    BibframeOtherResources,
)


def create_test_rows():
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
            uri="https://bluecore.info/work/e0d6-40f0-abb3-e9130622eb8a",
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
            data=pathlib.Path("tests/blue-core-work.jsonld").read_text(),
            type="works",
        ),
        # Instance
        Instance(
            id=2,
            uri="https://bluecore.info/instance/75d831b9-e0d6-40f0-abb3-e9130622eb8a",
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
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
        # ResourceBibframeClass
        ResourceBibframeClass(
            id=1,
            bf_class_id=1,
            resource_id=2,
        ),
        ResourceBibframeClass(
            id=2,
            bf_class_id=2,
            resource_id=1,
        ),
        # Version
        Version(
            id=1,
            resource_id=1,
            data=pathlib.Path("tests/blue-core-work.jsonld").read_text(),
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        ),
        Version(
            id=2,
            resource_id=2,
            data=pathlib.Path("tests/blue-core-instance.jsonld").read_text(),
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
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
        assert resource_bf_class.resource.uri.startswith(
            "https://bluecore.info/instance"
        )
        assert resource_bf_class.bf_class.name == "Instance"


def test_instance(pg_session):
    with pg_session() as session:
        instance = session.query(Instance).where(Instance.id == 2).first()
        assert instance.uri.startswith("https://bluecore.info/instance")
        assert instance.data
        assert instance.created_at
        assert instance.updated_at
        assert instance.work is not None
        assert instance.work.uri.startswith("https://bluecore.info/work")


def test_work(pg_session):
    with pg_session() as session:
        work = session.query(Work).where(Work.id == 1).first()
        assert work.uri.startswith("https://bluecore.info/work")
        assert work.data
        assert work.created_at
        assert work.updated_at
        assert len(work.instances) > 0


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
        assert version.updated_at
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
