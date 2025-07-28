from sqlalchemy import (
    event,
    ForeignKey,
    Integer,
)

from sqlalchemy.orm import (
    mapped_column,
    Mapped,
)

from bluecore_models.models.resource import ResourceBase
from bluecore_models.utils.db import (
    add_bf_classes,
    add_version,
    update_bf_classes,
    set_jsonld,
)


class Work(ResourceBase):
    __tablename__ = "works"
    id: Mapped[int] = mapped_column(
        Integer, ForeignKey("resource_base.id"), primary_key=True
    )

    __mapper_args__ = {
        "polymorphic_identity": "works",
    }

    def __repr__(self):
        return f"<Work {self.uri}>"


event.listen(Work.data, "set", set_jsonld, retval=True)


@event.listens_for(Work, "after_insert")
def create_version_bf_classes(mapper, connection, target):
    """
    Creates a Version and associated Bibframe Classes
    """
    add_version(connection, target)
    add_bf_classes(connection, target)


@event.listens_for(Work, "after_update")
def update_version_bf_classes(mapper, connection, target):
    """
    Updates a Version and associated Bibframe Classes
    """
    add_version(connection, target)
    update_bf_classes(connection, target)
