"""Module for BIBFRAME Instances"""

from sqlalchemy import (
    event,
    ForeignKey,
    Integer,
)

from sqlalchemy.orm import (
    mapped_column,
    Mapped,
    relationship,
)

from bluecore_models.models.resource import ResourceBase
from bluecore_models.models.work import Work
from bluecore_models.utils.db import (
    add_bf_classes,
    add_version,
    update_bf_classes,
    set_jsonld,
)


class Instance(ResourceBase):
    __tablename__ = "instances"

    id: Mapped[int] = mapped_column(
        Integer, ForeignKey("resource_base.id"), primary_key=True
    )
    work_id: Mapped[int] = mapped_column(Integer, ForeignKey("works.id"), nullable=True)
    work: Mapped["Work"] = relationship(
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
    add_version(connection, target)
    add_bf_classes(connection, target)


@event.listens_for(Instance, "after_update")
def update_version_bf_classes(mapper, connection, target):
    """
    Updates a Version and associated Bibframe Classes
    """
    add_version(connection, target)
    update_bf_classes(connection, target)


event.listen(Instance.data, "set", set_jsonld, retval=True)
