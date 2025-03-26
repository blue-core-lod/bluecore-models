from datetime import datetime


from sqlalchemy import (
    DateTime,
    ForeignKey,
    Integer,
    String,
)

from sqlalchemy.orm import (
    mapped_column,
    Mapped,
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

    def __repr__(self):
        return f"<ResourceBibframeClass {self.bf_class.name} for {self.resource.uri}>"
