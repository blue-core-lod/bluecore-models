from sqlalchemy import (
    ForeignKey,
    Integer,
)

from sqlalchemy.orm import (
    mapped_column,
    Mapped,
    relationship,
)

from bluecore.models.resource import ResourceBase

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