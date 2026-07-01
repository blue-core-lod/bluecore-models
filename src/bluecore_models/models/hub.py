"""Module for BIBFRAME Hubs"""

from typing import Any

from sqlalchemy import (
    Connection,
    ForeignKey,
    Integer,
    event,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from bluecore_models.models.resource import ResourceBase
from bluecore_models.utils.db import (
    add_bf_classes,
    add_version,
    update_bf_classes,
)


class Hub(ResourceBase):
    __tablename__ = "hubs"

    id: Mapped[int] = mapped_column(
        Integer, ForeignKey("resource_base.id"), primary_key=True
    )
    works: Mapped[list["Work"]] = relationship(  # type: ignore  # noqa: F821
        "Work", primaryjoin="Hub.id == Work.hub_id", back_populates="hub"
    )

    __mapper_args__ = {
        "polymorphic_identity": "hubs",
    }

    def __repr__(self):
        return f"<Hub {self.uri}>"


@event.listens_for(Hub, "after_insert")
def create_version_bf_classes(mapper: Any, connection: Connection, target: Hub):
    """
    Creates a Version and associated Bibframe Classes
    """
    add_version(connection, target)
    add_bf_classes(connection, target)


@event.listens_for(Hub, "after_update")
def update_version_bf_classes(mapper: Any, connection: Connection, target: Hub):
    """
    Updates a Version and associated Bibframe Classes
    """
    add_version(connection, target)
    update_bf_classes(connection, target)
