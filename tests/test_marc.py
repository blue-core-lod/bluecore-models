"""Tests for Blue Core policy applied to MARC-derived BIBFRAME.

The fixture is real marc2bibframe2 output, not hand-written RDF. That matters:
the previous implementation of replace_dlc_assigner (in bluecore-workflows)
looked for rdf:resource on bf:assigner, a shape the transform never emits, and
was a silent no-op for weeks because its tests hand-wrote the XML they wanted.
Regenerate the fixture with:

    marc-bibframe tests/data/marc2bibframe2-dlc.marcxml -f rdfxml \
        -o tests/data/marc2bibframe2-dlc.rdf
"""

from pathlib import Path

import pytest
from rdflib import Graph, Literal, URIRef

from bluecore_models.namespaces import BF, RDF
from bluecore_models.utils.marc import (
    CBC_ORG_URI,
    DLC_ORG_URI,
    replace_dlc_assigner,
)

FIXTURE = Path(__file__).parent / "data" / "marc2bibframe2-dlc.rdf"


@pytest.fixture
def graph() -> Graph:
    return Graph().parse(FIXTURE, format="xml")


def test_fixture_has_the_shape_the_transform_actually_emits(graph):
    """Guard against going back to hand-written RDF that cannot fail."""
    assert list(graph.subjects(BF.assigner, DLC_ORG_URI))
    assert list(graph.subjects(BF.agent, DLC_ORG_URI))
    assert (DLC_ORG_URI, RDF.type, BF.Agent) in graph


def test_rewrites_the_assigner(graph):
    replace_dlc_assigner(graph)

    assert not list(graph.subjects(BF.assigner, DLC_ORG_URI))
    assert len(list(graph.subjects(BF.assigner, CBC_ORG_URI))) == 1


def test_leaves_bf_agent_pointing_at_dlc(graph):
    """The record really was created by LC; only the assigner is ours."""
    replace_dlc_assigner(graph)

    assert list(graph.subjects(BF.agent, DLC_ORG_URI))


def test_leaves_the_dlc_agent_description_alone(graph):
    replace_dlc_assigner(graph)

    assert (DLC_ORG_URI, RDF.type, BF.Agent) in graph
    assert (DLC_ORG_URI, BF.code, Literal("DLC")) in graph


def test_does_not_move_the_description_onto_cbc(graph):
    """Repointing a reference must not relabel CBC as DLC."""
    replace_dlc_assigner(graph)

    assert (CBC_ORG_URI, BF.code, Literal("DLC")) not in graph


def test_is_idempotent(graph):
    replace_dlc_assigner(graph)
    once = len(graph)
    replace_dlc_assigner(graph)

    assert len(graph) == once


def test_leaves_other_assigners_alone(graph):
    other = URIRef("http://id.loc.gov/vocabulary/organizations/cst")
    subject = URIRef("http://example.org/thing")
    graph.add((subject, BF.assigner, other))

    replace_dlc_assigner(graph)

    assert (subject, BF.assigner, other) in graph


def test_graph_without_dlc_is_untouched():
    graph = Graph()
    graph.add(
        (URIRef("http://example.org/a"), BF.assigner, URIRef("http://example.org/b"))
    )
    before = len(graph)

    replace_dlc_assigner(graph)

    assert len(graph) == before
