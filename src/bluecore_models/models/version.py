from datetime import datetime

from sqlalchemy import DateTime, Integer, ForeignKey, String
from sqlalchemy.orm import mapped_column, Mapped, relationship
from sqlalchemy.dialects.postgresql import JSONB

from bluecore_models.models.base import Base
from bluecore_models.models.resource import ResourceBase

from contextvars import ContextVar

CURRENT_USER_ID: ContextVar[str | None] = ContextVar("current_user_id", default=None)


class Version(Base):
    __tablename__ = "versions"

    id: Mapped[int] = mapped_column(primary_key=True)
    resource_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("resource_base.id"), nullable=False
    )
    resource: Mapped[ResourceBase] = relationship("ResourceBase", backref="versions")
    data: Mapped[bytes] = mapped_column(JSONB, nullable=False)
    keycloak_user_id: Mapped[str | None] = mapped_column(
        String(128), index=True, nullable=True
    )
    created_at = mapped_column(DateTime, default=datetime.utcnow)

    def __repr__(self):
        who = getattr(self, "keycloak_user_id", None) or "unknown"
        return f"<Version at {self.created_at} by {who} for {self.resource.uri}>"
