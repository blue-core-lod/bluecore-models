"""Module for BIBFRAME Instances"""

from typing import Any

from sqlalchemy import (
    Connection,
    ForeignKey,
    Integer,
    event,
)
from sqlalchemy.orm import (
    Mapped,
    mapped_column,
    relationship,
)

from bluecore_models.models.resource import ResourceBase
from bluecore_models.utils.db import (
    add_bf_classes,
    add_version,
    update_bf_classes,
)


class Instance(ResourceBase):
    __tablename__ = "instances"

    id: Mapped[int] = mapped_column(
        Integer, ForeignKey("resource_base.id"), primary_key=True
    )
    work_id: Mapped[int] = mapped_column(Integer, ForeignKey("works.id"), nullable=True)
    work: Mapped["Work"] = relationship(  # type: ignore  # noqa: F821
        "Work", foreign_keys=work_id, back_populates="instances"
    )
    __mapper_args__ = {
        "polymorphic_identity": "instances",
    }

    def __repr__(self):
        return f"<Instance {self.uri}>"


@event.listens_for(Instance, "after_insert")
def create_version_bf_classes(mapper: Any, connection: Connection, target: Instance):
    """
    Creates a Version and associated Bibframe Classes
    """
    add_version(connection, target)
    add_bf_classes(connection, target)


@event.listens_for(Instance, "after_update")
def update_version_bf_classes(mapper: Any, connection: Connection, target: Instance):
    """
    Updates a Version and associated Bibframe Classes
    """
    add_version(connection, target)
    update_bf_classes(connection, target)
