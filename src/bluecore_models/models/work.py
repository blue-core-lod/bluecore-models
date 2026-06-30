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

from bluecore_models.models.hub import Hub
from bluecore_models.models.resource import ResourceBase
from bluecore_models.utils.db import (
    add_bf_classes,
    add_version,
    update_bf_classes,
)


class Work(ResourceBase):
    __tablename__ = "works"

    id: Mapped[int] = mapped_column(
        Integer, ForeignKey("resource_base.id"), primary_key=True
    )
    hub_id: Mapped[int] = mapped_column(Integer, ForeignKey("hubs.id"), nullable=True)
    hub: Mapped["Hub"] = relationship(
        "Hub", foreign_keys=hub_id, back_populates="works"
    )
    instances: Mapped[list["Instance"]] = relationship(  # type: ignore  # noqa: F821
        "Instance", primaryjoin="Work.id == Instance.work_id", back_populates="work"
    )

    __mapper_args__ = {
        "polymorphic_identity": "works",
    }

    def __repr__(self):
        return f"<Work {self.uri}>"


@event.listens_for(Work, "after_insert")
def create_version_bf_classes(mapper: Any, connection: Connection, target: Work):
    """
    Creates a Version and associated Bibframe Classes
    """
    add_version(connection, target)
    add_bf_classes(connection, target)


@event.listens_for(Work, "after_update")
def update_version_bf_classes(mapper: Any, connection: Connection, target: Work):
    """
    Updates a Version and associated Bibframe Classes
    """
    add_version(connection, target)
    update_bf_classes(connection, target)
