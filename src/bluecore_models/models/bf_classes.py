from datetime import datetime

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
)
from sqlalchemy.orm import (
    Mapped,
    mapped_column,
    relationship,
)

from bluecore_models.models.base import Base
from bluecore_models.models.resource import ResourceBase


class BibframeClass(Base):
    __tablename__ = "bibframe_classes"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String, nullable=False)
    uri: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    created_at = mapped_column(DateTime, default=datetime.utcnow)
    updated_at = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )

    def __repr__(self):
        return f"<BibframeClass {self.name}>"


class ResourceBibframeClass(Base):
    __tablename__ = "resource_bibframe_classes"

    id: Mapped[int] = mapped_column(primary_key=True)
    bf_class_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("bibframe_classes.id"), nullable=False
    )
    bf_class: Mapped[BibframeClass] = relationship("BibframeClass")
    resource_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("resource_base.id"), nullable=False
    )
    resource: Mapped[ResourceBase] = relationship("ResourceBase", backref="classes")

    __table_args__ = (
        # update_bf_classes looks up a resource's classes by resource_id on every
        # update, and deleting from resource_base checks this foreign key.
        # Without the index both scan the whole table.
        Index("index_resource_bibframe_classes_on_resource_id", resource_id),
    )

    def __repr__(self):
        return f"<ResourceBibframeClass {self.bf_class.name} for {self.resource.uri}>"
