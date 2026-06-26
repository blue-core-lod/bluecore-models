from sqlalchemy import ForeignKey, Integer, event
from sqlalchemy.orm import Mapped, mapped_column

from bluecore_models.models.resource import ResourceBase
from bluecore_models.utils.db import add_version


class Template(ResourceBase):
    """
    Stores resource templates (e.g. Sinopia profiles) used to drive editing.

    A Template is a first-class Bluecore resource: like Works, Instances and
    Hubs it is assigned a ``uuid`` and a minted ``uri`` (``.../templates/{uuid}``).
    Unlike them, its ``data`` is plain JSON rather than JSON-LD, so it is not
    framed when persisted (the set_jsonld handler in resource.py skips Templates).
    """

    __tablename__ = "templates"
    id: Mapped[int] = mapped_column(
        Integer, ForeignKey("resource_base.id"), primary_key=True
    )

    __mapper_args__ = {
        "polymorphic_identity": "templates",
    }

    def __repr__(self):
        return f"<Template {self.uri or self.id}>"


@event.listens_for(Template, "after_insert")
def create_version(mapper, connection, target):
    """Record a Version when a Template is created."""
    add_version(connection, target)


@event.listens_for(Template, "after_update")
def update_version(mapper, connection, target):
    """Record a Version when a Template is modified."""
    add_version(connection, target)
