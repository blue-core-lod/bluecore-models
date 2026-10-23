"""The 20261023 migration, run for real: upgrading adds the resource_id index
on resource_bibframe_classes, and downgrading removes it."""

import os

from alembic import command
from alembic.config import Config
from pytest_mock_resources import create_postgres_fixture
from sqlalchemy import text

BEFORE = "20261004"
AFTER = "20261023"
INDEX_NAME = "index_resource_bibframe_classes_on_resource_id"

# A clean database, built by the migrations themselves like production
migration_engine = create_postgres_fixture()


# Points alembic at the throwaway test database
def alembic_config(engine) -> Config:
    os.environ["DATABASE_URL"] = engine.url.render_as_string(hide_password=False)
    return Config("alembic.ini")


def _index_exists(engine) -> bool:
    with engine.begin() as connection:
        return (
            connection.execute(
                text("SELECT 1 FROM pg_indexes WHERE indexname = :name"),
                {"name": INDEX_NAME},
            ).scalar()
            is not None
        )


def test_upgrade_adds_and_downgrade_removes_the_index(migration_engine):
    config = alembic_config(migration_engine)
    command.upgrade(config, BEFORE)
    assert not _index_exists(migration_engine)

    command.upgrade(config, AFTER)
    assert _index_exists(migration_engine)

    command.downgrade(config, BEFORE)
    assert not _index_exists(migration_engine)
