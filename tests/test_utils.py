import pathlib

import rdflib

from bluecore.utils.graph import (
    BF,
    BFLC,
    MADS,
    generate_entity_graph,
    init_graph,
)

def test_init_graph():
    graph = init_graph()
    assert graph.namespace_manager.store.namespace("bf") == rdflib.URIRef(BF)
    assert graph.namespace_manager.store.namespace("bflc") == rdflib.URIRef(BFLC) 
    assert graph.namespace_manager.store.namespace("mads") == rdflib.URIRef(MADS)


def test_generate_entity_graph():
    loc_graph = init_graph()
    loc_graph.parse(data=pathlib.Path("tests/23807141.jsonld").read_text(), format="json-ld")
    work_uri = rdflib.URIRef("http://id.loc.gov/resources/works/23807141")
    dcterm_part_of = loc_graph.value(subject=work_uri, predicate=rdflib.DCTERMS.isPartOf)
    assert dcterm_part_of == rdflib.URIRef("http://id.loc.gov/resources/works")
    work_graph = generate_entity_graph(loc_graph, work_uri)
    assert len(work_graph) == 118

    work_title = work_graph.value(subject=work_uri, predicate=BF.title)
    main_title = work_graph.value(subject=work_title, predicate=BF.mainTitle)
    assert str(main_title).startswith("HBR guide to generative AI for managers")
    
    # Tests if DCTERMs triples are filtered out of entity graph
    work_dcterm_part_of = work_graph.value(subject=work_uri, predicate=rdflib.DCTERMS.isPartOf)
    assert work_dcterm_part_of is None

