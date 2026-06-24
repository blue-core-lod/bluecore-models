"""Utility functions for working with RDF graphs."""

import datetime
import logging
from typing import Any

from pyld import jsonld
from rdflib import BNode, DCTERMS, Graph, Literal, Node, RDF, URIRef, XSD

from bluecore_models.namespaces import BF, BFLC, LCLOCAL, MADS

logger = logging.getLogger(__name__)

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


def _remove_bnode(graph: Graph, bnode: BNode) -> None:
    """Recursively removes a blank node and any blank nodes it references."""
    for pred, obj in list(graph.predicate_objects(subject=bnode)):
        graph.remove((bnode, pred, obj))
        # remove any nested blank nodes (e.g. bf:agent [ a bf:Agent ... ])
        if isinstance(obj, BNode):
            _remove_bnode(graph, obj)


def _remove_admin_metadata(graph: Graph, subject: URIRef | None = None):
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
        _remove_bnode(graph, admin_metadata)
        # remove the link from the resource to the AdminMetadata node
        graph.remove((s, BF.adminMetadata, admin_metadata))


def generate_admin_metadata(**kwargs):
    """
    Generates two bf:AdminMetadata blank nodes for incoming RDF resources that are
    derived from existing RDF resources
    """
    desc_auth: URIRef = kwargs.get(
        "desc_authentication", URIRef("http://id.loc.gov/vocabulary/marcauthen/pcc")
    )
    desc_lang: URIRef = kwargs.get(
        "desc_language", URIRef("http://id.loc.gov/vocabulary/languages/eng")
    )
    desc_level: URIRef = kwargs.get(
        "desc_level", URIRef("http://id.loc.gov/ontologies/bibframe-2-6-0/")
    )
    graph: Graph = kwargs["graph"]
    bluecore_uri: URIRef = kwargs["bluecore_uri"]
    remove_existing: bool = kwargs.get("remove_existing", True)
    source_uri: URIRef = kwargs["source_uri"]
    status: URIRef = kwargs.get(
        "status", URIRef("http://id.loc.gov/vocabulary/mstatus/c")
    )

    time_stamp = datetime.datetime.now(datetime.UTC)

    if remove_existing:
        _remove_admin_metadata(graph, bluecore_uri)

    # First bf:AdminMetadata
    first_admin_metadata = BNode()
    graph.add((bluecore_uri, BF.adminMetadata, first_admin_metadata))
    graph.add((first_admin_metadata, RDF.type, BF.AdminMetadata))
    graph.add(
        (
            first_admin_metadata,
            BF.date,
            Literal(time_stamp.isoformat(), datatype=XSD.dateTime),
        )
    )
    graph.add((first_admin_metadata, BF.derivedFrom, source_uri))
    graph.add((first_admin_metadata, BF.status, status))

    # Second bf:AdminMetadata
    second_admin_metadata = BNode()
    graph.add((bluecore_uri, BF.adminMetadata, second_admin_metadata))
    graph.add((second_admin_metadata, RDF.type, BF.AdminMetadata))
    # imported lazily to avoid a circular import with bluecore_models.models
    from bluecore_models.models.version import CURRENT_USER_ID

    try:
        cataloger_id = CURRENT_USER_ID.get()
    finally:
        if not cataloger_id:
            cataloger_id = "Unknown"
    graph.add((second_admin_metadata, BFLC.catalogerId, Literal(cataloger_id)))
    graph.add((second_admin_metadata, BF.descriptionAuthentication, desc_auth))
    graph.add(
        (second_admin_metadata, BF.date, Literal(time_stamp.strftime("%Y-%m-%d")))
    )
    graph.add((second_admin_metadata, BF.descriptionLanguage, desc_lang))
    graph.add((second_admin_metadata, BF.descriptionLevel, desc_level))


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
