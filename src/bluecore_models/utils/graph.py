"""Utility functions for working with RDF graphs."""

import json
import logging
from typing import Dict
from uuid import uuid4

from pyld import jsonld
from rdflib import Graph, URIRef, RDF, Node, DCTERMS, BNode
from rdflib.plugins.sparql import prepareUpdate
from rdflib.query import ResultRow

from bluecore_models.namespaces import BF, BFLC, LCLOCAL, MADS

UPDATE_SPARQL = prepareUpdate("""
DELETE {
  ?old_subject ?p ?o .
  ?s ?pp $old_subject
}
INSERT {
  ?bluecore_uri ?p ?o .
  ?s ?pp ?bluecore_uri .
}
WHERE {
  {
    ?old_subject ?p ?o .
  }
  UNION {
    ?s ?pp ?old_subject .
  }
}
""")


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


def _exclude_uri_from_other_resources(uri: Node) -> bool:
    """Checks if uri is in the BF, MADS, or RDF namespaces"""
    return uri in BF or uri in MADS or uri in RDF  # type: ignore


def _expand_bnode(graph: Graph, entity_graph: Graph, bnode: BNode):
    """Expand a blank node in the entity graph."""
    for pred, obj in graph.predicate_objects(subject=bnode):
        if _check_for_namespace(pred) or _check_for_namespace(obj):
            continue
        entity_graph.add((bnode, pred, obj))
        if isinstance(obj, BNode):
            _expand_bnode(graph, entity_graph, obj)


def _is_work_or_instance(uri: Node, graph: Graph) -> bool:
    """Checks if uri is a BIBFRAME Work or Instance"""
    for class_ in graph.objects(subject=uri, predicate=RDF.type):
        # In the future we may want to include Work and Instances subclasses
        # maybe through inference
        if class_ == BF.Work or class_ == BF.Instance:
            return True
    return False


def _mint_uri(env_root: str, type_of: str) -> tuple:
    """
    Mints a Work or Instance URI based on the environment.
    """
    uuid = uuid4()
    if not type_of.endswith("s"):
        type_of = f"{type_of}s"
    if env_root.endswith("/"):
        env_root = env_root[0:-1]
    return f"{env_root}/{type_of}/{uuid}", str(uuid)


def _update_graph(**kwargs) -> Graph:
    """
    Updates graph using a Blue Core URI subject. If incoming subject is
    an URI, create a new derivedFrom assertion.
    """
    graph: Graph = kwargs["graph"]
    bluecore_uri: str = kwargs["bluecore_uri"]
    bluecore_type: str = kwargs["bluecore_type"]

    match bluecore_type.lower():
        case "works" | "work":
            object_uri = BF.Work

        case "instances" | "instance":
            object_uri = BF.Instance

    external_subject = graph.value(predicate=RDF.type, object=object_uri)
    if external_subject is None:
        raise ValueError(f"Cannot find external subject with a type of {object_uri}")

    graph.update(
        UPDATE_SPARQL,
        initBindings={
            "old_subject": external_subject,  # type: ignore
            "bluecore_uri": URIRef(bluecore_uri),  # type: ignore
        },
    )

    if not isinstance(external_subject, BNode):
        graph.add((URIRef(bluecore_uri), BF.derivedFrom, external_subject))
    return graph


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


def generate_other_resources(record_graph: Graph, entity_graph: Graph) -> list:
    """
    Takes a Record Graph and Entity Graph and returns a list of dictionaries
    where each dict contains the sub-graphs and URIs that referenced in the
    entity graph and present in the record graph.
    """
    other_resources = []
    logger.info(f"Size of entity graph {len(entity_graph)}")
    for row in entity_graph.query("""
      SELECT DISTINCT ?object
      WHERE {
        ?subject ?predicate ?object .
        FILTER(isIRI(?object))
      }
    """):
        assert isinstance(row, ResultRow)
        uri = row[0]
        if _exclude_uri_from_other_resources(uri) or _is_work_or_instance(
            uri, record_graph
        ):
            continue
        other_resource_graph = generate_entity_graph(record_graph, uri)
        if len(other_resource_graph) > 0:
            other_resources.append(
                {
                    "uri": str(uri),
                    "graph": other_resource_graph.serialize(format="json-ld"),
                }
            )
    return other_resources


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


def handle_external_subject(**kwargs) -> dict:
    """
    Handles external subject terms in new Blue Core descriptions
    """
    raw_jsonld = kwargs["data"]
    env_root = kwargs["bluecore_base_url"]
    bluecore_type = kwargs["type"]

    graph = init_graph()
    graph.parse(data=raw_jsonld, format="json-ld")

    bluecore_uri, uuid = _mint_uri(env_root, bluecore_type)
    modified_graph = _update_graph(
        graph=graph, bluecore_uri=bluecore_uri, bluecore_type=bluecore_type
    )

    return {
        "uri": bluecore_uri,
        "data": json.loads(modified_graph.serialize(format="json-ld")),
        "uuid": uuid,
    }
