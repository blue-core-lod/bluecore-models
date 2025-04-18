from datetime import datetime

from sqlalchemy import DateTime, Integer, ForeignKey

from sqlalchemy.orm import (
    mapped_column,
    Mapped,
    relationship,
)

from sqlalchemy.dialects.postgresql import JSONB

from bluecore_models.models.base import Base
from bluecore_models.models.resource import ResourceBase


class Version(Base):
    __tablename__ = "versions"

    id: Mapped[int] = mapped_column(primary_key=True)
    resource_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("resource_base.id"), nullable=False
    )
    resource: Mapped[ResourceBase] = relationship("ResourceBase", backref="versions")
    data: Mapped[bytes] = mapped_column(JSONB, nullable=False)
    created_at = mapped_column(DateTime, default=datetime.utcnow)

    def __repr__(self):
        return f"<Version at {self.created_at} for {self.resource.uri}>"
