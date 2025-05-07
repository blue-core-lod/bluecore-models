from datetime import datetime, UTC
from sqlalchemy import DateTime, String
from sqlalchemy.orm import mapped_column, Mapped
from sqlalchemy.dialects.postgresql import JSONB
from bluecore_models.models.base import Base


class ResourceBase(Base):
    __tablename__ = "resource_base"

    id: Mapped[int] = mapped_column(primary_key=True)
    type: Mapped[str] = mapped_column(String, nullable=False)
    data: Mapped[bytes] = mapped_column(JSONB, nullable=False)
    uri: Mapped[str] = mapped_column(String, nullable=True, unique=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    __mapper_args__ = {
        "polymorphic_on": type,
        "polymorphic_identity": "resource_base",
    }
