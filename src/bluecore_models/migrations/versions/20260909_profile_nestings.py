"""Record profile nestings as rows

A Sinopia template names itself with sinopia:hasResourceId and names the
templates it nests with sinopia:hasResourceTemplateId. Both are lifted out of
the JSON-LD: the id onto profiles.template_id, the references into
profile_nestings, so the relations can be queried in either direction.

The child is the template id as written, not a foreign key. Profiles routinely
name templates that are not stored, and dropping those references would lose
what the document says.

Revision ID: 20260909
Revises: 20260826
Create Date: 2026-09-09
"""

import sqlalchemy as sa
from alembic import op
from rdflib import Graph, URIRef

revision: str = "20260909"
down_revision: str | None = "20260826"
branch_labels = None
depends_on = None


SINOPIA = "http://sinopia.io/vocabulary/"
HAS_RESOURCE_ID = URIRef(f"{SINOPIA}hasResourceId")
HAS_RESOURCE_TEMPLATE_ID = URIRef(f"{SINOPIA}hasResourceTemplateId")


def _template_ids(data) -> tuple[str | None, set[str]]:
    """The template id a profile claims, and the ids of the templates it nests.

    A deliberate copy of template_ids() in models/profile.py rather than an
    import: a migration has to keep doing what it did when it was written, and
    the model is free to change.
    """
    graph = Graph()
    # rdflib cannot parse a list of JSON-LD nodes in one go, so feed it each.
    for node in data if isinstance(data, list) else [data]:
        graph.parse(data=node, format="json-ld")
    own = {str(obj) for obj in graph.objects(None, HAS_RESOURCE_ID)}
    nested = {str(obj) for obj in graph.objects(None, HAS_RESOURCE_TEMPLATE_ID)}
    return next(iter(sorted(own)), None), nested


def _backfill(connection) -> None:
    """Read the stored profiles and record what each one asserts.

    Parsed rather than queried in SQL: a profile may be stored expanded or
    compacted with a @context, and reading it as RDF handles both.
    """
    profiles = connection.execute(
        sa.text(
            "SELECT id, data FROM resource_base WHERE type = 'profiles' ORDER BY id"
        )
    ).all()
    for profile_id, data in profiles:
        own, nested = _template_ids(data)
        if own is not None:
            connection.execute(
                sa.text("UPDATE profiles SET template_id = :tid WHERE id = :id"),
                {"tid": own, "id": profile_id},
            )
        for child in sorted(nested):
            connection.execute(
                sa.text(
                    "INSERT INTO profile_nestings"
                    " (parent_profile_id, child_template_id)"
                    " VALUES (:id, :child) ON CONFLICT DO NOTHING"
                ),
                {"id": profile_id, "child": child},
            )


def upgrade() -> None:
    op.add_column("profiles", sa.Column("template_id", sa.String(), nullable=True))
    op.create_index("ix_profiles_template_id", "profiles", ["template_id"])

    op.create_table(
        "profile_nestings",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("parent_profile_id", sa.Integer(), nullable=False),
        sa.Column("child_template_id", sa.String(), nullable=False),
        sa.ForeignKeyConstraint(
            ["parent_profile_id"], ["profiles.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("parent_profile_id", "child_template_id"),
    )
    op.create_index(
        "ix_profile_nestings_child_template_id",
        "profile_nestings",
        ["child_template_id"],
    )

    _backfill(op.get_bind())


def downgrade() -> None:
    op.drop_index("ix_profile_nestings_child_template_id", "profile_nestings")
    op.drop_table("profile_nestings")
    op.drop_index("ix_profiles_template_id", "profiles")
    op.drop_column("profiles", "template_id")
