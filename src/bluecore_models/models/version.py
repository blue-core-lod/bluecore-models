from contextvars import ContextVar
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from bluecore_models.models.base import Base
from bluecore_models.models.resource import ResourceBase

CURRENT_USER_ID: ContextVar[str | None] = ContextVar("current_user_id", default=None)


class Version(Base):
    __tablename__ = "versions"

    id: Mapped[int] = mapped_column(primary_key=True)
    resource_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("resource_base.id"), nullable=False, index=True
    )
    resource: Mapped[ResourceBase] = relationship(
        "ResourceBase", back_populates="versions"
    )
    data: Mapped[bytes] = mapped_column(JSONB, nullable=False)
    keycloak_user_id: Mapped[str | None] = mapped_column(
        String(128), index=True, nullable=True
    )
    created_at = mapped_column(DateTime, default=datetime.utcnow)

    def __repr__(self):
        who = getattr(self, "keycloak_user_id", None) or "unknown"
        return f"<Version at {self.created_at} by {who} for {self.resource.uri}>"
