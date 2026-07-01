from datetime import datetime
from typing import Any

from sqlalchemy import Connection, DateTime, ForeignKey, Index, Integer, event
from sqlalchemy.orm import (
    Mapped,
    mapped_column,
    relationship,
)

from bluecore_models.models.base import Base
from bluecore_models.models.resource import ResourceBase
from bluecore_models.utils.db import add_version


class OtherResource(ResourceBase):
    """
    Stores JSON or JSON-LD resources used to support Work and Instances.
    """

    __tablename__ = "other_resources"
    id: Mapped[int] = mapped_column(
        Integer, ForeignKey("resource_base.id"), primary_key=True
    )

    __mapper_args__ = {
        "polymorphic_identity": "other_resources",
    }

    def __repr__(self):
        return f"<OtherResource {self.uri or self.id}>"


class BibframeOtherResources(Base):
    """
    Creates relationships between Work or Instance and supporting resources
    """

    __tablename__ = "bibframe_other_resources"

    id: Mapped[int] = mapped_column(primary_key=True)
    other_resource_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("other_resources.id"), nullable=False
    )
    other_resource: Mapped[OtherResource] = relationship(
        "OtherResource", foreign_keys=other_resource_id
    )
    bibframe_resource_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("resource_base.id"), nullable=False
    )
    bibframe_resource: Mapped[ResourceBase] = relationship(
        "ResourceBase", back_populates="other_resources"
    )

    created_at = mapped_column(DateTime, default=datetime.utcnow)
    updated_at = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )

    __table_args__ = (
        # Neither foreign key is indexed by default in Postgres. bibframe_resource_id
        # backs the BluecoreGraph._link/_delete_other_links lookup and delete-by-work-
        # or-instance, which otherwise degrades to a sequential scan as this table
        # grows. other_resource_id is indexed too for symmetric lookups/joins from the
        # OtherResource side.
        Index(
            "index_bibframe_other_resources_on_bibframe_resource_id",
            "bibframe_resource_id",
        ),
        Index(
            "index_bibframe_other_resources_on_other_resource_id",
            "other_resource_id",
        ),
    )

    def __repr__(self):
        return f"<BibframeOtherResources {self.other_resource.uri or self.other_resource.id} for {self.bibframe_resource.uri}>"


@event.listens_for(OtherResource, "after_insert")
def create_version(mapper: Any, connection: Connection, target: OtherResource):
    """
    Creates a Version
    BibframeOtherResources are updated by bluecore_graph.py::_link
    """
    add_version(connection, target)


@event.listens_for(OtherResource, "after_update")
def update_version(mapper: Any, connection: Connection, target: OtherResource):
    """
    Updates a Version
    BibframeOtherResources are updated by bluecore_graph.py::_link
    """
    add_version(connection, target)
