from sqlalchemy import (
    event,
    insert,
    ForeignKey,
    Integer,
)

from sqlalchemy.orm import (
    mapped_column,
    Mapped,
)

from bluecore_models.models.resource import ResourceBase
from bluecore_models.models.version import Version
from bluecore_models.utils.db import add_bf_classes, update_bf_classes


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


@event.listens_for(Work, "after_insert")
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


@event.listens_for(Work, "after_update")
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
