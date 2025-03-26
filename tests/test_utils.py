import pathlib

import rdflib

from bluecore_models.utils.graph import (
    BF,
    BFLC,
    MADS,
    generate_entity_graph,
    generate_other_resources,
    init_graph,
    _is_work_or_instance,
)


def test_init_graph():
    graph = init_graph()
    assert graph.namespace_manager.store.namespace("bf") == rdflib.URIRef(BF)
    assert graph.namespace_manager.store.namespace("bflc") == rdflib.URIRef(BFLC)
    assert graph.namespace_manager.store.namespace("mads") == rdflib.URIRef(MADS)


def test_generate_entity_graph():
    loc_graph = init_graph()
    loc_graph.parse(
        data=pathlib.Path("tests/23807141.jsonld").read_text(), format="json-ld"
    )
    work_uri = rdflib.URIRef("http://id.loc.gov/resources/works/23807141")
    dcterm_part_of = loc_graph.value(
        subject=work_uri, predicate=rdflib.DCTERMS.isPartOf
    )
    assert dcterm_part_of == rdflib.URIRef("http://id.loc.gov/resources/works")
    work_graph = generate_entity_graph(loc_graph, work_uri)
    assert len(work_graph) == 118

    work_title = work_graph.value(subject=work_uri, predicate=BF.title)
    main_title = work_graph.value(subject=work_title, predicate=BF.mainTitle)
    assert str(main_title).startswith("HBR guide to generative AI for managers")

    # Tests if DCTERMs triples are filtered out of entity graph
    work_dcterm_part_of = work_graph.value(
        subject=work_uri, predicate=rdflib.DCTERMS.isPartOf
    )
    assert work_dcterm_part_of is None


def test_generate_other_resources():
    loc_graph = init_graph()
    loc_graph.parse(
        data=pathlib.Path("tests/23807141.jsonld").read_text(), format="json-ld"
    )
    work_uri = rdflib.URIRef("http://id.loc.gov/resources/works/23807141")
    work_graph = generate_entity_graph(loc_graph, work_uri)
    other_work_resources = generate_other_resources(loc_graph, work_graph)
    assert len(other_work_resources) == 25
    sorted_other_work_resources = sorted(other_work_resources, key=lambda x: x["uri"])
    assert sorted_other_work_resources[0]["uri"].startswith(
        "http://id.loc.gov/authorities/subjects"
    )
    assert sorted_other_work_resources[-1]["uri"].startswith(
        "http://id.loc.gov/vocabulary/relators/aut"
    )

    instance_uri = rdflib.URIRef("http://id.loc.gov/resources/instances/23807141")
    instance_graph = generate_entity_graph(loc_graph, instance_uri)
    other_instance_resources = generate_other_resources(loc_graph, instance_graph)
    assert len(other_instance_resources) == 14
    sorted_other_instance_resources = sorted(
        other_instance_resources, key=lambda x: x["uri"]
    )
    assert sorted_other_instance_resources[0]["uri"].startswith(
        "http://id.loc.gov/vocabulary/carriers/cr"
    )
    assert sorted_other_instance_resources[-1]["uri"].startswith(
        "http://id.loc.gov/vocabulary/organizations/dlcmrc"
    )


def test_is_work_or_instance():
    loc_graph = init_graph()
    work_uri = rdflib.URIRef("http://id.loc.gov/resources/works/23807141")
    # Adds a fake class to work
    loc_graph.add((work_uri, rdflib.RDF.type, MADS.Resource))
    loc_graph.add((work_uri, rdflib.RDF.type, BF.Work))
    assert _is_work_or_instance(work_uri, loc_graph)
