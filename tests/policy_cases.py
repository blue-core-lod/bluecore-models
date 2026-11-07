"""
Today's behaviour of `save_graph`, written down as a policy.

Each row of the two policy tables in #213 (what happens to each resource, and
which cited URIs become links) is a key of `ROWS`. Each `PolicyCase` is one
outcome under one row: it sets up a record, saves it against a real database
and asserts what the code does today. `render_markdown` turns the cases into the
table catalogers read. Run this file to print that table.
"""

import sys
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

from rdflib import Graph, URIRef
from sqlalchemy.orm import Session, sessionmaker

from bluecore_models.models import BibframeOtherResources
from bluecore_models.namespaces import BF

Decision = Literal["chosen", "inherited", "undecided"]


@dataclass(frozen=True)
class PolicyCase:
    id: str
    row: str
    situation: str
    outcome: str
    sentence: str
    decision: Decision
    follow_up: str | None
    run: Callable[[sessionmaker], None]


# Row id to the row's text in #213. R1-R11 are the resources table in order,
# L1-L8 the links table in order, X1 and X2 the related questions.
ROWS: dict[str, str] = {
    "R1": "ingest / blank Work / Instance / Hub",
    "R2": "ingest / URI resource with its own adminMetadata (not our stub marker)",
    "R3": "ingest / URI resource without adminMetadata, when the record uses adminMetadata at all",
    "R4": "ingest / any URI resource, when the record has no adminMetadata",
    "R5": "ingest / Other Resource",
    "R6": "write(K) / the root blank node of K",
    "R7": "write(K) / URI resource of K",
    "R8": "write(K) / any other blank node",
    "R9": "write(K) / resource of another kind",
    "R10": "write(K) / Other Resource",
    "R11": "write(K) / (all)",
    "L1": "described in the payload, or already held in `other_resources`",
    "L2": "neither described nor held",
    "L3": "a BIBFRAME, MADS or RDF vocabulary term",
    "L4": "typed as a Work, Instance or Hub in the payload",
    "L5": "object of `instanceOf`, `hasInstance`, `expressionOf`, `hasExpression`, `relatedTo`, `hasItem`, `itemOf`, `bf:associatedResource` or `bflc:associatedResource`",
    "L6": "under `http://id.loc.gov/resources/`",
    "L7": "cited only under `bf:adminMetadata`",
    "L8": "an LC Item",
    "X1": "Hub links and labels: `expressionOf` only sets `hub_id` when the payload describes the Hub",
    "X2": "Re-ingest links: on re-ingest, a PRIMARY resource that `_keep_existing` protects keeps its stored description, but its Other Resource links are rebuilt from the incoming record",
}

CASES: list[PolicyCase] = []

# Places where #213's text misreads the code, found while writing cases.
TABLE_CORRECTIONS: list[str] = []


def ext_uri(kind: str) -> str:
    """An LC-shaped URI with a fresh uuid, the way resources arrive in an ingest."""
    return f"http://id.loc.gov/resources/{kind}/{uuid.uuid4()}"


def bc_uri(kind: str) -> str:
    """A Bluecore URI with a fresh uuid."""
    return f"https://bcld.info/{kind}/{uuid.uuid4()}"


def minted(graph: Graph, source: str) -> str:
    """The Bluecore URI a save minted for a source URI, read back from the graph."""
    for admin_metadata in graph.subjects(BF.derivedFrom, URIRef(source)):
        subject = graph.value(predicate=BF.adminMetadata, object=admin_metadata)
        if subject is not None:
            return str(subject)
    raise AssertionError(f"no bluecore uri was minted for {source}")


def statuses(data: dict[str, Any]) -> list[str]:
    """Every bf:status @id recorded in a resource's AdminMetadata nodes."""
    admin_metadata = data.get("adminMetadata", [])
    if isinstance(admin_metadata, dict):
        admin_metadata = [admin_metadata]
    found: list[str] = []
    for block in admin_metadata:
        if not isinstance(block, dict):
            continue
        status = block.get("status")
        for item in status if isinstance(status, list) else [status]:
            if isinstance(item, dict) and "@id" in item:
                found.append(item["@id"])
    return found


def linked(session: Session, resource: Any) -> list[str]:
    """The URIs of the Other Resources a Work or Instance is linked to."""
    return [
        link.other_resource.uri
        for link in session.query(BibframeOtherResources).where(
            BibframeOtherResources.bibframe_resource_id == resource.id
        )
    ]


def row(session: Session, model: Any, uri: str) -> Any:
    """The stored row of the given model with this URI, or None."""
    return session.query(model).where(model.uri == uri).first()


def _cell(text: str) -> str:
    return " ".join(text.split()).replace("|", "\\|")


def render_markdown(cases: list[PolicyCase]) -> str:
    """One table of the cases, ordered by `ROWS` and then by the order given."""
    order = {row_id: n for n, row_id in enumerate(ROWS)}
    ordered = sorted(cases, key=lambda case: order[case.row])
    lines = [
        "| Row | Situation | Today | For catalogers | Decision |",
        "| --- | --- | --- | --- | --- |",
    ]
    for case in ordered:
        decision: str = case.decision
        if case.decision == "inherited" and case.follow_up:
            decision = f"inherited ({case.follow_up})"
        cells = [case.row, case.situation, case.outcome, case.sentence, decision]
        lines.append("| " + " | ".join(_cell(c) for c in cells) + " |")
    return "\n".join(lines)


if __name__ == "__main__":
    sys.stdout.write(render_markdown(CASES) + "\n")
