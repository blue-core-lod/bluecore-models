from contextvars import ContextVar
from datetime import UTC, datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from bluecore_models.models.base import Base
from bluecore_models.models.resource import ResourceBase

CURRENT_USER_ID: ContextVar[str | None] = ContextVar("current_user_id", default=None)
CURRENT_USERNAME: ContextVar[str | None] = ContextVar("current_username", default=None)


class Version(Base):
    __tablename__ = "versions"

    __table_args__ = (
        # The versions API addresses a single version by its ISO created_at
        # value, so that pair has to identify at most one row. add_version()
        # sets created_at from resource.updated_at rather than now(), which is
        # what makes uniqueness a real constraint rather than a formality --
        # see the guard in add_version() for the case that used to violate it.
        #
        # Postgres backs this with a unique btree index on
        # (resource_id, created_at), which is also the index serving the
        # per-resource listing (WHERE resource_id = ? ORDER BY created_at), and
        # whose leading column covers resource_id-only lookups. So no separate
        # index is declared here, and the older single-column
        # index_versions_on_resource_id is dropped.
        UniqueConstraint(
            "resource_id", "created_at", name="uq_versions_resource_id_created_at"
        ),
        Index("index_versions_on_resource_id_id", "resource_id", "id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    resource_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("resource_base.id"), nullable=False
    )
    resource: Mapped[ResourceBase] = relationship(
        "ResourceBase", back_populates="versions"
    )
    data: Mapped[bytes] = mapped_column(JSONB, nullable=False)
    keycloak_user_id: Mapped[str | None] = mapped_column(
        String(128), index=True, nullable=True
    )
    keycloak_username: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime | None] = mapped_column(
        DateTime, default=lambda: datetime.now(UTC)
    )

    def __repr__(self):
        # Same precedence the versions API uses when rendering the author.
        who = (
            getattr(self, "keycloak_username", None)
            or getattr(self, "keycloak_user_id", None)
            or "unknown"
        )
        return f"<Version at {self.created_at} by {who} for {self.resource.uri}>"
