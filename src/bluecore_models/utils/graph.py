"""Utility functions for working with RDF graphs."""

import logging
from typing import Any, NamedTuple

import bibframe_json
from pyld import jsonld
from rdflib import (
    DCTERMS,
    RDF,
    RDFS,
    BNode,
    Graph,
    IdentifiedNode,
    Literal,
    Node,
    URIRef,
)
from rdflib.plugins import sparql

from bluecore_models.namespaces import BF, BFLC, LCLOCAL, MADS

logger = logging.getLogger(__name__)

# Rewrites every occurrence of ?old_uri to ?new_uri in a graph, in both subject
# position (?old_uri ?p ?o) and object position (?s ?pp ?old_uri).
_REPLACE_URI_SPARQL = sparql.prepareUpdate("""
DELETE {
  ?old_uri ?p ?o .
  ?s ?pp ?old_uri .
}
INSERT {
  ?new_uri ?p ?o .
  ?s ?pp ?new_uri .
}
WHERE {
  {
    ?old_uri ?p ?o .
  }
  UNION {
    ?s ?pp ?old_uri .
  }
}
""")

# The context comes from bibframe-json, which publishes it.
CONTEXT_URL: str = bibframe_json.CONTEXT_URL
CONTEXT: dict[str, Any] = bibframe_json.context()["@context"]

# The legacy context is transitional, and the condition for deleting it is a number rather
# than a judgment. Once the reframe DAG has swept an environment, every row
# there names its context and the absent case cannot arise, so:
#
#     SELECT count(*) FROM resource_base
#     WHERE type <> 'profiles' AND NOT data ? '@context';
#
# At zero, in every environment, LEGACY_CONTEXT goes and `terms_for` raises on
# a document with no `@context` instead of guessing one. Profiles are excluded
# because their data is a JSON-LD array and was never framed. Do not read zero
# once and delete: a deploy still running an older bluecore-models writes rows
# without the marker, so the count has to stay at zero after every writer is
# upgraded, not merely reach it.
LEGACY_CONTEXT: dict[str, Any] = {
    "@vocab": "http://id.loc.gov/ontologies/bibframe/",
    "bflc": "http://id.loc.gov/ontologies/bflc/",
    "mads": "http://www.loc.gov/mads/rdf/v1#",
    "rdf": "http://www.w3.org/1999/02/22-rdf-syntax-ns#",
    "rdfs": "http://www.w3.org/2000/01/rdf-schema#",
    "hasInstance": {"@type": "@id"},
    "hasWork": {"@type": "@id"},
    "instanceOf": {"@type": "@id"},
}


def _refuse_to_fetch(url: str, options: dict[str, Any] | None = None) -> dict:
    """The fallback for a context bibframe-json does not ship.

    Framing has to refuse the same contexts reading refuses, and it has to
    refuse them on purpose. `bibframe_json.document_loader()` otherwise falls
    through to pyld's default, which is whichever of these pyld settled on at
    import:

        try:    _default_document_loader = requests_document_loader()
        except ImportError:
            _default_document_loader = dummy_document_loader

    `requests` is not a dependency here today, so that default raises and the
    write path refuses by accident. Anything that pulls `requests` in -- and
    plenty of libraries do, transitively -- would turn the fallback into a real
    HTTP loader and framing would start fetching inside a flush, with no change
    to any code in this repo.
    """
    raise ValueError(
        f"{url} is not a context this package ships, and fetching one is not "
        f"something this library does; bibframe_json.VERSIONS has "
        f"{bibframe_json.VERSIONS}"
    )


# So pyld answers for CONTEXT_URL out of the installed package while framing,
# and refuses anything else rather than reaching for the network.
jsonld.set_document_loader(bibframe_json.document_loader(_refuse_to_fetch))


def validate_jsonld(document: dict[str, Any], uri: str) -> list[Any]:
    """Check a framed document against bibframe-json, and report rather than refuse.

    Deliberately not a gate. The documents most worth knowing about are the ones
    framing has just mangled, and refusing to store those is refusing to store
    the only copy. So this logs and returns; the caller writes either way.

    The `ontology` layer is off. Those findings are about BIBFRAME's own domains
    and ranges, which real records contradict often enough that they are
    warnings about cataloging rather than about this shape.

    One known class of finding, as of writing: a blank node carrying an `@id`,
    in roughly 9% of Works. The labels are load-bearing there -- something else
    in the same document refers to them -- so they are not strippable, and
    skolemizing them is tracked separately.
    """
    try:
        findings = bibframe_json.validate(document, ontology=False)
    except Exception as error:  # noqa: BLE001 - monitoring must not break a write
        logger.warning(f"{uri}: could not validate: {error}")
        return []
    if findings:
        logger.info(
            f"{uri}: {len(findings)} bibframe-json findings: "
            + "; ".join(str(f) for f in findings[:5])
        )
    return findings


def terms_for(document: dict[str, Any]) -> dict[str, Any]:
    """The context a document is read with: ours, or none, or an error.

    Two forms are accepted and there is no third:

    1. no `@context`, which is read with LEGACY_CONTEXT
    2. a `@context` that is a URL bibframe-json ships, resolved from the
       installed package

    Anything else raises. That rules out an inline context, an array of them,
    and a URL we do not ship, which between them are every way a document can
    reach for a context we have not vetted.

    The alternative was to allow inline contexts and try to establish that each
    one holds no remote reference, and the trouble is that a context can reach
    the network in more ways than are obvious: a URL in an array entry, an
    `@import`, a scoped `@context` on a term definition. rdflib follows all of
    them. Each is easy to handle once seen and the set is easy to believe you
    have finished. An allowlist does not have to be finished -- a reader
    checking that this library makes no network request has one function to
    read and two cases in it.

    What that costs is a document whose context genuinely lives elsewhere,
    which can no longer be read here at all. Nothing in Blue Core is such a
    document: every stored resource is framed on the way in and names our
    context, and every profile in stage carries no context at all, being
    expanded JSON-LD with full property URIs. A caller who needs otherwise
    wants rdflib directly rather than this function.
    """
    named = document.get("@context")
    if named is None:
        return LEGACY_CONTEXT
    # One rejection for both ways of failing the rule, since a caller can do
    # nothing different about an inline context than about a URL we do not
    # ship: neither is readable here.
    shipped = bibframe_json.context_for(named) if isinstance(named, str) else None
    if shipped is None:
        # Named rather than repr'd: an inline context is 251 terms, and a
        # traceback carrying all of them buries the sentence explaining it.
        got = (
            repr(named)
            if isinstance(named, str)
            else f"an inline {type(named).__name__}"
        )
        raise ValueError(
            f"@context must be absent or a URL bibframe-json ships "
            f"({', '.join(bibframe_json.VERSIONS)}), got {got}. An inline "
            f"context is not read here because it can reference a remote one."
        )
    return shipped["@context"]


def _without_context(document: dict[str, Any]) -> dict[str, Any]:
    """The document minus `@context`, as a copy of it.

    The context is resolved separately and handed to rdflib as `context=`, so
    it has to come out of the document first or the parser finds the URL and
    fetches it. It does a copy so as not to mutate an object that is given and
    cause hard to track bugs if there is ever multithreading.
    """
    return {key: value for key, value in document.items() if key != "@context"}


def bind_namespaces(graph: Graph) -> Graph:
    """Bind the prefixes this codebase expects to see in serialized output.

    Applied after parsing as well as before it. rdflib's JSON-LD parser replaces
    the store's prefix bindings with those of the context it is handed, and
    LEGACY_CONTEXT reaches BIBFRAME through `@vocab` rather than a `bf` prefix,
    so parsing with a context would otherwise drop the `bf` binding and change
    how every Turtle serialisation reads.
    """
    graph.namespace_manager.bind("bf", BF, override=True)
    graph.namespace_manager.bind("bflc", BFLC, override=True)
    graph.namespace_manager.bind("mads", MADS, override=True)
    graph.namespace_manager.bind("lclocal", LCLOCAL, override=True)
    return graph


def init_graph() -> Graph:
    """Initialize a new RDF graph with the necessary namespaces."""
    return bind_namespaces(Graph())


def load_jsonld(jsonld_data: list[Any] | dict[str, Any]) -> Graph:
    """
    Load a JSON-LD represented as a dict (or a list of dicts) into a rdflib Graph.

    **A context is never fetched**, and the rule that guarantees it is small
    enough to check by reading `terms_for`: a document carries no `@context`,
    or one that is a URL bibframe-json ships. Anything else raises ValueError.
    An array of node objects is held to the same rule member by member.

    The restriction is deliberate. This runs inside ORM flushes, where a fetch
    holds a transaction open against a third-party server, and across sweeps of
    the whole table, where nothing caches the result and the same URL would be
    asked for once per row.

    It is an allowlist on purpose. The alternative, allowing an inline context
    and checking it for remote references, means finding every way a context
    can reach the network -- a URL in an array, an `@import`, a scoped
    `@context` on a term definition -- and rdflib follows all of them. Refusing
    inline contexts closes the ways nobody has thought of as well.

    So a URL is resolved out of the installed bibframe-json and handed to
    rdflib as `context=`, rather than being left in the document for the parser
    to find and fetch. `_refuse_to_fetch` holds the same line on the framing
    side, where pyld does the resolving instead of rdflib.
    """
    graph = init_graph()
    # rdflib's json-ld parsing from a python object doesn't support a list yet
    # see: https://github.com/RDFLib/rdflib/issues/3166
    match jsonld_data:
        case list():
            # An array document is an array of node objects, each of which may
            # carry its own @context -- JSON-LD 1.1 §4.1, "Using multiple
            # contexts". So every member is checked on its own, against the
            # same rule.
            #
            # Passing no context, as this used to, was wrong twice over. A
            # member naming its context by URL had it fetched -- once per
            # document and never cached, so a sweep would ask the same server
            # once a row, and a profile save would do it inside a flush while
            # holding a transaction open. A member with no context lost its
            # terms silently, leaving `@type: ["Work"]` to resolve "Work" as a
            # relative IRI against the process's working directory.
            for obj in jsonld_data:
                if not isinstance(obj, dict):
                    # Refused rather than handed to rdflib. A string member is
                    # read as a document in its own right, context and all, so
                    # it was the one way left to reach the network from here --
                    # and an array of anything but node objects is not JSON-LD
                    # to begin with.
                    raise TypeError(
                        f"a JSON-LD array holds node objects, got a "
                        f"{type(obj).__name__}"
                    )
                terms = terms_for(obj)
                graph.parse(
                    data=_without_context(obj),  # type: ignore[arg-type]
                    format="json-ld",
                    context=terms,
                )
        case dict():
            terms = terms_for(jsonld_data)
            graph.parse(
                data=_without_context(jsonld_data),  # type: ignore[arg-type]
                format="json-ld",
                context=terms,
            )
        case _:
            # TypeError rather than the ValueError this used to raise, to match
            # the member check above and `reframe` in bluecore-workflows, which
            # says the same thing about the same mistake. Nothing caught the
            # old type specifically; `profile_refs` catches both.
            raise TypeError(
                f"JSON-LD must be a list or dict, got {type(jsonld_data).__name__}"
            )

    # Parsing with a context replaces the store's prefix bindings. See
    # bind_namespaces.
    return bind_namespaces(graph)


def replace_uri(graph: Graph, old_uri: IdentifiedNode, new_uri: URIRef) -> None:
    """
    Rewrite every occurrence of old_uri to new_uri in the graph, in both subject
    position (old_uri ?p ?o) and object position (?s ?pp old_uri). old_uri may be
    a blank node or a URIRef; new_uri is always a real (minted) URIRef.
    """
    graph.update(
        _REPLACE_URI_SPARQL,
        initBindings={"old_uri": old_uri, "new_uri": new_uri},
    )


def _check_for_namespace(node: Node) -> bool:
    """Check if a node is in the LCLOCAL or DCTERMS namespace."""
    return node in LCLOCAL or node in DCTERMS  # type: ignore


def _expand_bnode(graph: Graph, entity_graph: Graph, bnode: BNode) -> None:
    """Expand a blank node in the entity graph."""

    # if the blank node is already present in the entity graph there's no need to add it
    # this prevents infinite recursion
    if bnode in entity_graph.subjects():
        return

    for pred, obj in graph.predicate_objects(subject=bnode):
        if _check_for_namespace(pred) or _check_for_namespace(obj):
            continue
        entity_graph.add((bnode, pred, obj))
        if isinstance(obj, BNode):
            _expand_bnode(graph, entity_graph, obj)


def _term_key(term: Node) -> str:
    """A comparison key for a URI or literal.

    Deliberately avoids Node.n3(), which raises on the malformed URIs that turn up
    in real catalog data (MARC subfield text that leaked into a URI, for example).
    Datatype and language are kept significant, so "1987"^^xsd:date and "1987" are
    different values, as are the same string tagged @en and @fr.
    """
    if isinstance(term, Literal):
        return f"lit\x1f{term}\x1f{term.datatype}\x1f{term.language}"
    return f"ref\x1f{term}"


def _bnode_fingerprint(
    graph: Graph, bnode: BNode, ancestors: frozenset[BNode] = frozenset()
) -> str:
    """A content fingerprint for a blank node's subtree.

    Two blank nodes with equal fingerprints describe the same thing, even though
    their generated identifiers differ. For

        [ a bf:Title ; bf:mainTitle "AI & society" ]

    the fingerprint is, with URIs shortened and the \x1f separators shown as ~:

        {ref~rdf:type ref~bf:Title|ref~bf:mainTitle lit~AI & society~None~None}

    The parts are sorted, so the order statements arrived in makes no difference,
    and nested blank nodes are fingerprinted recursively so nesting is compared
    structurally rather than by identifier.

    ancestors is the path from the outermost node to this one, which is how a cycle
    is spotted. It is a frozenset, and a fresh one is passed at each level, so a
    blank node reachable from two different branches is still fingerprinted in full
    in both -- a single set shared across the recursion would report the second
    occurrence as a cycle and give the wrong answer.
    """
    if bnode in ancestors:
        return "<cycle>"
    parts = []
    for pred, obj in graph.predicate_objects(subject=bnode):
        if isinstance(obj, BNode):
            key = _bnode_fingerprint(graph, obj, ancestors | {bnode})
        else:
            key = _term_key(obj)
        parts.append(f"{_term_key(pred)} {key}")
    return "{" + "|".join(sorted(parts)) + "}"


class DuplicateValue(NamedTuple):
    """A resource carrying the same blank node value more than once.

    subject/predicate locate it and copies is how many identical values there are,
    so copies - 1 are redundant. label is a rendering of the value for a log
    message, or None where the value has nothing to render (see _duplicate_label).

    redundant holds those copies: every copy past the first, which is
    what strip_duplicate_bnode_values removes. Which one is kept is arbitrary and
    makes no difference, the group being identical in content by construction.
    """

    subject: Node
    predicate: Node
    copies: int
    label: str | None
    redundant: tuple[BNode, ...]


# Predicates carrying a human-readable rendering of a value, best first.
_LABEL_PREDICATES = (BF.mainTitle, RDFS.label, MADS.authoritativeLabel, RDF.value)


def _duplicate_label(graph: Graph, bnode: BNode) -> str | None:
    """A short rendering of a value for the log message, when it has one.

    Only the value's own labelling properties are consulted. A value with none of
    them -- an rdf:List, a bf:Contribution whose label sits on its bf:agent -- is
    reported by subject and predicate alone, which is enough to find it in the
    source.
    """
    for predicate in _LABEL_PREDICATES:
        value = graph.value(subject=bnode, predicate=predicate)
        if value is not None:
            return str(value).strip()
    return None


def find_duplicate_bnode_values(graph: Graph) -> list[DuplicateValue]:
    """Find blank node values repeated with identical content under one property.

    Blank nodes have no identity, so two of them are distinct terms even when they
    describe exactly the same thing. RDF collapses a repeated URI or literal for
    free; it cannot do that for blank nodes, so a document asserting the same blank
    node value twice leaves the resource holding two values that persist, export and
    display as duplicates. This is reported:

        <.../instances/20133027> bf:title [ a bf:Title ; bf:mainTitle "AI & society" ] ,
                                          [ a bf:Title ; bf:mainTitle "AI & society" ] .

    Repeating a property is not itself a problem, and most of what looks like
    repetition here is legitimate. None of these are reported:

        # values that differ, however slightly -- comparison is by content, all the
        # way down, with rdf:type, datatype and language all significant
        <.../works/20133027> bf:title [ a bf:Title        ; bf:mainTitle "AI & society" ] ,
                                      [ a bf:VariantTitle ; bf:mainTitle "AI & society" ] ,
                                      [ a bf:VariantTitle ; bf:mainTitle "AI and society" ] .

        # a repeated URI or literal, which RDF has already merged into one triple
        <.../works/20133027> bf:subject <.../subjects/sh85008180> ,
                                        <.../subjects/sh85008180> .

        # the same value on two different resources, which says something about each
        <.../works/20133027>     bf:adminMetadata [ a bf:AdminMetadata ; bf:date "2026" ] .
        <.../instances/20133027> bf:adminMetadata [ a bf:AdminMetadata ; bf:date "2026" ] .

    A finding therefore always means a redundant assertion.

    Read-only: reports what it finds and changes nothing.
    """
    duplicates = []
    for subject, predicate in set(graph.subject_predicates()):
        by_content: dict[str, list[BNode]] = {}
        for obj in graph.objects(subject=subject, predicate=predicate):
            if isinstance(obj, BNode):
                by_content.setdefault(_bnode_fingerprint(graph, obj), []).append(obj)
        for nodes in by_content.values():
            if len(nodes) > 1:
                duplicates.append(
                    DuplicateValue(
                        subject,
                        predicate,
                        len(nodes),
                        _duplicate_label(graph, nodes[0]),
                        tuple(nodes[1:]),
                    )
                )
    return duplicates


def remove_bnode(graph: Graph, bnode: BNode) -> None:
    """Recursively removes a blank node and any blank nodes it references."""
    for pred, obj in list(graph.predicate_objects(subject=bnode)):
        graph.remove((bnode, pred, obj))
        # remove any nested blank nodes (e.g. bf:agent [ a bf:Agent ... ])
        if isinstance(obj, BNode):
            remove_bnode(graph, obj)


def _remove_unshared_bnode(graph: Graph, bnode: BNode) -> None:
    """Remove a blank node's description, stopping at anything still shared.

    A blank node can be the object of more than one triple: two values can nest
    the same node rather than a copy of it, and _bnode_fingerprint contemplates
    exactly that in noting a node "reachable from two different branches". Such a
    node is left described, because something we were not asked to touch is still
    pointing at it -- and sharing a nested node is *why* two values come out
    identical, so it is the ordinary case here rather than a curiosity.

    The caller unlinks the node first, so the entry check sees the graph without
    that reference. Each edge is likewise removed before its object is considered,
    so a nested node isn't held up by the very edge we are removing.

    Anything left behind is unreachable from the subject, and generate_entity_graph
    walks out from there, so it is never persisted. A node caught in a cycle is
    held by its own descendant and so stays, which is that same harmless case.
    """
    if (None, None, bnode) in graph:
        return
    for pred, obj in list(graph.predicate_objects(subject=bnode)):
        graph.remove((bnode, pred, obj))
        if isinstance(obj, BNode):
            _remove_unshared_bnode(graph, obj)


def strip_duplicate_bnode_values(graph: Graph) -> list[DuplicateValue]:
    """Remove blank node values a resource carries more than once.

    See find_duplicate_bnode_values for what counts as a duplicate, and for the
    kinds of repetition that are left alone. One copy of each duplicated value is
    kept, so nothing is lost: a finding always means the same value asserted more
    than once, never a distinction being drawn.

    Mutates the graph and returns what it removed, so the caller can report on it.
    Running it again reports nothing, there being nothing left to remove.
    """
    duplicates = find_duplicate_bnode_values(graph)
    for duplicate in duplicates:
        for bnode in duplicate.redundant:
            graph.remove((duplicate.subject, duplicate.predicate, bnode))
            _remove_unshared_bnode(graph, bnode)
    return duplicates


def generate_entity_graph(graph: Graph, entity: Node) -> Graph:
    """Generate an entity graph from a larger RDF graph."""
    entity_graph = init_graph()
    for pred, obj in graph.predicate_objects(subject=entity):
        if _check_for_namespace(pred) or _check_for_namespace(obj):
            continue
        entity_graph.add((entity, pred, obj))
        if isinstance(obj, BNode):
            _expand_bnode(graph, entity_graph, obj)
    return entity_graph


def get_bf_classes(rdf_data: list[Any] | dict[str, Any], uri: str) -> list:
    """Restrieves all of the resource's BIBFRAME classes from a graph."""
    graph = load_jsonld(rdf_data)
    classes = []
    for class_ in graph.objects(subject=URIRef(uri), predicate=RDF.type):
        if class_ in BF:  # type: ignore
            classes.append(class_)
    return classes


def _as_arrays(node: Any) -> Any:
    """Force every property that is not a JSON-LD keyword to a list.

    @context is left alone. It is a vocabulary rather than data, and coercing its
    values would rewrite it into something no processor can read. Value Objects
    are similarly not converted.

    If we ever have a more detailed context using @set we could rely simply on
    JSON-LD compaction here.
    """
    if isinstance(node, list):
        return [_as_arrays(item) for item in node]
    if not isinstance(node, dict):
        return node
    framed = {}
    for key, value in node.items():
        if key == "@context":
            framed[key] = value
            continue
        # convert unless it's a Value Object (only a single value)
        if key == "@type" and "@value" not in node:
            framed[key] = value if isinstance(value, list) else [value]
            continue
        value = _as_arrays(value)
        framed[key] = (
            value if key.startswith("@") or isinstance(value, list) else [value]
        )
    return framed


def frame_jsonld(
    bluecore_uri: str, jsonld_data: list[Any] | dict[str, Any]
) -> dict[str, Any]:
    """Frames the JSON-LD data to a specific structure.

    Every property in the result is a list, even of one value. See _as_arrays.
    The coercion adds and removes no triples, and applying it twice is the same
    as applying it once, so it is safe to re-run over already framed data -- which
    the reframe DAG in bluecore-workflows relies on.

    The result names its context by URL. pyld returns the terms inlined, which is
    around 12,000 bytes of vocabulary in front of the description it is about, so
    the URL replaces them: a document that says which context framed it can be
    read back without anyone having to assume. terms_for is the other half.
    """
    framed = _as_arrays(
        jsonld.frame(
            jsonld_data,
            {
                "@context": CONTEXT,
                "@id": bluecore_uri,
                "@embed": "@always",
            },
        )
    )
    framed["@context"] = CONTEXT_URL
    return framed


def framed_for_storage(
    bluecore_uri: str, jsonld_data: list[Any] | dict[str, Any]
) -> dict[str, Any]:
    """What the database should hold for this resource.

    One definition of the write path, because there are two callers and they
    have already drifted apart once. `set_jsonld` calls this on an ORM write;
    the reframe DAG in bluecore-workflows calls it directly, because going
    through the ORM would fire `after_update` and record a cataloging edit for
    what is only a re-serialisation.

    That DAG used to reproduce the logic instead, and when `@context` started
    being stored it was still stripping it on the way out -- which would have
    quietly undone the change for every row it touched. Reproducing a write path
    is the kind of duplication that looks harmless until the original moves.

    A document with no `@context` gets LEGACY_CONTEXT, since its compact keys
    came from there. One that names or carries a context is left to say so for
    itself.
    """
    if isinstance(jsonld_data, dict) and "@context" not in jsonld_data:
        jsonld_data = {**jsonld_data, "@context": LEGACY_CONTEXT}
    return frame_jsonld(bluecore_uri, jsonld_data)
