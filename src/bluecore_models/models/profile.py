import logging
import re
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
from bluecore_models.models.version import Version
from bluecore_models.utils.db import add_version
from bluecore_models.utils.graph import load_jsonld

logger = logging.getLogger(__name__)

SINOPIA = "http://sinopia.io/vocabulary/"

# Sinopia's vocabulary, not ours: a profile names the profiles it nests with
# sinopia:hasResourceTemplateId. The predicate says "template" because that is
# what Sinopia calls these; everything on our side of the line says profile.
HAS_RESOURCE_TEMPLATE_ID = URIRef(f"{SINOPIA}hasResourceTemplateId")

# A reference to one version of a profile: {profile uri}/version/{versions.id},
# the URI GET /profiles/{uuid}/version/{version_id} serves.
VERSION_URI = re.compile(r"(?P<profile_uri>.+)/version/(?P<version_id>[0-9]+)")


def split_version_uri(value: str) -> tuple[str, int | None]:
    """The profile URI a reference names, and the version id if it names one.

    A reference without a version segment comes back unchanged, with None.
    """
    match = VERSION_URI.fullmatch(value)
    if match is None:
        return value, None
    return match["profile_uri"], int(match["version_id"])


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
    stored as profile URIs, or as URIs of one version of a profile (see
    ``split_version_uri``), and lifted into ``profile_relations`` on save, so the
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


def _resolve_child_ids(connection, refs: set[str]) -> set[int]:
    """The profile ids the given references name, skipping any that name nothing.

    A reference to one version of a profile names that profile: an edge is
    between two profiles, whichever version of the child the parent uses. Whether
    that version exists is for bluecore_api to check, where there is someone to
    tell; here it would only turn a bad reference into a missing edge.

    Core SQL because this runs inside a flush, where only a Connection is
    available.
    """
    refs = {split_version_uri(ref)[0] for ref in refs}
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


def latest_version_uri(session: Session, profile: Profile) -> str | None:
    """The URI of a profile's newest version, or None if it has none yet.

    Ordered by id as well as created_at because a version takes its created_at
    from the profile's updated_at, which is the transaction's start time: two
    versions written in one transaction, as cascade_nested_versions writes them,
    share it.
    """
    version_id = session.scalars(
        select(Version.id)
        .where(Version.resource_id == profile.id)
        .order_by(Version.created_at.desc(), Version.id.desc())
        .limit(1)
    ).first()
    if version_id is None:
        return None
    return f"{profile.uri}/version/{version_id}"


def _pin_nested(data: Any, pins: dict[str, str]) -> Any | None:
    """A copy of a profile's data with its nesting references pinned, or None.

    pins maps a profile URI to the version URI that references to it should
    name, whether they name a version now or not. None means no reference
    changed, so there is no new version to make.

    Walked as JSON and edited on a copy, rather than parsed as RDF and written
    out again: a profile is stored in the shape the client sent, and Sinopia
    Editor reads it back in that shape.
    """
    pinned = deepcopy(data)
    changed = False
    stack = [pinned]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            for key, value in node.items():
                # expanded or prefixed, as in profile_refs
                if key.rsplit("/", 1)[-1].rsplit(":", 1)[-1] != "hasResourceTemplateId":
                    stack.append(value)
                    continue
                for obj in value if isinstance(value, list) else [value]:
                    ref = obj.get("@id") if isinstance(obj, dict) else None
                    if not isinstance(ref, str):
                        continue
                    version_uri = pins.get(split_version_uri(ref)[0])
                    if version_uri is not None and ref != version_uri:
                        obj["@id"] = version_uri
                        changed = True
        elif isinstance(node, list):
            stack.extend(node)
    return pinned if changed else None


def cascade_nested_versions(session: Session, profile: Profile) -> list[Profile]:
    """Give every profile that nests this one a new version pinning its newest.

    Call it after changing a profile, in the same transaction, so the change
    and every version it causes commit together. It goes all the way up: a
    parent that gets a new version is nested by others, which then need one too.
    The new versions are saved through the ORM, so the usual hooks record them
    and their relations, and they carry the same author as the change that
    caused them.

    Each profile is saved once, after everything it nests that changed, so it
    pins their new versions: a profile reached through two parents gets one new
    version, not two. A nesting cycle has no such order. Its profiles are saved
    once each, in id order, and the cascade stops.

    Returns the profiles given a new version, in the order they were saved.
    """
    session.flush()
    relations = ProfileRelation.__table__

    # every profile above this one, and the (child, parent) edges among them
    edges: set[tuple[int, int]] = set()
    ancestors: set[int] = set()
    frontier = {profile.id}
    while frontier:
        rows = session.execute(
            select(relations.c.child_profile_id, relations.c.parent_profile_id).where(
                relations.c.child_profile_id.in_(frontier)
            )
        ).all()
        edges.update((child, parent) for child, parent in rows)
        frontier = {parent for _, parent in rows} - ancestors - {profile.id}
        ancestors |= frontier
    if not ancestors:
        return []

    version_uri = latest_version_uri(session, profile)
    if version_uri is None:
        return []
    pins = {profile.uri: version_uri}
    profiles = {
        p.id: p
        for p in session.scalars(select(Profile).where(Profile.id.in_(ancestors)))
    }
    # the profiles above this one that each is still waiting on
    waiting = {
        parent: {child for child, p in edges if p == parent and child in ancestors}
        for parent in ancestors
    }

    updated = []
    while waiting:
        ready = [parent for parent, children in waiting.items() if not children]
        parent = profiles[min(ready or waiting)]
        pinned = _pin_nested(parent.data, pins)
        if pinned is not None:
            parent.data = pinned
            session.flush()
            updated.append(parent)
        if (parent_version := latest_version_uri(session, parent)) is not None:
            pins[parent.uri] = parent_version
        del waiting[parent.id]
        for children in waiting.values():
            children.discard(parent.id)
    return updated


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
