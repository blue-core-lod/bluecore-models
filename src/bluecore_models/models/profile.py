import logging
from copy import deepcopy
from typing import Any, ClassVar

from rdflib import URIRef
from sqlalchemy import (
    CheckConstraint,
    ForeignKey,
    Integer,
    UniqueConstraint,
    delete,
    event,
    inspect,
    select,
)
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import (
    Mapped,
    Session,
    column_property,
    mapped_column,
    relationship,
)

from bluecore_models.models.base import Base
from bluecore_models.models.resource import ResourceBase
from bluecore_models.utils.db import add_version
from bluecore_models.utils.graph import load_jsonld

logger = logging.getLogger(__name__)

SINOPIA = "http://sinopia.io/vocabulary/"

# Sinopia's vocabulary, not ours: a profile names the profiles it nests with
# sinopia:hasResourceTemplateId. The predicate says "template" because that is
# what Sinopia calls these; everything on our side of the line says profile.
HAS_RESOURCE_TEMPLATE_ID = URIRef(f"{SINOPIA}hasResourceTemplateId")


def _names_a_remote_context(data: Any) -> bool:
    """Whether the document points at an @context we would have to fetch.

    A string @context is a URL. Resolving one means an HTTP request, and this
    runs inside a flush, so it is never worth making: a profile save would wait
    on a third-party server while holding a transaction open, and fail outright
    if that server were unreachable.
    """
    nodes = data if isinstance(data, list) else [data]
    for node in nodes:
        if not isinstance(node, dict):
            continue
        context = node.get("@context")
        entries = context if isinstance(context, list) else [context]
        if any(isinstance(entry, str) for entry in entries):
            return True
    return False


def profile_refs(data: Any) -> set[str]:
    """The profiles a profile's data says it nests, as written.

    Read as RDF rather than walked as dicts. A profile's data is stored in
    whatever shape the client sent -- expanded, or compacted with an inline
    @context -- because set_jsonld does not frame Profiles, and reading the
    graph makes both shapes the same question.

    Parsed from a copy: load_jsonld writes an @context into a dict it is handed,
    and the stored document has to come back out exactly as it arrived.

    A document this cannot read yields no references instead of raising. The
    model layer stores what it is given -- on main a Profile accepted any JSON
    at all -- so refusing the save here would reject documents that used to
    persist fine, and it is bluecore_api that rejects bad input, where there is
    a cataloger to tell. The warning is the record that it happened.
    """
    if not isinstance(data, list | dict):
        logger.warning(f"cannot read profile references from {type(data).__name__}")
        return set()
    if _names_a_remote_context(data):
        logger.warning("skipping profile references: @context would need fetching")
        return set()
    try:
        graph = load_jsonld(deepcopy(data))
    except (AttributeError, RuntimeError, TypeError, ValueError) as error:
        # rdflib defines no parse-error type for JSON-LD; it lets plain builtins
        # out of whatever it tripped over. These four are what malformed
        # documents were observed to raise -- a non-string @context, a list
        # holding non-objects, a non-string @language. Anything outside them is
        # a failure mode nobody has seen, so let it surface rather than quietly
        # turning it into "this profile nests nothing".
        logger.warning(f"cannot parse profile JSON-LD: {error}")
        return set()
    return {str(obj) for obj in graph.objects(None, HAS_RESOURCE_TEMPLATE_ID)}


class ProfileRelation(Base):
    """One profile nesting another.

    Both ends are foreign keys to profiles, so the relation is the database's to
    enforce rather than the application's: a reference to a profile that does not
    exist cannot be recorded, deleting either end removes the edge, and no
    profile can nest itself.

    A profile may nest many, and a profile may be nested by many -- on stage, 33
    of 91 nested profiles have more than one parent and one has 14 -- so this
    cannot collapse into a column on profiles.
    """

    __tablename__ = "profile_relations"
    __table_args__ = (
        UniqueConstraint("child_profile_id", "parent_profile_id"),
        CheckConstraint(
            "child_profile_id <> parent_profile_id",
            name="ck_profile_relations_no_self_nesting",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    child_profile_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("profiles.id", ondelete="CASCADE"), nullable=False
    )
    parent_profile_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("profiles.id", ondelete="CASCADE"), nullable=False
    )

    def __repr__(self):
        return f"<ProfileRelation {self.parent_profile_id} -> {self.child_profile_id}>"


class Profile(ResourceBase):
    """
    Stores resource profiles (e.g. Sinopia profiles) used to drive editing.

    A Profile is a first-class Bluecore resource: like Works, Instances and
    Hubs it is assigned a ``uuid`` and a minted ``uri`` (``.../profiles/{uuid}``).
    Unlike them, its ``data`` is not framed when persisted because Sinopia
    Editor requires it to be in a particular shape (the set_jsonld handler
    in resource.py skips Profiles).

    A profile's data may name other profiles that it nests. Those references are
    stored as profile URIs and lifted into ``profile_relations`` on save, so the
    nesting can be queried in either direction -- see ``children``, ``parents``
    and ``is_nested``.
    """

    __tablename__ = "profiles"
    id: Mapped[int] = mapped_column(
        Integer, ForeignKey("resource_base.id"), primary_key=True
    )

    __mapper_args__: ClassVar[dict[str, Any]] = {
        "polymorphic_identity": "profiles",
    }

    # viewonly: the rows are written by the after_insert/after_update hooks
    # below, from what the data asserts. The data is the authority, so letting
    # the ORM also manage the collection would give it a second, conflicting
    # one.
    children: Mapped[list["Profile"]] = relationship(
        "Profile",
        secondary="profile_relations",
        primaryjoin="Profile.id == ProfileRelation.parent_profile_id",
        secondaryjoin="Profile.id == ProfileRelation.child_profile_id",
        back_populates="parents",
        viewonly=True,
    )
    parents: Mapped[list["Profile"]] = relationship(
        "Profile",
        secondary="profile_relations",
        primaryjoin="Profile.id == ProfileRelation.child_profile_id",
        secondaryjoin="Profile.id == ProfileRelation.parent_profile_id",
        back_populates="children",
        viewonly=True,
    )

    def __repr__(self):
        return f"<Profile {self.uri or self.id}>"


# Declared out here because it correlates to Profile.id, which does not exist
# until the class is built.
#
# For filtering in SQL -- select(Profile).where(~Profile.is_nested) becomes one
# statement with an inlined NOT EXISTS, which the planner runs as an anti-join.
# Reading it off an instance that did not select it issues a query, so it is an
# N+1 in a loop; use it in the query, not after.
#
# Derived rather than stored: a boolean column would go stale the moment the
# only profile nesting this one was deleted or edited, which is what the
# ON DELETE CASCADE above means it cannot do.
#
# deferred so that an ordinary select(Profile) does not carry the subquery.
Profile.is_nested = column_property(
    select(ProfileRelation.id)
    .where(ProfileRelation.child_profile_id == Profile.id)
    .correlate_except(ProfileRelation)
    .exists(),
    deferred=True,
)


def resolve_ref(session: Session, value: str) -> Profile | None:
    """The Profile a nesting reference names, or None if it names nothing stored.

    References are profile URIs. A value naming a Work, an Instance or nothing
    at all resolves to None -- the join table has no way to record it, and the
    API turns that into a 422 rather than storing a dangling edge.
    """
    return session.scalars(select(Profile).where(Profile.uri == value)).first()


def _resolve_child_ids(connection, refs: set[str]) -> set[int]:
    """The profile ids the given references name, skipping any that name nothing.

    Core SQL rather than resolve_ref() because this runs inside a flush, where
    only a Connection is available.
    """
    if not refs:
        return set()
    profiles = Profile.__table__
    resources = ResourceBase.__table__
    stmt = (
        select(profiles.c.id)
        .select_from(profiles.join(resources, profiles.c.id == resources.c.id))
        .where(resources.c.uri.in_(refs))
    )
    return set(connection.scalars(stmt))


def sync_profile_relations(connection, profile: Profile) -> None:
    """Bring profile_relations into line with what this profile's data asserts.

    Only rows where this profile is the parent are touched, so saving a profile
    never rewrites another profile's edges.

    Deliberately Core SQL in an after_insert/after_update hook rather than a
    before_flush listener on the Session class. A before_flush listener would be
    needed to mutate an ORM collection -- mapper-level flush events fire once the
    flush plan is fixed, too late for collection changes -- but it installs
    itself on every Session in the process at import time, and iterates
    session.new and session.dirty on every flush whether or not a Profile is
    involved. Writing the rows directly sidesteps both.
    """
    child_ids = _resolve_child_ids(connection, profile_refs(profile.data))
    # A profile naming itself is not nested by anything; the CHECK constraint
    # would reject the row, so never offer it one.
    child_ids.discard(profile.id)

    relations = ProfileRelation.__table__
    stale = delete(relations).where(relations.c.parent_profile_id == profile.id)
    if child_ids:
        stale = stale.where(relations.c.child_profile_id.notin_(child_ids))
    connection.execute(stale)

    if child_ids:
        # Deletes run first and ON CONFLICT covers the rest, so re-asserting an
        # edge that is already recorded is a no-op rather than a unique
        # violation.
        connection.execute(
            pg_insert(relations)
            .values(
                [
                    {"parent_profile_id": profile.id, "child_profile_id": child_id}
                    for child_id in sorted(child_ids)
                ]
            )
            .on_conflict_do_nothing()
        )


@event.listens_for(Profile, "after_insert")
def create_version(mapper, connection, target):
    """Record a Version when a Profile is created."""
    add_version(connection, target)


@event.listens_for(Profile, "after_update")
def update_version(mapper, connection, target):
    """Record a Version when a Profile is modified."""
    add_version(connection, target)


@event.listens_for(Profile, "after_insert")
@event.listens_for(Profile, "after_update")
def record_relations(mapper, connection, target):
    """Re-derive what a Profile nests, whenever its data changes.

    The nesting comes from profile.data and nothing else, so a change to data is
    the only thing worth reacting to. after_update fires on any change to the
    row though -- renaming a uri, for instance -- and re-deriving then would
    parse the whole JSON-LD document again only to delete and reinsert the rows
    that are already there, handing them new ids on the way. Inspecting the data
    attribute rather than the whole object keeps the work to saves that could
    have changed the answer.

    add_version is guarded the same way, for its own reason: see its docstring
    on why a save that did not touch data must not record a Version.
    """
    if inspect(target).attrs.data.history.has_changes():
        sync_profile_relations(connection, target)
