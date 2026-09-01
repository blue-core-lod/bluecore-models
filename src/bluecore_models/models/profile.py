import json
from typing import Any, ClassVar

from sqlalchemy import Boolean, ForeignKey, Integer, event, false, text
from sqlalchemy.orm import Mapped, mapped_column

from bluecore_models.models.resource import ResourceBase
from bluecore_models.utils.db import add_version

SINOPIA = "http://sinopia.io/vocabulary/"

HAS_RESOURCE_TEMPLATE_ID = f"{SINOPIA}hasResourceTemplateId"

HAS_RESOURCE_ID = f"{SINOPIA}hasResourceId"


def _values_for(data: Any, predicate: str) -> set[str]:
    """Collect the template ids ``predicate`` asserts.

    Read both forms: Sinopia writes hasResourceId as a literal ({"@value": id})
    but hasResourceTemplateId as an IRI reference ({"@id": id}).
    """
    nodes = data if isinstance(data, list) else [data]
    return {
        value[key]
        for node in nodes
        if isinstance(node, dict)
        for value in node.get(predicate, [])
        if isinstance(value, dict)
        for key in ("@value", "@id")
        if key in value
    }


def _nested_template_ids(data: Any) -> set[str]:
    """The ids of the templates this one nests.

    Every hasResourceTemplateId marks a nesting, so the surrounding property
    structure needs no walking.
    """
    return _values_for(data, HAS_RESOURCE_TEMPLATE_ID)


def _own_template_ids(data: Any) -> set[str]:
    """The ids this template claims as its own."""
    return _values_for(data, HAS_RESOURCE_ID)


def _patterns(predicate: str, resource_id: str) -> list[str]:
    """jsonb containment patterns for ``predicate`` naming this template id.

    One per serialization, since @> matches an exact shape. A language tag needs
    no pattern of its own: @> matches a subset of keys.
    """
    return [
        json.dumps([{predicate: [{key: resource_id}]}]) for key in ("@value", "@id")
    ]


def _contains_resource_id(resource_id: str) -> list[str]:
    """Patterns matching the template that claims this resource id."""
    return _patterns(HAS_RESOURCE_ID, resource_id)


def _nests_resource_id(resource_id: str) -> list[str]:
    """Patterns matching templates that nest this resource id."""
    return _patterns(HAS_RESOURCE_TEMPLATE_ID, resource_id)


def _referenced_by_another(connection, profile_id: int, resource_ids: set[str]) -> bool:
    """Does a template other than ``profile_id`` nest any of these ids?"""
    for resource_id in resource_ids:
        for pattern in _nests_resource_id(resource_id):
            referenced = connection.execute(
                text(
                    "SELECT 1 FROM resource_base"
                    " WHERE type = 'profiles' AND id <> :id"
                    "   AND data @> CAST(:pattern AS jsonb)"
                    " LIMIT 1"
                ),
                {"id": profile_id, "pattern": pattern},
            ).first()
            if referenced is not None:
                return True
    return False


def _set_nested(connection, profile_id: int, nested: bool) -> None:
    """Set one profile's flag directly."""
    connection.execute(
        text("UPDATE profiles SET is_nested = :nested WHERE id = :id"),
        {"id": profile_id, "nested": nested},
    )


def _set_nested_by_template_id(connection, resource_id: str, nested: bool) -> None:
    """Set the flag on whichever profile claims ``resource_id`` as its own."""
    for pattern in _contains_resource_id(resource_id):
        connection.execute(
            text(
                "UPDATE profiles SET is_nested = :nested WHERE id IN ("
                "  SELECT id FROM resource_base"
                "  WHERE type = 'profiles' AND data @> CAST(:pattern AS jsonb)"
                ")"
            ),
            {"pattern": pattern, "nested": nested},
        )


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
    is_nested: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=false(), default=False
    )

    __mapper_args__: ClassVar[dict[str, Any]] = {
        "polymorphic_identity": "profiles",
    }

    def __repr__(self):
        return f"<Profile {self.uri or self.id}>"


@event.listens_for(Profile, "after_insert")
def create_version(mapper, connection, target):
    """Record a Version when a Profile is created."""
    add_version(connection, target)


@event.listens_for(Profile, "before_update")
def release_dropped_nestings(mapper, connection, target):
    """Un-nest templates this one has stopped nesting.

    Reads the old data back from the row, which this event still sees unwritten.
    Attribute history is empty here: each commit expires ``data``, so assigning
    a new value records no old one.
    """
    stored = connection.execute(
        text("SELECT data FROM resource_base WHERE id = :id"), {"id": target.id}
    ).scalar()
    if stored is None:
        return
    dropped = _nested_template_ids(stored) - _nested_template_ids(target.data)
    for resource_id in dropped:
        # Stay hidden if another template still nests it. target is excluded:
        # its stored row is the old one until this update lands.
        if _referenced_by_another(connection, target.id, {resource_id}):
            continue
        _set_nested_by_template_id(connection, resource_id, False)


@event.listens_for(Profile, "after_insert")
@event.listens_for(Profile, "after_update")
def sync_nested_flags(mapper, connection, target):
    """Flag the templates this one nests, and this one if anything nests it.

    Both directions, because either can be saved first: a parent cannot flag a
    child that does not exist yet, so a child arriving later checks for itself.
    """
    for resource_id in _nested_template_ids(target.data):
        _set_nested_by_template_id(connection, resource_id, True)
    _set_nested(
        connection,
        target.id,
        _referenced_by_another(connection, target.id, _own_template_ids(target.data)),
    )


@event.listens_for(Profile, "after_update")
def update_version(mapper, connection, target):
    """Record a Version when a Profile is modified."""
    add_version(connection, target)
