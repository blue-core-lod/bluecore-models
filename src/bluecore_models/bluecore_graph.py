import datetime
import json
import logging
from uuid import uuid4

from psycopg2 import errors as psycopg2_errors
from rdflib import XSD, BNode, Graph, IdentifiedNode, Literal, Namespace, Node, URIRef
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError, OperationalError
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
from bluecore_models.namespaces import BF, BFLC, MADS, RDF
from bluecore_models.utils.graph import generate_entity_graph, replace_uri

logger = logging.getLogger(__name__)

# - DeadlockDetected / SerializationFailure: two writers locked shared rows in a
#   conflicting way; Postgres aborted one of them.
# - UniqueViolation: two writers both did get-or-create on the same brand-new
#   shared Other Resource uri and both tried to INSERT it. On a retry the SELECT
#   finds the row the winner committed and takes the update/no-op path instead.
RETRYABLE_PG_ERRORS = (
    psycopg2_errors.DeadlockDetected,
    psycopg2_errors.SerializationFailure,
    psycopg2_errors.UniqueViolation,
)

# How many times to attempt save() before giving up on serialization failures.
SAVE_MAX_ATTEMPTS = 3

# Default AdminMetadata values used when a caller doesn't override them.
DEFAULT_STATUS = URIRef("http://id.loc.gov/vocabulary/mstatus/c")
DEFAULT_AGENT = URIRef("http://id.loc.gov/vocabulary/organizations/cbc")
DEFAULT_DESC_AUTH = URIRef("http://id.loc.gov/vocabulary/marcauthen/pcc")
DEFAULT_DESC_LANG = URIRef("http://id.loc.gov/vocabulary/languages/eng")
DEFAULT_DESC_LEVEL = URIRef("http://id.loc.gov/ontologies/bibframe-2-6-0/")


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
    or IntegrityError wrapping the psycopg2 error in .orig).
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
    """
    bg = BluecoreGraph(graph, namespace)
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
    6. Save relationships between the Works, Instances and Other Resources, being
       careful to remove existing many-to-many relations with Other Resources prior
       to adding new ones.
    """

    def __init__(self, graph: Graph, namespace: str = "https://bcld.info/"):
        """
        Instantiate a BluecoreGraph using an rdflib Graph, and an optional
        Bluecore Namespace URL: the default is https://bcld.info.
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
        # The per-entity subgraph extractions (works/instances/hubs/others) are
        # expensive and get called repeatedly across the save. We cache them keyed
        # on _revision, which is bumped every time the graph is mutated so the
        # cache can't go stale. Every mutation of self.graph funnels through
        # _infer (once, here) and _switch_uris (during minting) -- keep it that
        # way, or bump _revision at any new mutation site (see _bump_revision).
        self._revision = 0
        self._subgraph_cache: dict[str, tuple[int, list[Graph]]] = {}
        self._infer()

    def _bump_revision(self) -> None:
        """
        Record that self.graph has been mutated, invalidating the subgraph cache.
        Call this from anywhere that changes self.graph.
        """
        self._revision += 1

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

        # create inverse properties
        inverse_properties = [
            (BF.hasInstance, BF.instanceOf),
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
        Returns a list of subgraphs for subjects of a given type.
        """
        return [
            generate_entity_graph(self.graph, s)
            for s in self.graph.subjects(RDF.type, bibframe_class)
        ]

    def _extract_others(self) -> list[Graph]:
        """
        Returns a list of subgraphs for resources that are referenced but not fully
        described in the Bluecore Graph.
        """
        others = []
        other_uris = set()

        for g in self.works() + self.instances():
            # iterate through each object in the graph
            for o in g.objects():
                # ignore the object if it:
                # - is not a URI (exclude BNodes, Literals)
                # - is a resource from the Bibframe or MADS vocabularies
                # - is a Bibframe Work or Instance that is in g1
                if (
                    not isinstance(o, URIRef)
                    or self._exclude_uri_from_other_resources(o)
                    or self._is_bibframe_resource(o, self.graph)
                ):
                    continue

                # otherwise return the object URI, and its graph
                if o in self.graph.subjects() and o not in other_uris:
                    others.append(generate_entity_graph(self.graph, o))
                    other_uris.add(o)

        return others

    def _generate_admin_metadata(
        self,
        bluecore_uri: URIRef,
        source_uri: URIRef,
        status: URIRef = DEFAULT_STATUS,
        agent: URIRef = DEFAULT_AGENT,
        desc_auth: URIRef = DEFAULT_DESC_AUTH,
        desc_lang: URIRef = DEFAULT_DESC_LANG,
        desc_level: URIRef = DEFAULT_DESC_LEVEL,
    ):
        """
        Generates two bf:AdminMetadata blank nodes for incoming RDF resources that are
        derived from existing RDF resources
        """

        time_stamp = datetime.datetime.now(datetime.UTC)

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

    def _remove_bnode(self, graph: Graph, bnode: BNode) -> None:
        """Recursively removes a blank node and any blank nodes it references."""
        for pred, obj in list(graph.predicate_objects(subject=bnode)):
            graph.remove((bnode, pred, obj))
            # remove any nested blank nodes (e.g. bf:agent [ a bf:Agent ... ])
            if isinstance(obj, BNode):
                self._remove_bnode(graph, obj)

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
            self._remove_bnode(graph, admin_metadata)
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
                self._remove_bnode(graph, obj)
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

        # there should only be one subject
        if len(uris) == 0:
            raise BluecoreGraphError(f"Unable to find subject URI for {class_}")
        elif len(uris) != 1:
            # try removing any bnodes when more than one subject was found
            uris = list(filter(lambda s: isinstance(s, URIRef), uris))
            if len(uris) != 1:
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
                    resource = (
                        session.query(sqla_class)
                        .where(
                            func.jsonb_path_query_first(
                                sqla_class.data,
                                '$.adminMetadata[*].derivedFrom."@id"',
                            ).op("#>>")("{}")
                            == str(uri)
                        )
                        .first()
                    )
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
        replace_uri(self.graph, derived_from, bluecore_uri)
        # only add derivedFrom assertions for URIs
        if isinstance(derived_from, URIRef):
            self._generate_admin_metadata(bluecore_uri, derived_from)

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

            if obj is not None and not is_primary:
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

    def _link(self, session) -> None:
        """
        Save relations between Instances, Works and Other Resources in the graph.
        """

        # Cache resource lookups for the duration of this link operation. _link
        # resolves the same Works, Instances and Other Resources repeatedly (a
        # Work is re-queried for every Other Resource it links to, etc.); caching
        # by (type, uri) turns those repeats into a single query each. Every
        # resource was flushed by save() before _link runs, so the first lookup
        # already sees it.
        cache: dict[tuple, object] = {}

        # use bibframe:instanceOf assertions to link instances with works
        #
        # Maybe we should have a simple inference step early on that infers missing
        # bibframe:instanceOf assertions, and possible other inverse properties
        # that we might rely on?

        # sort all the link iterations by URI so that, like _persist_resources, concurrent
        # transactions acquire row locks in the same deterministic order.

        for s, o in sorted(
            self.graph.subject_objects(BF.instanceOf),
            key=lambda pair: (str(pair[0]), str(pair[1])),
        ):
            logger.info(f"linking {s} to {o}")
            instance = self._get_first(session, Instance, s, cache)
            work = self._get_first(session, Work, o, cache)
            instance.work = work
            session.add(instance)

        # use bibframe:hasInstance to link works with instances
        for s, o in sorted(
            self.graph.subject_objects(BF.hasInstance),
            key=lambda pair: (str(pair[0]), str(pair[1])),
        ):
            logger.info(f"linking {s} to {o}")
            work = self._get_first(session, Work, s, cache)
            instance = self._get_first(session, Instance, o, cache)
            instance.work = work
            session.add(instance)

        # link Works and Instances to their Other Resources, which is a bit more
        # complex since a Work or Instance has a many to many relationship with
        # Other Resources

        # first remove any existing Other Resource linkages between Works and
        # Instances so that they can be replaced with the new ones
        self._delete_other_links(BF.Work, session, cache)
        self._delete_other_links(BF.Instance, session, cache)

        work_graphs = sorted(self.works(), key=lambda g: str(self._subject(g, BF.Work)))
        instance_graphs = sorted(
            self.instances(), key=lambda g: str(self._subject(g, BF.Instance))
        )

        for other_graph in sorted(self.others(), key=lambda g: str(self._subject(g))):
            other_uri = self._subject(other_graph)

            # look at each Work graph and see if the Other Resource URI appears
            # as an object within it. If it does create a link between the Work and
            # the Other Resource.
            for g in work_graphs:
                if other_uri in g.objects():
                    work_uri = self._subject(g, BF.Work)
                    logger.info(f"linking {work_uri} to {other_uri}")
                    work_model = self._resolve(session, Work, work_uri, cache)
                    other_model = self._resolve(
                        session, OtherResource, other_uri, cache
                    )
                    session.add(
                        BibframeOtherResources(
                            bibframe_resource=work_model, other_resource=other_model
                        )
                    )

            # do the same thing for each Instance graph
            # TODO: maybe this very similar logic could be abstracted into a helper method
            # but that might also further obscure what is going on?
            for g in instance_graphs:
                if other_uri in g.objects():
                    instance_uri = self._subject(g, BF.Instance)
                    logger.info(f"linking {instance_uri} to {other_uri}")
                    instance_model = self._resolve(
                        session, Instance, instance_uri, cache
                    )
                    other_model = self._resolve(
                        session, OtherResource, other_uri, cache
                    )
                    session.add(
                        BibframeOtherResources(
                            bibframe_resource=instance_model, other_resource=other_model
                        )
                    )

    def _delete_other_links(
        self, class_: URIRef, session: Session, cache: dict
    ) -> None:
        """
        Delete existing links to OtherResources so that they can be replaced
        with new ones.
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
