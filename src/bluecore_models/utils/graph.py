"""Utility functions for working with RDF graphs."""

import logging

import rdflib

from uuid import uuid4

from rdflib.plugins.sparql import prepareUpdate

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


BF = rdflib.Namespace("http://id.loc.gov/ontologies/bibframe/")
BFLC = rdflib.Namespace("http://id.loc.gov/ontologies/bflc/")
LCLOCAL = rdflib.Namespace("http://id.loc.gov/ontologies/lclocal/")
MADS = rdflib.Namespace("http://www.loc.gov/mads/rdf/v1#")

logger = logging.getLogger(__name__)


def init_graph() -> rdflib.Graph:
    """Initialize a new RDF graph with the necessary namespaces."""
    new_graph = rdflib.Graph()
    new_graph.namespace_manager.bind("bf", BF)
    new_graph.namespace_manager.bind("bflc", BFLC)
    new_graph.namespace_manager.bind("mads", MADS)
    new_graph.namespace_manager.bind("lclocal", LCLOCAL)
    return new_graph


def _check_for_namespace(node: rdflib.URIRef) -> bool:
    """Check if a node is in the LCLOCAL or DCTERMS namespace."""
    return node in LCLOCAL or node in rdflib.DCTERMS


def _exclude_uri_from_other_resources(uri: rdflib.URIRef) -> bool:
    """Checks if uri is in the BF, MADS, or RDF namespaces"""
    return uri in BF or uri in MADS or uri in rdflib.RDF


def _expand_bnode(graph: rdflib.Graph, entity_graph: rdflib.Graph, bnode: rdflib.BNode):
    """Expand a blank node in the entity graph."""
    for pred, obj in graph.predicate_objects(subject=bnode):
        if _check_for_namespace(pred) or _check_for_namespace(obj):
            continue
        entity_graph.add((bnode, pred, obj))
        if isinstance(obj, rdflib.BNode):
            _expand_bnode(graph, entity_graph, obj)


def _is_work_or_instance(uri: rdflib.URIRef, graph: rdflib.Graph) -> bool:
    """Checks if uri is a BIBFRAME Work or Instance"""
    for class_ in graph.objects(subject=uri, predicate=rdflib.RDF.type):
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


def _update_graph(**kwargs) -> rdflib.Graph:
    """
    Updates graph using a Blue Core URI subject. If incoming subject is
    an URI, create a new RDF triple.
    """
    graph: rdflib.Graph = kwargs["graph"]
    bluecore_uri: str = kwargs["bluecore_uri"]
    bluecore_type: str = kwargs["bluecore_type"]

    match bluecore_type.lower():
        case "works" | "work":
            object_uri = BF.Work

        case "instances" | "instance":
            object_uri = BF.Instance

    external_subject = graph.value(predicate=rdflib.RDF.type, object=object_uri)
    if external_subject is None:
        raise ValueError(f"Cannot find external subject with a type of {object_uri}")

    graph.update(
        UPDATE_SPARQL,
        initBindings={
            "old_subject": external_subject,
            "bluecore_uri": rdflib.URIRef(bluecore_uri),
        },
    )

    if not isinstance(external_subject, rdflib.BNode):
        graph.add((rdflib.URIRef(bluecore_uri), BF.derivedFrom, external_subject))
    return graph


def generate_entity_graph(graph: rdflib.Graph, entity: rdflib.URIRef) -> rdflib.Graph:
    """Generate an entity graph from a larger RDF graph."""
    entity_graph = init_graph()
    for pred, obj in graph.predicate_objects(subject=entity):
        if _check_for_namespace(pred) or _check_for_namespace(obj):
            continue
        entity_graph.add((entity, pred, obj))
        if isinstance(obj, rdflib.BNode):
            _expand_bnode(graph, entity_graph, obj)
    return entity_graph


def generate_other_resources(
    record_graph: rdflib.Graph, entity_graph: rdflib.Graph
) -> list:
    """
    Takes a Record Graph and Entity Graph and returns a list of dictionaries
    where each dict contains the sub-graphs and URIs that referenced in the
    entity graph and present in the record graph.
    """
    other_resources = []
    logger.error(f"Size of entity graph {len(entity_graph)}")
    for row in entity_graph.query("""
      SELECT DISTINCT ?object
      WHERE {
        ?subject ?predicate ?object .
        FILTER(isIRI(?object))
      }
    """):
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


def get_bf_classes(rdf_data: str, uri: str) -> list:
    """Restrieves all of the resource's BIBFRAME classes from a graph."""
    graph = init_graph()
    graph.parse(data=rdf_data, format="json-ld")
    classes = []
    for class_ in graph.objects(subject=rdflib.URIRef(uri), predicate=rdflib.RDF.type):
        if class_ in BF:
            classes.append(class_)
    return classes


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
        "data": modified_graph.serialize(format="json-ld"),
        "uuid": uuid,
    }
