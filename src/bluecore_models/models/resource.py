from datetime import UTC, datetime
from typing import Any, ClassVar

from sqlalchemy import Computed, Connection, DateTime, Index, String, Uuid, event, text
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR
from sqlalchemy.orm import Mapped, mapped_column, relationship

from bluecore_models.models.base import Base
from bluecore_models.utils.graph import framed_for_storage, validate_jsonld


class ResourceBase(Base):
    __tablename__ = "resource_base"

    id: Mapped[int] = mapped_column(primary_key=True)
    type: Mapped[str] = mapped_column(String, nullable=False)
    data: Mapped[bytes] = mapped_column(JSONB, nullable=False)
    uuid: Mapped[Uuid] = mapped_column(Uuid, nullable=True, unique=True, index=True)
    uri: Mapped[str] = mapped_column(String, nullable=True, unique=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )
    other_resources: Mapped[list["BibframeOtherResources"]] = relationship(  # type: ignore  # noqa: F821
        "BibframeOtherResources", back_populates="bibframe_resource"
    )
    versions: Mapped[list["Version"]] = relationship(  # type: ignore  # noqa: F821
        "Version", back_populates="resource", order_by="Version.id"
    )
    """
    Boost mainTitle with highest ranking order at A, subtitle at B, and uri at C.
    Rest of the data is indexed without weights.
    All of them will be indexed with both 'simple' and 'english' configurations with unaccent.
    jsonb_to_tsv is a custom function that extracts text from jsonb and converts to tsvector - see pg_ext_func.py.
    bluecore_normalize handles symbols and romanization marks before unaccenting - see pg_ext_func.py.
    """
    data_vector: Mapped[bytes] = mapped_column(
        TSVECTOR,
        Computed(
            "setweight(jsonb_to_tsv('simple', data->'title', 'mainTitle'), 'A') || "
            "setweight(jsonb_to_tsv('english', data->'title', 'mainTitle'), 'A') || "
            "setweight(jsonb_to_tsv('simple', data->'title', 'subtitle'), 'B') || "
            "setweight(jsonb_to_tsv('english', data->'title', 'subtitle'), 'B') || "
            "setweight(to_tsvector('simple', coalesce(uri, '')), 'C') || "
            "setweight(to_tsvector('english', coalesce(uri, '')), 'C') || "
            "to_tsvector('simple', bluecore_normalize(coalesce(data::text, ''))) || "
            "to_tsvector('english', bluecore_normalize(coalesce(data::text, '')))",
            persisted=True,
        ),
    )
    # This vector lets PostgreSQL search title values only
    title_vector: Mapped[bytes] = mapped_column(
        TSVECTOR,
        Computed(
            "setweight(bluecore_titles_to_tsv('simple', data->'title', 'mainTitle'), 'A') || "
            "setweight(bluecore_titles_to_tsv('english', data->'title', 'mainTitle'), 'A') || "
            "setweight(bluecore_titles_to_tsv('simple', data->'title', 'subtitle'), 'B') || "
            "setweight(bluecore_titles_to_tsv('english', data->'title', 'subtitle'), 'B')",
            persisted=True,
        ),
    )

    __mapper_args__: ClassVar[dict[str, Any]] = {
        "polymorphic_on": type,
        "polymorphic_identity": "resource_base",
    }

    __table_args__ = (
        # composite btree index supporting the dedup lookup in
        # BluecoreGraph._mint_all_uris, which filters on both the polymorphic type
        # discriminator and the bf:derivedFrom @id recorded in a resource's nested
        # adminMetadata. Leading with type, then the derivedFrom @id expression, lets
        # the planner satisfy both equality predicates from the index prefix instead
        # of scanning every row of a given type (which degrades linearly as the table
        # grows). The derivedFrom assertion lives on one of the adminMetadata array
        # elements (position not guaranteed), so we index jsonb_path_query_first(...),
        # which is IMMUTABLE and returns the single derivedFrom @id regardless of
        # position.
        Index(
            "index_resource_base_on_type_derivedfrom_id",
            "type",
            text(
                "(jsonb_path_query_first(data, '$.adminMetadata[*].derivedFrom.\"@id\"') #>> '{}')"
            ),
        ),
        Index(
            "index_resource_base_on_data_vector", data_vector, postgresql_using="gin"
        ),
        Index(
            "index_resource_base_on_title_vector", title_vector, postgresql_using="gin"
        ),
        Index("index_resource_base_on_uuid", uuid),
        Index("type_idx", type),
    )


# ==============================================================================
# Ensure created_at and updated_at are exactly the same when inserting.
# (if created_at time not present)
# ------------------------------------------------------------------------------
@event.listens_for(ResourceBase, "before_insert", propagate=True)
def set_created_and_updated(mapper: Any, connection: Connection, target: ResourceBase):
    now = datetime.now(UTC)
    if not target.created_at:
        target.created_at = now
    if not target.updated_at:
        target.updated_at = now


def set_jsonld(target, value, oldvalue, initiator) -> dict[str, Any] | None:
    """
    An ORM event handler that ensures JSON-LD data is framed prior to persisting it
    to the database. Note the ordering of properties used in constructors
    matters, since target.uri must be set on the object prior to setting data.

    So this will work:

        >>> w = Work(uri="https://example.com", data={ ... })

    but this will not:

        >>> w = Work(data={...}, uri="https://example.com")

    Also, if the target is a Profile the data will not be framed, since
    sinopia-editor expects it to be in a particular shape. Maybe someday
    we can frame it?!
    """
    # Local import to avoid a circular import: profile.py imports ResourceBase
    # from this module. The data setter only fires on a constructed instance, by
    # which point all model modules are loaded.
    from bluecore_models.models.profile import Profile

    if isinstance(target, Profile):
        return value
    elif target.uri is None and value is not None:
        raise ValueError(
            "For automatic jsonld framing to work you must ensure the uri property is set before the data property, even when constructing an object."
        )
    elif value is not None:
        doc = framed_for_storage(target.uri, value)
        # Monitoring, not a gate. A write that refused a non-conforming document
        # would refuse the record hardest to recover: the one framing has just
        # broken. Logged so the rate is visible instead of assumed.
        validate_jsonld(doc, target.uri)
        return doc
    else:
        return None


# propagate=True lets this event fire for Work, Instance and OtherResource types
event.listen(ResourceBase.data, "set", set_jsonld, retval=True, propagate=True)
