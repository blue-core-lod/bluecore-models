"""Utilities for working with Blue Core Database"""

import rdflib

from sqlalchemy import delete, insert, select
from bluecore_models.models.bf_classes import BibframeClass, ResourceBibframeClass

from bluecore_models.utils.graph import get_bf_classes


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
