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
    created_at = mapped_column(DateTime())
    updated_at = mapped_column(DateTime())

    __mapper_args__ = {
        "polymorphic_on": type,
        "polymorphic_identity": "resource_base",
    }

    def __init__(self, **kwargs):
        time_now = datetime.now(UTC)
        if "created_at" not in kwargs:
            kwargs["created_at"] = time_now
        if "updated_at" not in kwargs:
            kwargs["updated_at"] = time_now
        super().__init__(**kwargs)
