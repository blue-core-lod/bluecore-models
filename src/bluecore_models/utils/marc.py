"""Blue Core policy applied to BIBFRAME converted from MARC.

The conversion itself lives in the marc-bibframe package, which knows nothing
about Blue Core. What it hands back is generic BIBFRAME; the functions here
apply the decisions that are ours rather than the Library of Congress's.
"""

from rdflib import Graph, URIRef

from bluecore_models.namespaces import BF

DLC_ORG_URI = URIRef("http://id.loc.gov/vocabulary/organizations/dlc")
CBC_ORG_URI = URIRef("http://id.loc.gov/vocabulary/organizations/cbc")


def replace_dlc_assigner(graph: Graph) -> None:
    """Repoint bf:assigner from the DLC organization to CBC, in place.

    marc2bibframe2 names DLC as the assigner of identifiers it derives from
    the record, because that is what the MARC says. Blue Core records are
    assigned by CBC, so the assigner is ours to correct.

    Only bf:assigner is touched. Other statements about DLC -- bf:agent on
    admin metadata, the bf:code and rdf:type on the agent itself -- describe
    the Library of Congress accurately and are left alone.

    Note that the CBC URI is left undescribed, exactly as the DLC URI would
    have been had it not also been the bf:agent. Saving the graph will create
    an OtherResource for it from whatever the payload knows, which is a URI
    and nothing else.
    """
    for subject in list(graph.subjects(BF.assigner, DLC_ORG_URI)):
        graph.remove((subject, BF.assigner, DLC_ORG_URI))
        graph.add((subject, BF.assigner, CBC_ORG_URI))
