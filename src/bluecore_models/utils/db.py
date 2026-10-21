import rdflib
from sqlalchemy import delete, insert, inspect, select

from bluecore_models.models.bf_classes import BibframeClass, ResourceBibframeClass
from bluecore_models.models.instance_export import InstanceExport
from bluecore_models.models.version import (
    CURRENT_GROUP,
    CURRENT_USER_ID,
    CURRENT_USERNAME,
    Version,
)
from bluecore_models.utils.graph import get_bf_classes


def _new_bf_classs(connection, bf_class: rdflib.URIRef) -> int:
    stmt = insert(BibframeClass.__table__).values(
        name=bf_class.split("/")[-1], uri=str(bf_class)
    )
    result = connection.execute(stmt)
    return result.inserted_primary_key[0]


def add_bf_classes(connection, resource):
    """Add Bibframe classes to a resource"""
    bf_classes = get_bf_classes(resource.data, resource.uri)
    for class_ in bf_classes:
        stmt = select(BibframeClass.__table__).where(BibframeClass.uri == str(class_))
        result = connection.execute(stmt)
        bf_class_id = result.scalar()
        if not bf_class_id:
            bf_class_id = _new_bf_classs(connection, class_)
        stmt = insert(ResourceBibframeClass.__table__).values(
            bf_class_id=bf_class_id, resource_id=resource.id
        )
        connection.execute(stmt)


def add_version(connection, resource):
    """
    Adds a Version when the resource's JSON-LD data was inserted or changed.

    The check is on the data attribute specifically, rather than on the whole
    object. ResourceBase subclasses keep their foreign keys on their own tables
    (Instance.work_id, Work.hub_id), so a save that only links resources
    together fires after_update with one of those columns dirty while
    resource_base itself is untouched. A whole-object check counts that as a
    modification and writes a version recording no change -- and since
    resource_base was not written, ResourceBase.updated_at's onupdate never
    fired either, so that version also duplicates the previous one's
    created_at, which is the identifier the versions API hands out.

    keycloak_user_id is the JWT 'sub' claim, an opaque GUID; keycloak_username
    is the 'preferred_username' claim, which is what sinopia-editor shows to
    catalogers. Both are read from context vars set by the HTTP layer and are
    left NULL for non-HTTP writers such as batch ingest and migrations.
    """
    uid = CURRENT_USER_ID.get()
    username = CURRENT_USERNAME.get()

    if inspect(resource).attrs.data.history.has_changes():
        stmt = insert(Version.__table__).values(
            resource_id=resource.id,
            data=resource.data,
            keycloak_user_id=uid,
            keycloak_username=username,
            created_at=resource.updated_at,
        )
        connection.execute(stmt)


def update_bf_classes(connection, resource):
    """Update Bibframe classes for a resource"""
    bf_classes = get_bf_classes(resource.data, resource.uri)
    latest_bf_classes = {str(bf_class) for bf_class in bf_classes}
    stmt = (
        select(BibframeClass.__table__.columns.uri)
        .where(BibframeClass.id == ResourceBibframeClass.bf_class_id)
        .where(ResourceBibframeClass.resource_id == resource.id)
    )
    result = connection.execute(stmt)
    existing_bf_classes = set(result.scalars())
    removed_classes = existing_bf_classes - latest_bf_classes
    added_classes = latest_bf_classes - existing_bf_classes
    for bf_class in removed_classes:
        stmt = select(BibframeClass.__table__).where(BibframeClass.uri == bf_class)
        result = connection.execute(stmt)
        bf_class_id = result.scalar()
        stmt = (
            delete(ResourceBibframeClass.__table__)
            .where(ResourceBibframeClass.bf_class_id == bf_class_id)
            .where(ResourceBibframeClass.resource_id == resource.id)
        )
        connection.execute(stmt)
    for bf_class in added_classes:
        stmt = select(BibframeClass.__table__).where(BibframeClass.uri == bf_class)
        result = connection.execute(stmt)
        bf_class_id = result.scalar()
        if not bf_class_id:
            bf_class_id = _new_bf_classs(connection, bf_class)
        stmt = insert(ResourceBibframeClass.__table__).values(
            bf_class_id=bf_class_id, resource_id=resource.id
        )
        connection.execute(stmt)


def add_instance_export(
    session,
    instance,
    *,
    target_identifier: str | None = None,
    version: Version | None = None,
    keycloak_user_id: str | None = None,
    keycloak_username: str | None = None,
    keycloak_group: str | None = None,
) -> InstanceExport:
    """
    Records that an Instance was exported to a target system such as FOLIO or
    Alma, and returns the new InstanceExport. The caller commits.

    The user, username and group are read from the context vars the HTTP layer
    sets from the token; the keyword arguments override them, for non-HTTP
    callers such as bluecore-workflows. version defaults to the Instance's
    latest Version.

    Unlike add_version(), which leaves the user NULL for batch writers, this
    raises ValueError when the user id or group can't be determined, since an
    anonymous export isn't a useful audit record.
    """
    uid = keycloak_user_id or CURRENT_USER_ID.get()
    username = keycloak_username or CURRENT_USERNAME.get()
    group = keycloak_group or CURRENT_GROUP.get()
    if not uid:
        raise ValueError("Cannot record an Instance export without a Keycloak user id")
    if not group:
        raise ValueError("Cannot record an Instance export without a Keycloak group")

    if version is None:
        # The Instance needs its id, and its Version from the after_insert hook
        session.flush()
        version = session.scalars(
            select(Version)
            .where(Version.resource_id == instance.id)
            .order_by(Version.id.desc())
            .limit(1)
        ).first()

    export = InstanceExport(
        instance=instance,
        instance_uri=instance.uri,
        version=version,
        keycloak_user_id=uid,
        keycloak_username=username,
        keycloak_group=group,
        target_identifier=target_identifier,
    )
    session.add(export)
    return export
