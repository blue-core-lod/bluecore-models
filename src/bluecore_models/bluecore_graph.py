import datetime
import json
import logging
from uuid import uuid4

from psycopg import errors as psycopg_errors
from rdflib import XSD, BNode, Graph, IdentifiedNode, Literal, Namespace, Node, URIRef
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
    *RELATION_PREDICATES,
)

# LC's namespace for Works, Instances, Hubs and Items, which are never Other
# Resources however they are cited.
LC_RESOURCES = "http://id.loc.gov/resources/"

# How many uris go into one IN clause when looking up held Other Resources, well
# under psycopg's 65535 bind-parameter limit.
HELD_LOOKUP_CHUNK = 5000


class BluecoreGraphError(Exception):
    """Raised for BluecoreGraph errors that aren't a type mismatch."""


# (predicate, rdf:type) pairs identifying blank nodes that are stripped out of
# a resource's graph before it is persisted to the database, e.g. removing
# OCLC number identifiers that shouldn't be exposed.
EXCLUDED_TRIPLE_TYPES = [
    (BF.identifiedBy, BF.OclcNumber),
]


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
        self.graph = graph
        self.source = source
        # The per-entity subgraph extractions (works/instances/hubs/others) are
        # expensive and get called repeatedly across the save. We cache them keyed
        # on _revision, which is bumped every time the graph is mutated so the
        # cache can't go stale. Every mutation of self.graph funnels through
        # _name_new_resource and _infer (once each, here) and _switch_uris
        # (during minting) -- keep it that
        # way, or bump _revision at any new mutation site (see _bump_revision).
        self._revision = 0
        self._subgraph_cache: dict[str, tuple[int, list[Graph]]] = {}
        # Whether this save should tell described resources from stubs,
        # which ones arrived described, and what each minted uri was called
        # beforehand. Set by save() and _switch_uris, see _is_stub.
        self._spot_stubs = False
        self._described: set[str] = set()
        self._minted_from: dict[str, str] = {}
        # Resources this save inserted, which have no existing links to protect.
        self._created: set[str] = set()
        # True when whoever called save() didn't name the kind of resource it
        # was writing: the API routes always do, the record loader never does.
        # So this is how we know a save came from a load. See _keep_existing.
        self._ingest = False
        # The kind of resource an API save names as the one it is writing; any
        # other Work or Instance in the graph is only a reference to it, so it
        # keeps the links it already has. None on the bulk path. See save().
        self._primary_class = None
        # Work these out before _infer, which adds links back up (bf:instanceOf
        # for a bf:hasInstance) that would make a new resource look nested.
        #
        # The blank node a payload named with an empty @id, if any. See
        # _name_new_resource.
        self._new_resource = self._name_new_resource()
        # The blank node Works, Instances and Hubs no other one describes, which
        # are the only ones that can be the resource being created; and the
        # blank nodes nothing with a URI reaches. See _find_roots.
        self._roots, self._unattached = self._find_roots()
        # The one root a save() with a primary_class is creating, once save()
        # has found it; every other blank node stays inline. See _find_root.
        self._root: BNode | None = None
        # The URI save() minted for that root, so a caller creating a resource
        # knows which one it created. None when the save created nothing.
        self.created_uri: URIRef | None = None
        self._infer()

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
        if self._new_resource is not None:
            return {self._new_resource}, unattached
        roots = {
            node
            for node in blank
            if not any(node in r for record, r in reached.items() if record != node)
        }
        return roots, unattached

    def _find_root(self, primary_class) -> BNode | None:
        """
        Find the blank node a save() writing primary_class is creating, or None if
        it isn't creating one (an update, where the resource has a URI).

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
        if primary_class not in RECORD_CLASSES:
            return None
        name = str(primary_class).removeprefix(str(BF))
        if self._new_resource is not None:
            if (self._new_resource, RDF.type, primary_class) not in self.graph:
                raise BluecoreGraphError(
                    f"The resource with an empty @id is not a {name}"
                )
            return self._new_resource

        nodes = {
            node
            for node in self.graph.subjects(RDF.type, primary_class)
            # LC types Hubs as both bf:Hub and bf:Work; works() leaves them out
            if not (primary_class == BF.Work and (node, RDF.type, BF.Hub) in self.graph)
        }
        blank = {node for node in nodes if isinstance(node, BNode)}
        candidates = blank & self._unattached
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
        roots = candidates & self._roots
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

    def _bump_revision(self) -> None:
        """
        Record that self.graph has been mutated, invalidating the subgraph cache.
        Call this from anywhere that changes self.graph.
        """
        self._revision += 1

    def _anonymous_description(self, subject: Node) -> bool:
        """
        Is this subject a blank node that shouldn't get a record of its own?

        A resource newly created in an editor arrives without a URI, and minting one
        for it is the whole point. The API says which kind it is authoritatively
        writing (primary_class), and the one blank node of that kind no other
        resource describes is the resource being created, so this returns False for
        it. Every other blank node stays inline: it has no identity of its own until
        something gives it a URI (see _find_roots). Before save() knows the
        primary_class, every root counts.

        A bulk-loaded record is different. Its blank nodes are inline descriptions of
        resources it merely refers to: LC catalog data routinely states
        bf:expressionOf against an anonymous bf:Hub, and nests an anonymous bf:Work
        under bf:instanceOf. Minting a URI for one of those invents an identity
        nothing can ever match again, so a reload of the same record mints another,
        and -- for Hubs -- they compete with the identified Hub for the single
        Work.hub_id, where sort order decides which one wins.
        """
        if isinstance(subject, URIRef):
            return False
        if self._ingest:
            return True
        if self._primary_class is not None:
            return subject != self._root
        return subject not in self._roots

    def _subgraphs(self, key: str, compute) -> list[Graph]:
        """
        Return the cached subgraph list for `key`, recomputing it (via `compute`)
        only when the graph has changed since it was last cached. The cache is
        keyed on _revision, so it invalidates itself whenever the graph is mutated
        (see _bump_revision).

        The returned graphs are shared/cached -- callers must not mutate them in
        place (copy first, or bump _revision if mutating self.graph).
        """
        cached = self._subgraph_cache.get(key)
        if cached is None or cached[0] != self._revision:
            cached = (self._revision, compute())
            self._subgraph_cache[key] = cached
        return cached[1]

    def works(self) -> list[Graph]:
        """
        Returns a list of Bibframe Work rdflib Graphs, where each graph is for a
        distinct Work. Excludes Hubs, which LC catalog data types as both bf:Hub
        and bf:Work; they are handled separately by hubs().
        """
        return self._subgraphs(
            "works",
            lambda: [
                generate_entity_graph(self.graph, s)
                for s in self.graph.subjects(RDF.type, BF.Work)
                if (s, RDF.type, BF.Hub) not in self.graph
                and not self._anonymous_description(s)
            ],
        )

    def hubs(self) -> list[Graph]:
        """
        Returns a list of Bibframe Hub rdflib Graphs, where each graph is for a
        distinct Hub.
        """
        return self._subgraphs("hubs", lambda: self._extract_subgraphs(BF.Hub))

    def instances(self) -> list[Graph]:
        """
        Returns a list of Bibframe Instance rdflib Graphs, where each graph is for a
        distinct Instance.
        """
        return self._subgraphs(
            "instances", lambda: self._extract_subgraphs(BF.Instance)
        )

    def others(self) -> list[Graph]:
        """
        Return a list of "Other Resource" rdflib Graphs, where each graph is for a
        distinct resource.
        """
        return self._subgraphs("others", self._extract_others)

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

        # save resources from the graph to the database. Only the
        # primary_class is authoritatively upserted; other kinds are
        # references (create-if-absent, never overwrite).
        # See _persist_resources.
        # primary_class is None means every kind is authoritative.
        def is_primary(kind) -> bool:
            return primary_class is None or primary_class == kind

        # A record describes a few resources and carries only a stub of the ones
        # it points at, giving the described ones adminMetadata and the stubs none. Note
        # them now: minting later generates adminMetadata of our own for every
        # resource, which washes the distinction away. Only do this when the
        # caller hasn't said what it is writing, and only if this record uses
        # adminMetadata at all, or everything would look like a stub.
        # A resource we previously wrote as a stub doesn't count as described
        # just because it comes back carrying the adminMetadata we gave it.
        self._described = {
            str(s)
            for s in self.graph.subjects(BF.adminMetadata, None)
            if not self._marked_stub(s)
        }
        self._spot_stubs = primary_class is None and bool(self._described)
        self._ingest = primary_class is None
        self._primary_class = primary_class
        self._root = self._find_root(primary_class)
        # which resources are anonymous descriptions may have changed with them
        self._bump_revision()
        # Strip blank node values the payload itself duplicated, before anything
        # here has had a chance to alter the graph. A caller is answerable for what
        # it sent us, not for what our own normalisation went on to do with it -- if
        # a later step leaves two blank nodes identical, that is ours to fix rather
        # than something to attribute to the payload in the log.
        self._strip_duplicate_values()

        # An explicit write from the API is a real description, so it isn't a
        # stub any more.
        if primary_class is not None:
            self._clear_stub_marker()

        # Strip the triples we never persist (see EXCLUDED_TRIPLE_TYPES) from the
        # graph once, before minting extracts subgraphs. The per-entity subgraphs
        # are then already clean, so _persist_resources can serialize the shared
        # (cached) subgraph directly rather than copying each one just to strip
        # these out. Idempotent, so re-running under a retry is a no-op.
        self._strip_excluded_triples()

        try:
            for attempt in Retrying(
                retry=retry_if_exception(_is_retryable_pg_error),
                stop=stop_after_attempt(max_attempts),
                before_sleep=log_retry,
                reraise=True,
            ):
                with attempt, session_maker() as session:
                    # start each attempt with a clean record of what was inserted
                    self._created = set()

                    # resolve URIs in the graph to their Bluecore equivalent or mint them as appropriate.
                    # note: OtherResources keep their original URI
                    self._mint_all_uris(BF.Hub, session)
                    self._mint_all_uris(BF.Work, session)
                    self._mint_all_uris(BF.Instance, session)

                    self._persist_resources(BF.Hub, session, is_primary(BF.Hub))
                    self._persist_resources(BF.Work, session, is_primary(BF.Work))
                    self._persist_resources(
                        BF.Instance, session, is_primary(BF.Instance)
                    )
                    # Other Resources have no catchall URI (class_ is None) and
                    # are the OtherResource kind for the primary check. On the
                    # bulk path (primary_class None), update_other_resources can
                    # opt out of re-describing already-present ones.
                    other_is_primary = primary_class == OtherResource or (
                        primary_class is None and update_other_resources
                    )
                    self._persist_resources(None, session, other_is_primary)

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
                        self._link(session)
                    finally:
                        session.autoflush = prev_autoflush

                    # all changes are part of one transaction!
                    session.commit()
        except (OperationalError, IntegrityError) as error:
            logger.error(f"Exceeded {max_attempts} attempts with error {error}")
            raise

    def _infer(self) -> None:
        """
        Infer some triples that we rely on, and which may be missing.
        """
        self._bump_revision()  # mutates self.graph

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

    def _extract_subgraphs(self, bibframe_class: URIRef) -> list[Graph]:
        """
        Returns a list of subgraphs for subjects of a given type, leaving out the
        anonymous descriptions kept inline in another resource (see
        _anonymous_description).
        """
        return [
            generate_entity_graph(self.graph, s)
            for s in self.graph.subjects(RDF.type, bibframe_class)
            if not self._anonymous_description(s)
        ]

    def _extract_others(self) -> list[Graph]:
        """
        Returns a list of subgraphs for the cited resources this payload describes,
        which are the Other Resource rows the save writes. Which ones get linked is
        decided by what each Work or Instance cites, see _link.
        """
        others = []
        other_uris = set()

        for g in self.works() + self.instances() + self.hubs():
            # only a uri the payload says something about has a description to store
            for o in self._cited_uris(g):
                if (o, None, None) in self.graph and o not in other_uris:
                    others.append(generate_entity_graph(self.graph, o))
                    other_uris.add(o)

        return others

    def _cited_uris(self, g: Graph) -> set[URIRef]:
        """
        The uris a resource's subgraph cites that could be Other Resources: every
        object uri, nested ones included, except terms from the Bibframe, MADS or
        RDF vocabularies and Works, Instances or Hubs, each stored as its own record.
        """
        return {
            o
            for o in g.objects()
            if isinstance(o, URIRef)
            and not self._exclude_uri_from_other_resources(o)
            and not self._is_bibframe_resource(o, self.graph)
        }

    def _linkable_uris(
        self, g: Graph, cited: set[URIRef], described: set[IdentifiedNode]
    ) -> set[URIRef]:
        """
        The cited uris a Work or Instance may be linked to as Other Resources,
        if we hold a row for them. Never linked are the objects of
        RECORD_PREDICATES and uris in LC_RESOURCES, which are Works, Instances,
        Hubs or Items even when a row we hold says otherwise.

        A uri cited only under bf:adminMetadata is linked only when this payload
        describes it. _generate_admin_metadata puts the same few vocabulary terms
        (and the derivedFrom source) on every resource it mints, so linking them
        by uri alone would tie every record in the catalog to them.
        """
        admin_nodes: set[Node] = set()
        queue = list(g.objects(predicate=BF.adminMetadata))
        while queue:
            node = queue.pop()
            if node in admin_nodes:
                continue
            admin_nodes.add(node)
            queue.extend(o for o in g.objects(node) if isinstance(o, BNode))

        return {
            o
            for s, p, o in g
            if isinstance(o, URIRef)
            and o in cited
            and p not in RECORD_PREDICATES
            and not str(o).startswith(LC_RESOURCES)
            and (o in described or (p != BF.adminMetadata and s not in admin_nodes))
        }

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

        self._remove_admin_metadata(self.graph, bluecore_uri)

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

        try:
            cataloger_id = CURRENT_USER_ID.get()
        finally:
            if not cataloger_id:
                cataloger_id = "Unknown"
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

    def _remove_admin_metadata(self, graph: Graph, subject: URIRef | None = None):
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

    def _remove_triples_by_type(
        self, graph: Graph, predicate: URIRef, type_: URIRef
    ) -> None:
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

    def _subject(self, graph: Graph, class_: Node | None = None) -> IdentifiedNode:
        """
        Gets the subject from the supplied graph using the RDF type class. The
        subject must be an IdentifiedNode: either a URIRef or a BNode. If
        class_ is None, try to guess at the subject by assuming there is only one
        subject URIRef in the supplied graph.

        This method throws several exceptions because it is important for
        downstream processing that it behaves in a predictable way.
        """
        if isinstance(class_, Node):
            uris = list(set(graph.subjects(RDF.type, class_)))
        elif class_ is None:
            # TODO: this is a bit of a guess as to what the subject URI is for the
            # OtherResource graph, which assumes there is one subject URI and ignores BNodes.
            uris = list(set(filter(lambda s: isinstance(s, URIRef), graph.subjects())))
        else:
            raise BluecoreGraphError("Unexpected class_ type, must be URIRef or None")

        # a blank node described inline in another resource isn't the subject of
        # a subgraph, even though it is in the subgraph of the resource holding it
        uris = [s for s in uris if not self._anonymous_description(s)]

        # there should only be one subject
        if len(uris) == 0:
            raise BluecoreGraphError(f"Unable to find subject URI for {class_}")
        elif len(uris) != 1:
            # try removing any bnodes when more than one subject was found
            bnodes = len([s for s in uris if not isinstance(s, URIRef)])
            uris = list(filter(lambda s: isinstance(s, URIRef), uris))
            if len(uris) == 0:
                raise BluecoreGraphError(
                    f"Found {bnodes} blank node subjects for {class_} and no URI"
                )
            elif len(uris) != 1:
                raise BluecoreGraphError(
                    f"Found more than one subject URI for {class_}: {uris}"
                )

        # ensure we've got a BNode or URIRef
        if not isinstance(uris[0], IdentifiedNode):
            raise TypeError(f"Found unexpected subject identifier: {uris[0]}")

        return uris[0]

    def _mint_all_uris(self, class_: URIRef, session: Session) -> None:
        """
        Examine Bibframe Works and Instances in the graph, and mint Bluecore URIs for
        them as needed. This method takes into account that a resource with a non-Bluecore
        URI may already be in the database under in its derivedFrom URI.
        """
        match class_:
            case BF.Hub:
                subgraphs = self.hubs()
                sqla_class = Hub
            case BF.Work:
                subgraphs = self.works()
                sqla_class = Work
            case BF.Instance:
                subgraphs = self.instances()
                sqla_class = Instance
            case _:
                raise BluecoreGraphError(f"Can't mint URIs for class of type {class_}")

        for sg in subgraphs:
            uri = self._subject(sg, class_)

            if self._is_bluecore_uri(uri):
                # there's nothing to do here if it's already a bluecore URI
                continue
            else:
                # first look in the graph we are saving for an adminMetadata
                # derivedFrom assertion that indicates an already minted bluecore
                # URI for the external URI

                bluecore_uri = self._derived_from_subject(uri)

                # if not found look up the URI in the database to see if it has been
                # previously saved with an adminMetadata derivedFrom assertion

                if bluecore_uri is None:
                    resource = self._derived_from_query(
                        sqla_class, uri, session
                    ).first()
                    if resource is not None:
                        bluecore_uri = resource.uri

                # if we found an existing bluecore URI then we can update the graph to use it

                if bluecore_uri is not None:
                    self._switch_uris(
                        derived_from=uri, bluecore_uri=URIRef(bluecore_uri)
                    )

                # otherwise we need to mint a new bluecore uri and update the graph

                else:
                    derived_from = uri
                    bluecore_uri = self._mint_uri(class_)
                    self._switch_uris(derived_from, bluecore_uri)

    @staticmethod
    def _derived_from_query(
        sqla_class: type[Hub] | type[Work] | type[Instance],
        uri: IdentifiedNode,
        session: Session,
    ) -> Query:
        """
        Builds the query that finds a saved resource of the given class whose
        adminMetadata records a bf:derivedFrom assertion for the supplied URI.
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
        return session.query(sqla_class).where(
            sqla_class.type == sqla_class.__mapper__.polymorphic_identity,
            func.jsonb_path_query_first(
                sqla_class.data,
                literal_column("""'$.adminMetadata[*].derivedFrom."@id"'::jsonpath"""),
            ).op("#>>")(literal_column("'{}'::text[]"))
            == str(uri),
        )

    def _mint_uri(self, class_: URIRef) -> URIRef:
        """
        Mints a Bluecore URI for the given class.
        """
        uuid = uuid4()
        match class_:
            case BF.Hub:
                type_of = "hubs"
            case BF.Work:
                type_of = "works"
            case BF.Instance:
                type_of = "instances"
            case _:
                raise BluecoreGraphError(
                    f"Can't mint Bluecore URI for class of type {class_}"
                )

        return self.namespace[f"{type_of}/{uuid}"]

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

    def _switch_uris(self, derived_from: IdentifiedNode, bluecore_uri: URIRef) -> None:
        """
        Updates the supplied graph so that assertions involving the
        derived_from as the subject now use the bluecore_uri in its place.
        A bibframe:derivedFrom assertion is added to record the relationship
        if the derived_from is URIRef.
        """
        self._bump_revision()  # replace_uri + _generate_admin_metadata mutate self.graph
        self._minted_from[str(bluecore_uri)] = str(derived_from)
        if self._root is not None and derived_from == self._root:
            self.created_uri = bluecore_uri
        replace_uri(self.graph, derived_from, bluecore_uri)
        # only add derivedFrom assertions for URIs
        if isinstance(derived_from, URIRef):
            self._generate_admin_metadata(
                bluecore_uri, derived_from, stub=self._arrived_as_stub(bluecore_uri)
            )

    def _exclude_uri_from_other_resources(self, uri: Node) -> bool:
        """Checks if uri is in the BF, MADS, or RDF namespaces"""
        return uri in BF or uri in MADS or uri in RDF  # type: ignore

    def _is_bibframe_resource(self, uri: Node, graph: Graph) -> bool:
        """Checks if uri is a BIBFRAME Work or Instance"""
        for class_ in graph.objects(subject=uri, predicate=RDF.type):
            # In the future we may want to include Work, Instance and Hub subclasses
            # maybe through inference?
            if class_ in [BF.Work, BF.Instance, BF.Hub]:
                return True
        return False

    def _is_bluecore_uri(self, uri) -> bool:
        return uri in self.namespace

    def _strip_excluded_triples(self) -> None:
        """
        Remove the triples we never persist (see EXCLUDED_TRIPLE_TYPES) from
        self.graph. Called once from save() before subgraphs are extracted, so the
        cached per-entity subgraphs -- and the JSON-LD serialized from them in
        _persist_resources -- never contain these triples, and _persist_resources
        can serialize the shared subgraphs without copying them first.
        """
        self._bump_revision()  # mutates self.graph
        for predicate, type_ in EXCLUDED_TRIPLE_TYPES:
            self._remove_triples_by_type(self.graph, predicate, type_)

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
        self._bump_revision()  # mutates self.graph
        for admin_metadata in list(self.graph.subjects(BF.status, STUB_STATUS)):
            if (admin_metadata, RDF.type, BF.AdminMetadata) not in self.graph:
                continue
            self.graph.remove((admin_metadata, BF.status, STUB_STATUS))
            self.graph.add((admin_metadata, BF.status, DEFAULT_STATUS))

    def _arrived_as_stub(self, uri: Node) -> bool:
        """
        Whether this record carries only a stub of the resource rather than a
        description of it, which we tell from it arriving without adminMetadata
        of its own, or with the marker we wrote when it was last a stub.
        """
        if not self._spot_stubs:
            return False
        # uri may have been minted since, so check the uri it arrived with
        return self._minted_from.get(str(uri), str(uri)) not in self._described

    def _is_stub(self, class_: URIRef | None, uri: Node) -> bool:
        """Other Resources never carry adminMetadata, so they are left out."""
        return class_ is not None and self._arrived_as_stub(uri)

    def _keep_existing(self, class_: URIRef | None, uri: Node, obj) -> bool:
        """
        Leave a resource alone when an outside record comes round again for
        something we already describe in full, since it has nothing to add and
        would wipe out whatever has been edited since.
        """
        if class_ is None or not self._ingest:
            return False
        # already ours (a Marva or API save): it was never minted, so let it write
        if str(uri) not in self._minted_from:
            return False
        # a stub is only a placeholder, so a real description always replaces it
        return not self._stored_is_stub(obj)

    def _stored_is_stub(self, obj) -> bool:
        """Whether what we hold is still a placeholder rather than a description."""
        admin_metadata = obj.data.get("adminMetadata", []) if obj.data else []
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

    def _keeps_own_links(self, class_: URIRef, graph: Graph) -> bool:
        """
        Whether this resource's existing links should be left alone: it is one we
        only have a stub of, or only a reference to because an API save named a
        different kind as the one it is writing (an Instance save names its Work
        through bf:instanceOf), and it was already here to have links in
        the first place. One we just created has none, so it links like anything
        else.
        """
        uri = self._subject(graph, class_)
        is_reference = self._primary_class is not None and self._primary_class != class_
        return (is_reference or self._is_stub(class_, uri)) and str(
            uri
        ) not in self._created

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

    def _persist_resources(
        self, class_: URIRef | None, session: Session, is_primary: bool = True
    ) -> None:
        """
        Persist resources of the supplied type to the given database session. If
        the type is None then Other Resources are saved.

        When is_primary is True these resources are authoritatively upserted;
        otherwise they are references, which we create if absent but never
        overwrite -- so a reference (even one carrying a display label) can't
        clobber an existing full description. (save() sets is_primary per kind
        from its primary_class argument; None there means every kind is primary,
        the original behavior.)
        """
        match class_:
            case BF.Hub:
                resources = self.hubs()
                sqla_class = Hub
            case BF.Work:
                resources = self.works()
                sqla_class = Work
            case BF.Instance:
                resources = self.instances()
                sqla_class = Instance
            case None:
                resources = self.others()
                sqla_class = OtherResource

        # Write Works, Instances, and shared Other Resources in a deterministic,
        # sorted-by-URI order. Concurrent transactions then acquire locks on
        # these hot rows in the same order and serialize instead of deadlocking.
        if class_ is None:
            resources = sorted(resources, key=lambda g: str(self._subject(g, class_)))

        # Fetch the resources of this batch that already exist in a single query,
        # keyed by uri, rather than a SELECT per resource -- so re-saves and large
        # batches don't fan out into a round-trip each.
        subjects = [self._subject(g, class_) for g in resources]
        existing = {
            obj.uri: obj
            for obj in session.query(sqla_class).where(
                sqla_class.uri.in_([str(uri) for uri in subjects])
            )
        }

        for g, uri in zip(resources, subjects):
            obj = existing.get(str(uri))

            if obj is not None and (
                not is_primary
                or self._is_stub(class_, uri)
                or self._keep_existing(class_, uri, obj)
            ):
                # a reference to an existing resource: link it (later, in _link)
                # but never overwrite its stored description. Skip before building
                # its JSON-LD -- serializing and re-framing it would be wasted work
                # (this is the hot path when batch-loading records that share many
                # Other Resources).
                logger.debug(f"keeping existing {uri} (referenced, not primary)")
                continue

            # Serialize the shared (cached) subgraph directly. save() already
            # stripped EXCLUDED_TRIPLE_TYPES from self.graph up front (see
            # _strip_excluded_triples), so there's nothing to remove here and no
            # need to copy the subgraph to protect it from an in-place mutation.
            data = json.loads(g.serialize(format="json-ld"))

            if obj:
                obj.data = data
                logger.info(f"updating {uri}")
                # Sqlalchemy ORM already does a comparison between the retrieved object
                # and new object and will only do an UPDATE if they are different
                session.add(obj)
            else:
                if sqla_class == OtherResource:
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
                self._created.add(str(uri))

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

        # Sort all the link iterations by URI so that, like _persist_resources,
        # concurrent transactions acquire row locks in the same deterministic order.

        # use bibframe:instanceOf to link instances with works. A payload stating the
        # relationship the other way round, as bf:hasInstance, has already had this
        # direction added for it by _infer.
        for s, o in sorted(
            self.graph.subject_objects(BF.instanceOf),
            key=lambda pair: (str(pair[0]), str(pair[1])),
        ):
            # an anonymous description has no record of its own to link
            if self._anonymous_description(s) or self._anonymous_description(o):
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
            if self._anonymous_description(s) or self._anonymous_description(o):
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
        self._delete_other_links(BF.Work, session, cache)
        self._delete_other_links(BF.Instance, session, cache)

        # Only rebuild links for resources this record describes; a stub would
        # otherwise drop that resource's links to its subjects and agents.
        work_graphs = [
            g
            for g in sorted(self.works(), key=lambda g: str(self._subject(g, BF.Work)))
            if not self._keeps_own_links(BF.Work, g)
        ]
        instance_graphs = [
            g
            for g in sorted(
                self.instances(), key=lambda g: str(self._subject(g, BF.Instance))
            )
            if not self._keeps_own_links(BF.Instance, g)
        ]

        # Link each one to every Other Resource it cites that this payload
        # describes, and to every one it cites by uri alone that we hold a row
        # for: the links follow from the resource's own data, so a save citing a
        # term by uri alone doesn't lose them. A uri we hold nothing about stays
        # a bare reference. See _linkable_uris for what is never linked.
        described = {self._subject(g) for g in self.others()}
        cited = []
        for sqla_class, class_, graphs in (
            (Work, BF.Work, work_graphs),
            (Instance, BF.Instance, instance_graphs),
        ):
            for g in graphs:
                linkable = self._linkable_uris(g, self._cited_uris(g), described)
                cited.append((sqla_class, self._subject(g, class_), linkable))

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

    def _delete_other_links(
        self, class_: URIRef, session: Session, cache: dict
    ) -> None:
        """
        Delete existing links to OtherResources so that they can be replaced
        with new ones. Resources this record only stubs out keep the links they
        have, since a stub isn't a description of them.
        """
        match class_:
            case BF.Work:
                graphs = self.works()
                sqla_class = Work
            case BF.Instance:
                graphs = self.instances()
                sqla_class = Instance

        for g in graphs:
            uri = self._subject(g, class_)
            if self._keeps_own_links(class_, g):
                continue
            bf_resource = self._resolve(session, sqla_class, uri, cache)

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
