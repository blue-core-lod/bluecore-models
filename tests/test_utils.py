import json
from pathlib import Path

import rdflib
from pyld import jsonld
from rdflib import DCTERMS, RDF, RDFS, BNode, Literal, URIRef
from rdflib.compare import to_isomorphic

from bluecore_models.utils.graph import (
    BF,
    BFLC,
    CONTEXT,
    MADS,
    _as_arrays,
    _expand_bnode,
    find_duplicate_bnode_values,
    frame_jsonld,
    generate_entity_graph,
    init_graph,
    load_jsonld,
    replace_uri,
    strip_duplicate_bnode_values,
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

    # and one of the two is named as the copy to remove
    assert len(dup.redundant) == 1
    assert dup.redundant[0] in graph.objects(instance_uri, BF.title)


def test_strip_duplicate_bnode_values():
    """
    Stripping the LC record leaves one of the two identical titles, with its own
    description intact, and doesn't touch the note that was never duplicated.
    """
    graph = init_graph()
    graph.parse(data=LC_REPEATED_DESCRIPTION, format="xml")
    instance_uri = URIRef("http://id.loc.gov/resources/instances/20133027")
    before = len(graph)

    stripped = strip_duplicate_bnode_values(graph)

    assert len(stripped) == 1
    assert stripped[0].copies == 2

    # one title survives, still described
    titles = list(graph.objects(instance_uri, BF.title))
    assert len(titles) == 1
    assert graph.value(subject=titles[0], predicate=BF.mainTitle) == Literal(
        "AI & society"
    )
    assert (titles[0], RDF.type, BF.Title) in graph

    # the note is the control: never duplicated, so never touched
    assert len(list(graph.objects(instance_uri, BF.note))) == 1

    # the discarded title's description went with it, and nothing else did: the
    # bf:title edge, plus the rdf:type and bf:mainTitle describing the node
    assert len(graph) == before - 3

    # and there is nothing left to find, in this pass or another
    assert find_duplicate_bnode_values(graph) == []
    assert strip_duplicate_bnode_values(graph) == []


def test_strip_duplicate_bnode_values_keeps_shared_node():
    """
    The same blank node can be the value of more than one resource. Unlinking it
    from the resource that duplicated it must leave the other resource's
    assertion, and its description, alone.
    """
    graph = init_graph()
    work = URIRef("http://example.com/work")
    other = URIRef("http://example.com/other")

    shared, copy = BNode(), BNode()
    for node in (shared, copy):
        graph.add((work, BF.title, node))
        graph.add((node, RDF.type, BF.Title))
        graph.add((node, BF.mainTitle, Literal("AI and society")))
    graph.add((other, BF.title, shared))

    stripped = strip_duplicate_bnode_values(graph)
    assert len(stripped) == 1
    assert stripped[0].copies == 2

    # the work is down to one title
    assert len(list(graph.objects(work, BF.title))) == 1

    # whichever copy was dropped, the other resource keeps its value described
    kept = graph.value(subject=other, predicate=BF.title)
    assert kept == shared
    assert graph.value(subject=shared, predicate=BF.mainTitle) == Literal(
        "AI and society"
    )
    assert (shared, RDF.type, BF.Title) in graph


def test_strip_duplicate_bnode_values_keeps_shared_nested_node():
    """
    Two values can be distinct nodes that nest the *same* node, rather than
    copies of it: here two bf:Contribution nodes share one bf:agent. Sharing a
    nested node is what makes the parents identical in content in the first
    place, so this shape arrives already reported as a duplicate -- and removing
    one parent's description must not follow the shared agent down and strip the
    description the surviving parent still points at.
    """
    graph = init_graph()
    work = URIRef("http://example.com/work")

    agent = BNode()
    graph.add((agent, RDF.type, BF.Agent))
    graph.add((agent, RDFS.label, Literal("Jane Austen")))

    for _ in range(2):
        contribution = BNode()
        graph.add((work, BF.contribution, contribution))
        graph.add((contribution, RDF.type, BF.Contribution))
        graph.add((contribution, BF.agent, agent))

    stripped = strip_duplicate_bnode_values(graph)
    assert len(stripped) == 1
    assert stripped[0].copies == 2

    # one contribution survives, still typed and still pointing at the agent
    contributions = list(graph.objects(work, BF.contribution))
    assert len(contributions) == 1
    assert (contributions[0], RDF.type, BF.Contribution) in graph
    assert graph.value(subject=contributions[0], predicate=BF.agent) == agent

    # and the agent it points at is still described
    assert graph.value(subject=agent, predicate=RDFS.label) == Literal("Jane Austen")
    assert (agent, RDF.type, BF.Agent) in graph


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

    # so stripping leaves the graph exactly as it was
    before = len(graph)
    assert strip_duplicate_bnode_values(graph) == []
    assert len(graph) == before


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


def _scalar_properties(node, found=None):
    """Every property whose value is not a list, at any depth.

    @context is skipped: it is a vocabulary rather than data, and its values are
    left as they are.
    """
    found = [] if found is None else found
    if isinstance(node, list):
        for item in node:
            _scalar_properties(item, found)
        return found
    if not isinstance(node, dict):
        return found
    for key, value in node.items():
        if key == "@context":
            continue
        if not key.startswith("@") and not isinstance(value, list):
            found.append(key)
        _scalar_properties(value, found)
    return found


def test_frame_jsonld_leaves_no_scalar_properties():
    """Every property is a list, even with one value, at every depth.

    The point of the coercion: before it, 66 of 134 properties in a 300 record
    sample came out sometimes as a bare value and sometimes as a list, so a
    consumer had to branch on the type of every value it touched.
    """
    with Path("tests/data/23807141.jsonld").open() as fo:
        framed = frame_jsonld(
            "http://id.loc.gov/resources/instances/23807141", json.load(fo)
        )

    assert _scalar_properties(framed) == []
    # and the thing that prompted it: a single identifier is still a list
    assert isinstance(framed["identifiedBy"], list)


def test_frame_jsonld_coercion_adds_and_removes_no_triples():
    """Coercing to arrays is a change of shape, not of meaning.

    Compared against the same document framed without the coercion, since a
    one-value list and a bare value are the same statement in JSON-LD. If this
    ever fails, the reframe backfill in bluecore-workflows would be rewriting
    history rather than reserialising it.
    """
    with Path("tests/data/23807141.jsonld").open() as fo:
        source = json.load(fo)

    uri = "http://id.loc.gov/resources/instances/23807141"
    coerced = frame_jsonld(uri, source)
    uncoerced = jsonld.frame(
        source, {"@context": CONTEXT, "@id": uri, "@embed": "@always"}
    )

    assert to_isomorphic(load_jsonld(dict(coerced))) == to_isomorphic(
        load_jsonld(dict(uncoerced))
    )


def test_frame_jsonld_is_idempotent():
    """Re-framing already framed data changes nothing.

    The backfill may be run more than once -- and the plan for it uses exactly
    this property as its check, by re-running in dry-run mode afterwards and
    expecting zero changes.
    """
    with Path("tests/data/23807141.jsonld").open() as fo:
        source = json.load(fo)

    uri = "http://id.loc.gov/resources/instances/23807141"
    once = frame_jsonld(uri, source)
    twice = frame_jsonld(uri, dict(once))

    assert twice == once


def test_frame_jsonld_makes_node_types_a_list():
    """A resource's @type is a list, however many types it has.

    It is the property a consumer reads most often, and it was the last one
    arriving both ways: over 200 corpus records, 201 resources came out with a
    list and 199 with a bare string.
    """
    with Path("tests/data/23807141.jsonld").open() as fo:
        framed = frame_jsonld(
            "http://id.loc.gov/resources/instances/23807141", json.load(fo)
        )

    assert isinstance(framed["@type"], list)

    def every_type(node, found):
        if isinstance(node, list):
            for item in node:
                every_type(item, found)
        elif isinstance(node, dict):
            if "@type" in node and "@value" not in node:
                found.append(node["@type"])
            for key, value in node.items():
                # not @context: a term definition such as {"@type": "@id"} is a
                # dict with a scalar @type and no @value, so it is indis-
                # tinguishable from a node to a walker that does not skip it
                if key != "@context":
                    every_type(value, found)
        return found

    types = every_type(framed, [])
    assert types, "the fixture has typed nodes"
    assert all(isinstance(t, list) for t in types), "at every depth, not just the top"


def test_frame_jsonld_leaves_a_datatype_alone():
    """In a value object @type is the literal's datatype, and must stay a string.

    The spec requires it, and pyld enforces it: wrapping it produces JSON-LD that
    will not expand, which loses every triple in the document rather than raising
    anywhere that points at the cause. This is the guard for that, because the
    mistake is invisible until something tries to read the data back.
    """
    coerced = _as_arrays(
        {
            "@type": "Place",
            "code": {
                "@value": "enk",
                "@type": "http://www.w3.org/2001/XMLSchema#string",
            },
        }
    )

    assert coerced["@type"] == ["Place"], "a node's type is a list"
    assert coerced["code"][0]["@type"] == "http://www.w3.org/2001/XMLSchema#string", (
        "a datatype is not"
    )

    # and the whole thing still parses, which is what the guard protects
    graph = load_jsonld({"@context": CONTEXT, "@id": "http://example.org/1", **coerced})
    assert len(graph) > 0
