"""Utilities for working with Blue Core Database"""

from sqlalchemy import insert, select
from sqlalchemy.orm import Session
from bluecore.models.bf_classes import BibframeClass, ResourceBibframeClass

from bluecore.utils.graph import get_bf_classes


def add_bf_classes(connection, resource):
    """Add Bibframe classes to a resource"""
    bf_classes = get_bf_classes(resource.data, resource.uri)
    for class_ in bf_classes:
        stmt = select(BibframeClass.__table__).where(BibframeClass.uri == str(class_))
        result = connection.execute(stmt)
        bf_class_id = result.scalar()
        if not bf_class_id:
            stmt = insert(BibframeClass.__table__).values(
                name=class_.split("/")[-1], uri=str(class_)
            )
            result = connection.execute(stmt)
            bf_class_id = result.inserted_primary_key[0]
        stmt = insert(ResourceBibframeClass.__table__).values(
            bf_class_id=bf_class_id, resource_id=resource.id
        )
        connection.execute(stmt)
