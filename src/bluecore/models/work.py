from sqlalchemy import (
    ForeignKey,
    Integer,
)

from sqlalchemy.orm import (
    mapped_column,
    Mapped,
)

from bluecore.models.resource import ResourceBase


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