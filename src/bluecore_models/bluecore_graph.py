import copy
import datetime
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from typing import Any, Protocol
from uuid import uuid4

from psycopg import errors as psycopg_errors
from rdflib import XSD, BNode, Graph, IdentifiedNode, Literal, Namespace, Node, URIRef
from rdflib.plugins.stores.memory import Memory
from sqlalchemy import func, literal_column
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Query
from sqlalchemy.orm.session import Session, sessionmaker
from tenacity import Retrying, retry_if_exception, stop_after_attempt

from bluecore_models.models import (
    BibframeOtherResources,
    Hub,
    Instance,
    OtherResource,
    Work,
)
from bluecore_models.models.version import CURRENT_USER_ID
from bluecore_models.namespaces import BF, BFLC, MADS, RDF, RDFS
from bluecore_models.utils.graph import (
    NEW_RESOURCE,
    generate_entity_graph,
    remove_bnode,
    replace_uri,
    strip_duplicate_bnode_values,
)

logger = logging.getLogger(__name__)

# - DeadlockDetected / SerializationFailure: two writers locked shared rows in a
#   conflicting way; Postgres aborted one of them.
# - UniqueViolation: two writers both did get-or-create on the same brand-new
#   shared Other Resource uri and both tried to INSERT it. On a retry the SELECT
#   finds the row the winner committed and takes the update/no-op path instead.
RETRYABLE_PG_ERRORS = (
    psycopg_errors.DeadlockDetected,
    psycopg_errors.SerializationFailure,
    psycopg_errors.UniqueViolation,
)

# How many times to attempt save() before giving up on serialization failures.
SAVE_MAX_ATTEMPTS = 3

# Default AdminMetadata values used when a caller doesn't override them.
DEFAULT_STATUS = URIRef("http://id.loc.gov/vocabulary/mstatus/c")
DEFAULT_AGENT = URIRef("http://id.loc.gov/vocabulary/organizations/cbc")
DEFAULT_DESC_AUTH = URIRef("http://id.loc.gov/vocabulary/marcauthen/pcc")
DEFAULT_DESC_LANG = URIRef("http://id.loc.gov/vocabulary/languages/eng")
DEFAULT_DESC_LEVEL = URIRef("http://id.loc.gov/ontologies/bibframe-2-6-0/")

# Recorded as the status of a resource we only have a stub of, so a placeholder
# isn't taken for a real description. mstatus/incmp is "incomplete".
#
# Note that this term is also what marc2bibframe2 emits from MARC 505 ind1=1,
# onto a Work's bf:TableOfContents, to mean the contents note lists only some of
# the parts. That is a different statement in a different position, so anything
# looking for our marker has to check it is on adminMetadata rather than just
# searching the graph for the term -- see _marked_stub and _clear_stub_marker.
STUB_STATUS = URIRef("http://id.loc.gov/vocabulary/mstatus/incmp")

# The kinds of resource that get a record of their own. A blank node of one of
# these only gets one if it is the resource being created. See _find_roots.
RECORD_CLASSES = (BF.Work, BF.Instance, BF.Hub)

# Predicates whose object is a Work, Instance, Hub or Item record rather than an
# Other Resource. A uri cited through one of these, and not described in the
# payload, is never linked as an Other Resource on the strength of a held row:
# that row may be a record we once stored without its bf type. See _link.
RECORD_PREDICATES = (
    BF.instanceOf,
    BF.hasInstance,
    BF.expressionOf,
    BF.hasExpression,
    BF.relatedTo,
    BF.hasItem,
    BF.itemOf,
    BF.associatedResource,
    BFLC.associatedResource,
)

# LC's namespace for Works, Instances, Hubs and Items, which are never Other
# Resources however they are cited.
LC_RESOURCES = "http://id.loc.gov/resources/"

# How many uris go into one IN clause when looking up held Other Resources or
# stored identities, well under psycopg's 65535 bind-parameter limit.
HELD_LOOKUP_CHUNK = 5000


class BluecoreGraphError(Exception):
    """Raised for BluecoreGraph errors that aren't a type mismatch."""


# (predicate, rdf:type) pairs identifying blank nodes that are stripped out of
# a resource's graph before it is persisted to the database, e.g. removing
# OCLC number identifiers that shouldn't be exposed.
EXCLUDED_TRIPLE_TYPES = [
    (BF.identifiedBy, BF.OclcNumber),
]


class Kind(Enum):
    """The kinds of resource a save persists, each in a table of its own."""

    HUB = "hub"
    WORK = "work"
    INSTANCE = "instance"
    OTHER = "other"

    @property
    def bf_class(self) -> URIRef | None:
        """The BIBFRAME class of the kind; Other Resources have none."""
        return _BF_CLASSES[self]


_BF_CLASSES: dict[Kind, URIRef | None] = {
    Kind.HUB: BF.Hub,
    Kind.WORK: BF.Work,
    Kind.INSTANCE: BF.Instance,
    Kind.OTHER: None,
}


@dataclass(frozen=True)
class Ingest:
    """
    A save that names no kind, as the record loader's do: every resource is
    authoritative, except that update_others=False makes Other Resources
    references (created if absent, never overwritten).
    """

    update_others: bool

    def is_primary(self, kind: Kind) -> bool:
        return kind != Kind.OTHER or self.update_others


@dataclass(frozen=True)
class Write:
    """
    A save that names the kind of resource it is writing, as the API's do: only
    that kind is authoritative, and every other resource is a reference. kind is
    None when the caller's primary_class names no kind we store, so every
    resource is a reference.
    """

    kind: Kind | None

    def is_primary(self, kind: Kind) -> bool:
        return kind == self.kind


Mode = Ingest | Write


def _mode(primary_class: object, update_other_resources: bool) -> Mode:
    """
    The mode of a save, from save()'s primary_class and update_other_resources.
    This is the only place primary_class is interpreted. It is compared, never
    converted: a str equal to a class URI is no kind, as a URIRef never equals a
    str.
    """
    if primary_class is None:
        return Ingest(update_others=update_other_resources)
    if primary_class == OtherResource:
        return Write(Kind.OTHER)
    for kind in (Kind.HUB, Kind.WORK, Kind.INSTANCE):
        if primary_class == kind.bf_class:
            return Write(kind)
    return Write(None)


class Role(Enum):
    """What a save does with a resource, decided from the payload and the mode."""

    PRIMARY = "primary"  # authoritatively upserted
    REFERENCE = "reference"  # created if absent; keeps its existing links
    STUB = "stub"  # placeholder; replaced by a real description
    INLINE = "inline"  # anonymous; stays in its parent's record


# The role of each resource, keyed on what it was called before minting and on
# its kind: a URI typed both Work and Instance is two resources, and a save
# writing one of those kinds treats the other as a reference.
Roles = dict[tuple[IdentifiedNode, Kind], Role]


def _classify(
    graph: Graph,
    mode: Mode | None,
    roots: set[BNode],
    root: BNode | None,
    described: set[str],
    arrived_as: dict[str, IdentifiedNode] | None = None,
) -> Roles:
    """
    The role of every Hub, Work, Instance and Other Resource in graph, before
    minting. mode is None before save() has set one, when the accessors read the
    graph as it is. arrived_as maps the uris an earlier save of the same graph
    minted back to what they were called when they arrived.

    A blank node has no identity of its own, so it stays inline in whatever
    describes it, except the one resource an API save is creating (root) or,
    before a save, any root (see BluecoreGraph._find_roots). Every blank node of
    a bulk load is inline: LC catalog data routinely states bf:expressionOf
    against an anonymous bf:Hub and nests an anonymous bf:Work under
    bf:instanceOf, and minting a URI for one of those invents an identity nothing
    can ever match again.

    A bulk-loaded record describes a few resources and carries only a stub of the
    ones it points at: described holds the ones with adminMetadata of their own
    (see BluecoreGraph._prepare). When the record uses adminMetadata at all, any
    other Hub, Work or Instance in it is a stub, judged by the name it arrived
    with.
    """
    arrived_as = arrived_as or {}
    records = {
        (s, kind)
        for kind in (Kind.HUB, Kind.WORK, Kind.INSTANCE)
        for s in graph.subjects(RDF.type, kind.bf_class)
        # LC types Hubs as both bf:Hub and bf:Work; they are Hubs
        if not (kind == Kind.WORK and (s, RDF.type, BF.Hub) in graph)
        and isinstance(s, IdentifiedNode)
    }
    others = {
        (s, Kind.OTHER)
        for s in set(graph.subjects())
        if isinstance(s, URIRef)
        and not any((s, RDF.type, class_) in graph for class_ in RECORD_CLASSES)
    }

    def role(node: IdentifiedNode, kind: Kind) -> Role:
        if isinstance(node, BNode):
            if isinstance(mode, Ingest):
                return Role.INLINE
            if isinstance(mode, Write) and node != root:
                return Role.INLINE
            if mode is None and node not in roots:
                return Role.INLINE
        if (
            kind != Kind.OTHER
            and isinstance(mode, Ingest)
            and described
            and str(arrived_as.get(str(node), node)) not in described
        ):
            return Role.STUB
        return _role_by_mode(mode, kind)

    return {(node, kind): role(node, kind) for node, kind in records | others}


def _role_by_mode(mode: Mode | None, kind: Kind) -> Role:
    """
    The role of a resource of kind with nothing else to decide it. It is all an
    Other Resource's role depends on, so it also covers the ones the save
    describes itself (the stub status, see _describe_stub_status).
    """
    if mode is None or mode.is_primary(kind):
        return Role.PRIMARY
    return Role.REFERENCE


_SQLA_CLASSES: dict[
    Kind, type[Hub] | type[Work] | type[Instance] | type[OtherResource]
] = {
    Kind.HUB: Hub,
    Kind.WORK: Work,
    Kind.INSTANCE: Instance,
    Kind.OTHER: OtherResource,
}

# Where a minted Bluecore URI puts each kind. Other Resources keep their own URI.
_COLLECTIONS = {
    Kind.HUB: "hubs",
    Kind.WORK: "works",
    Kind.INSTANCE: "instances",
}


@dataclass(frozen=True)
class Entry:
    """
    One resource a save writes: its subject, its kind, its role, its subgraph,
    the subject it arrived with (before minting gave it a Bluecore URI), whether
    minting switched it (onto a new uri, a stored one, or, for a resource whose
    adminMetadata says it is derived from itself, its own), and the uris it
    cites that could be Other Resources (see _cited_uris; empty for an Other
    Resource, which links nothing). save() builds its entries once minting is
    done, when the graph has stopped changing; the accessors (works() and so on)
    build fresh ones from the graph as it is.
    """

    subject: IdentifiedNode
    kind: Kind
    role: Role
    graph: Graph
    original: IdentifiedNode
    minted: bool
    cited: frozenset[URIRef]


class Action(Enum):
    """What a save does to a resource's row, decided from its role and the row."""

    INSERT = "insert"
    UPDATE = "update"
    KEEP = "keep"


class Stored(Protocol):
    """What _action reads of the row we hold for a resource: its data."""

    @property
    def data(self) -> Any: ...


def _action(entry: Entry, existing: Stored | None, mode: Mode) -> Action:
    """
    What the save does to the row of entry, given the row we hold for it, if
    any. Something absent is created whatever its role, from whatever the
    payload knows about it. A reference or a stub never overwrites a stored
    description, which may be a full one.

    And an ingest leaves a resource alone when an outside record comes round
    again for something we already describe in full, since it has nothing to
    add and would wipe out whatever has been edited since. That's a resource
    minted from an external URI; one that arrives with its Bluecore URI (a
    Marva or API save) is ours, so it writes. A stored stub is only a
    placeholder, so a real description always replaces it.
    """
    if existing is None:
        return Action.INSERT
    if entry.role in (Role.REFERENCE, Role.STUB):
        return Action.KEEP
    if (
        entry.kind != Kind.OTHER
        and isinstance(mode, Ingest)
        and entry.minted
        and not _stored_is_stub(existing)
    ):
        return Action.KEEP
    return Action.UPDATE


def _stored_is_stub(existing: Stored) -> bool:
    """Whether what we hold is still a placeholder rather than a description."""
    data = existing.data
    admin_metadata = data.get("adminMetadata", []) if data else []
    if isinstance(admin_metadata, dict):
        admin_metadata = [admin_metadata]
    for block in admin_metadata:
        if not isinstance(block, dict):
            continue
        status = block.get("status")
        for item in status if isinstance(status, list) else [status]:
            if isinstance(item, dict) and item.get("@id") == str(STUB_STATUS):
                return True
    return False


class Citation(Enum):
    """What a uri a resource cites is, which decides whether it is stored and linked."""

    VOCABULARY = "vocabulary"  # a BIBFRAME, MADS or RDF term
    RECORD = "record"  # a Work, Instance or Hub in the payload, its own record
    ITEM = "item"  # an Item: stored if described, never linked (inherited)
    EXCLUDED = "excluded"  # a record's uri: stored if described, never linked
    ADMIN_ONLY = "admin only"  # under bf:adminMetadata: linked only if described
    OTHER = "other"  # an Other Resource: linked if described or held

    @property
    def stored(self) -> bool:
        """Whether the save stores a description of it as an Other Resource."""
        return self not in (Citation.VOCABULARY, Citation.RECORD)


def _admin_nodes(g: Graph) -> set[Node]:
    """The blank nodes under a resource's bf:adminMetadata, nested ones included."""
    admin_nodes: set[Node] = set()
    queue = list(g.objects(predicate=BF.adminMetadata))
    while queue:
        node = queue.pop()
        if node in admin_nodes:
            continue
        admin_nodes.add(node)
        queue.extend(o for o in g.objects(node) if isinstance(o, BNode))
    return admin_nodes


def _citation(
    graph: Graph, s: Node, p: URIRef, o: URIRef, admin_nodes: set[Node]
) -> Citation:
    """
    What the uri o is, cited by the triple (s, p, o) of a resource's subgraph.
    graph is the whole payload, which says what o is typed as; admin_nodes are
    the resource's adminMetadata nodes (see _admin_nodes).

    The objects of RECORD_PREDICATES and the uris under LC_RESOURCES are Works,
    Instances, Hubs or Items even when a row we hold says otherwise, so they are
    never linked. A uri cited under bf:adminMetadata is linked only when the
    payload describes it: _generate_admin_metadata puts the same few vocabulary
    terms (and the derivedFrom source) on every resource it mints, so linking
    them by uri alone would tie every record in the catalog to them.
    """
    if o in BF or o in MADS or o in RDF:
        return Citation.VOCABULARY
    if any((o, RDF.type, class_) in graph for class_ in RECORD_CLASSES):
        return Citation.RECORD
    if p in RECORD_PREDICATES or str(o).startswith(LC_RESOURCES):
        if (o, RDF.type, BF.Item) in graph or str(o).startswith(
            LC_RESOURCES + "items/"
        ):
            return Citation.ITEM
        return Citation.EXCLUDED
    if p == BF.adminMetadata or s in admin_nodes:
        return Citation.ADMIN_ONLY
    return Citation.OTHER


def _is_retryable_pg_error(error: BaseException) -> bool:
    """
    A graph save is only retried when Postgres aborted the transaction with one
    of the retryable errors above (surfaced by sqlalchemy as an OperationalError
    or IntegrityError wrapping the psycopg error in .orig).
    """
    return isinstance(error, (OperationalError, IntegrityError)) and isinstance(
        error.orig, RETRYABLE_PG_ERRORS
    )


def save_graph(
    session_maker: sessionmaker,
    graph: Graph,
    namespace="https://bcld.info/",
    primary_class=None,
    update_other_resources: bool = True,
    source: str | None = None,
) -> Graph:
    """
    Use the supplied database sessionmaker to create a database session and
    persist all the resources found in the Graph. The possibly modified graph is
    returned, which will contain any new URIs that were minted, as well as
    bibframe:derivedFrom assertions for the original URIs.

    primary_class marks which kind of resource the caller is authoritatively
    writing: BF.Work, BF.Instance, BF.Hub, or the OtherResource model class.
    Resources of that kind are fully upserted; resources of any other kind are
    treated as references -- created if absent but never overwritten -- so a
    reference (even one carrying a display label) can't clobber an existing full
    description. When primary_class is None (e.g. the bulk loader) every resource
    is authoritative, preserving the original behavior.

    update_other_resources only applies when primary_class is None: set it False
    to treat Other Resources as references (create-if-absent, never overwrite)
    even on the bulk path. Batch loading of Works/Instances references many shared
    Other Resources it doesn't need to re-describe; skipping those updates avoids
    re-framing/re-writing each one on every record. (Their descriptions are then
    maintained by a separate process.)

    source names where the graph came from -- a filename, a URL, a request. It is
    only used in log messages and errors, so that a rejected payload can be traced
    back to the thing that produced it. Worth passing when loading in bulk, where
    the resource URIs in a message may not identify the file they arrived in.
    """
    bg = BluecoreGraph(graph, namespace, source=source)
    bg.save(
        session_maker,
        primary_class=primary_class,
        update_other_resources=update_other_resources,
    )
    return bg.graph


def _remove_triples_by_type(graph: Graph, predicate: URIRef, type_: URIRef) -> None:
    """
    Removes blank nodes reachable via predicate that are rdf:typed as
    type_ (e.g. predicate=BF.identifiedBy, type_=BF.OclcNumber) from the
    supplied graph, along with the predicate assertion that points to them.
    """
    for s, obj in list(graph.subject_objects(predicate=predicate)):
        if not isinstance(obj, BNode):
            continue
        if (obj, RDF.type, type_) in graph:
            remove_bnode(graph, obj)
            graph.remove((s, predicate, obj))


# The fields of rdflib's in-memory store, which _copy_graph copies.
_MEMORY_FIELDS = frozenset(
    {
        "_Store__node_pickler",
        "dispatcher",
        "identifier",
        "_Memory__spo",
        "_Memory__pos",
        "_Memory__osp",
        "_Memory__namespace",
        "_Memory__prefix",
        "_Memory__context_obj_map",
        "_Memory__tripleContexts",
        "_Memory__contextTriples",
        "_Memory__all_contexts",
        "_Memory__defaultContexts",
    }
)


def _nested_copy(index: dict, depth: int) -> dict:
    """A copy of index and of the dicts depth levels below it."""
    if depth == 0:
        return dict(index)
    return {key: _nested_copy(value, depth - 1) for key, value in index.items()}


def _copy_graph(graph: Graph) -> Graph:
    """
    A copy of graph that changes on its own, and gives its resources in the
    same order.

    The save reads resources in the order the graph gives them (works(),
    minting, inserts), which for rdflib's in-memory store is the order their
    triples were added, kept in its indexes and nowhere else. Iterating a graph
    yields its triples in hash order, so a copy made triple by triple would
    order them differently in each process. So the copy takes the store's
    indexes as they are, sharing the terms, which are immutable. That reaches
    into the store's private fields: a graph whose store isn't laid out as
    _MEMORY_FIELDS expects is deep-copied instead, which keeps the order too,
    only more slowly.
    """
    store = graph.store
    if (
        type(graph) is not Graph
        or type(store) is not Memory
        or set(vars(store)) != _MEMORY_FIELDS
    ):
        return copy.deepcopy(graph)

    fields = vars(store)
    copied = Memory()
    into = vars(copied)
    for name in ("_Memory__spo", "_Memory__pos", "_Memory__osp"):
        into[name] = _nested_copy(fields[name], 2)
    for name in ("_Memory__namespace", "_Memory__prefix"):
        into[name] = dict(fields[name])
    # None until the store holds a triple
    default = fields["_Memory__defaultContexts"]
    into["_Memory__defaultContexts"] = None if default is None else dict(default)
    into["_Memory__tripleContexts"] = _nested_copy(fields["_Memory__tripleContexts"], 1)
    into["_Memory__contextTriples"] = {
        key: set(triples) for key, triples in fields["_Memory__contextTriples"].items()
    }
    # The store maps each context back to its graph; the copy's is the copy.
    graph_copy = Graph(store=copied, identifier=graph.identifier)
    into["_Memory__context_obj_map"] = {
        key: graph_copy for key in fields["_Memory__context_obj_map"]
    }
    into["_Memory__all_contexts"] = {graph_copy}
    return graph_copy


def _remove_admin_metadata(graph: Graph, subject: URIRef | None = None) -> None:
    """
    Removes existing AdminMetadata nodes. If a subject is supplied only that
    resource's AdminMetadata is removed; otherwise all AdminMetadata in the graph
    is removed. Scoping to a subject matters when the graph holds more than one
    resource, so regenerating one resource's AdminMetadata doesn't wipe another's.
    """
    for s, admin_metadata in graph.subject_objects(predicate=BF.adminMetadata):
        if subject is not None and s != subject:
            continue
        if not isinstance(admin_metadata, BNode):
            continue
        # remove all triples describing the AdminMetadata blank node (and any nested ones)
        remove_bnode(graph, admin_metadata)
        # remove the link from the resource to the AdminMetadata node
        graph.remove((s, BF.adminMetadata, admin_metadata))


class _GraphView:
    """
    Finds the resources in a graph, as entries: a plan's graph as it arrived
    (which the accessors read before a save), or a resolution's once minting
    has renamed them.
    """

    graph: Graph
    # what each uri was called when this save classified it, and before
    # minting gave it a Bluecore URI (see _classified_as and _original)
    renamed: dict[str, IdentifiedNode]
    minted_from: dict[str, IdentifiedNode]

    def _view_plan(self) -> "SavePlan":
        """The plan whose roles this graph's resources have."""
        raise NotImplementedError

    def _subjects_of(self, kind: Kind, roles: Roles) -> list[IdentifiedNode]:
        """
        The Hubs, Works or Instances (kind) that get a record of their own, in
        graph order. Leaves out the anonymous descriptions kept inline in another
        resource (Role.INLINE, see _classify) and, from Works, the Hubs, which
        LC catalog data types as both bf:Hub and bf:Work.
        """
        # Other Resources have no class, and subjects(RDF.type, None) is every
        # typed subject
        if kind.bf_class is None:
            raise BluecoreGraphError("Other Resources aren't found by class")
        return [
            s
            for s in self.graph.subjects(RDF.type, kind.bf_class)
            # always true of these; it tells mypy so
            if isinstance(s, IdentifiedNode)
            and not (kind == Kind.WORK and (s, RDF.type, BF.Hub) in self.graph)
            and self._view_plan().role(roles, self._classified_as(s, kind, roles), kind)
            != Role.INLINE
        ]

    def _original(self, subject: IdentifiedNode) -> IdentifiedNode:
        """What subject was called before minting gave it a Bluecore URI."""
        return self.minted_from.get(str(subject), subject)

    def _classified_as(
        self, subject: IdentifiedNode, kind: Kind, roles: Roles
    ) -> IdentifiedNode:
        """
        What the resource of this kind called subject was called when this save
        classified it, which for a graph saved before is the Bluecore URI that
        save minted rather than its original. Minting a Work onto the uri of an
        Instance that derives from it renames the uri, but the Instance was
        classified under the uri it has now.
        """
        renamed = self.renamed.get(str(subject))
        if renamed is not None and (renamed, kind) in roles:
            return renamed
        return subject

    def _extract(self, kind: Kind) -> list[Entry]:
        """The Hubs, Works or Instances (kind) in the graph, as entries."""
        roles = self._view_plan().current_roles()
        entries = []
        for subject in self._subjects_of(kind, roles):
            graph = generate_entity_graph(self.graph, subject)
            original = self._original(subject)
            entries.append(
                Entry(
                    subject=subject,
                    kind=kind,
                    role=self._view_plan().role(
                        roles, self._classified_as(subject, kind, roles), kind
                    ),
                    graph=graph,
                    original=original,
                    minted=str(subject) in self.minted_from,
                    cited=frozenset(self._cited_uris(graph)),
                )
            )
        return entries

    def _extract_others(self, citing: list[Entry]) -> list[Entry]:
        """
        The cited resources this payload describes, which are the Other Resource
        rows the save writes, found from the entries citing them. Which ones get
        linked is decided by what each Work or Instance cites, see _link.
        """
        roles = self._view_plan().current_roles()
        others = []
        other_uris = set()

        for entry in citing:
            # only a uri the payload says something about has a description to store
            for o in entry.cited:
                if (o, None, None) in self.graph and o not in other_uris:
                    others.append(
                        Entry(
                            subject=o,
                            kind=Kind.OTHER,
                            role=self._view_plan().role(roles, o, Kind.OTHER),
                            graph=generate_entity_graph(self.graph, o),
                            original=o,
                            minted=False,
                            cited=frozenset(),
                        )
                    )
                    other_uris.add(o)

        return others

    def _extract_entries(self) -> dict[Kind, list[Entry]]:
        """Every resource the save writes, by kind."""
        entries = {
            kind: self._extract(kind) for kind in (Kind.HUB, Kind.WORK, Kind.INSTANCE)
        }
        entries[Kind.OTHER] = self._extract_others(
            entries[Kind.WORK] + entries[Kind.INSTANCE] + entries[Kind.HUB]
        )
        return entries

    def _cited_uris(self, g: Graph) -> set[URIRef]:
        """
        The uris a resource's subgraph cites that could be Other Resources: every
        object uri, nested ones included, that the save would store a description
        of (see Citation.stored).
        """
        admin_nodes = _admin_nodes(g)
        return {
            o
            for s, p, o in g
            if isinstance(o, URIRef)
            and isinstance(p, URIRef)
            and _citation(self.graph, s, p, o, admin_nodes).stored
        }

    def _linkable_uris(
        self, g: Graph, cited: frozenset[URIRef], described: set[IdentifiedNode]
    ) -> set[URIRef]:
        """
        The cited uris a Work or Instance may be linked to as Other Resources,
        if we hold a row for them: those that one of its triples cites as an
        Other Resource, or under bf:adminMetadata when this payload describes
        them. See _citation.
        """
        admin_nodes = _admin_nodes(g)
        linkable = set()
        for s, p, o in g:
            if not (isinstance(o, URIRef) and isinstance(p, URIRef) and o in cited):
                continue
            citation = _citation(self.graph, s, p, o, admin_nodes)
            if citation == Citation.OTHER or (
                citation == Citation.ADMIN_ONLY and o in described
            ):
                linkable.add(o)
        return linkable


class SavePlan(_GraphView):
    """
    What a save is going to do, decided from the payload alone: the payload
    copied and normalised, and the role of every resource in it (see
    _classify). It reads no database. The caller's graph is copied first, so a
    save never changes it.
    """

    def __init__(
        self, graph: Graph, namespace: Namespace, source: str | None = None
    ) -> None:
        self.graph = _copy_graph(graph)
        self.namespace = namespace
        self.source = source
        # How the save writes, and what it decides once it knows (see
        # prepare): None and empty until then.
        self.mode: Mode | None = None
        self.described: set[str] = set()
        # For a plan saving again what an earlier save saved, which resources
        # arrived described the first time (see after); None for a payload.
        self.carried_described: set[str] | None = None
        self.roles: Roles = {}
        self.root: BNode | None = None
        self.recorded_blank_nodes: set[BNode] = set()
        # What each Bluecore URI in the graph was called when it arrived, for a
        # graph saved before (see _classify's arrived_as).
        self.arrived_as: dict[str, IdentifiedNode] = {}
        # Nothing has been renamed before minting, and the names minted before
        # are the ones it arrived with.
        self.renamed = {}
        self.minted_from = self.arrived_as
        # Work these out before _infer, which adds links back up (bf:instanceOf
        # for a bf:hasInstance) that would make a new resource look nested.
        #
        # The blank node a payload named with an empty @id, if any. See
        # _name_new_resource.
        self.new_resource = self._name_new_resource()
        # The blank node Works, Instances and Hubs no other one describes, which
        # are the only ones that can be the resource being created; and the
        # blank nodes nothing with a URI reaches. See _find_roots.
        self.roots, self.unattached = self._find_roots()
        self._infer()

    def _view_plan(self) -> "SavePlan":
        return self

    def resolve(self, session: Session) -> "Resolution":
        """
        What saving this plan would do now, read on session: identities, the
        rows we hold and each resource's action. It writes nothing, and works on
        a copy of the graph (see Resolution), so the plan can be resolved again.
        """
        resolution = Resolution(self)
        resolution.fill(session)
        return resolution

    def apply(
        self,
        session_maker: sessionmaker,
        max_attempts: int = SAVE_MAX_ATTEMPTS,
        on_attempt: Callable[["Resolution"], None] | None = None,
    ) -> "Resolution":
        """
        Save this plan in one transaction, as a fresh resolution of it says,
        and return that resolution. Under concurrent writers a transaction can
        lose a race and be aborted by Postgres with a deadlock or serialization
        failure, so the whole save is retried on a fresh session up to
        max_attempts times. on_attempt is told of each attempt's resolution
        before it resolves. Nothing here decides what to write: the resolution
        does (see Resolution and _action).
        """
        committed: Resolution | None = None

        def log_retry(retry_state) -> None:
            error = retry_state.outcome.exception()
            logger.warning(
                f"retrying graph save after {type(error.orig).__name__} "
                f"(attempt {retry_state.attempt_number} of {max_attempts})"
            )

        try:
            for attempt in Retrying(
                retry=retry_if_exception(_is_retryable_pg_error),
                stop=stop_after_attempt(max_attempts),
                before_sleep=log_retry,
                reraise=True,
            ):
                with attempt, session_maker() as session:
                    # Each attempt resolves afresh, on a copy of the plan: a
                    # retry looks every identity up again, since another save
                    # may have committed one meanwhile. Report the attempt
                    # before resolving, so a failure leaves it to read.
                    resolution = Resolution(self)
                    if on_attempt is not None:
                        on_attempt(resolution)
                    resolution.fill(session)

                    # Each entry's action decides what is written: see _action.
                    for kind in (Kind.HUB, Kind.WORK, Kind.INSTANCE, Kind.OTHER):
                        resolution._persist(kind, session)

                    # flush so the just-added resources have ids and are
                    # visible to _link's uri lookups. Required because the
                    # session may have autoflush disabled (as the
                    # bluecore_api session does), in which case _link's
                    # queries would otherwise not see them and would build
                    # link rows with null foreign keys.
                    session.flush()

                    # link all the works, instances and other resources together in the db.
                    # _link issues many per-uri lookups; with autoflush on, each one
                    # re-flushes the pending link changes (and re-fires update events /
                    # bf-class recomputation). The resources it reads were already
                    # flushed above, so turn autoflush off for the link phase and let
                    # commit() do the single final flush. (bluecore_api's session already
                    # runs with autoflush disabled.)
                    prev_autoflush = session.autoflush
                    session.autoflush = False
                    try:
                        resolution._link(session)
                    finally:
                        session.autoflush = prev_autoflush

                    # all changes are part of one transaction!
                    session.commit()
                    committed = resolution
        except (OperationalError, IntegrityError) as error:
            logger.error(f"Exceeded {max_attempts} attempts with error {error}")
            raise
        assert committed is not None
        return committed

    def after(self, resolution: "Resolution") -> "SavePlan":
        """
        The plan for saving again what resolution saved: its graph, minted, with
        what each Bluecore URI was called when it arrived. The resource being
        created and the roots are the ones found when the payload arrived.
        """
        plan = SavePlan.__new__(SavePlan)
        plan.graph = _copy_graph(resolution.graph)
        plan.namespace = self.namespace
        plan.source = self.source
        plan.mode = None
        plan.described = set()
        # Judge stubs as the first save did: minting writes adminMetadata,
        # and our stub marker, onto everything it mints.
        plan.carried_described = (
            self.carried_described
            if self.carried_described is not None
            else set(self.described)
        )
        plan.roles = {}
        plan.root = None
        plan.recorded_blank_nodes = set()
        plan.arrived_as = dict(resolution.minted_from)
        plan.renamed = {}
        plan.minted_from = plan.arrived_as
        plan.new_resource = self.new_resource
        plan.roots, plan.unattached = self.roots, self.unattached
        return plan

    @classmethod
    def from_graph(
        cls,
        graph: Graph,
        namespace: Namespace,
        mode: Mode,
        source: str | None = None,
    ) -> "SavePlan":
        """The plan for saving graph under mode."""
        plan = cls(graph, namespace, source)
        plan.prepare(mode)
        plan.normalise()
        return plan

    def normalise(self) -> None:
        """
        Clean the graph up for saving, once its roles are decided: drop the
        values the payload duplicated, the stub marker of a resource an API
        save really describes, and the triples we never persist.
        """
        # Strip blank node values the payload itself duplicated, before anything
        # here has had a chance to alter the graph. A caller is answerable for what
        # it sent us, not for what our own normalisation went on to do with it -- if
        # a later step leaves two blank nodes identical, that is ours to fix rather
        # than something to attribute to the payload in the log.
        self._strip_duplicate_values()

        # An explicit write from the API is a real description, so it isn't a
        # stub any more.
        if isinstance(self.mode, Write):
            self._clear_stub_marker()

        # Strip the triples we never persist (see EXCLUDED_TRIPLE_TYPES) from the
        # graph once, before any subgraph is extracted. Each entry's subgraph is
        # then already clean, so Resolution._persist can serialize it directly
        # rather than copying it just to strip these out.
        self._strip_excluded_triples()

    def _name_new_resource(self) -> BNode | None:
        """
        Turn the resource a payload names with an empty @id (see NEW_RESOURCE) into
        a blank node, like any other resource being created, and return it.

        Linked Data Platform only gives the empty @id this meaning as a subject
        (https://www.w3.org/TR/ldp/#ldpc-post-rdfnullrel). It is taken as an
        object too, so that a part can point back at the Work being created.

        Any other relative @id is refused. It resolves against NEW_RESOURCE to a
        URI of its own, which would otherwise be taken for a real identity and
        get a record.
        """
        new_resource = URIRef(NEW_RESOURCE)
        # without the slash, which "../x" resolves without
        prefix = NEW_RESOURCE.rstrip("/")
        for triple in self.graph:
            for node in (triple[0], triple[2]):
                if (
                    isinstance(node, URIRef)
                    and node.startswith(prefix)
                    and node != new_resource
                ):
                    relative = str(node).removeprefix(NEW_RESOURCE)
                    raise BluecoreGraphError(
                        f'Unsupported relative @id "{relative}": use "" for the '
                        "resource being created, or a blank node"
                    )
        triples = list(self.graph.triples((new_resource, None, None))) + list(
            self.graph.triples((None, None, new_resource))
        )
        if not triples:
            return None
        bnode = BNode()
        for s, p, o in triples:
            self.graph.remove((s, p, o))
            self.graph.add(
                (
                    bnode if s == new_resource else s,
                    p,
                    bnode if o == new_resource else o,
                )
            )
        return bnode

    def _find_roots(self) -> tuple[set[BNode], set[BNode]]:
        """
        Find the blank node Works, Instances and Hubs that are resources in their
        own right, rather than described inline in another resource.

        A blank node has no identity of its own, so one described inside another
        resource -- such as the Work the editor's Monograph Work template puts
        under bf:accompaniedBy, an Instance under a new Work's bf:hasInstance, or
        the nameless Work LC describes at the other end of a bf:relation -- stays
        inline in that resource's record until something gives it a URI.
        So a root is a blank node no other Work, Instance or Hub reaches. A payload
        can also say which one is being created with an empty @id, which settles
        it when the shape doesn't: a new Work whose part points back at it with
        bf:partOf is reached from that part.

        Also returns the blank nodes nothing with a URI reaches, the ones save()
        has to find a single root among (see _find_root).

        This runs before _infer, which adds the links back up (bf:instanceOf for a
        bf:hasInstance) that would make a root look nested.
        """

        def reached_from(start: Node) -> set[Node]:
            reached: set[Node] = set()
            queue = [start]
            while queue:
                for o in self.graph.objects(queue.pop()):
                    if isinstance(o, BNode) and o not in reached:
                        reached.add(o)
                        queue.append(o)
            return reached

        records = {
            s
            for class_ in RECORD_CLASSES
            for s in self.graph.subjects(RDF.type, class_)
        }
        reached = {record: reached_from(record) for record in records}
        blank = {record for record in records if isinstance(record, BNode)}
        unattached = blank.difference(
            *(r for record, r in reached.items() if isinstance(record, URIRef))
        )
        if self.new_resource is not None:
            return {self.new_resource}, unattached
        roots = {
            node
            for node in blank
            if not any(node in r for record, r in reached.items() if record != node)
        }
        return roots, unattached

    def _find_root(self, kind: Kind | None) -> BNode | None:
        """
        Find the blank node a save() writing kind is creating, or None if it
        isn't creating one (an update, where the resource has a URI, or a kind
        that isn't a Hub, Work or Instance).

        A resource named with an empty @id is the one, wherever it sits. Otherwise
        a lone blank node of the class is the one, even if another resource points
        at it (a new Instance's bf:instanceOf, say). Only when there are several
        does it matter which is a root.

        Raises a BluecoreGraphError when the payload doesn't say which one it is
        (two blank node Works that each describe the other, or two that nothing
        connects), or when the only blank node of the class is inside a resource
        with a URI. That resource is only referred to, so isn't rewritten, and the
        new one would be lost.
        """
        if kind is None or kind.bf_class is None:
            return None
        primary_class = kind.bf_class
        name = str(primary_class).removeprefix(str(BF))
        if self.new_resource is not None:
            if (self.new_resource, RDF.type, primary_class) not in self.graph:
                raise BluecoreGraphError(
                    f"The resource with an empty @id is not a {name}"
                )
            return self.new_resource

        nodes = {
            node
            for node in self.graph.subjects(RDF.type, primary_class)
            # LC types Hubs as both bf:Hub and bf:Work; works() leaves them out
            if not (primary_class == BF.Work and (node, RDF.type, BF.Hub) in self.graph)
        }
        blank = {node for node in nodes if isinstance(node, BNode)}
        candidates = blank & self.unattached
        if not candidates:
            if blank and nodes == blank:
                raise BluecoreGraphError(
                    f"Nothing to create: the blank node {name} is described inside "
                    "a resource with a URI, which isn't rewritten. Give the "
                    f"{name} being created an empty @id."
                )
            return None
        if len(candidates) == 1:
            return candidates.pop()
        roots = candidates & self.roots
        if len(roots) == 1:
            return roots.pop()
        if roots:
            problem = f"{len(roots)} blank node {name}s, none described inside another"
        else:
            problem = (
                f"{len(candidates)} blank node {name}s, each described inside another"
            )
        raise BluecoreGraphError(
            f"Unable to tell which resource is being created: found {problem}. "
            "Give the one being created an empty @id."
        )

    def prepare(self, mode: Mode) -> None:
        """
        Decide what this save does with each resource, before anything changes
        the graph: the mode, which resources arrived described, the resource an
        API save is creating, and every resource's role. Reads no database.
        """
        # A record describes a few resources and carries only a stub of the ones
        # it points at, giving the described ones adminMetadata and the stubs none. Note
        # them now: minting later generates adminMetadata of our own for every
        # resource, which washes the distinction away. Only an ingest tells
        # stubs apart, and only if this record uses adminMetadata at all, or
        # everything would look like a stub (see _classify).
        # A resource we previously wrote as a stub doesn't count as described
        # just because it comes back carrying the adminMetadata we gave it.
        if self.carried_described is not None:
            # saving again: what arrived described, by the names it arrived with
            self.described = set(self.carried_described)
        else:
            self.described = {
                str(s)
                for s in self.graph.subjects(BF.adminMetadata, None)
                if not self._marked_stub(s)
            }
        self.mode = mode
        self.root = None
        try:
            if isinstance(mode, Write):
                self.root = self._find_root(mode.kind)
        finally:
            # classify even when _find_root refuses the payload, so the
            # accessors still read it under this mode, with nothing created
            self.roles = _classify(
                self.graph,
                mode,
                self.roots,
                self.root,
                self.described,
                self.arrived_as,
            )
            self.recorded_blank_nodes = {
                node
                for (node, _), role in self.roles.items()
                if isinstance(node, BNode) and role != Role.INLINE
            }

    def current_roles(self) -> Roles:
        """
        The roles save() decided, or, before a save, the roles of the graph as it
        is, which the accessors read.
        """
        if self.mode is None:
            return _classify(self.graph, None, self.roots, None, set())
        return self.roles

    def role(self, roles: Roles, original: IdentifiedNode, kind: Kind) -> Role:
        """The role of the resource of this kind that arrived as original."""
        if kind == Kind.OTHER:
            return _role_by_mode(self.mode, kind)
        role = roles.get((original, kind))
        if role is None:
            raise BluecoreGraphError(f"{original} was not classified as a {kind.value}")
        return role

    def _infer(self) -> None:
        """
        Infer some triples that we rely on, and which may be missing.
        """
        # Create inverse properties, so a relationship is found whichever end the
        # payload stated it from. BIBFRAME declares both pairs owl:inverseOf one
        # another, which licenses the inverse whatever the ends turn out to be.
        inverse_properties = [
            (BF.hasInstance, BF.instanceOf),
            (BF.hasExpression, BF.expressionOf),
        ]
        for pred1, pred2 in inverse_properties:
            # add inverse of pred1
            for s, o in self.graph.subject_objects(predicate=pred1):
                self.graph.add((o, pred2, s))
            # add inverse of pred2
            for s, o in self.graph.subject_objects(predicate=pred2):
                self.graph.add((o, pred1, s))

        # now infer some resource types
        type_rules = [
            # (property, inferred Object type)
            (BF.hasInstance, BF.Instance),
            (BF.instanceOf, BF.Work),
        ]
        for predicate, object_type in type_rules:
            for o in self.graph.objects(predicate=predicate):
                self.graph.add((o, RDF.type, object_type))

    def _strip_excluded_triples(self) -> None:
        """
        Remove the triples we never persist (see EXCLUDED_TRIPLE_TYPES) from
        self.graph. Called once from save() before subgraphs are extracted, so the
        entries' subgraphs -- and the JSON-LD serialized from them in
        Resolution._persist -- never contain these triples, and Resolution._persist
        can serialize each one without copying it first.
        """
        for predicate, type_ in EXCLUDED_TRIPLE_TYPES:
            _remove_triples_by_type(self.graph, predicate, type_)

    def _marked_stub(self, subject: Node) -> bool:
        """Whether we recorded this resource as a stub the last time we saw it."""
        return any(
            (admin_metadata, BF.status, STUB_STATUS) in self.graph
            for admin_metadata in self.graph.objects(subject, BF.adminMetadata)
        )

    def _clear_stub_marker(self) -> None:
        """
        Drop the "incomplete" status of a resource that is now really described.

        Only adminMetadata carries our marker, so this is scoped to those nodes
        rather than sweeping the graph for the term.
        """
        for admin_metadata in list(self.graph.subjects(BF.status, STUB_STATUS)):
            if (admin_metadata, RDF.type, BF.AdminMetadata) not in self.graph:
                continue
            self.graph.remove((admin_metadata, BF.status, STUB_STATUS))
            self.graph.add((admin_metadata, BF.status, DEFAULT_STATUS))

    def _strip_duplicate_values(self) -> None:
        """
        Drop blank node values a resource carries more than once, keeping one of
        each. See find_duplicate_bnode_values for what counts as a duplicate, and
        for the kinds of repetition that are left alone.

        Called first from save(), so it judges the graph as it was handed to us
        rather than after our own normalisation has been over it.
        See blue-core-lod/bluecore-models#170.
        """
        where = f"{self.source}: " if self.source else ""
        for dup in strip_duplicate_bnode_values(self.graph):
            value = f": {dup.label!r}" if dup.label else ""
            logger.warning(
                f"{where}duplicate value: <{dup.subject}> had {dup.copies} "
                f"identical <{dup.predicate}> values{value}; "
                f"stripped {dup.copies - 1}"
            )


class Resolution(_GraphView):
    """
    What a save of a plan does at this moment: what each resource is called,
    the row we hold for it, and what the save does to that row (its Action).
    It is worked out on a copy of the plan's graph, which minting renames, so
    the plan is never changed and can be resolved again: on a retry, or as a dry
    run that's thrown away. Resolving reads the database and writes nothing.
    """

    def __init__(self, plan: "SavePlan") -> None:
        self.plan = plan
        self.namespace = plan.namespace
        self.graph = _copy_graph(plan.graph)
        self.minted_from = dict(plan.arrived_as)
        self.renamed = {}
        # The URI minted for the root a Write is creating, so a caller creating
        # a resource knows which one it created. None when it creates nothing.
        self.created_uri: URIRef | None = None
        # Each resource the save writes, by kind, once minting is done.
        self.entries: dict[Kind, list[Entry]] = {}
        # The row we hold for each resource, and what the save does to it, by
        # uri. See _action.
        self.stored: dict[str, Hub | Work | Instance | OtherResource] = {}
        self.actions: dict[str, Action] = {}

    def _view_plan(self) -> "SavePlan":
        return self.plan

    def fill(self, session: Session) -> None:
        """Resolve on session: mint, find the entries and decide their actions."""
        # resolve URIs in the graph to their Bluecore equivalent or mint them as
        # appropriate. note: OtherResources keep their original URI
        self._mint_all_uris(Kind.HUB, session)
        self._mint_all_uris(Kind.WORK, session)
        self._mint_all_uris(Kind.INSTANCE, session)

        # Nothing changes the graph after minting, so extract each resource
        # once, here.
        self.entries = self._extract_entries()

        for kind in (Kind.HUB, Kind.WORK, Kind.INSTANCE, Kind.OTHER):
            self._decide_actions(kind, session)

    def _decide_actions(self, kind: Kind, session: Session) -> None:
        """
        Fetch the rows we hold for the resources of kind, and decide from each
        entry's role and row what the save does to it (see _action).
        """
        entries = self.entries[kind]
        # A cited resource described only by triples generate_entity_graph
        # leaves out has nothing to store. The save has always refused one,
        # with the error the subject lookup that preceded Entry raised.
        if kind == Kind.OTHER and any(len(entry.graph) == 0 for entry in entries):
            raise BluecoreGraphError("Unable to find subject URI for None")

        # Fetch the resources of this batch that already exist in a single query,
        # keyed by uri, rather than a SELECT per resource -- so re-saves and large
        # batches don't fan out into a round-trip each.
        mode = self.plan.mode
        if mode is None:
            raise BluecoreGraphError("Only a plan prepared for a save resolves")
        sqla_class = _SQLA_CLASSES[kind]
        existing = {
            obj.uri: obj
            for obj in session.query(sqla_class).where(
                sqla_class.uri.in_([str(entry.subject) for entry in entries])
            )
        }
        for entry in entries:
            uri = str(entry.subject)
            row = existing.get(uri)
            if row is not None:
                self.stored[uri] = row
            self.actions[uri] = _action(entry, row, mode)

    def _generate_admin_metadata(
        self,
        bluecore_uri: URIRef,
        source_uri: URIRef,
        status: URIRef = DEFAULT_STATUS,
        agent: URIRef = DEFAULT_AGENT,
        desc_auth: URIRef = DEFAULT_DESC_AUTH,
        desc_lang: URIRef = DEFAULT_DESC_LANG,
        desc_level: URIRef = DEFAULT_DESC_LEVEL,
        stub: bool = False,
    ):
        """
        Generates two bf:AdminMetadata blank nodes for incoming RDF resources that are
        derived from existing RDF resources. stub records that we hold only a
        placeholder description of this resource.
        """

        time_stamp = datetime.datetime.now(datetime.UTC)

        # what we hold of a stub is incomplete, whatever the caller asked for
        if stub:
            status = STUB_STATUS
            self._describe_stub_status()

        _remove_admin_metadata(self.graph, bluecore_uri)

        # First bf:AdminMetadata
        first_admin_metadata = BNode()
        self.graph.add((bluecore_uri, BF.adminMetadata, first_admin_metadata))
        self.graph.add((first_admin_metadata, RDF.type, BF.AdminMetadata))
        self.graph.add((first_admin_metadata, BF.agent, agent))
        self.graph.add(
            (
                first_admin_metadata,
                BF.date,
                Literal(time_stamp.isoformat(), datatype=XSD.dateTime),
            )
        )
        self.graph.add((first_admin_metadata, BF.derivedFrom, source_uri))
        self.graph.add((first_admin_metadata, BF.status, status))

        # Second bf:AdminMetadata
        second_admin_metadata = BNode()
        self.graph.add((bluecore_uri, BF.adminMetadata, second_admin_metadata))
        self.graph.add((second_admin_metadata, RDF.type, BF.AdminMetadata))

        cataloger_id = CURRENT_USER_ID.get() or "Unknown"
        self.graph.add((second_admin_metadata, BFLC.catalogerId, Literal(cataloger_id)))
        self.graph.add((second_admin_metadata, BF.descriptionAuthentication, desc_auth))
        self.graph.add(
            (second_admin_metadata, BF.date, Literal(time_stamp.strftime("%Y-%m-%d")))
        )
        self.graph.add((second_admin_metadata, BF.descriptionLanguage, desc_lang))
        self.graph.add((second_admin_metadata, BF.descriptionLevel, desc_level))

    def _describe_stub_status(self) -> None:
        """
        Describe the status term we mark stubs with.

        _extract_others only stores an Other Resource for a referenced uri that
        also appears as a subject in the graph, so a bare reference would leave
        the UI nothing but a uri to display. Incoming LC records describe the
        vocabulary terms they use the same way.
        """
        self.graph.add((STUB_STATUS, RDF.type, BF.Status))
        self.graph.add((STUB_STATUS, RDFS.label, Literal("incomplete")))
        self.graph.add((STUB_STATUS, BF.code, Literal("incmp")))

    def _mint_all_uris(self, kind: Kind, session: Session) -> None:
        """
        Examine the Bibframe Hubs, Works or Instances (kind) in the graph, and
        mint Bluecore URIs for them as needed. This method takes into account that
        a resource with a non-Bluecore URI may already be in the database under in
        its derivedFrom URI.
        """
        # there's nothing to do for one that already has a bluecore URI
        uris = [
            uri
            for uri in self._subjects_of(kind, self.plan.roles)
            if not self._is_bluecore_uri(uri)
        ]
        # Which of them were previously saved with an adminMetadata derivedFrom
        # assertion, looked up all at once. Minting changes the graph but not the
        # database, so the answers hold for the whole loop.
        stored = self._stored_identities(kind, uris, session)

        for uri in uris:
            # first look in the graph we are saving for an adminMetadata
            # derivedFrom assertion that indicates an already minted bluecore
            # URI for the external URI, then in the database
            found: str | None = self._derived_from_subject(uri)
            if found is None:
                found = stored.get(str(uri))

            # if we found an existing bluecore URI then we can update the graph to
            # use it, otherwise we need to mint a new one
            self._switch_uris(
                derived_from=uri,
                bluecore_uri=URIRef(found)
                if found is not None
                else self._mint_uri(kind),
                kind=kind,
            )

    def _stored_identities(
        self, kind: Kind, uris: list[IdentifiedNode], session: Session
    ) -> dict[str, str]:
        """
        The Bluecore uri each of these uris was previously saved under as a Hub,
        Work or Instance (kind), found from its adminMetadata derivedFrom, a chunk
        at a time. Where more than one resource records the same derivedFrom, the
        first row Postgres returns wins, as .first() did for a single uri.
        """
        values = [str(uri) for uri in uris]
        stored: dict[str, str] = {}
        for start in range(0, len(values), HELD_LOOKUP_CHUNK):
            chunk = values[start : start + HELD_LOOKUP_CHUNK]
            for derived_from, uri in self._derived_from_query(
                _SQLA_CLASSES[kind], chunk, session
            ):
                stored.setdefault(derived_from, uri)
        return stored

    @staticmethod
    def _derived_from_query(
        sqla_class: type[Hub] | type[Work] | type[Instance],
        uris: list[str],
        session: Session,
    ) -> Query:
        """
        Builds the query that finds the saved resources of the given class whose
        adminMetadata records a bf:derivedFrom assertion for one of the supplied
        URIs, as (derived_from, uri) rows.
        """
        # The query has to filter on both type and the derivedFrom @id to use
        # index_resource_base_on_type_derivedfrom_id, which leads with type.
        # Querying a joined-table subclass doesn't add the type filter by
        # itself, and without it Postgres reads the whole index on every
        # lookup, so each save gets slower as the table grows.
        #
        # The path and '{}' are rendered as typed SQL literals rather than
        # bound parameters: psycopg sends parameters server-side as varchar,
        # which doesn't resolve to jsonpath / text[], and the expression has to
        # match the functional index on derivedFrom for the planner to use it.
        derived_from = func.jsonb_path_query_first(
            sqla_class.data,
            literal_column("""'$.adminMetadata[*].derivedFrom."@id"'::jsonpath"""),
        ).op("#>>")(literal_column("'{}'::text[]"))
        return session.query(derived_from, sqla_class.uri).where(
            sqla_class.type == sqla_class.__mapper__.polymorphic_identity,
            derived_from.in_(uris),
        )

    def _mint_uri(self, kind: Kind) -> URIRef:
        """
        Mints a Bluecore URI for a Hub, Work or Instance.
        """
        if kind not in _COLLECTIONS:
            raise BluecoreGraphError("Can't mint Bluecore URIs for Other Resources")
        return self.namespace[f"{_COLLECTIONS[kind]}/{uuid4()}"]

    def _derived_from_subject(self, source_uri: Node) -> URIRef | None:
        """
        Looks in the graph for a resource whose bf:adminMetadata records a
        bf:derivedFrom assertion for the supplied source URI, returning that
        resource's URI (or None if there isn't one). The derivedFrom assertion
        lives on the AdminMetadata blank node rather than the resource itself.
        """
        for admin_metadata in self.graph.subjects(
            predicate=BF.derivedFrom, object=source_uri
        ):
            subject = self.graph.value(
                predicate=BF.adminMetadata, object=admin_metadata
            )
            if subject is not None:
                return subject
        return None

    def _switch_uris(
        self, derived_from: IdentifiedNode, bluecore_uri: URIRef, kind: Kind
    ) -> None:
        """
        Updates the supplied graph so that assertions involving the
        derived_from as the subject now use the bluecore_uri in its place.
        A bibframe:derivedFrom assertion is added to record the relationship
        if the derived_from is URIRef, marking a stub of kind as one.
        """
        self.minted_from[str(bluecore_uri)] = derived_from
        self.renamed[str(bluecore_uri)] = derived_from
        if self.plan.root is not None and derived_from == self.plan.root:
            self.created_uri = bluecore_uri
        replace_uri(self.graph, derived_from, bluecore_uri)
        # only add derivedFrom assertions for URIs
        if isinstance(derived_from, URIRef):
            self._generate_admin_metadata(
                bluecore_uri,
                derived_from,
                stub=self.plan.roles.get((derived_from, kind)) == Role.STUB,
            )

    def _is_bluecore_uri(self, uri) -> bool:
        return uri in self.namespace

    def _is_inline(self, node: Node) -> bool:
        """
        Whether, in this save, node is a blank node with no record of its own:
        every one is, except the resource an API save is creating.
        """
        return isinstance(node, BNode) and node not in self.plan.recorded_blank_nodes

    def _keeps_own_links(self, entry: Entry) -> bool:
        """
        Whether this resource's existing links should be left alone: it is one we
        only have a stub of, or only a reference to because an API save named a
        different kind as the one it is writing (an Instance save names its Work
        through bf:instanceOf), and it was already here to have links in
        the first place. One we just inserted has none, so it links like
        anything else.
        """
        return (
            entry.role in (Role.REFERENCE, Role.STUB)
            and self.actions.get(str(entry.subject)) != Action.INSERT
        )

    def _persist(self, kind: Kind, session: Session) -> None:
        """
        Persist the resources of the supplied kind to the given database session,
        as each one's action says (see Resolution and _action).
        """
        entries = self.entries[kind]
        sqla_class = _SQLA_CLASSES[kind]

        if kind == Kind.OTHER:
            # Only Other Resources are written in URI order. They keep their
            # external URIs, so two transactions can insert the same one; taking
            # the locks in the same order makes them serialize instead of
            # deadlocking. Hubs, Works and Instances don't need it: SQLAlchemy
            # already issues UPDATEs in primary-key order, and a freshly minted
            # UUID can't collide on INSERT.
            entries = sorted(entries, key=lambda entry: str(entry.subject))

        for entry in entries:
            uri, g = entry.subject, entry.graph
            action = self.actions[str(uri)]

            if action == Action.KEEP:
                # an existing resource we don't overwrite: link it (later, in
                # _link) but leave its stored description. Skip before building
                # its JSON-LD -- serializing and re-framing it would be wasted work
                # (this is the hot path when batch-loading records that share many
                # Other Resources).
                logger.debug(f"keeping existing {uri} ({entry.role.value})")
                continue

            # Serialize the entry's subgraph directly. save() already stripped
            # EXCLUDED_TRIPLE_TYPES from self.graph up front (see
            # _strip_excluded_triples), so there's nothing to remove here.
            data = json.loads(g.serialize(format="json-ld"))

            if action == Action.UPDATE:
                obj = self.stored[str(uri)]
                obj.data = data
                logger.info(f"updating {uri}")
                # Sqlalchemy ORM already does a comparison between the retrieved object
                # and new object and will only do an UPDATE if they are different
                session.add(obj)
            else:
                if kind == Kind.OTHER:
                    uuid = None
                else:
                    uuid = str(uri).split("/")[-1]

                # create-if-absent for both primary and referenced resources; the
                # stored data is the full extracted subgraph (whatever the payload
                # knows -- inline triples + inferred type/link), so a referenced
                # resource is created with a label etc. if it isn't in the db yet.
                obj = sqla_class(uri=str(uri), uuid=uuid, data=data)
                logger.info(f"inserting {uri}")
                session.add(obj)

    def _link(self, session) -> None:
        """
        Save relations between Hubs, Instances, Works and Other Resources in the
        graph: bf:instanceOf and bf:hasInstance tie Instances to Works,
        bf:expressionOf ties a Work to its Hub, and a Work or Instance is linked to
        every Other Resource it cites that we hold a row for.
        """

        # Cache resource lookups for the duration of this link operation. _link
        # resolves the same Works, Instances and Other Resources repeatedly (a
        # Work is re-queried for every Other Resource it links to, etc.); caching
        # by (type, uri) turns those repeats into a single query each. Every
        # resource was flushed by save() before _link runs, so the first lookup
        # already sees it.
        cache: dict[tuple, object] = {}

        # Sort all the link iterations by URI so that, like _persist,
        # concurrent transactions acquire row locks in the same deterministic order.

        # use bibframe:instanceOf to link instances with works. A payload stating the
        # relationship the other way round, as bf:hasInstance, has already had this
        # direction added for it by _infer.
        for s, o in sorted(
            self.graph.subject_objects(BF.instanceOf),
            key=lambda pair: (str(pair[0]), str(pair[1])),
        ):
            # an anonymous description has no record of its own to link
            if self._is_inline(s) or self._is_inline(o):
                continue
            logger.info(f"linking {s} to {o}")
            instance = self._get_first(session, Instance, s, cache)
            work = self._get_first(session, Work, o, cache)
            instance.work = work
            session.add(instance)

        # use bibframe:expressionOf to link works with hubs. A payload stating the
        # relationship the other way round, as bf:hasExpression, has already had this
        # direction added for it by _infer.
        for s, o in sorted(
            self.graph.subject_objects(BF.expressionOf),
            key=lambda pair: (str(pair[0]), str(pair[1])),
        ):
            # an anonymous description has no record of its own to link
            if self._is_inline(s) or self._is_inline(o):
                continue
            # Only link a Hub this document actually describes: an expressionOf
            # pointing at a Hub described elsewhere has no row here to link to. And
            # skip a subject that is itself a Hub, since LC types Hubs as both
            # bf:Hub and bf:Work and works() leaves those out.
            if (o, RDF.type, BF.Hub) not in self.graph:
                continue
            if (s, RDF.type, BF.Hub) in self.graph:
                continue
            logger.info(f"linking {s} to {o}")
            work = self._get_first(session, Work, s, cache)
            hub = self._get_first(session, Hub, o, cache)
            work.hub = hub
            session.add(work)

        # link Works and Instances to their Other Resources, which is a bit more
        # complex since a Work or Instance has a many to many relationship with
        # Other Resources

        # first remove any existing Other Resource linkages between Works and
        # Instances so that they can be replaced with the new ones
        self._delete_other_links(Kind.WORK, session, cache)
        self._delete_other_links(Kind.INSTANCE, session, cache)

        # Link each Work and Instance to every Other Resource it cites that this
        # payload describes, and to every one it cites by uri alone that we hold
        # a row for: the links follow from the resource's own data, so a save
        # citing a term by uri alone doesn't lose them. A uri we hold nothing
        # about stays a bare reference. See _linkable_uris for what is never
        # linked. Only rebuild links for resources this record describes; a stub
        # would otherwise drop that resource's links to its subjects and agents.
        described = {entry.subject for entry in self.entries[Kind.OTHER]}
        cited = []
        for kind in (Kind.WORK, Kind.INSTANCE):
            for entry in sorted(
                self.entries[kind], key=lambda entry: str(entry.subject)
            ):
                if self._keeps_own_links(entry):
                    continue
                linkable = self._linkable_uris(entry.graph, entry.cited, described)
                cited.append((_SQLA_CLASSES[kind], entry.subject, linkable))

        # resolve the cited uris a chunk at a time rather than one query per uri,
        # fetching only the ids a link row needs
        all_cited = sorted({str(uri) for _, _, uris in cited for uri in uris})
        held: dict[str, int] = {}
        for start in range(0, len(all_cited), HELD_LOOKUP_CHUNK):
            chunk = all_cited[start : start + HELD_LOOKUP_CHUNK]
            held.update(
                (uri, id_)
                for id_, uri in session.query(
                    OtherResource.id, OtherResource.uri
                ).where(OtherResource.uri.in_(chunk))
            )

        for sqla_class, resource_uri, uris in cited:
            for other_uri in sorted(uris):
                other_id = held.get(str(other_uri))
                if other_id is None:
                    continue
                logger.info(f"linking {resource_uri} to {other_uri}")
                resource_model = self._resolve(session, sqla_class, resource_uri, cache)
                session.add(
                    BibframeOtherResources(
                        bibframe_resource=resource_model, other_resource_id=other_id
                    )
                )

    def _delete_other_links(self, kind: Kind, session: Session, cache: dict) -> None:
        """
        Delete the existing Other Resource links of the Works or Instances (kind)
        so that they can be replaced with new ones. Resources this record only
        stubs out keep the links they have, since a stub isn't a description of
        them.
        """
        for entry in self.entries[kind]:
            if self._keeps_own_links(entry):
                continue
            bf_resource = self._resolve(
                session, _SQLA_CLASSES[kind], entry.subject, cache
            )

            session.query(BibframeOtherResources).filter(
                BibframeOtherResources.bibframe_resource == bf_resource
            ).delete()

    def _resolve(self, session: Session, sqla_class, uri: Node, cache: dict):
        """
        Look up a resource of the given type by uri, caching the result (keyed by
        type + uri) so repeated lookups within a single _link operation only hit
        the database once. Returns None if there is no matching resource.
        """
        key = (sqla_class, str(uri))
        if key not in cache:
            cache[key] = (
                session.query(sqla_class).where(sqla_class.uri == str(uri)).first()
            )
        return cache[key]

    def _get_first(
        self, session: Session, sqla_class: Instance | Work, uri: Node, cache: dict
    ):
        """
        Look up the first object of the given type in the database and return it
        or throw an exception.
        """
        obj = self._resolve(session, sqla_class, uri, cache)
        if obj is None:
            raise BluecoreGraphError(f"Unable to find in db: uri={uri}")

        return obj


class BluecoreGraph:
    """
    The BluecoreGraph is instantiated using an existing rdflib Graph for a set
    of Bibframe Hubs, Works, Instances and "Other" Resources, which are all available
    using methods that return them as subgraphs. The save method is then used to
    persist the graph to a given database. If you want to change the default
    namespace that is used for the bluecore instance you can pass that in with
    the namespace parameter.

    The heuristic that BluecoreGraph uses for updating the database:

    1. Infer any missing assertions in the data that we rely on (notably rdf:type).
    2. Extract subgraphs for Hubs, Works, Instances and Other Resources in the larger
       graph. Note: LC catalog data types Hubs as both bf:Hub and bf:Work; they are
       extracted as Hubs and excluded from Works to avoid double-processing.
    3. Examine each Hub, Work, Instance and Other Resource graph to see if it has a
       Bluecore subject URI.
    4. If it doesn't have a Bluecore subject URI mint one for it, update the graph
       to use it, and preserve the original URI as a bibframe:derivedFrom assertion.
    5. Save (or update) each Hub, Work, Instance and Other Resource to the database.
    6. Save relationships between the Hubs, Works, Instances and Other Resources,
       being careful to remove existing many-to-many relations with Other Resources
       prior to adding new ones.

    Stubs: when the save has no primary_class (ingest mode) and the record
    describes something with adminMetadata of its own, a Hub, Work or Instance
    without adminMetadata is a stub: a placeholder for something described
    elsewhere. It is marked mstatus/incmp when its Bluecore URI is minted.

    References: when the save names a primary_class, resources of the other kinds
    are created if they are absent and never overwritten.

    Inline blank nodes: anonymous Works, Instances and Hubs stay inside their
    parent's record rather than getting a record of their own, except the one
    resource an API save is creating.

    The link rule: Other Resource links follow each Work's and Instance's own
    data, leaving out the predicates in RECORD_PREDICATES and the URIs under
    LC_RESOURCES, which are records rather than Other Resources.
    """

    def __init__(
        self,
        graph: Graph,
        namespace: str = "https://bcld.info/",
        source: str | None = None,
    ):
        """
        Instantiate a BluecoreGraph using an rdflib Graph, and an optional
        Bluecore Namespace URL: the default is https://bcld.info. source names where
        the graph came from and is used only in log messages (see save_graph).
        """
        if not isinstance(namespace, str):
            raise TypeError(f"default namespace cannot be {namespace}")
        elif not namespace.startswith("http"):
            raise BluecoreGraphError(
                f"default namespace must be a URL, got {namespace}"
            )
        elif not namespace.endswith("/"):
            namespace += "/"
        self.namespace = Namespace(namespace)
        self.source = source
        # The payload, copied, and what is decided from it alone. See SavePlan.
        self._plan = SavePlan(graph, self.namespace, source)
        # The resolution of the last attempt to save, committed or not; None
        # before a save. See Resolution.
        self._resolution: Resolution | None = None

    # What the plan decided and the last attempt resolved, under the names the
    # rest of the class reads.

    def _view(self) -> _GraphView:
        """The graph the accessors read: the last save's, else the payload's."""
        return self._resolution if self._resolution is not None else self._plan

    @property
    def graph(self) -> Graph:
        return self._view().graph

    @property
    def created_uri(self) -> URIRef | None:
        """
        The URI save() minted for the root it was creating, so a caller creating
        a resource knows which one it created. None when the save created
        nothing.
        """
        return self._resolution.created_uri if self._resolution is not None else None

    @property
    def _entries(self) -> dict[Kind, list[Entry]]:
        # each resource the save writes, by kind; empty until a save
        return self._resolution.entries if self._resolution is not None else {}

    @property
    def _actions(self) -> dict[str, Action]:
        return self._resolution.actions if self._resolution is not None else {}

    @property
    def _roles(self) -> Roles:
        return self._plan.roles

    @property
    def _new_resource(self) -> BNode | None:
        return self._plan.new_resource

    def _prepare(self, mode: Mode) -> None:
        """Decide the roles under mode (see SavePlan.prepare)."""
        self._plan.prepare(mode)

    def _find_root(self, kind: Kind | None) -> BNode | None:
        return self._plan._find_root(kind)

    def _role(self, roles: Roles, original: IdentifiedNode, kind: Kind) -> Role:
        return self._plan.role(roles, original, kind)

    def works(self) -> list[Graph]:
        """
        Returns a list of Bibframe Work rdflib Graphs, where each graph is for a
        distinct Work. Excludes Hubs, which LC catalog data types as both bf:Hub
        and bf:Work; they are handled separately by hubs().
        """
        return [entry.graph for entry in self._view()._extract(Kind.WORK)]

    def hubs(self) -> list[Graph]:
        """
        Returns a list of Bibframe Hub rdflib Graphs, where each graph is for a
        distinct Hub.
        """
        return [entry.graph for entry in self._view()._extract(Kind.HUB)]

    def instances(self) -> list[Graph]:
        """
        Returns a list of Bibframe Instance rdflib Graphs, where each graph is for a
        distinct Instance.
        """
        return [entry.graph for entry in self._view()._extract(Kind.INSTANCE)]

    def others(self) -> list[Graph]:
        """
        Return a list of "Other Resource" rdflib Graphs, where each graph is for a
        distinct resource.
        """
        view = self._view()
        citing = (
            view._extract(Kind.WORK)
            + view._extract(Kind.INSTANCE)
            + view._extract(Kind.HUB)
        )
        return [entry.graph for entry in view._extract_others(citing)]

    def save(
        self,
        session_maker: sessionmaker,
        max_attempts: int = SAVE_MAX_ATTEMPTS,
        primary_class=None,
        update_other_resources: bool = True,
    ) -> None:
        """
        Persists the graph to the database using the supplied sqlalchemy
        sessionmaker. All the database modifications are made using a single
        transaction.

        Under concurrent writers a transaction can still lose a race and be
        aborted by Postgres with a deadlock or serialization failure. Since a
        graph save is self-contained and idempotent, we simply re-run the whole
        thing on a fresh session up to max_attempts times.
        """

        def log_retry(retry_state) -> None:
            error = retry_state.outcome.exception()
            logger.warning(
                f"retrying graph save after {type(error.orig).__name__} "
                f"(attempt {retry_state.attempt_number} of {max_attempts})"
            )

        mode = _mode(primary_class, update_other_resources)
        if self._resolution is not None:
            # saving again what the last save saved
            self._plan = self._plan.after(self._resolution)
            self._resolution = None
        self._prepare(mode)
        self._plan.normalise()
        self._plan.apply(session_maker, max_attempts, on_attempt=self._attempted)

    def _attempted(self, resolution: "Resolution") -> None:
        """Note an attempt to save, so that what it did can be read even if it fails."""
        self._resolution = resolution
