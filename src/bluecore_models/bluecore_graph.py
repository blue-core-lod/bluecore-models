import json
import logging
from uuid import uuid4

from rdflib import Graph, URIRef, Namespace, Node
from rdflib.plugins import sparql
from sqlalchemy.orm.session import sessionmaker, Session

from bluecore_models.namespaces import BF, MADS, RDF
from bluecore_models.models import Work, Instance, OtherResource
from bluecore_models.utils.graph import generate_entity_graph


logger = logging.getLogger(__name__)


def save_graph(session_maker: sessionmaker, graph: Graph) -> Graph:
    """
    Use the supplied database sessionmaker to create a database session and
    persist all the resources found in the Graph. The possibly modified graph is
    returned, which will contain any new URIs that were minted, as well as
    bibframe:derivedFrom assertions for the original URIs.
    """
    bg = BluecoreGraph(graph)
    bg.save(session_maker)
    return bg.graph


class BluecoreGraph:
    """
    The BluecoreGraph is instantiated using an existing rdflib Graph for a set
    of Bibframe Works, Instances and "Other" Resources, which are all available
    using methods that return them as subgraphs. The save method is then used to
    persist the graph to a given database. If you want to change the default
    namespace that is used for the bluecore instance you can pass that in with
    the namespace parameter.
    """

    def __init__(self, graph: Graph, namespace="https://bcld.info/"):
        """ """
        if not namespace.endswith("/"):
            namespace += "/"
        self.namespace = Namespace(namespace)
        self.graph = graph

    def works(self) -> list[Graph]:
        """
        Returns a list of Bibframe Work rdflib Graphs, where each graph is a
        distinct Work.
        """

        return self._extract_subgraphs(BF.Work)

    def instances(self) -> list[Graph]:
        """
        Returns a list of Bibframe Instance rdflib Graphs, where each graph is a
        distinct Instance.
        """
        return self._extract_subgraphs(BF.Instance)

    def others(self) -> list[Graph]:
        """
        Return a list of "Other Resource" rdflib Graphs, where each graph is a
        distinct resource.
        """
        return self._extract_others()

    def save(self, session_maker: sessionmaker) -> None:
        """
        Persists the graph to the database using the supplied sqlalchemy
        sessionmaker. All the database modifications are made using a single
        transaction.
        """
        with session_maker() as session:
            # resolve URIs in the graph to their Bluecore equivalent
            # or mint them as appropriate
            self._mint_all_uris(BF.Work, session)
            self._mint_all_uris(BF.Instance, session)

            # save resources from the graph to the database
            self._save(BF.Work, session)
            self._save(BF.Instance, session)
            self._save("Other", session)  # there is no URI for Other Resources

            self._link(session)

            # all this is done as one transaction!
            session.commit()

    def _save(self, class_, session: Session) -> None:
        """
        Persist resources of the supplied type to the given database session.
        """
        match class_:
            case BF.Work:
                resources = self.works()
                sqla_class = Work
            case BF.Instance:
                resources = self.instances()
                sqla_class = Instance
            case "Other":
                resources = self.others()
                sqla_class = OtherResource

        for g in resources:
            uri = self._subject(g, class_)
            data = json.loads(g.serialize(format="json-ld"))

            obj = session.query(sqla_class).where(sqla_class.uri == uri).first()

            if obj:
                obj.data = data
                session.add(obj)
            else:
                if sqla_class == OtherResource:
                    uuid = None
                else:
                    uuid = str(uri).split("/")[-1]

                obj = sqla_class(uri=str(uri), uuid=uuid, data=data)
                session.add(obj)

    def _link(self, session) -> None:
        """
        Save relations between Instances, Works and Other Resources in the graph.
        """

        # use bibframe:instanceOf assertions to link instances with works
        for s, o in self.graph.subject_objects(BF.instanceOf):
            instance = session.query(Instance).where(Instance.uri == s).first()
            work = session.query(Work).where(Work.uri == o).first()
            instance.work = work
            session.add(instance)

        # use bibframe:hasInstance to link works with instances
        for s, o in self.graph.subject_objects(BF.hasInstance):
            work = session.query(Work).where(Work.uri == s).first()
            instance = session.query(Instance).where(Instance.uri == o).first()
            instance.work = work
            session.add(instance)

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

    def _subject(self, graph: Graph, class_: URIRef) -> Node:
        """
        Gets the subject URI from the supplied graph using the rdf type class.
        """

        # there should only be one subject URI in the graph for the given class
        if class_ != "Other":
            uris = list(graph.subjects(RDF.type, class_))
        else:
            # TODO: this is a bit of a guess as to what the subject URI is for the
            # OtherResource graph, which assumes there is one subject URI and ignores BNodes.
            uris = list(set(filter(lambda s: isinstance(s, URIRef), graph.subjects())))

        if len(uris) == 0:
            raise Exception(f"Unable to find subject URI for {class_}")
        elif len(uris) != 1:
            raise Exception(f"Found more than one URI for {class_}: {uris}")

        return uris[0]

    def _mint_all_uris(self, class_: URIRef, session: Session) -> None:
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

        for sg in subgraphs:
            uri = self._subject(sg, class_)

            if self._is_bluecore_uri(uri):
                continue
            else:
                resource = (
                    session.query(sqla_class)
                    .where(sqla_class.data["derivedFrom"]["@id"] == uri)
                    .first()
                )
                if resource is not None:
                    self._add_derived_from(
                        derived_from_uri=uri, bluecore_uri=URIRef(resource.uri)
                    )
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

    def _is_bluecore_uri(self, uri) -> bool:
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
