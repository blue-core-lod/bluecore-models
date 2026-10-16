"""The 20261004 migration, run for real: records that already exist get their
identifiers filled in, and downgrading removes everything it added."""

import json
import os

from alembic import command
from alembic.config import Config
from pytest_mock_resources import create_postgres_fixture
from sqlalchemy import text

BEFORE = "20261001"
AFTER = "20261004"

# A clean database, built by the migrations themselves like production
migration_engine = create_postgres_fixture()


# Points alembic at the throwaway test database
def alembic_config(engine) -> Config:
    os.environ["DATABASE_URL"] = engine.url.render_as_string(hide_password=False)
    return Config("alembic.ini")


def test_existing_records_are_filled_in(migration_engine):
    config = alembic_config(migration_engine)
    command.upgrade(config, BEFORE)

    # A record ingested before the migration, written without the ORM
    data = {"identifiedBy": [{"@type": "Lccn", "rdf:value": "sn 85009985 "}]}
    with migration_engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO resource_base (type, data, uri, created_at, updated_at)"
                " VALUES ('works', :data, 'https://bcld.info/works/before', now(), now())"
            ),
            {"data": json.dumps(data)},
        )

    command.upgrade(config, AFTER)

    with migration_engine.begin() as connection:
        identifiers = connection.execute(
            text("SELECT identifiers FROM resource_base")
        ).scalar()
        assert identifiers == ["lccn:sn85009985", "sn85009985"]

    command.downgrade(config, BEFORE)

    with migration_engine.begin() as connection:
        assert (
            connection.execute(
                text(
                    "SELECT count(*) FROM information_schema.columns"
                    " WHERE table_name = 'resource_base' AND column_name = 'identifiers'"
                )
            ).scalar()
            == 0
        )
        assert (
            connection.execute(
                text("SELECT to_regproc('public.bluecore_identifiers')")
            ).scalar()
            is None
        )
