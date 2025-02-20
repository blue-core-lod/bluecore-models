import rdflib

BF = rdflib.Namespace("http://id.loc.gov/ontologies/bibframe/")
BFLC = rdflib.Namespace("http://id.loc.gov/ontologies/bflc/")
LCLOCAL = rdflib.Namespace("http://id.loc.gov/ontologies/lclocal/")
MADS = rdflib.Namespace("http://www.loc.gov/mads/rdf/v1#")

def init_graph():
    new_graph = rdflib.Graph()
    new_graph.namespace_manager.bind("bf", BF)
    new_graph.namespace_manager.bind("bflc", BFLC)
    new_graph.namespace_manager.bind("mads", MADS)
    new_graph.namespace_manager.bind("lclocal", LCLOCAL)
    return new_graph

def _check_for_namespace(node):
    return node in LCLOCAL or node in rdflib.DCTERMS

def _expand_bnode(graph: rdflib.Graph, entity_graph: rdflib.Graph, bnode: rdflib.BNode):
    for pred, obj in graph.predicate_objects(subject=bnode):
        if _check_for_namespace(pred) or _check_for_namespace(obj):
            continue
        entity_graph.add((bnode, pred, obj))
        if isinstance(obj, rdflib.BNode):
            _expand_bnode(graph, entity_graph, obj)

def generate_entity_graph(graph: rdflib.Graph, entity: rdflib.URIRef) -> rdflib.Graph:
    entity_graph = init_graph()
    for pred, obj in graph.predicate_objects(subject=entity):
        if _check_for_namespace(pred) or _check_for_namespace(obj):
            continue
        entity_graph.add((entity, pred, obj))
        if isinstance(obj, rdflib.BNode):
            _expand_bnode(graph, entity_graph, obj)
    return entity_graph