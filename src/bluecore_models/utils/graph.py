"""Utility functions for working with RDF graphs."""

import logging
from typing import Any, NamedTuple

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

CONTEXT: dict[str, Any] = {
    "@vocab": "http://id.loc.gov/ontologies/bibframe/",
    "bflc": "http://id.loc.gov/ontologies/bflc/",
    "mads": "http://www.loc.gov/mads/rdf/v1#",
    "rdf": "http://www.w3.org/1999/02/22-rdf-syntax-ns#",
    "rdfs": "http://www.w3.org/2000/01/rdf-schema#",
    "hasInstance": {"@type": "@id"},
    "hasWork": {"@type": "@id"},
    "instanceOf": {"@type": "@id"},
}


def init_graph() -> Graph:
    """Initialize a new RDF graph with the necessary namespaces."""
    new_graph = Graph()
    new_graph.namespace_manager.bind("bf", BF)
    new_graph.namespace_manager.bind("bflc", BFLC)
    new_graph.namespace_manager.bind("mads", MADS)
    new_graph.namespace_manager.bind("lclocal", LCLOCAL)
    return new_graph


def load_jsonld(jsonld_data: list[Any] | dict[str, Any]) -> Graph:
    """
    Load a JSON-LD represented as a Python list or dict into a rdflib Graph.
    """
    graph = init_graph()
    # rdflib's json-ld parsing from a python object doesn't support a list yet
    # see: https://github.com/RDFLib/rdflib/issues/3166
    match jsonld_data:
        case list():
            # parse each JSON-LD dict in the list into the graph
            for obj in jsonld_data:
                graph.parse(data=obj, format="json-ld")
        case dict():
            if "@context" not in jsonld_data:
                jsonld_data["@context"] = CONTEXT
            graph.parse(data=jsonld_data, format="json-ld")  # type: ignore
        case _:
            raise ValueError(
                f"JSON-LD must be a list or dict, got {type(jsonld_data).__name__}"
            )

    return graph


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
    """

    subject: Node
    predicate: Node
    copies: int
    label: str | None


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

    A finding therefore always means a redundant assertion rather than a distinction
    to be interpreted. It usually means the document described the resource in more
    than one place: LC's marc2bibframe2 v3.0.0 decomposed each Item's bf:itemOf
    back-link and gave it a courtesy bf:title, so an Instance with three Items ended
    up with four identical titles.

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
                    )
                )
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


def frame_jsonld(
    bluecore_uri: str, jsonld_data: list[Any] | dict[str, Any]
) -> dict[str, Any]:
    """Frames the JSON-LD data to a specific structure."""
    return jsonld.frame(
        jsonld_data,
        {
            "@context": CONTEXT,
            "@id": bluecore_uri,
            "@embed": "@always",
        },
    )
