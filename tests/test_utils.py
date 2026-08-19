import json
from pathlib import Path

import rdflib
from rdflib import DCTERMS, RDF, BNode, Literal, URIRef

from bluecore_models.utils.graph import (
    BF,
    BFLC,
    MADS,
    _expand_bnode,
    find_duplicate_bnode_values,
    generate_entity_graph,
    init_graph,
    load_jsonld,
    replace_uri,
)


def test_init_graph():
    graph = init_graph()
    assert graph.namespace_manager.store.namespace("bf") == URIRef(BF)
    assert graph.namespace_manager.store.namespace("bflc") == URIRef(BFLC)
    assert graph.namespace_manager.store.namespace("mads") == URIRef(MADS)
    assert len(graph) == 0


def test_load_jsonld():
    with Path("tests/data/23807141.jsonld").open() as fo:
        graph = load_jsonld(json.load(fo))
    assert graph.namespace_manager.store.namespace("bf") == URIRef(BF)
    assert graph.namespace_manager.store.namespace("bflc") == URIRef(BFLC)
    assert graph.namespace_manager.store.namespace("mads") == URIRef(MADS)
    assert len(graph) == 324


def test_generate_entity_graph():
    with Path("tests/data/23807141.jsonld").open() as fo:
        loc_graph = load_jsonld(json.load(fo))

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


# LC serializations routinely describe the same resource more than once in a
# single document: the Instance appears standalone, and again nested inside the
# Work's bf:hasInstance. The nested copy is abbreviated -- it repeats the title
# but carries none of the notes. Compare the two descriptions of
# instances/20133027 in https://id.loc.gov/resources/instances/20133027.rdf
LC_REPEATED_DESCRIPTION = """<?xml version="1.0"?>
<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
         xmlns:rdfs="http://www.w3.org/2000/01/rdf-schema#"
         xmlns:bf="http://id.loc.gov/ontologies/bibframe/">
  <bf:Instance rdf:about="http://id.loc.gov/resources/instances/20133027">
    <bf:title>
      <bf:Title><bf:mainTitle>AI &amp; society</bf:mainTitle></bf:Title>
    </bf:title>
    <bf:note>
      <bf:Note><rdfs:label>Electronic</rdfs:label></bf:Note>
    </bf:note>
  </bf:Instance>
  <bf:Work rdf:about="http://id.loc.gov/resources/works/20133027">
    <bf:hasInstance>
      <bf:Instance rdf:about="http://id.loc.gov/resources/instances/20133027">
        <bf:title>
          <bf:Title><bf:mainTitle>AI &amp; society</bf:mainTitle></bf:Title>
        </bf:title>
      </bf:Instance>
    </bf:hasInstance>
  </bf:Work>
</rdf:RDF>
"""


def test_repeated_description_duplicates_bnode_values():
    """
    Demonstrates blue-core-lod/bluecore-workflows#161.

    A resource described more than once in a document ends up with duplicate
    values for its blank node properties. Each <bf:Title> element parses to a
    *separate* blank node, so the two descriptions of the Instance contribute two
    title nodes that are distinct by identity but identical in content. Blank
    nodes can't be merged by identity the way repeated URIs and literals are, and
    generate_entity_graph unions every description of the subject, so the
    extracted entity keeps both -- and the duplicate would then be persisted and
    displayed as a repeated title.

    Note the note is *not* duplicated: it appears only in the full description,
    while the abbreviated nested copy repeats just the title. That asymmetry is
    the signature of this bug, and it rules out the graph simply having been
    loaded or merged twice -- either of those would duplicate the note as well.
    """
    graph = init_graph()
    graph.parse(data=LC_REPEATED_DESCRIPTION, format="xml")
    instance_uri = URIRef("http://id.loc.gov/resources/instances/20133027")

    # the source document carries two distinct title bnodes for its one
    # editorial title, because blank nodes can't be merged by identity
    assert len(list(graph.objects(instance_uri, BF.title))) == 2

    entity_graph = generate_entity_graph(graph, instance_uri)

    # the note survives exactly once -- the control for the title below
    assert len(list(entity_graph.objects(instance_uri, BF.note))) == 1

    # ...but the title comes through twice
    titles = [
        str(entity_graph.value(subject=title, predicate=BF.mainTitle))
        for title in entity_graph.objects(instance_uri, BF.title)
    ]
    assert titles == ["AI & society", "AI & society"]

    # which is what find_duplicate_bnode_values reports
    dupes = find_duplicate_bnode_values(graph)
    assert len(dupes) == 1
    dup = dupes[0]
    assert dup.subject == instance_uri
    assert dup.predicate == BF.title
    assert dup.copies == 2
    assert dup.label == "AI & society"


def test_find_duplicate_bnode_values_ignores_distinct_values():
    """
    Detection is by content, so genuinely different values are not reported --
    including ones differing only by an extra type or a language tag -- and neither
    are repeated URIs or literals, which RDF already merges on its own.
    """
    graph = init_graph()
    work = URIRef("http://example.com/work")

    # two variant titles differing only in rdf:type
    plain, typed = BNode(), BNode()
    for node in (plain, typed):
        graph.add((work, BF.title, node))
        graph.add((node, RDF.type, BF.VariantTitle))
        graph.add((node, BF.mainTitle, Literal("AI and society")))
    graph.add(
        (typed, RDF.type, URIRef("http://id.loc.gov/vocabulary/vartitletype/por"))
    )

    # same lexical value, different language tags
    for tag in ("en", "fr"):
        node = BNode()
        graph.add((work, BF.title, node))
        graph.add((node, BF.mainTitle, Literal("AI and society", lang=tag)))

    # a repeated literal and a repeated URI are one triple each already
    graph.add((work, BF.note, Literal("same")))
    graph.add((work, BF.note, Literal("same")))
    graph.add((work, BF.subject, URIRef("http://example.com/topic")))

    assert find_duplicate_bnode_values(graph) == []
    assert len(list(graph.objects(work, BF.title))) == 4


def test_replace_uri():
    """
    replace_uri rewrites a URI everywhere it appears -- as a subject and as an
    object referenced by other resources.
    """
    old = URIRef("http://example.com/old")
    new = URIRef("http://example.com/new")
    other = URIRef("http://example.com/other")

    graph = init_graph()
    # old appears as a subject...
    graph.add((old, rdflib.RDF.type, BF.Work))
    graph.add((old, BF.title, Literal("A title")))
    # ...and as an object referenced by another resource.
    graph.add((other, BF.relatedTo, old))

    replace_uri(graph, old, new)

    # old is gone from every position, new takes its place.
    assert old not in set(graph.subjects()) | set(graph.objects())
    assert (new, rdflib.RDF.type, BF.Work) in graph
    assert (new, BF.title, Literal("A title")) in graph
    assert (other, BF.relatedTo, new) in graph


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
