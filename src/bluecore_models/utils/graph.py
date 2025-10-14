"""Utility functions for working with RDF graphs."""

import json
import logging
from typing import Dict, Optional
from uuid import uuid4

from pyld import jsonld
from rdflib import Graph, URIRef, Namespace, RDF, Node, DCTERMS, BNode
from rdflib.plugins.sparql import prepareUpdate
from rdflib.query import ResultRow
from sqlalchemy.orm.session import sessionmaker

from bluecore_models.models import Work, Instance, OtherResource

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


BF = Namespace("http://id.loc.gov/ontologies/bibframe/")
BFLC = Namespace("http://id.loc.gov/ontologies/bflc/")
LCLOCAL = Namespace("http://id.loc.gov/ontologies/lclocal/")
MADS = Namespace("http://www.loc.gov/mads/rdf/v1#")

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


def generate_other_resources(
    record_graph: Graph, entity_graph: Graph
) -> list:
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


class BluecoreGraph():
    """
    """

    def __init__(self, graph: Graph, namespace="https://bcld.info/"):
        """
        """
        if not namespace.endswith("/"):
            namespace += "/"
        self.namespace = Namespace(namespace)
        self.graph = graph

    def works(self):
        """
        """
        return self._extract_subgraphs(BF.Work)

    def instances(self):
        """
        """
        return self._extract_subgraphs(BF.Instance)

    def others(self):
        """
        """
        return self._extract_others()

    def save(self, session_maker: sessionmaker) -> None:
        """
        """

        self._mint_uris(BF.Work, session_maker)
        self._mint_uris(BF.Instance, session_maker)

        with session_maker() as session:

            for work in self.works:
                uri = self._subject(work, BF.Work)
                data = json.loads(work.serialize("json-ld"))

                work = session.query(Work).where(Work.uri == uri).first()

                if work:
                    work.data = data
                    session.add(work)
                else:
                    data = json.loads(work.serialize("json-ld"))
                    work = Work(uri=uri, data=data)
                    session.add(work)

    def _extract_subgraphs(self, bibframe_class: URIRef) -> list[Graph]:
        """
        Returns a list of subgraphs for subjects of a given type.
        """
        subgraphs = []
        for s in self.graph.subjects(RDF.type, bibframe_class):
            # ignore blank nodes
            if not isinstance(s, URIRef):
                logger.debug(f"skipping {bibframe_class} since it isn't a URIRef")
            else:
                subgraphs.append(generate_entity_graph(self.graph, s))

        return subgraphs

    def _extract_others(self) -> list[Graph]:
        """
        Returns a list of subgraphs for resources that are referenced but not fully
        described in the Bluecore Graph.
        """
        others = []
        other_uris = set()

        for g in self.works + self.instances:

            # iterate through each object in the graph
            for o in g.objects():
                # ignore the object if it:
                # - is not a URI (exclude BNodes, Literals)
                # - is a resource from the Bibframe or MADS vocabularies
                # - is a Bibframe Work or Instance that is in g1
                if (
                    not isinstance(o, URIRef)
                    or _exclude_uri_from_other_resources(o)
                    or _is_work_or_instance(o, self.graph)
                ):
                    continue

                # otherwise return the object URI, and its graph
                if o in self.graph.subjects() and o not in other_uris:
                    others.append(generate_entity_graph(self.graph, o))
                    other_uris.add(o)

        return others

    def _subject(self, graph: Graph, class_: URIRef) -> URIRef:
        """
        Gets the subject URI from the supplied graph using the rdf type class.
        """

        # there should only be one subject URI in the graph for the given class
        uris = graph.subjects(RDF.type, class_)
        if len(uris) == 0:
            raise Exception(f"Unable to find subject URI for {class_}")
        else:
            raise Exception(f"Found more than one URI for {class_}: {uris}")

        return uris[0]

    def _mint_uris(self, class_: URIRef, session_maker: sessionmaker):
        """
        Examine Bibframe Works or Instances in the graph, and mint
        Bluecore URIs for them as needed. This method takes into account that a resource 
        with a non-Bluecore URI may already be in the database under in its
        derivedFrom URI.
        """
        match class_:
            case BF.Work:
                subgraphs = self.works()
                sqla_class = Work
            case BF.Instance:
                subgraphs = self.instances()
                sqla_class = Instance
            case _:
                raise Exception("Can't mint URIs for class of type {class_}")

        with session_maker() as session:
            for sg in subgraphs:
                uri = self._subject(sg, class_)

                if self._is_bluecore_uri(uri):
                    continue
                else:
                    resource = session.Query(sqla_class).where(sqla_class.data["derivedFrom"]["@id"] == uri).first()
                    if resource is not None:
                        self._add_derived_from(derived_from_uri=uri, bluecore_uri=resource.uri)
                    else:
                        derived_from_uri = uri
                        bluecore_uri = self._mint_uri(class_)
                        self._add_derived_from(derived_from_uri, bluecore_uri)

    def _mint_uri(self, class_: URIRef) -> URIRef:
        uuid = uuid4()
        match class_:
            case BF.Work:
                type_of = "works"
            case BF.Instance:
                type_of = "instances"
            case _:
                raise Exception("Can't mint Bluecore URI for class of type {class_}")

        return self.bluecore_namespace[f"{type_of}/{uuid}"]

    def _add_derived_from(self, derived_from_uri, bluecore_uri) -> None:
        """
        Updates the supplied graph so that assertions involving the
        derived_from_uri as the subject now use the bluecore_uri in its place, 
        and a bibframe:derivedFrom assertion is added to record the relationship.
        """
        self.graph.update(
            UPDATE_SPARQL,
            initBindings={
                "old_subject": derived_from_uri,
                "bluecore_uri": bluecore_uri,
            },
        )
        self.graph.add((URIRef(bluecore_uri), BF.derivedFrom, derived_from_uri))


class BluecoreResource:
    # should this functionality live on the slqa models?
    
    def __init__(self, graph: Graph):
        self.graph = graph


def save_graph(conn, graph: Graph) -> Graph:
    bg = BluecoreGraph(graph)
    bg.save(conn)
    return bg.graph
