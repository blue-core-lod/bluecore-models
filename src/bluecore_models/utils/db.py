"""Utilities for working with Blue Core Database"""

from typing import Optional

import rdflib
from sqlalchemy import delete, insert, select
from sqlalchemy.orm import object_session

from bluecore_models.models.bf_classes import BibframeClass, ResourceBibframeClass
from bluecore_models.models.version import Version, CURRENT_USER_ID
from bluecore_models.utils.graph import get_bf_classes, frame_jsonld


def _new_bf_classs(connection, bf_class: rdflib.URIRef) -> int:
    stmt = insert(BibframeClass.__table__).values(
        name=bf_class.split("/")[-1], uri=str(bf_class)
    )
    result = connection.execute(stmt)
    return result.inserted_primary_key[0]


def add_bf_classes(connection, resource):
    """Add Bibframe classes to a resource"""
    bf_classes = get_bf_classes(resource.data, resource.uri)
    for class_ in bf_classes:
        stmt = select(BibframeClass.__table__).where(BibframeClass.uri == str(class_))
        result = connection.execute(stmt)
        bf_class_id = result.scalar()
        if not bf_class_id:
            bf_class_id = _new_bf_classs(connection, class_)
        stmt = insert(ResourceBibframeClass.__table__).values(
            bf_class_id=bf_class_id, resource_id=resource.id
        )
        connection.execute(stmt)


def add_version(connection, resource):
    """
    Adds a Version if the resource had been modified.
    """
    try:
        uid = CURRENT_USER_ID.get()
    except Exception:
        uid = None

    if object_session(resource).is_modified(resource, include_collections=False):
        stmt = insert(Version.__table__).values(
            resource_id=resource.id,
            data=resource.data,
            keycloak_user_id=uid,
            created_at=resource.updated_at,
        )
        connection.execute(stmt)


def update_bf_classes(connection, resource):
    """Update Bibframe classes for a resource"""
    bf_classes = get_bf_classes(resource.data, resource.uri)
    latest_bf_classes = set(str(bf_class) for bf_class in bf_classes)
    stmt = (
        select(BibframeClass.__table__.columns.uri)
        .where(BibframeClass.id == ResourceBibframeClass.bf_class_id)
        .where(ResourceBibframeClass.resource_id == resource.id)
    )
    result = connection.execute(stmt)
    existing_bf_classes = set(bf_class_uri for bf_class_uri in result.scalars())
    removed_classes = existing_bf_classes - latest_bf_classes
    added_classes = latest_bf_classes - existing_bf_classes
    for bf_class in removed_classes:
        stmt = select(BibframeClass.__table__).where(BibframeClass.uri == bf_class)
        result = connection.execute(stmt)
        bf_class_id = result.scalar()
        stmt = (
            delete(ResourceBibframeClass.__table__)
            .where(ResourceBibframeClass.bf_class_id == bf_class_id)
            .where(ResourceBibframeClass.resource_id == resource.id)
        )
        connection.execute(stmt)
    for bf_class in added_classes:
        stmt = select(BibframeClass.__table__).where(BibframeClass.uri == bf_class)
        result = connection.execute(stmt)
        bf_class_id = result.scalar()
        if not bf_class_id:
            bf_class_id = _new_bf_classs(connection, bf_class)
        stmt = insert(ResourceBibframeClass.__table__).values(
            bf_class_id=bf_class_id, resource_id=resource.id
        )
        connection.execute(stmt)


def set_jsonld(target, value, oldvalue, initiator) -> Optional[dict]:
    """
    A ORM event handler that ensures JSON-LD data is framed prior to persisting it
    to the database. Note the ordering of properties used in constructors
    matters, since target.uri must be set on the object prior to setting data.

    So this will work:

        >>> w = Work(uri="https://example.com", data={ ... })

    but this will not:

        >>> w = Work(data={...}, uri="https://example.com")

    """
    if target.uri is None and value is not None:
        raise ValueError(
            "For automatic jsonld framing to work you must ensure the uri property is set before the data property, even when constructing an object."
        )
    elif value is not None:
        return frame_jsonld(target.uri, value)
    else:
        return None
