"""Record profile nesting as a foreign-keyed relation, with URI references

A Sinopia profile names itself with sinopia:hasResourceId and names the profiles
it nests with sinopia:hasResourceTemplateId. Both are written as id strings
("pcc:bf2:Role"), which cannot be a foreign key to anything.

This migration converts every reference that resolves into the referenced
profile's URI, then lifts the nesting into profile_relations with a real foreign
key on both ends. Resources point at profiles the same way, via a root
sinopia:hasResourceTemplate literal, so those are converted too.

Both conversions are the structural half of #180: the value stops being a
literal and becomes an IRI reference. Appending a version segment to an IRI is
what remains there.

Everything is Core SQL. Alembic bypasses the ORM, so no after_update listener
fires and no Version rows are written -- resources are pinned to existing
version ids, and new rows would move the "current" version out from under them.

Revision ID: 20261001
Revises: 20260916
Create Date: 2026-10-01
"""

import json

import sqlalchemy as sa
from alembic import context, op

# revision identifiers, used by Alembic.
revision: str = "20261001"
down_revision: str = "20260916"
branch_labels = None
depends_on = None

SINOPIA = "http://sinopia.io/vocabulary/"

# A profile's own id, the profiles it nests, and the profile a Work or Instance
# was created from.
HAS_RESOURCE_ID = "hasResourceId"
HAS_RESOURCE_TEMPLATE_ID = "hasResourceTemplateId"
HAS_RESOURCE_TEMPLATE = "hasResourceTemplate"


class DryRunComplete(Exception):
    """Raised at the end of a dry run so the transaction is rolled back.

    Alembic stamps alembic_version inside the same transaction as the migration,
    so a dry run that returned normally would leave the database claiming to be
    migrated while nothing had been applied -- and the next real `upgrade head`
    would be a no-op, with the API expecting a profile_relations table that does
    not exist. Raising aborts the transaction instead, and PostgreSQL's
    transactional DDL rolls the stamp and the table back together.

    The report is printed before this is raised, so the operator still sees it.
    """


def _dry_run() -> bool:
    return context.get_x_argument(as_dictionary=True).get("dry_run") == "true"


def _local_name(key: str) -> str:
    """The local name of a JSON-LD key, expanded or prefixed.

    Profiles are stored in whatever shape the client sent, because set_jsonld
    does not frame them. So a predicate may arrive as the full URI or as
    "sinopia:hasResourceId", and matching on the local name reads both without
    needing to resolve an @context.
    """
    return key.rsplit("/", 1)[-1].rsplit(":", 1)[-1]


def _objects(data, local_name: str):
    """Every JSON-LD object node recorded under the given predicate.

    Walked as JSON rather than parsed as RDF on purpose. Reserializing from a
    graph would reshape the stored document, and these documents are stored
    unframed precisely so Sinopia Editor gets back the shape it sent. Walking
    also reads a compacted document, whose Sinopia prefix the default @context
    cannot resolve.
    """
    found = []
    stack = [data]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            for key, value in node.items():
                if _local_name(key) == local_name:
                    found.extend(value if isinstance(value, list) else [value])
                else:
                    stack.append(value)
        elif isinstance(node, list):
            stack.extend(node)
    return found


def _lexical(obj) -> str | None:
    """The written form of a JSON-LD object, whether IRI, literal or bare string."""
    if isinstance(obj, str):
        return obj
    if isinstance(obj, dict):
        value = obj.get("@id") or obj.get("@value")
        return value if isinstance(value, str) else None
    return None


def _profile_uris_by_id(connection) -> tuple[dict[str, str], dict[str, list[int]]]:
    """Map each profile's sinopia:hasResourceId to its URI.

    Transient: held for the length of this migration and never stored. Once
    references are URIs nothing queries a profile by its id string, so there is
    no reason to promote it to a column.
    """
    rows = connection.execute(
        sa.text(
            "SELECT id, uri, data FROM resource_base"
            " WHERE type = 'profiles' ORDER BY id"
        )
    ).all()
    uri_by_id: dict[str, str] = {}
    claimants: dict[str, list[int]] = {}
    for row_id, uri, data in rows:
        ids = {
            value
            for obj in _objects(data, HAS_RESOURCE_ID)
            if (value := _lexical(obj)) is not None
        }
        for value in sorted(ids):
            claimants.setdefault(value, []).append(row_id)
            uri_by_id.setdefault(value, uri)
    return uri_by_id, claimants


def _dedupe(connection, claimants: dict[str, list[int]]) -> list[tuple[str, int]]:
    """Delete profiles that duplicate another profile's id, keeping the lowest id.

    A duplicate makes a reference ambiguous, and an ambiguous reference cannot
    become a single foreign key. On stage one key is claimed twice --
    bluecore:bf2:Agent:AgentOnly, two profiles loaded three seconds apart in the
    same harvest, identical but for their uuid -- but nothing here is specific to
    that case.

    Not reversible by downgrade(). Acceptable only because Blue Core has no
    production deployment.
    """
    deleted = []
    for key, ids in sorted(claimants.items()):
        for row_id in sorted(ids)[1:]:
            deleted.append((key, row_id))
    if _dry_run() or not deleted:
        return deleted

    ids = [row_id for _, row_id in deleted]
    # Ordered by dependency: every table with a non-primary-key foreign key to
    # resource_base, then the subclass row, then the base row.
    for table, column in (
        ("bibframe_other_resources", "bibframe_resource_id"),
        ("versions", "resource_id"),
        ("resource_bibframe_classes", "resource_id"),
        ("profiles", "id"),
        ("resource_base", "id"),
    ):
        connection.execute(
            sa.text(f"DELETE FROM {table} WHERE {column} = ANY(:ids)"), {"ids": ids}
        )
    return deleted


def _rewrite_refs(connection, uri_by_id: dict[str, str]) -> tuple[int, list[str]]:
    """Turn every profile's nesting references into IRI references to profile URIs.

    A value already shaped as a profile URI is left as it is. A value naming
    nothing stored is also left alone, and returned so it can be reported: the
    relation cannot hold it, and silently dropping it would lose what the
    document says.
    """
    rows = connection.execute(
        sa.text(
            "SELECT id, data FROM resource_base WHERE type = 'profiles' ORDER BY id"
        )
    ).all()
    known_uris = set(uri_by_id.values())
    rewritten = 0
    unresolved = []
    for row_id, data in rows:
        changed = False
        for obj in _objects(data, HAS_RESOURCE_TEMPLATE_ID):
            written = _lexical(obj)
            if written is None or written in known_uris:
                continue
            uri = uri_by_id.get(written)
            if uri is None:
                unresolved.append(written)
                continue
            if not isinstance(obj, dict):
                # A bare string cannot be rewritten in place; expanded
                # documents always use an object node, so this would mean an
                # unexpected shape worth reporting rather than guessing at.
                unresolved.append(written)
                continue
            obj.pop("@value", None)
            obj.pop("@language", None)
            obj["@id"] = uri
            changed = True
            rewritten += 1
        if changed and not _dry_run():
            connection.execute(
                sa.text("UPDATE resource_base SET data = :data WHERE id = :id"),
                {"data": json.dumps(data), "id": row_id},
            )
    return rewritten, unresolved


def _rewrite_resource_refs(
    connection, uri_by_id: dict[str, str]
) -> tuple[int, list[str]]:
    """Point Works and Instances at the profile they were created from by URI.

    Their root sinopia:hasResourceTemplate is a bare literal in framed JSON-LD,
    at a fixed key, so this needs no parsing:

        "http://sinopia.io/vocabulary/hasResourceTemplate": ["pcc:bf2:Monograph:Instance"]

    becomes

        "http://sinopia.io/vocabulary/hasResourceTemplate": [{"@id": "https://..."}]

    The value changes from a literal to an IRI reference and stays that way
    across reframing: CONTEXT declares no sinopia prefix and no term for this
    predicate, and gives {"@type": "@id"} only to hasInstance, hasWork and
    instanceOf, so it will not compact back to a bare string.
    """
    key = f"{SINOPIA}{HAS_RESOURCE_TEMPLATE}"
    rows = connection.execute(
        sa.text(
            "SELECT id, data FROM resource_base"
            " WHERE type IN ('works', 'instances') AND data ? :key ORDER BY id"
        ),
        {"key": key},
    ).all()
    rewritten = 0
    unresolved = []
    for row_id, data in rows:
        values = data.get(key)
        values = values if isinstance(values, list) else [values]
        converted = []
        changed = False
        for obj in values:
            written = _lexical(obj)
            if written is None:
                converted.append(obj)
                continue
            if written in uri_by_id.values():
                converted.append({"@id": written})
                continue
            uri = uri_by_id.get(written)
            if uri is None:
                unresolved.append(written)
                converted.append(obj)
                continue
            converted.append({"@id": uri})
            changed = True
            rewritten += 1
        if changed and not _dry_run():
            data[key] = converted
            connection.execute(
                sa.text("UPDATE resource_base SET data = :data WHERE id = :id"),
                {"data": json.dumps(data), "id": row_id},
            )
    return rewritten, unresolved


def _populate(connection) -> int:
    """Record one profile_relations row per reference, now that all are URIs.

    Set-based: a reference is a URI, resource_base.uri is unique, and only rows
    that are profiles can be either end, so the join does the resolving. The
    CHECK constraint rejects a profile nesting itself, so exclude that here
    rather than letting the insert fail.
    """
    if _dry_run():
        return 0
    result = connection.execute(
        sa.text(
            f"""
            INSERT INTO profile_relations (parent_profile_id, child_profile_id)
            SELECT DISTINCT parent.id, child.id
            FROM resource_base parent
            JOIN profiles parent_p ON parent_p.id = parent.id
            CROSS JOIN LATERAL jsonb_path_query(
                parent.data, '$.**."{SINOPIA}{HAS_RESOURCE_TEMPLATE_ID}"[*]."@id"'
            ) AS ref(value)
            JOIN resource_base child ON child.uri = ref.value #>> '{{}}'
            JOIN profiles child_p ON child_p.id = child.id
            WHERE parent.id <> child.id
            ON CONFLICT DO NOTHING
            """
        )
    )
    return result.rowcount


def upgrade() -> None:
    connection = op.get_bind()

    uri_by_id, claimants = _profile_uris_by_id(connection)
    deleted = _dedupe(connection, claimants)
    if deleted:
        uri_by_id, _ = _profile_uris_by_id(connection)

    profile_rewrites, profile_unresolved = _rewrite_refs(connection, uri_by_id)
    resource_rewrites, resource_unresolved = _rewrite_resource_refs(
        connection, uri_by_id
    )

    if not _dry_run():
        op.create_table(
            "profile_relations",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("child_profile_id", sa.Integer(), nullable=False),
            sa.Column("parent_profile_id", sa.Integer(), nullable=False),
            sa.ForeignKeyConstraint(
                ["child_profile_id"], ["profiles.id"], ondelete="CASCADE"
            ),
            sa.ForeignKeyConstraint(
                ["parent_profile_id"], ["profiles.id"], ondelete="CASCADE"
            ),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("child_profile_id", "parent_profile_id"),
            sa.CheckConstraint(
                "child_profile_id <> parent_profile_id",
                name="ck_profile_relations_no_self_nesting",
            ),
        )
        op.create_index(
            "ix_profile_relations_child_profile_id",
            "profile_relations",
            ["child_profile_id"],
        )
    edges = _populate(connection)

    print()
    print(f"{'DRY RUN: ' if _dry_run() else ''}profile relations migration")
    print(f"  duplicate profiles deleted : {len(deleted)}")
    for key, row_id in deleted:
        print(f"      resource_base.id={row_id} claimed {key}")
    print(f"  profile refs rewritten     : {profile_rewrites}")
    print(f"  resource refs rewritten    : {resource_rewrites}")
    print(f"  profile_relations rows      : {edges}")
    unresolved = sorted(set(profile_unresolved) | set(resource_unresolved))
    print(f"  references naming nothing  : {len(unresolved)}")
    for value in unresolved:
        print(f"      {value}")
    print()

    if _dry_run():
        raise DryRunComplete(
            "dry run: nothing was written and revision 20261001 was NOT applied"
        )


def downgrade() -> None:
    """Drop the relation.

    The reference rewrite is not reversed and the deduplicated profiles are not
    restored. Both are one-way, which is only acceptable because Blue Core has
    no production deployment. The rewritten references remain valid: they are
    profile URIs, and the editor resolves those directly.
    """
    op.drop_index("ix_profile_relations_child_profile_id", "profile_relations")
    op.drop_table("profile_relations")
