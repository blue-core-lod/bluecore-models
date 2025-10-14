import json
import logging
from uuid import uuid4

from rdflib import Graph, URIRef, Namespace, Node
from rdflib.plugins import sparql
from sqlalchemy.orm.session import sessionmaker

from bluecore_models.namespaces import BF, MADS, RDF
from bluecore_models.models import Work, Instance, OtherResource
from bluecore_models.utils.graph import generate_entity_graph


logger = logging.getLogger(__name__)


def save_graph(session_maker: sessionmaker, graph: Graph) -> Graph:
    """
    """
    bg = BluecoreGraph(graph)
    bg.save(session_maker)
    return bg.graph


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
        self._save(BF.Work, session_maker)
        self._save(BF.Instance, session_maker)

    def _save(self, class_, session_maker):
        match class_:
            case BF.Work:
                resources = self.works()
                sqla_class = Work
            case BF.Instance:
                resources = self.instances()
                sqla_class = Instance

        with session_maker() as session:
            for g in resources:
                uri = self._subject(g, class_)
                data = json.loads(g.serialize(format="json-ld"))

                obj = session.query(sqla_class).where(sqla_class.uri == uri).first()

                if obj:
                    obj.data = data
                    session.add(obj)
                else:
                    uuid = uri.split("/")[-1]
                    obj = sqla_class(uri=str(uri), uuid=uuid, data=data)
                    session.add(obj)

            session.commit()


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
                    or self._is_work_or_instance(o, self.graph)
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
        uris = list(graph.subjects(RDF.type, class_))
        if len(uris) == 0:
            raise Exception(f"Unable to find subject URI for {class_}")
        elif len(uris) != 1:
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
                    resource = session.query(sqla_class).where(sqla_class.data["derivedFrom"]["@id"] == uri).first()
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

        return self.namespace[f"{type_of}/{uuid}"]

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

    def _exclude_uri_from_other_resources(self, uri: Node) -> bool:
        """Checks if uri is in the BF, MADS, or RDF namespaces"""
        return uri in BF or uri in MADS or uri in RDF  # type: ignore

    def _is_work_or_instance(self, uri: Node, graph: Graph) -> bool:
        """Checks if uri is a BIBFRAME Work or Instance"""
        for class_ in graph.objects(subject=uri, predicate=RDF.type):
            # In the future we may want to include Work and Instances subclasses
            # maybe through inference
            if class_ == BF.Work or class_ == BF.Instance:
                return True
        return False

    def _is_bluecore_uri(self, uri):
        return uri in self.namespace


UPDATE_SPARQL = sparql.prepareUpdate("""
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


