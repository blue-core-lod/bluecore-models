"""
The save policy: what a resource is in a save (its Kind and Role), what the
save does to its row (its Action), and what each URI a Work or Instance cites
is (its Citation). These are decided from the payload, the mode and the row we
hold, without the database; see the package docstring.
"""

from dataclasses import dataclass
from enum import Enum
from typing import Any, Protocol

from rdflib import BNode, Graph, IdentifiedNode, Node, URIRef

from bluecore_models.models import OtherResource
from bluecore_models.namespaces import BF, BFLC, MADS, RDF

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
) -> Roles:
    """
    The role of every Hub, Work, Instance and Other Resource in graph, before
    minting. mode is None before save() has set one, when the accessors read the
    graph as it is.

    A blank node has no identity of its own, so it stays inline in whatever
    describes it, except the one resource an API save is creating (root) or,
    before a save, any root (see SavePlan._find_roots). Every blank node of
    a bulk load is inline: LC catalog data routinely states bf:expressionOf
    against an anonymous bf:Hub and nests an anonymous bf:Work under
    bf:instanceOf, and minting a URI for one of those invents an identity nothing
    can ever match again.

    A bulk-loaded record describes a few resources and carries only a stub of the
    ones it points at: described holds the ones with adminMetadata of their own
    (see SavePlan.prepare). When the record uses adminMetadata at all, any
    other Hub, Work or Instance in it is a stub.
    """
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
            and str(node) not in described
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
