"""The 20261001 migration, run for real against a clean database.

tests/conftest.py's pg_session fixture builds the schema with
Base.metadata.create_all and never runs alembic, so nothing else in the suite
exercises a migration. These tests need their own database, migrated the way
production is.
"""

import json
import os

import pytest
from alembic import command
from alembic.config import Config
from pytest_mock_resources import create_postgres_fixture
from sqlalchemy import text

SINOPIA = "http://sinopia.io/vocabulary/"
HAS_RESOURCE_TEMPLATE = f"{SINOPIA}hasResourceTemplate"

BEFORE = "20260916"
AFTER = "20261001"

BASE = "https://bcld.info/profiles"

# A clean database: no create_all, no seed rows. The migration chain builds the
# schema itself, which is what it has to do in production anyway.
migration_engine = create_postgres_fixture()


def profile_doc(name: str, nests: tuple[str, ...] = ()) -> list[dict]:
    """Expanded JSON-LD for a profile, naming nested profiles by id string.

    This is the pre-migration shape: hasResourceId is a literal and
    hasResourceTemplateId is an IRI reference holding an id string rather than
    a URI.
    """
    doc: list[dict] = [
        {
            "@id": f"{BASE}/{name}",
            "@type": [f"{SINOPIA}ResourceTemplate"],
            f"{SINOPIA}hasResourceId": [{"@value": name, "@language": "en"}],
        }
    ]
    for i, nested in enumerate(nests):
        doc.append(
            {
                "@id": f"_:attributes{i}",
                "@type": [f"{SINOPIA}ResourcePropertyTemplate"],
                f"{SINOPIA}hasResourceTemplateId": [{"@id": nested}],
            }
        )
    return doc


def alembic_config(engine) -> Config:
    os.environ["DATABASE_URL"] = engine.url.render_as_string(hide_password=False)
    return Config("alembic.ini")


def insert_resource(connection, kind: str, name: str, data) -> int:
    """A resource_base row plus its subclass row, without going through the ORM.

    The ORM would frame the data and fire add_version, and this has to reproduce
    what is already stored.
    """
    row_id = connection.execute(
        text(
            "INSERT INTO resource_base (type, data, uri, uuid, created_at, updated_at)"
            " VALUES (:type, :data, :uri, gen_random_uuid(), now(), now())"
            " RETURNING id"
        ),
        {"type": kind, "data": json.dumps(data), "uri": f"{BASE}/{name}"}
        if kind == "profiles"
        else {
            "type": kind,
            "data": json.dumps(data),
            "uri": f"https://bcld.info/{kind}/{name}",
        },
    ).scalar()
    connection.execute(text(f"INSERT INTO {kind} (id) VALUES (:id)"), {"id": row_id})
    connection.execute(
        text(
            "INSERT INTO versions (resource_id, data, created_at)"
            " VALUES (:id, :data, now())"
        ),
        {"id": row_id, "data": json.dumps(data)},
    )
    return row_id


@pytest.fixture()
def migrated(migration_engine):
    """A database at 20260916, seeded with pre-migration data."""
    config = alembic_config(migration_engine)
    command.upgrade(config, BEFORE)

    with migration_engine.begin() as connection:
        ids = {
            "child": insert_resource(
                connection, "profiles", "Note:General", profile_doc("Note:General")
            ),
            "shared": insert_resource(
                connection, "profiles", "Note:Shared", profile_doc("Note:Shared")
            ),
            # Two parents nesting the same child, by id string.
            "parent_a": insert_resource(
                connection,
                "profiles",
                "Work:Map",
                profile_doc("Work:Map", ("Note:General", "Note:Shared")),
            ),
            "parent_b": insert_resource(
                connection,
                "profiles",
                "Work:Text",
                profile_doc("Work:Text", ("Note:Shared",)),
            ),
            # A reference naming nothing stored, and a profile naming itself.
            "haunted": insert_resource(
                connection,
                "profiles",
                "Haunted",
                profile_doc("Haunted", ("pcc:bf2:Agent:Code",)),
            ),
            "looping": insert_resource(
                connection, "profiles", "Loop", profile_doc("Loop", ("Loop",))
            ),
            # A duplicate claimant: same hasResourceId as "child", higher id.
            "duplicate": insert_resource(
                connection,
                "profiles",
                "Note:General:Copy",
                profile_doc("Note:General"),
            ),
            # Resources pointing at a profile by id string, framed.
            "work": insert_resource(
                connection,
                "works",
                "w1",
                {"@id": f"{BASE}/w1", HAS_RESOURCE_TEMPLATE: ["Work:Map"]},
            ),
            "instance": insert_resource(
                connection,
                "instances",
                "i1",
                {"@id": f"{BASE}/i1", HAS_RESOURCE_TEMPLATE: ["Work:Text"]},
            ),
        }
    return migration_engine, config, ids


def fetch(connection, row_id: int):
    return connection.execute(
        text("SELECT data FROM resource_base WHERE id = :id"), {"id": row_id}
    ).scalar()


def refs_of(data) -> list[str]:
    key = f"{SINOPIA}hasResourceTemplateId"
    return [obj["@id"] for node in data for obj in node.get(key, [])]


def test_dry_run_changes_nothing(migrated):
    engine, config, ids = migrated
    with engine.begin() as connection:
        before = fetch(connection, ids["parent_a"])
        versions_before = connection.execute(
            text("SELECT count(*) FROM versions")
        ).scalar()

    config.cmd_opts = type("Opts", (), {"x": ["dry_run=true"]})()
    # The dry run aborts deliberately so its transaction -- including the
    # alembic_version stamp -- is rolled back.
    with pytest.raises(Exception, match="dry run"):
        command.upgrade(config, AFTER)

    with engine.begin() as connection:
        assert fetch(connection, ids["parent_a"]) == before
        assert (
            connection.execute(text("SELECT count(*) FROM versions")).scalar()
            == versions_before
        )
        # Crucially: the revision must NOT be stamped as applied. Otherwise the
        # database would claim to be migrated and the next real upgrade would
        # be a no-op, leaving no profile_relations table behind.
        assert (
            connection.execute(text("SELECT version_num FROM alembic_version")).scalar()
            == BEFORE
        )
        # The duplicate is still there, and so is its id-string reference.
        assert (
            connection.execute(
                text("SELECT count(*) FROM profiles WHERE id = :id"),
                {"id": ids["duplicate"]},
            ).scalar()
            == 1
        )
        assert (
            connection.execute(text("SELECT to_regclass('profile_relations')")).scalar()
            is None
        )


def test_upgrade_rewrites_refs_and_records_relations(migrated):
    engine, config, ids = migrated
    with engine.begin() as connection:
        versions_before = connection.execute(
            text("SELECT count(*) FROM versions")
        ).scalar()

    command.upgrade(config, AFTER)

    with engine.begin() as connection:
        # Profile refs became profile URIs.
        assert sorted(refs_of(fetch(connection, ids["parent_a"]))) == [
            f"{BASE}/Note:General",
            f"{BASE}/Note:Shared",
        ]

        # Resource refs became IRI references, not literals.
        work = fetch(connection, ids["work"])
        assert work[HAS_RESOURCE_TEMPLATE] == [{"@id": f"{BASE}/Work:Map"}]
        instance = fetch(connection, ids["instance"])
        assert instance[HAS_RESOURCE_TEMPLATE] == [{"@id": f"{BASE}/Work:Text"}]

        # The duplicate claimant is gone, lowest id kept.
        assert (
            connection.execute(
                text("SELECT count(*) FROM resource_base WHERE id = :id"),
                {"id": ids["duplicate"]},
            ).scalar()
            == 0
        )
        assert (
            connection.execute(
                text("SELECT count(*) FROM resource_base WHERE id = :id"),
                {"id": ids["child"]},
            ).scalar()
            == 1
        )

        # One edge per resolvable reference; the shared child has two parents.
        edges = set(
            connection.execute(
                text(
                    "SELECT parent_profile_id, child_profile_id FROM profile_relations"
                )
            ).all()
        )
        assert edges == {
            (ids["parent_a"], ids["child"]),
            (ids["parent_a"], ids["shared"]),
            (ids["parent_b"], ids["shared"]),
        }

        # An unresolvable ref records no edge and is left as written.
        assert refs_of(fetch(connection, ids["haunted"])) == ["pcc:bf2:Agent:Code"]

        # A profile naming itself records no edge -- the CHECK forbids it.
        assert (
            connection.execute(
                text(
                    "SELECT count(*) FROM profile_relations"
                    " WHERE parent_profile_id = :id"
                ),
                {"id": ids["looping"]},
            ).scalar()
            == 0
        )

        # The rewrite must not create versions: resources are pinned to
        # existing version ids, so new rows would move "current" under them.
        # One version row went with the deleted duplicate.
        assert (
            connection.execute(text("SELECT count(*) FROM versions")).scalar()
            == versions_before - 1
        )


def test_rerunning_over_migrated_data_changes_nothing(migrated):
    """Downgrade then upgrade again, which is how a re-run actually happens.

    downgrade() drops the relation but deliberately leaves the rewritten
    references in place, so the second upgrade runs over data that is already
    URI-shaped. It has to reach the same relations and touch no documents.
    """
    engine, config, ids = migrated
    command.upgrade(config, AFTER)
    with engine.begin() as connection:
        first = sorted(
            connection.execute(
                text(
                    "SELECT parent_profile_id, child_profile_id FROM profile_relations"
                )
            ).all()
        )
        documents = {
            name: fetch(connection, row_id)
            for name, row_id in ids.items()
            if name != "duplicate"
        }
        versions = connection.execute(text("SELECT count(*) FROM versions")).scalar()

    command.downgrade(config, BEFORE)
    command.upgrade(config, AFTER)

    with engine.begin() as connection:
        assert (
            sorted(
                connection.execute(
                    text(
                        "SELECT parent_profile_id, child_profile_id"
                        " FROM profile_relations"
                    )
                ).all()
            )
            == first
        )
        for name, data in documents.items():
            assert fetch(connection, ids[name]) == data, name
        assert (
            connection.execute(text("SELECT count(*) FROM versions")).scalar()
            == versions
        )


def test_downgrade_drops_the_relation(migrated):
    engine, config, _ = migrated
    command.upgrade(config, AFTER)
    command.downgrade(config, BEFORE)

    with engine.begin() as connection:
        assert (
            connection.execute(text("SELECT to_regclass('profile_relations')")).scalar()
            is None
        )
