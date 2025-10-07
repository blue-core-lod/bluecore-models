import json
import logging
from uuid import uuid4

from rdflib import Graph, URIRef, Namespace, Node, IdentifiedNode
from rdflib.plugins import sparql
from sqlalchemy.orm.session import sessionmaker, Session

from bluecore_models.namespaces import BF, MADS, RDF
from bluecore_models.models import Work, Instance, OtherResource, BibframeOtherResources
from bluecore_models.utils.graph import generate_entity_graph


logger = logging.getLogger(__name__)


def save_graph(
    session_maker: sessionmaker, graph: Graph, namespace="https://bcld.info/"
) -> Graph:
    """
    Use the supplied database sessionmaker to create a database session and
    persist all the resources found in the Graph. The possibly modified graph is
    returned, which will contain any new URIs that were minted, as well as
    bibframe:derivedFrom assertions for the original URIs.
    """
    bg = BluecoreGraph(graph, namespace)
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

    The heuristic that BluecoreGraph uses for updating the database:

    1. Extract subgraphs for Works, Instances and Other Resources in the larger graph.
    2. Examine each Work, Instance and Other Resource graph to see if it has a
       Bluecore subject URI.
    3. If it doesn't have a Bluecore subject URI mint one for it, update the graph
       to use it, and preserve the original URI as a bibframe:derivedFrom assertion.
    4. Save (or update) each Work, Instance and Other Resource to the database.
    5. Save relationships between the Works, Instances and Other Resources, being
       careful to remove existing many-to-many relations with Other Resources prior
       to adding new ones.
    """

    def __init__(self, graph: Graph, namespace: str = "https://bcld.info/"):
        """
        Instantiate a BluecoreGraph using an rdflib Graph, and an optional
        Bluecore Namespace URL: the default is https://bcld.info.
        """
        if not isinstance(namespace, str):
            raise Exception(f"default namespace cannot be {namespace}")
        elif not namespace.startswith("http"):
            raise Exception(f"default namespace must be a URL, got {namespace}")
        elif not namespace.endswith("/"):
            namespace += "/"
        self.namespace = Namespace(namespace)
        self.graph = graph

    def works(self) -> list[Graph]:
        """
        Returns a list of Bibframe Work rdflib Graphs, where each graph is for a
        distinct Work.
        """
        return self._extract_subgraphs(BF.Work)

    def instances(self) -> list[Graph]:
        """
        Returns a list of Bibframe Instance rdflib Graphs, where each graph is for a
        distinct Instance.
        """
        return self._extract_subgraphs(BF.Instance)

    def others(self) -> list[Graph]:
        """
        Return a list of "Other Resource" rdflib Graphs, where each graph is for a
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
            # resolve URIs in the graph to their Bluecore equivalent or mint them as appropriate.
            # note: OtherResources keep their original URI
            self._mint_all_uris(BF.Work, session)
            self._mint_all_uris(BF.Instance, session)

            # save resources from the graph to the database
            self._save(BF.Work, session)
            self._save(BF.Instance, session)
            self._save(None, session)  # there is no catchall URI for Other Resources

            # link all the works, instaces and other resources together in the db
            self._link(session)

            # all changes are part of one transaction!
            session.commit()

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
                    or self._is_work_or_instance(o, self.graph)
                ):
                    continue

                # otherwise return the object URI, and its graph
                if o in self.graph.subjects() and o not in other_uris:
                    others.append(generate_entity_graph(self.graph, o))
                    other_uris.add(o)

        return others

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
            raise Exception("Unexpected class_ type, must be URIRef or None")

        # there should only be one subject
        if len(uris) == 0:
            raise Exception(f"Unable to find subject URI for {class_}")
        elif len(uris) != 1:
            # try removing any bnodes when more than one subject was found
            uris = list(filter(lambda s: isinstance(s, URIRef), uris))
            if len(uris) != 1:
                raise Exception(f"Found more than one subject URI for {class_}: {uris}")

        # ensure we've got a BNode or URIRef
        if not isinstance(uris[0], IdentifiedNode):
            raise Exception("Found unexpected subject identifier: {uris[0]}")

        return uris[0]

    def _mint_all_uris(self, class_: URIRef, session: Session) -> None:
        """
        Examine Bibframe Works and Instances in the graph, and mint Bluecore URIs for
        them as needed. This method takes into account that a resource with a non-Bluecore
        URI may already be in the database under in its derivedFrom URI.
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
                # there's nothing to do here if its a bluecore URI
                continue
            else:
                # look up the URI in the database to see if it has been
                # previously saved with a derivedFrom assertion
                # if this becomes slow we may want to add an postgres index
                resource = (
                    session.query(sqla_class)
                    .where(sqla_class.data["derivedFrom"]["@id"] == uri)
                    .first()
                )

                # if we found a resource then we can update our graph to use the
                # bluecore URI that was found
                if resource is not None:
                    self._switch_uris(
                        derived_from=uri, bluecore_uri=URIRef(resource.uri)
                    )
                # otherwise we need to create a new bluecore reource
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
            case BF.Work:
                type_of = "works"
            case BF.Instance:
                type_of = "instances"
            case _:
                raise Exception("Can't mint Bluecore URI for class of type {class_}")

        return self.namespace[f"{type_of}/{uuid}"]

    def _switch_uris(self, derived_from: IdentifiedNode, bluecore_uri: URIRef) -> None:
        """
        Updates the supplied graph so that assertions involving the
        derived_from as the subject now use the bluecore_uri in its place.
        A bibframe:derivedFrom assertion is added to record the relationship
        if the derived_from is URIRef.
        """
        self.graph.update(
            UPDATE_SPARQL,
            initBindings={
                "old_subject": derived_from,
                "bluecore_uri": bluecore_uri,
            },
        )
        # only add derivedFrom assertions for URIs
        if isinstance(derived_from, URIRef):
            self.graph.add((bluecore_uri, BF.derivedFrom, derived_from))

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

    def _save(self, class_: URIRef | None, session: Session) -> None:
        """
        Persist resources of the supplied type to the given database session. If
        the type is None then Other Resources are saved.
        """
        match class_:
            case BF.Work:
                resources = self.works()
                sqla_class = Work
            case BF.Instance:
                resources = self.instances()
                sqla_class = Instance
            case None:
                resources = self.others()
                sqla_class = OtherResource

        for g in resources:
            uri = self._subject(g, class_)
            data = json.loads(g.serialize(format="json-ld"))

            obj = session.query(sqla_class).where(sqla_class.uri == uri).first()

            if obj:
                obj.data = data
                logger.info(f"updating {uri}")
                session.add(obj)
            else:
                if sqla_class == OtherResource:
                    uuid = None
                else:
                    uuid = str(uri).split("/")[-1]

                obj = sqla_class(uri=str(uri), uuid=uuid, data=data)
                logger.info(f"inserting {uri}")
                session.add(obj)

    def _link(self, session) -> None:
        """
        Save relations between Instances, Works and Other Resources in the graph.
        """

        # use bibframe:instanceOf assertions to link instances with works
        for s, o in self.graph.subject_objects(BF.instanceOf):
            logger.info(f"linking {s} to {o}")
            instance = session.query(Instance).where(Instance.uri == s).first()
            work = session.query(Work).where(Work.uri == o).first()
            instance.work = work
            session.add(instance)

        # use bibframe:hasInstance to link works with instances
        for s, o in self.graph.subject_objects(BF.hasInstance):
            logger.info(f"linking {s} to {o}")
            work = session.query(Work).where(Work.uri == s).first()
            instance = session.query(Instance).where(Instance.uri == o).first()
            instance.work = work
            session.add(instance)

        # link Works and Instances to their Other Resources, which is a bit more
        # complex since a Work or Instance has a many to many relationship with
        # Other Resources

        # first remove any existing Other Resource linkages between Works and
        # Instances so that they can be replaced with the new ones
        self._delete_other_links(BF.Work, session)
        self._delete_other_links(BF.Instance, session)

        work_graphs = self.works()
        instance_graphs = self.instances()

        for other_graph in self.others():
            other_uri = self._subject(other_graph)

            # look at each Work graph and see if the Other Resource URI appears
            # as an object within it. If it does create a link between the Work and
            # the Other Resource.
            for g in work_graphs:
                if other_uri in g.objects():
                    work_uri = self._subject(g, BF.Work)
                    logger.info(f"linking {work_uri} to {other_uri}")
                    work_model = session.query(Work).where(Work.uri == work_uri).first()
                    other_model = (
                        session.query(OtherResource)
                        .where(OtherResource.uri == other_uri)
                        .first()
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
                    instance_model = (
                        session.query(Instance)
                        .where(Instance.uri == instance_uri)
                        .first()
                    )
                    other_model = (
                        session.query(OtherResource)
                        .where(OtherResource.uri == other_uri)
                        .first()
                    )
                    session.add(
                        BibframeOtherResources(
                            bibframe_resource=instance_model, other_resource=other_model
                        )
                    )

    def _delete_other_links(self, class_: URIRef, session: Session) -> None:
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
            bf_resource = (
                session.query(sqla_class).where(sqla_class.uri == str(uri)).first()
            )

            session.query(BibframeOtherResources).filter(
                BibframeOtherResources.bibframe_resource == bf_resource
            ).delete()


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
