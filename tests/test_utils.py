import json
from pathlib import Path

import rdflib
from rdflib import URIRef, Literal, DCTERMS

from bluecore_models.utils.graph import (
    BF,
    BFLC,
    MADS,
    generate_entity_graph,
    init_graph,
    load_jsonld,
    _expand_bnode,
)


def test_init_graph():
    graph = init_graph()
    assert graph.namespace_manager.store.namespace("bf") == URIRef(BF)
    assert graph.namespace_manager.store.namespace("bflc") == URIRef(BFLC)
    assert graph.namespace_manager.store.namespace("mads") == URIRef(MADS)
    assert len(graph) == 0


def test_load_jsonld():
    graph = load_jsonld(json.load(Path("tests/23807141.jsonld").open()))
    assert graph.namespace_manager.store.namespace("bf") == URIRef(BF)
    assert graph.namespace_manager.store.namespace("bflc") == URIRef(BFLC)
    assert graph.namespace_manager.store.namespace("mads") == URIRef(MADS)
    assert len(graph) == 324


def test_generate_entity_graph():
    loc_graph = load_jsonld(json.load(Path("tests/23807141.jsonld").open()))

    work_uri = URIRef("http://id.loc.gov/resources/works/23807141")
    dcterm_part_of = loc_graph.value(
        subject=work_uri, predicate=rdflib.DCTERMS.isPartOf
    )
    assert dcterm_part_of == URIRef("http://id.loc.gov/resources/works")
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


def test_bnode_expansion():
    """
    When Work and Instances refer to each other as BNodes we need to ensure we
    don't get caught in infinite recursion.
    """

    batch_graph = init_graph()
    entity_graph = init_graph()

    work_bnode = rdflib.BNode()
    instance_bnode = rdflib.BNode()

    # add six assertions for the work and instance that are linked together as bnodes
    batch_graph.add((work_bnode, rdflib.RDF.type, BF.Work))
    batch_graph.add((work_bnode, BF.hasInstance, instance_bnode))
    batch_graph.add((work_bnode, BF.acquisitionTerms, Literal("(b&w film copy neg.)")))
    batch_graph.add((instance_bnode, rdflib.RDF.type, BF.Instance))
    batch_graph.add((instance_bnode, BF.instanceOf, work_bnode))
    batch_graph.add((work_bnode, BF.dimensions, Literal("28 cm")))

    # add two assertions that should be ignored because they are in DCTERMS
    batch_graph.add(
        (work_bnode, DCTERMS.title, Literal("Ignored DublinCore title for work"))
    )
    batch_graph.add(
        (
            instance_bnode,
            DCTERMS.title,
            Literal("Ignored DublinCore title for instance"),
        )
    )

    # populate entity_graph using the batch_graph
    _expand_bnode(batch_graph, entity_graph, work_bnode)

    assert len(entity_graph) == 6, "DCTERMS assertions should be ignored"
