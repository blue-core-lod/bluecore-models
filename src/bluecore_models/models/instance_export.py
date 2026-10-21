"""Module for recording exports of BIBFRAME Instances to other systems"""

from datetime import UTC, datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from bluecore_models.models.base import Base


class InstanceExport(Base):
    """
    One export of an Instance to a target system such as FOLIO or Alma: who
    exported it, on behalf of which Keycloak group, when, and the identifier
    the record received there.

    Rows are written explicitly with add_instance_export() in utils/db.py, not
    by an ORM event, since an export doesn't change the Instance's data.

    These rows are an audit trail, so they outlive what they describe: both
    foreign keys are ON DELETE SET NULL, and instance_uri keeps a copy of the
    Instance's URI to identify it after it has been deleted.
    """

    __tablename__ = "instance_exports"

    __table_args__ = (
        Index(
            "index_instance_exports_on_instance_id_exported_at",
            "instance_id",
            "exported_at",
        ),
        Index("index_instance_exports_on_instance_uri", "instance_uri"),
        Index(
            "index_instance_exports_on_keycloak_group_exported_at",
            "keycloak_group",
            "exported_at",
        ),
        Index("index_instance_exports_on_keycloak_user_id", "keycloak_user_id"),
        Index("index_instance_exports_on_target_identifier", "target_identifier"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    instance_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("instances.id", ondelete="SET NULL"), nullable=True
    )
    instance: Mapped["Instance | None"] = relationship(  # type: ignore  # noqa: F821
        "Instance", back_populates="exports"
    )
    instance_uri: Mapped[str] = mapped_column(String, nullable=False)
    # The exact Version exported, so we know what the record looked like then
    version_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("versions.id", ondelete="SET NULL"), nullable=True
    )
    version: Mapped["Version | None"] = relationship("Version")  # type: ignore  # noqa: F821
    # Same claims and widths as Version.keycloak_user_id / keycloak_username
    keycloak_user_id: Mapped[str] = mapped_column(String(128), nullable=False)
    keycloak_username: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # The user's Keycloak group path, e.g. "/stanford"
    keycloak_group: Mapped[str] = mapped_column(String(255), nullable=False)
    # e.g. a FOLIO instance HRID or UUID, or an Alma MMS ID; may not be known
    # at export time
    target_identifier: Mapped[str | None] = mapped_column(String(255), nullable=True)
    exported_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=lambda: datetime.now(UTC)
    )

    def __repr__(self):
        who = self.keycloak_username or self.keycloak_user_id or "unknown"
        return (
            f"<InstanceExport of {self.instance_uri} by {who} "
            f"({self.keycloak_group}) at {self.exported_at}>"
        )
