from datetime import UTC, datetime
from typing import Optional

from sqlalchemy import Computed, DateTime, String, Uuid, event, text
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR
from sqlalchemy.orm import Mapped, mapped_column

from bluecore_models.models.base import Base
from bluecore_models.utils.graph import frame_jsonld


class ResourceBase(Base):
    __tablename__ = "resource_base"

    id: Mapped[int] = mapped_column(primary_key=True)
    type: Mapped[str] = mapped_column(String, nullable=False)
    data: Mapped[bytes] = mapped_column(JSONB, nullable=False)
    uuid: Mapped[Uuid] = mapped_column(Uuid, nullable=True, unique=True, index=True)
    uri: Mapped[str] = mapped_column(String, nullable=True, unique=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )
    data_vector: Mapped[bytes] = mapped_column(
        TSVECTOR, Computed(text("to_tsvector('english', data)"))
    )

    __mapper_args__ = {
        "polymorphic_on": type,
        "polymorphic_identity": "resource_base",
    }


# ==============================================================================
# Ensure created_at and updated_at are exactly the same when inserting.
# (if created_at time not present)
# ------------------------------------------------------------------------------
@event.listens_for(ResourceBase, "before_insert", propagate=True)
def set_created_and_updated(mapper, connection, target):
    now = datetime.now(UTC)
    if not target.created_at:
        target.created_at = now
    if not target.updated_at:
        target.updated_at = now


def set_jsonld(target, value, oldvalue, initiator) -> Optional[dict]:
    """
    An ORM event handler that ensures JSON-LD data is framed prior to persisting it
    to the database. Note the ordering of properties used in constructors
    matters, since target.uri must be set on the object prior to setting data.

    Also, if it is an OtherResource that has is_profile set to True, the data
    will not be framed, since it is not JSON-LD and has no uri.

    So this will work:

        >>> w = Work(uri="https://example.com", data={ ... })

    but this will not:

        >>> w = Work(data={...}, uri="https://example.com")

    """
    if hasattr(target, "is_profile") and target.is_profile is True:
        return value
    elif target.uri is None and value is not None:
        raise ValueError(
            "For automatic jsonld framing to work you must ensure the uri property is set before the data property, even when constructing an object."
        )
    elif value is not None:
        return frame_jsonld(target.uri, value)
    else:
        return None


# propagate=True lets this event fire for Work, Instance and OtherResource types
event.listen(ResourceBase.data, "set", set_jsonld, retval=True, propagate=True)
