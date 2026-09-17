from copy import deepcopy
from typing import Any, ClassVar

from rdflib import URIRef
from sqlalchemy import (
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
    event,
    select,
)
from sqlalchemy.orm import (
    Mapped,
    Session,
    mapped_column,
    object_session,
    relationship,
)

from bluecore_models.models.base import Base
from bluecore_models.models.resource import ResourceBase
from bluecore_models.utils.db import add_version
from bluecore_models.utils.graph import load_jsonld

SINOPIA = "http://sinopia.io/vocabulary/"

HAS_RESOURCE_TEMPLATE_ID = URIRef(f"{SINOPIA}hasResourceTemplateId")

HAS_RESOURCE_ID = URIRef(f"{SINOPIA}hasResourceId")


def template_ids(data: Any) -> tuple[str | None, set[str]]:
    """The template id a profile claims, and the ids of the templates it nests.

    Parsed as RDF rather than walked as dicts. A profile may be stored expanded
    or compacted with a @context, and the two predicates are written
    differently -- hasResourceId as a literal, hasResourceTemplateId as an IRI
    reference. Reading the graph makes all of that the same question.
    """
    # load_jsonld writes a @context into a dict it is handed, so parse a copy.
    # The stored document must come back out exactly as the client sent it.
    graph = load_jsonld(deepcopy(data))
    own = {str(obj) for obj in graph.objects(None, HAS_RESOURCE_ID)}
    nested = {str(obj) for obj in graph.objects(None, HAS_RESOURCE_TEMPLATE_ID)}
    return next(iter(sorted(own)), None), nested


class Profile(ResourceBase):
    """
    Stores resource profiles (e.g. Sinopia profiles) used to drive editing.

    A Profile is a first-class Bluecore resource: like Works, Instances and
    Hubs it is assigned a ``uuid`` and a minted ``uri`` (``.../profiles/{uuid}``).
    Unlike them, its ``data`` is not framed when persisted because Sinopia
    Editor requires it to be in a particular shape (the set_jsonld handler
    in resource.py skips Profiles).
    """

    __tablename__ = "profiles"
    id: Mapped[int] = mapped_column(
        Integer, ForeignKey("resource_base.id"), primary_key=True
    )
    template_id: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    nestings: Mapped[list["ProfileNesting"]] = relationship(
        "ProfileNesting",
        back_populates="parent",
        cascade="all, delete-orphan",
    )

    __mapper_args__: ClassVar[dict[str, Any]] = {
        "polymorphic_identity": "profiles",
    }

    def children(self) -> list["Profile"]:
        """The profiles this template nests.

        A reference to a template that is not stored resolves to nothing.
        """
        session = object_session(self)
        if session is None or not self.nestings:
            return []
        wanted = [nesting.child_template_id for nesting in self.nestings]
        return list(
            session.scalars(select(Profile).where(Profile.template_id.in_(wanted)))
        )

    def parents(self) -> list["Profile"]:
        """The profiles that nest this template."""
        session = object_session(self)
        if session is None or self.template_id is None:
            return []
        return list(
            session.scalars(
                select(Profile)
                .join(ProfileNesting, ProfileNesting.parent_id == Profile.id)
                .where(ProfileNesting.child_template_id == self.template_id)
                .where(ProfileNesting.parent_id != self.id)
            )
        )

    def __repr__(self):
        return f"<Profile {self.uri or self.id}>"


class ProfileNesting(Base):
    """One template naming another that it nests.

    The child is held as the template id Sinopia wrote rather than a foreign
    key. A profile may name a template that is not stored, and that is still
    what the document says. It also means a save only ever touches its own
    rows, so profiles can be saved in any order.
    """

    __tablename__ = "profile_nestings"
    __table_args__ = (UniqueConstraint("parent_id", "child_template_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    parent_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("profiles.id", ondelete="CASCADE"), nullable=False
    )
    parent: Mapped[Profile] = relationship("Profile", back_populates="nestings")
    child_template_id: Mapped[str] = mapped_column(String, nullable=False, index=True)

    def __repr__(self):
        return f"<ProfileNesting {self.parent_id} -> {self.child_template_id}>"


def _sync_nestings(profile: Profile) -> None:
    """Extract the profile's template id and the nestings its data asserts.

    Only the profile's own rows change, so no other profile is visited and save
    order does not matter.
    """
    profile.template_id, asserted = template_ids(profile.data)
    # Keep the rows that still apply rather than rebuilding the lot: a flush
    # emits its inserts before its deletes, so re-adding a row that is already
    # there trips the unique constraint.
    profile.nestings[:] = [
        nesting for nesting in profile.nestings if nesting.child_template_id in asserted
    ]
    held = {nesting.child_template_id for nesting in profile.nestings}
    profile.nestings.extend(
        ProfileNesting(child_template_id=child) for child in sorted(asserted - held)
    )


@event.listens_for(Session, "before_flush")
def sync_nestings(session, flush_context, instances):
    """Keep every pending Profile's nestings in step with its data.

    This runs before the flush plan is built. A mapper-level before_insert or
    before_update fires once the plan is fixed, too late for the collection
    changes to be written.
    """
    for obj in list(session.new) + list(session.dirty):
        if isinstance(obj, Profile):
            _sync_nestings(obj)


@event.listens_for(Profile, "after_insert")
def create_version(mapper, connection, target):
    """Record a Version when a Profile is created."""
    add_version(connection, target)


@event.listens_for(Profile, "after_update")
def update_version(mapper, connection, target):
    """Record a Version when a Profile is modified."""
    add_version(connection, target)
