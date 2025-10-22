from datetime import datetime

from sqlalchemy import Boolean, DateTime, Integer, ForeignKey

from sqlalchemy.orm import (
    mapped_column,
    Mapped,
    relationship,
)

from bluecore_models.models.base import Base
from bluecore_models.models.resource import ResourceBase


class OtherResource(ResourceBase):
    """
    Stores JSON or JSON-LD resources used to support Work and Instances.
    """

    __tablename__ = "other_resources"
    id: Mapped[int] = mapped_column(
        Integer, ForeignKey("resource_base.id"), primary_key=True
    )
    is_profile: Mapped[bool] = mapped_column(Boolean, default=False)

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
        "ResourceBase", backref="other_resources"
    )

    created_at = mapped_column(DateTime, default=datetime.utcnow)
    updated_at = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )

    def __repr__(self):
        return f"<BibframeOtherResources {self.other_resource.uri or self.other_resource.id} for {self.bibframe_resource.uri}>"
