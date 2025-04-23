"""Module for BIBFRAME Instances"""

from sqlalchemy import (
    event,
    insert,
    ForeignKey,
    Integer,
)

from sqlalchemy.orm import (
    mapped_column,
    Mapped,
    relationship,
)

from bluecore_models.models.resource import ResourceBase
from bluecore_models.models.version import Version
from bluecore_models.utils.db import add_bf_classes, update_bf_classes


class Instance(ResourceBase):
    __tablename__ = "instances"

    id: Mapped[int] = mapped_column(
        Integer, ForeignKey("resource_base.id"), primary_key=True
    )
    work_id: Mapped[int] = mapped_column(Integer, ForeignKey("works.id"), nullable=True)
    work: Mapped["Work"] = relationship(  # noqa
        "Work", foreign_keys=work_id, backref="instances"
    )

    __mapper_args__ = {
        "polymorphic_identity": "instances",
    }

    def __repr__(self):
        return f"<Instance {self.uri}>"


@event.listens_for(Instance, "after_insert")
def create_version_bf_classes(mapper, connection, target):
    """
    Creates a Version and associated Bibframe Classes
    """
    stmt = insert(Version.__table__).values(
        resource_id=target.id,
        data=target.data,
        created_at=target.updated_at,
    )
    connection.execute(stmt)
    add_bf_classes(connection, target)


@event.listens_for(Instance, "after_update")
def update_version_bf_classes(mapper, connection, target):
    """
    Updates a Version and associated Bibframe Classes
    """
    stmt = insert(Version.__table__).values(
        resource_id=target.id,
        data=target.data,
        created_at=target.updated_at,
    )
    connection.execute(stmt)
    update_bf_classes(connection, target)
