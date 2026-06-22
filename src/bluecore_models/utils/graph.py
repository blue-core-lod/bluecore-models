"""Utility functions for working with RDF graphs."""

import logging
from typing import Dict

from pyld import jsonld
from rdflib import Graph, URIRef, RDF, Node, DCTERMS, BNode

from bluecore_models.namespaces import BF, BFLC, LCLOCAL, MADS

logger = logging.getLogger(__name__)


def init_graph() -> Graph:
    """Initialize a new RDF graph with the necessary namespaces."""
    new_graph = Graph()
    new_graph.namespace_manager.bind("bf", BF)
    new_graph.namespace_manager.bind("bflc", BFLC)
    new_graph.namespace_manager.bind("mads", MADS)
    new_graph.namespace_manager.bind("lclocal", LCLOCAL)
    return new_graph


def load_jsonld(jsonld_data: list | dict) -> Graph:
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
            graph.parse(data=jsonld_data, format="json-ld")  # type: ignore
        case _:
            raise ValueError(
                f"JSON-LD must be a list or dict, got {type(jsonld_data).__name__}"
            )

    return graph


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


def get_bf_classes(rdf_data: list | dict, uri: str) -> list:
    """Restrieves all of the resource's BIBFRAME classes from a graph."""
    graph = load_jsonld(rdf_data)
    classes = []
    for class_ in graph.objects(subject=URIRef(uri), predicate=RDF.type):
        if class_ in BF:  # type: ignore
            classes.append(class_)
    return classes


def frame_jsonld(bluecore_uri: str, jsonld_data: list | dict) -> dict:
    """Frames the JSON-LD data to a specific structure."""
    context: Dict[str, str] = {
        "@vocab": "http://id.loc.gov/ontologies/bibframe/",
        "bflc": "http://id.loc.gov/ontologies/bflc/",
        "mads": "http://www.loc.gov/mads/rdf/v1#",
        "rdfs": "http://www.w3.org/2000/01/rdf-schema#",
    }
    doc = jsonld.frame(
        jsonld_data,
        {
            "@context": context,
            "@id": bluecore_uri,
            "@embed": "@always",
        },
    )

    return doc
