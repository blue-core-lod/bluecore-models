import json
import pathlib

from datetime import datetime, UTC
from uuid import UUID

import pytest

from pytest_mock_resources import create_sqlite_fixture, Rows

from sqlalchemy.orm import sessionmaker

from bluecore_models.models import (
    Base,
    BibframeClass,
    ResourceBibframeClass,  # noqa
    Instance,
    OtherResource,
    Version,  # noqa
    Work,
    BibframeOtherResources,
)


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
            data=json.load(pathlib.Path("tests/blue-core-work.jsonld").open()),
            uuid=UUID("629e9a53-7d5b-439c-a227-5efdbeb102e4"),
            type="works",
        ),
        # Instance
        Instance(
            id=2,
            uri="https://bluecore.info/instances/75d831b9-e0d6-40f0-abb3-e9130622eb8a",
            created_at=time_now,
            updated_at=time_now,
            data=json.load(pathlib.Path("tests/blue-core-instance.jsonld").open()),
            type="instances",
            uuid=UUID("9bd652f3-9e92-4aee-ba6c-cd33dcb43ffa"),
            work_id=1,
        ),
        # OtherResource
        OtherResource(
            id=3,
            uri="https://bluecore.info/other-resource/sample",
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
            data={"description": "Sample Other Resource"},
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
