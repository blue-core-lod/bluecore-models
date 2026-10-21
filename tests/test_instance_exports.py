"""InstanceExport: the audit trail of who exported an Instance, under which
Keycloak group, and when -- and that it outlives the Instance."""

import contextlib
import os
from datetime import datetime, timedelta

import pytest
from alembic import command
from alembic.config import Config
from pytest_mock_resources import create_postgres_fixture
from sqlalchemy import delete, inspect, select

from bluecore_models.models import (
    Instance,
    InstanceExport,
    ResourceBibframeClass,
    Version,
)
from bluecore_models.models.version import (
    CURRENT_GROUP,
    CURRENT_USER_ID,
    CURRENT_USERNAME,
)
from bluecore_models.utils.db import add_instance_export

INSTANCE_URI = "https://bluecore.info/instances/75d831b9-e0d6-40f0-abb3-e9130622eb8a"


@contextlib.contextmanager
def token_context(uid=None, username=None, group=None):
    """What the bluecore_api HTTP layer sets from the token"""
    tokens = [
        (CURRENT_USER_ID, CURRENT_USER_ID.set(uid)),
        (CURRENT_USERNAME, CURRENT_USERNAME.set(username)),
        (CURRENT_GROUP, CURRENT_GROUP.set(group)),
    ]
    try:
        yield
    finally:
        for var, token in reversed(tokens):
            var.reset(token)


def get_instance(session) -> Instance:
    return session.scalars(select(Instance).where(Instance.uri == INSTANCE_URI)).one()


def test_export_from_context(pg_session):
    with pg_session() as session:
        instance = get_instance(session)
        latest = instance.versions[-1]

        with token_context("a1b2-c3d4", "cataloger", "/stanford"):
            add_instance_export(session, instance, target_identifier="in00000123")
        session.commit()

        export = session.scalars(select(InstanceExport)).one()
        assert export.instance == instance
        assert export.instance_uri == INSTANCE_URI
        assert export.version_id == latest.id
        assert export.keycloak_user_id == "a1b2-c3d4"
        assert export.keycloak_username == "cataloger"
        assert export.keycloak_group == "/stanford"
        assert export.target_identifier == "in00000123"
        assert isinstance(export.exported_at, datetime)
        assert repr(export) == (
            f"<InstanceExport of {INSTANCE_URI} by cataloger (/stanford) "
            f"at {export.exported_at}>"
        )


def test_target_identifier_is_optional(pg_session):
    with pg_session() as session:
        with token_context("a1b2-c3d4", group="/stanford"):
            export = add_instance_export(session, get_instance(session))
        session.commit()

        assert export.target_identifier is None
        assert export.keycloak_username is None


def test_keyword_arguments_override_context(pg_session):
    with pg_session() as session:
        instance = get_instance(session)
        version = instance.versions[0]

        with token_context("a1b2-c3d4", "cataloger", "/stanford"):
            export = add_instance_export(
                session,
                instance,
                version=version,
                keycloak_user_id="service-account",
                keycloak_username="bluecore_workflows",
                keycloak_group="/cornell",
            )
        session.commit()

        assert export.version == version
        assert export.keycloak_user_id == "service-account"
        assert export.keycloak_username == "bluecore_workflows"
        assert export.keycloak_group == "/cornell"


@pytest.mark.parametrize(
    "uid, group, missing",
    [(None, "/stanford", "user id"), ("a1b2-c3d4", None, "group")],
)
def test_anonymous_export_raises(pg_session, uid, group, missing):
    with (
        pg_session() as session,
        token_context(uid, group=group),
        pytest.raises(ValueError, match=missing),
    ):
        add_instance_export(session, get_instance(session))


def test_exports_are_ordered(pg_session):
    with pg_session() as session:
        instance = get_instance(session)
        with token_context("a1b2-c3d4", group="/stanford"):
            first = add_instance_export(session, instance, target_identifier="1")
            second = add_instance_export(session, instance, target_identifier="2")
        # Inserted second, exported first
        second.exported_at = first.exported_at - timedelta(days=1)
        session.commit()

        session.expire(instance, ["exports"])
        assert [e.target_identifier for e in instance.exports] == ["2", "1"]


def test_exports_outlive_the_instance(pg_session):
    with pg_session() as session:
        instance = get_instance(session)
        with token_context("a1b2-c3d4", group="/stanford"):
            add_instance_export(session, instance)
        session.commit()

        # Children first, the way bluecore_api's delete_instance does it
        session.execute(
            delete(ResourceBibframeClass).where(
                ResourceBibframeClass.resource_id == instance.id
            )
        )
        session.execute(delete(Version).where(Version.resource_id == instance.id))
        session.delete(instance)
        session.commit()

        export = session.scalars(select(InstanceExport)).one()
        assert export.instance_id is None
        assert export.version_id is None
        assert export.instance_uri == INSTANCE_URI


# A clean database, built by the migrations themselves like production
migration_engine = create_postgres_fixture()


def test_migration(migration_engine):
    os.environ["DATABASE_URL"] = migration_engine.url.render_as_string(
        hide_password=False
    )
    config = Config("alembic.ini")

    command.upgrade(config, "20261005")
    inspector = inspect(migration_engine)
    assert {
        fk["referred_table"]: fk["options"]
        for fk in inspector.get_foreign_keys("instance_exports")
    } == {
        "instances": {"ondelete": "SET NULL"},
        "versions": {"ondelete": "SET NULL"},
    }
    assert {i["name"] for i in inspector.get_indexes("instance_exports")} == {
        "index_instance_exports_on_instance_id_exported_at",
        "index_instance_exports_on_instance_uri",
        "index_instance_exports_on_keycloak_group_exported_at",
        "index_instance_exports_on_keycloak_user_id",
        "index_instance_exports_on_target_identifier",
    }

    command.downgrade(config, "20261004")
    assert not inspect(migration_engine).has_table("instance_exports")
