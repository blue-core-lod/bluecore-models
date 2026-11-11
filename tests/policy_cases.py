"""
Today's behaviour of `save_graph`, written down as a policy.

Each row of the two policy tables in #213 (what happens to each resource, and
which cited URIs become links) is a key of `ROWS`. Each `PolicyCase` is one
outcome under one row: it sets up a record, saves it against a real database
and asserts what the code does today. Each also says what the code decides (its
`Answer`) and how to work that out with no database. `render_document` turns the
cases into docs/save-policy.md; run this file to print it.
"""

import json
import sys
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any, Literal

import psycopg.errors
import pytest
from rdflib import BNode, Graph, URIRef
from rdflib import Literal as RdfLiteral
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from bluecore_models.bluecore_graph import (
    DEFAULT_STATUS,
    STUB_STATUS,
    Action,
    BluecoreGraph,
    Citation,
    Entry,
    Ingest,
    Kind,
    Mode,
    Role,
    Write,
    _action,
    _admin_nodes,
    _citation,
    save_graph,
)
from bluecore_models.models import (
    BibframeOtherResources,
    Hub,
    Instance,
    OtherResource,
    Work,
)
from bluecore_models.namespaces import BF, BFLC, RDF, RDFS
from bluecore_models.utils.graph import CONTEXT_URL, load_jsonld

Decision = Literal["chosen", "inherited", "undecided"]

# What the code decides in a case, about the resource the case is about in its
# last save: its kind, role and action (no action for an inline one), or, for a
# case about a link, what the cited uri is.
type Answer = tuple[Kind, Role, Action | None] | Citation


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
    # the case's Answer, and how to work it out with no database
    answer: Answer
    check: Callable[[], Answer]


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

# Every case is inherited until the team reviews the table before the dev
# reload; cases with a follow-up of their own keep it.
REVIEW_BEFORE_RELOAD = "review before the dev reload (#221)"

# Places where #213's text misreads the code, found while writing cases.
TABLE_CORRECTIONS: list[str] = [
    (
        "X1: #213 says `expressionOf` to an undescribed Hub leaves `hub_id` unset. "
        "In fact the untyped Hub URI is taken for an Other Resource. When the "
        "Hub is not stored, an Other Resource row is created at its URI; when "
        "it is stored, the save fails with a unique-key collision "
        "(resource_base_uri_key) on the URI the Hub row already holds."
    ),
    (
        "L5: #213 lists `instanceOf` and `hasInstance` among the predicates "
        "whose objects are never linked. That exclusion can never apply today: "
        "inference types their objects as records (Work, Instance), which are "
        "then minted, so the cited URI is replaced before linking."
    ),
]


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


# What a stored row holds, as far as _action reads it: a full description, a
# stub, or (for an Other Resource) anything.
FULL = {"adminMetadata": [{"status": {"@id": str(DEFAULT_STATUS)}}]}
STUB = {"adminMetadata": [{"status": {"@id": str(STUB_STATUS)}}]}
ANY: dict[str, Any] = {}

INGEST = Ingest(update_others=True)


def resource_answer(
    g: Graph,
    mode: Mode,
    original: Any,
    kind: Kind,
    stored: dict[str, Any] | None = None,
) -> Answer:
    """
    What the code decides, with no database, for the resource of kind that
    arrives in g as original (a uri, or a blank node), saved under mode, when
    the row we hold for it has stored as its data (None: we hold none).
    """
    bg = BluecoreGraph(g)
    bg._prepare(mode)
    node = _node(original)
    role = bg._role(bg._roles, node, kind)
    if role == Role.INLINE:
        return (kind, role, None)
    # anything without a Bluecore URI is minted one
    subject = node if str(node).startswith(str(bg.namespace)) else URIRef(bc_uri("x"))
    entry = Entry(
        subject=subject,
        kind=kind,
        role=role,
        graph=Graph(),
        original=node,
        cited=frozenset(),
    )
    existing = None if stored is None else SimpleNamespace(data=stored)
    return (kind, role, _action(entry, existing, mode))


def citation_answer(g: Graph, s: Any, p: URIRef, o: Any) -> Citation:
    """What the cited uri o is, cited by (s, p, o) in g once the save has read it."""
    graph = BluecoreGraph(g).graph
    return _citation(graph, _node(s), p, URIRef(o), _admin_nodes(graph))


def _node(value: Any) -> Any:
    """A node as given, or a uri given as a string (a BNode is a str too)."""
    return value if isinstance(value, (URIRef, BNode)) else URIRef(value)


# --- what happens to each resource (R1-R11) -----------------------------------


def _work_data(session: Session, uri: str) -> str:
    """A stored Work's data as stable JSON text, for comparing and searching."""
    stored = row(session, Work, uri)
    assert stored is not None, f"no Work row at {uri}"
    return json.dumps(stored.data, sort_keys=True)


def _record(described: str, title: str, stub: str | None = None) -> Graph:
    """
    A record the way an ingest sees one: a Work it describes (with AdminMetadata
    of its own) and, optionally, a Work it only points at (the stub).
    """
    g = Graph()
    work = URIRef(described)
    g.add((work, RDF.type, BF.Work))
    title_node = BNode()
    g.add((work, BF.title, title_node))
    g.add((title_node, RDF.type, BF.Title))
    g.add((title_node, BF.mainTitle, RdfLiteral(title)))
    admin_metadata = BNode()
    g.add((work, BF.adminMetadata, admin_metadata))
    g.add((admin_metadata, RDF.type, BF.AdminMetadata))
    g.add((admin_metadata, BF.status, URIRef("http://id.loc.gov/vocabulary/mstatus/n")))
    if stub is not None:
        stub_work = URIRef(stub)
        g.add((work, BF.relatedTo, stub_work))
        g.add((stub_work, RDF.type, BF.Work))
        g.add((stub_work, RDFS.label, RdfLiteral("stub label")))
    return g


def _describe(g: Graph, uri: str, class_: URIRef, label: str) -> URIRef:
    """Describe an Other Resource the way the editor's expanded payload does."""
    g.add((URIRef(uri), RDF.type, class_))
    g.add((URIRef(uri), RDFS.label, RdfLiteral(label)))
    return URIRef(uri)


def _work_citing_agent(agent: str, label: str | None) -> tuple[Graph, str]:
    """A new Work citing an Agent, which is described only when a label is given."""
    g = Graph()
    work = bc_uri("works")
    g.add((URIRef(work), RDF.type, BF.Work))
    g.add((URIRef(work), BF.contribution, URIRef(agent)))
    if label is not None:
        _describe(g, agent, BF.Agent, label)
    return g, work


def _stored_other(session: Session, uri: str) -> str:
    stored = row(session, OtherResource, uri)
    assert stored is not None, f"no Other Resource row at {uri}"
    return json.dumps(stored.data, sort_keys=True)


def _instance_of_blank_work(instance_uri: str) -> Graph:
    """An Instance whose Work is described inside it, with no URI."""
    return load_jsonld(
        {
            "@context": CONTEXT_URL,
            "@id": instance_uri,
            "@type": "Instance",
            "title": {"@type": "Title", "mainTitle": "Gravity's rainbow"},
            "instanceOf": {
                "@type": "Work",
                "title": {"@type": "Title", "mainTitle": "Blank Work Title"},
            },
        }
    )


def _ingest_blank_record_stays_inline(session_maker: sessionmaker) -> None:
    instance_uri = bc_uri("instances")
    with session_maker() as session:
        works_before = session.query(Work).count()

    save_graph(session_maker, _instance_of_blank_work(instance_uri))

    with session_maker() as session:
        instance = row(session, Instance, instance_uri)
        assert instance is not None
        assert session.query(Work).count() == works_before
        assert instance.work is None
        assert "Blank Work Title" in json.dumps(instance.data)


def _ingest_described_new_inserted(session_maker: sessionmaker) -> None:
    source = ext_uri("works")
    out = save_graph(session_maker, _record(source, "Brand New"))
    uri = minted(out, source)

    with session_maker() as session:
        assert session.query(Work).where(Work.uri == uri).count() == 1
        work = row(session, Work, uri)
        assert str(DEFAULT_STATUS) in statuses(work.data)
        derived = work.data["adminMetadata"][0]["derivedFrom"]
        assert [d["@id"] for d in derived] == [source]


def _ingest_described_ours_updated(session_maker: sessionmaker) -> None:
    source = ext_uri("works")
    out = save_graph(
        session_maker,
        load_jsonld(
            {
                "@context": CONTEXT_URL,
                "@id": source,
                "@type": BF.Work,
                "title": ["Superman"],
            }
        ),
    )
    uri = minted(out, source)
    with session_maker() as session:
        described = dict(row(session, Work, uri).data)
    assert described["title"] == ["Superman"]

    described["title"] = ["Superman 2"]
    save_graph(session_maker, load_jsonld(described))

    with session_maker() as session:
        assert row(session, Work, uri).data["title"] == ["Superman 2"]


def _ingest_described_stub_replaced(session_maker: sessionmaker) -> None:
    described, stub = ext_uri("works"), ext_uri("works")
    first = save_graph(session_maker, _record(described, "Described", stub=stub))
    stub_uri = minted(first, stub)
    with session_maker() as session:
        assert str(STUB_STATUS) in statuses(row(session, Work, stub_uri).data)

    second = save_graph(session_maker, _record(stub, "Now Fully Described"))
    assert minted(second, stub) == stub_uri

    with session_maker() as session:
        data = _work_data(session, stub_uri)
        assert "Now Fully Described" in data
        assert str(STUB_STATUS) not in statuses(row(session, Work, stub_uri).data)


def _ingest_described_reingest_kept(session_maker: sessionmaker) -> None:
    source = ext_uri("works")
    out = save_graph(session_maker, _record(source, "REINGESTED 1"))
    uri = minted(out, source)

    with session_maker() as session:
        edited = dict(row(session, Work, uri).data)
    edited["title"] = {"@type": "Title", "mainTitle": "Cataloger Edit"}
    save_graph(session_maker, load_jsonld(edited), primary_class=BF.Work)
    with session_maker() as session:
        assert "Cataloger Edit" in _work_data(session, uri)

    save_graph(session_maker, _record(source, "REINGESTED 1"))

    with session_maker() as session:
        data = _work_data(session, uri)
        assert "Cataloger Edit" in data
        assert "REINGESTED 1" not in data


def _ingest_stub_new_inserted_incomplete(session_maker: sessionmaker) -> None:
    described, stub = ext_uri("works"), ext_uri("works")
    out = save_graph(session_maker, _record(described, "Described", stub=stub))

    with session_maker() as session:
        stubbed = row(session, Work, minted(out, stub))
        assert str(STUB_STATUS) in statuses(stubbed.data)
        assert str(DEFAULT_STATUS) not in statuses(stubbed.data)


def _ingest_stub_held_kept(session_maker: sessionmaker) -> None:
    source = ext_uri("works")
    out = save_graph(session_maker, _record(source, "Full Description"))
    uri = minted(out, source)
    with session_maker() as session:
        before = _work_data(session, uri)

    save_graph(session_maker, _record(ext_uri("works"), "Other", stub=source))

    with session_maker() as session:
        assert _work_data(session, uri) == before


def _ingest_stub_held_keeps_links(session_maker: sessionmaker) -> None:
    source = ext_uri("works")
    agent = f"http://id.loc.gov/rwo/agents/{uuid.uuid4()}"
    g = _record(source, "Described With Agent")
    g.add((URIRef(source), BF.contributor, URIRef(agent)))
    _describe(g, agent, BF.Agent, "Some Agent")
    out = save_graph(session_maker, g)
    uri = minted(out, source)
    with session_maker() as session:
        assert agent in linked(session, row(session, Work, uri))

    save_graph(session_maker, _record(ext_uri("works"), "Other", stub=source))

    with session_maker() as session:
        assert agent in linked(session, row(session, Work, uri))


def _ingest_stub_new_gets_links(session_maker: sessionmaker) -> None:
    agent = f"http://id.loc.gov/rwo/agents/{uuid.uuid4()}"
    holder_work = ext_uri("works")
    holder = _record(holder_work, "Holds The Agent")
    holder.add((URIRef(holder_work), BF.contributor, URIRef(agent)))
    _describe(holder, agent, BF.Agent, "Held Agent")
    save_graph(session_maker, holder)
    with session_maker() as session:
        assert row(session, OtherResource, agent) is not None

    described, stub = ext_uri("works"), ext_uri("works")
    g = _record(described, "Cites Through A Stub", stub=stub)
    # the stub cites the held Agent by URI, without describing it
    g.add((URIRef(stub), BF.contributor, URIRef(agent)))
    out = save_graph(session_maker, g)

    with session_maker() as session:
        stubbed = row(session, Work, minted(out, stub))
        assert str(STUB_STATUS) in statuses(stubbed.data)
        assert agent in linked(session, stubbed)


def _works_without_admin_metadata(first: str, second: str) -> Graph:
    g = Graph()
    for uri, title in ((first, "First Work"), (second, "Second Work")):
        title_node = BNode()
        g.add((URIRef(uri), RDF.type, BF.Work))
        g.add((URIRef(uri), BF.title, title_node))
        g.add((title_node, RDF.type, BF.Title))
        g.add((title_node, BF.mainTitle, RdfLiteral(title)))
    return g


def _ingest_no_admin_metadata_all_primary(session_maker: sessionmaker) -> None:
    first, second = ext_uri("works"), ext_uri("works")
    out = save_graph(session_maker, _works_without_admin_metadata(first, second))

    with session_maker() as session:
        for source in (first, second):
            work = row(session, Work, minted(out, source))
            assert work is not None
            assert str(DEFAULT_STATUS) in statuses(work.data)
            assert str(STUB_STATUS) not in statuses(work.data)


def _ingest_other_new_inserted(session_maker: sessionmaker) -> None:
    agent = f"http://id.loc.gov/rwo/agents/{uuid.uuid4()}"
    g, _ = _work_citing_agent(agent, "Fresh Agent")
    save_graph(session_maker, g, update_other_resources=False)

    with session_maker() as session:
        assert "Fresh Agent" in _stored_other(session, agent)


def _ingest_other_held_updated(session_maker: sessionmaker) -> None:
    agent = f"http://id.loc.gov/rwo/agents/{uuid.uuid4()}"
    save_graph(session_maker, _work_citing_agent(agent, "Original")[0])
    with session_maker() as session:
        assert "Original" in _stored_other(session, agent)

    save_graph(
        session_maker,
        _work_citing_agent(agent, "Updated")[0],
        update_other_resources=True,
    )

    with session_maker() as session:
        assert "Updated" in _stored_other(session, agent)


def _ingest_other_held_kept(session_maker: sessionmaker) -> None:
    agent = f"http://id.loc.gov/rwo/agents/{uuid.uuid4()}"
    save_graph(session_maker, _work_citing_agent(agent, "Original")[0])
    with session_maker() as session:
        before = _stored_other(session, agent)

    g, _ = _work_citing_agent(agent, "Changed")
    save_graph(session_maker, g, update_other_resources=False)

    with session_maker() as session:
        after = _stored_other(session, agent)
        assert after == before
        assert "Original" in after
        assert "Changed" not in after


def _new_work_accompanied_by(uri: str) -> Graph:
    """A Work being created, with no URI, accompanied by one that has a URI."""
    return load_jsonld(
        {
            "@context": CONTEXT_URL,
            "@type": "Work",
            "accompaniedBy": {"@id": uri, "@type": "Work"},
        }
    )


def _write_root_blank_inserted(session_maker: sessionmaker) -> None:
    graph = _new_work_accompanied_by(ext_uri("works"))
    bg = BluecoreGraph(graph)
    bg.save(session_maker, primary_class=BF.Work)

    assert bg.created_uri is not None
    assert bg.created_uri == next(bg.graph.subjects(BF.accompaniedBy))
    with session_maker() as session:
        assert row(session, Work, str(bg.created_uri)) is not None


def _write_uri_new_inserted(session_maker: sessionmaker) -> None:
    uri = bc_uri("works")
    g = Graph()
    g.add((URIRef(uri), RDF.type, BF.Work))
    save_graph(session_maker, g, primary_class=BF.Work)

    with session_maker() as session:
        assert session.query(Work).where(Work.uri == uri).count() == 1


def _write_uri_held_updated(session_maker: sessionmaker) -> None:
    uri = bc_uri("works")
    document: dict[str, Any] = {
        "@context": CONTEXT_URL,
        "@id": uri,
        "@type": BF.Work,
        "title": {"@type": "Title", "mainTitle": "Gravity's Rainbow"},
    }
    save_graph(session_maker, load_jsonld(document), primary_class=BF.Work)
    document["title"] = {"@type": "Title", "mainTitle": "Vineland"}
    save_graph(session_maker, load_jsonld(document), primary_class=BF.Work)

    with session_maker() as session:
        work = row(session, Work, uri)
        assert work.data["title"][0]["mainTitle"] == ["Vineland"]


def _new_work_with_blank_work() -> Graph:
    """A Work being created, accompanied by a Work described inside it."""
    return load_jsonld(
        {
            "@context": CONTEXT_URL,
            "@type": "Work",
            "accompaniedBy": {
                "@type": "Work",
                "language": {"@id": "http://id.loc.gov/vocabulary/languages/lat"},
            },
        }
    )


def _write_other_blank_inline(session_maker: sessionmaker) -> None:
    with session_maker() as session:
        works_before = session.query(Work).count()

    graph = save_graph(
        session_maker, _new_work_with_blank_work(), primary_class=BF.Work
    )
    parent = str(next(graph.subjects(BF.accompaniedBy)))

    with session_maker() as session:
        work = row(session, Work, parent)
        assert work.data["accompaniedBy"] == [
            {
                "@type": ["Work"],
                "language": [{"@id": "http://id.loc.gov/vocabulary/languages/lat"}],
            }
        ]
        # the blank Work has no row of its own: the parent is the only new Work
        assert session.query(Work).count() == works_before + 1


def _work_with_labelled_instance(work_uri: str, inst_uri: str) -> Graph:
    """A Work naming an Instance it only gives a label."""
    g = Graph()
    g.add((URIRef(work_uri), RDF.type, BF.Work))
    g.add((URIRef(work_uri), BF.hasInstance, URIRef(inst_uri)))
    g.add((URIRef(inst_uri), RDFS.label, RdfLiteral("just a label")))
    return g


def _instance_of_labelled_work(inst_uri: str, work_uri: str) -> Graph:
    """An Instance naming its Work, which it only gives a label."""
    g = Graph()
    g.add((URIRef(inst_uri), RDF.type, BF.Instance))
    g.add((URIRef(inst_uri), BF.instanceOf, URIRef(work_uri)))
    g.add((URIRef(work_uri), RDFS.label, RdfLiteral("display label")))
    return g


def _write_reference_new_inserted(session_maker: sessionmaker) -> None:
    work_uri, inst_uri = bc_uri("works"), bc_uri("instances")
    g = _work_with_labelled_instance(work_uri, inst_uri)
    save_graph(session_maker, g, primary_class=BF.Work)

    with session_maker() as session:
        inst = row(session, Instance, inst_uri)
        assert inst is not None
        assert "just a label" in json.dumps(inst.data)
        assert inst.work_id == row(session, Work, work_uri).id


def _write_reference_held_kept(session_maker: sessionmaker) -> None:
    work_uri = bc_uri("works")
    save_graph(
        session_maker,
        load_jsonld(
            {
                "@id": work_uri,
                "@type": BF.Work,
                "title": {"mainTitle": "Full Work", "@type": "Title"},
            }
        ),
    )
    with session_maker() as session:
        before = _work_data(session, work_uri)

    inst_uri = bc_uri("instances")
    g = _instance_of_labelled_work(inst_uri, work_uri)
    save_graph(session_maker, g, primary_class=BF.Instance)

    with session_maker() as session:
        assert _work_data(session, work_uri) == before
        assert "display label" not in before


def _write_reference_keeps_links(session_maker: sessionmaker) -> None:
    work_uri = bc_uri("works")
    subject = f"http://id.loc.gov/authorities/subjects/{uuid.uuid4()}"
    agent = f"http://id.loc.gov/rwo/agents/{uuid.uuid4()}"
    g = Graph()
    g.add((URIRef(work_uri), RDF.type, BF.Work))
    g.add((URIRef(work_uri), BF.subject, _describe(g, subject, BF.Topic, "S")))
    g.add((URIRef(work_uri), BF.contributor, _describe(g, agent, BF.Agent, "A")))
    save_graph(session_maker, g, primary_class=BF.Work)
    with session_maker() as session:
        before = sorted(linked(session, row(session, Work, work_uri)))
    assert before == sorted([subject, agent])

    inst_uri = bc_uri("instances")
    g = _instance_of_labelled_work(inst_uri, work_uri)
    save_graph(session_maker, g, primary_class=BF.Instance)

    with session_maker() as session:
        assert sorted(linked(session, row(session, Work, work_uri))) == before


def _write_other_reference_kept(session_maker: sessionmaker) -> None:
    agent = bc_uri("other_resources")
    g1, _ = _work_citing_agent(agent, "Ursula K. Le Guin")
    g1.add((URIRef(agent), BF.note, RdfLiteral("author")))
    save_graph(session_maker, g1, primary_class=BF.Work)
    with session_maker() as session:
        before = _stored_other(session, agent)

    g2, _ = _work_citing_agent(agent, "Le Guin")
    save_graph(session_maker, g2, primary_class=BF.Work)

    with session_maker() as session:
        assert _stored_other(session, agent) == before


def _write_other_primary_updated(session_maker: sessionmaker) -> None:
    agent = bc_uri("other_resources")
    g1, work = _work_citing_agent(agent, "Old Name")
    save_graph(session_maker, g1, primary_class=BF.Work)

    g2 = Graph()
    g2.add((URIRef(work), RDF.type, BF.Work))
    g2.add((URIRef(work), BF.contribution, URIRef(agent)))
    _describe(g2, agent, BF.Agent, "New Name")
    save_graph(session_maker, g2, primary_class=OtherResource)

    with session_maker() as session:
        stored = _stored_other(session, agent)
        assert "New Name" in stored
        assert "Old Name" not in stored


def _write_clears_stub_marker(session_maker: sessionmaker) -> None:
    described, stub = ext_uri("works"), ext_uri("works")
    out = save_graph(session_maker, _record(described, "Described", stub=stub))
    uri = minted(out, stub)
    with session_maker() as session:
        stubbed = row(session, Work, uri)
        assert str(STUB_STATUS) in statuses(stubbed.data)
        edited = dict(stubbed.data)

    edited["title"] = {"@type": "Title", "mainTitle": "Now Described By Hand"}
    save_graph(session_maker, load_jsonld(edited), primary_class=BF.Work)

    with session_maker() as session:
        work = row(session, Work, uri)
        assert "Now Described By Hand" in json.dumps(work.data)
        assert str(STUB_STATUS) not in statuses(work.data)


# Each case's last save, with no database: the graph it brings, the mode it
# saves under, and the row it meets.


def _check_blank_record_inline() -> Answer:
    g = _instance_of_blank_work(bc_uri("instances"))
    blank_work = next(g.objects(predicate=BF.instanceOf))
    return resource_answer(g, INGEST, blank_work, Kind.WORK)


def _check_described(stored: dict[str, Any] | None, bluecore: bool = False) -> Answer:
    source = bc_uri("works") if bluecore else ext_uri("works")
    return resource_answer(
        _record(source, "Described"), INGEST, source, Kind.WORK, stored
    )


def _check_stub(stored: dict[str, Any] | None) -> Answer:
    stub = ext_uri("works")
    g = _record(ext_uri("works"), "Points At A Stub", stub=stub)
    return resource_answer(g, INGEST, stub, Kind.WORK, stored)


def _check_no_admin_metadata() -> Answer:
    first = ext_uri("works")
    g = _works_without_admin_metadata(first, ext_uri("works"))
    return resource_answer(g, INGEST, first, Kind.WORK)


def _check_ingest_other(update_others: bool, stored: dict[str, Any] | None) -> Answer:
    agent = f"http://id.loc.gov/rwo/agents/{uuid.uuid4()}"
    g, _ = _work_citing_agent(agent, "Agent")
    mode = Ingest(update_others=update_others)
    return resource_answer(g, mode, agent, Kind.OTHER, stored)


def _check_root_blank() -> Answer:
    g = _new_work_accompanied_by(ext_uri("works"))
    root = next(g.subjects(BF.accompaniedBy))
    return resource_answer(g, Write(Kind.WORK), root, Kind.WORK)


def _check_write_uri(stored: dict[str, Any] | None) -> Answer:
    work = bc_uri("works")
    g = Graph()
    g.add((URIRef(work), RDF.type, BF.Work))
    return resource_answer(g, Write(Kind.WORK), work, Kind.WORK, stored)


def _check_nested_blank_inline() -> Answer:
    g = _new_work_with_blank_work()
    nested = next(g.objects(predicate=BF.accompaniedBy))
    return resource_answer(g, Write(Kind.WORK), nested, Kind.WORK)


def _check_reference_new() -> Answer:
    inst = bc_uri("instances")
    g = _work_with_labelled_instance(bc_uri("works"), inst)
    return resource_answer(g, Write(Kind.WORK), inst, Kind.INSTANCE)


def _check_reference_held() -> Answer:
    work = bc_uri("works")
    g = _instance_of_labelled_work(bc_uri("instances"), work)
    return resource_answer(g, Write(Kind.INSTANCE), work, Kind.WORK, FULL)


def _check_write_other(kind: Kind) -> Answer:
    agent = bc_uri("other_resources")
    g, _ = _work_citing_agent(agent, "Agent")
    return resource_answer(g, Write(kind), agent, Kind.OTHER, ANY)


def _check_write_stub() -> Answer:
    work = bc_uri("works")
    g = _record(work, "Now Described By Hand")
    return resource_answer(g, Write(Kind.WORK), work, Kind.WORK, STUB)


def _case(
    case_id: str,
    row_id: str,
    situation: str,
    outcome: str,
    sentence: str,
    run: Callable[[sessionmaker], None],
    *,
    answer: Answer,
    check: Callable[[], Answer],
    decision: Decision = "inherited",
    follow_up: str | None = REVIEW_BEFORE_RELOAD,
) -> PolicyCase:
    return PolicyCase(
        id=case_id,
        row=row_id,
        situation=situation,
        outcome=outcome,
        sentence=sentence,
        decision=decision,
        follow_up=follow_up,
        run=run,
        answer=answer,
        check=check,
    )


CASES.extend(
    [
        _case(
            "ingest-blank-record-stays-inline",
            "R1",
            "A bulk load brings a Work, Instance or Hub with no URI",
            "No record is made for it; its description stays inside its parent's data",
            "A description with no identifier of its own, such as a Work "
            "described inside an Instance, is not saved as a record. It is "
            "kept as part of the record that mentions it.",
            _ingest_blank_record_stays_inline,
            answer=(Kind.WORK, Role.INLINE, None),
            check=_check_blank_record_inline,
        ),
        _case(
            "ingest-described-new-inserted",
            "R2",
            "A bulk load brings a described resource we don't hold",
            "It is inserted under a new Bluecore URI, with the normal status and a note of where it came from",
            "A new record from LC is added to Blue Core under a Blue Core "
            "identifier, and remembers the LC identifier it came from.",
            _ingest_described_new_inserted,
            answer=(Kind.WORK, Role.PRIMARY, Action.INSERT),
            check=lambda: _check_described(None),
        ),
        _case(
            "ingest-described-ours-updated",
            "R2",
            "A bulk load brings a described resource that already has a Bluecore URI",
            "The stored description is replaced by the incoming one",
            "When a description that already carries its Blue Core "
            "identifier is loaded, it replaces what we hold.",
            _ingest_described_ours_updated,
            answer=(Kind.WORK, Role.PRIMARY, Action.UPDATE),
            check=lambda: _check_described(FULL, bluecore=True),
        ),
        _case(
            "ingest-described-stub-replaced",
            "R2",
            "A bulk load brings a full description of a resource we only hold as a stub",
            "The stub's description is replaced and the incomplete marker is removed",
            "A placeholder we kept because another record pointed at it is "
            "replaced as soon as a record that really describes it arrives.",
            _ingest_described_stub_replaced,
            answer=(Kind.WORK, Role.PRIMARY, Action.UPDATE),
            check=lambda: _check_described(STUB),
        ),
        _case(
            "ingest-described-reingest-kept",
            "R2",
            "A bulk load brings a described resource we hold in full, and a cataloger has edited it",
            "The stored description is kept; its Other Resource links are rebuilt from the incoming record",
            "LC sends a record again after you've edited it: your "
            "description is kept, but its subject and agent links are "
            "replaced with LC's.",
            _ingest_described_reingest_kept,
            answer=(Kind.WORK, Role.PRIMARY, Action.KEEP),
            check=lambda: _check_described(FULL),
            decision="inherited",
            follow_up="LC overlay policy",
        ),
        _case(
            "ingest-stub-new-inserted-incomplete",
            "R3",
            "A record points at a resource it doesn't describe, and we don't hold it",
            "A stub is inserted, marked incomplete rather than with the normal status",
            "A resource that a record only mentions is saved as a "
            "placeholder and marked incomplete, so it can be told apart "
            "from a real description.",
            _ingest_stub_new_inserted_incomplete,
            answer=(Kind.WORK, Role.STUB, Action.INSERT),
            check=lambda: _check_stub(None),
        ),
        _case(
            "ingest-stub-held-kept",
            "R3",
            "A record points at a resource it doesn't describe, and we hold a full description",
            "The stored description is left exactly as it was",
            "A record that only mentions a resource never replaces a full "
            "description we already hold.",
            _ingest_stub_held_kept,
            answer=(Kind.WORK, Role.STUB, Action.KEEP),
            check=lambda: _check_stub(FULL),
        ),
        _case(
            "ingest-stub-held-keeps-links",
            "R3",
            "A record points at a resource it doesn't describe, and we hold it with links",
            "The resource keeps the links its own description gave it",
            "A record that only mentions a resource does not remove the "
            "agents and subjects already linked to it.",
            _ingest_stub_held_keeps_links,
            answer=(Kind.WORK, Role.STUB, Action.KEEP),
            check=lambda: _check_stub(FULL),
        ),
        _case(
            "ingest-stub-new-gets-links",
            "R3",
            "A record's stub cites an Other Resource we already hold, by URI",
            "The new stub is linked to that Other Resource",
            "A placeholder created from a mention is still linked to the "
            "agents and subjects it cites, when we already hold them.",
            _ingest_stub_new_gets_links,
            answer=(Kind.WORK, Role.STUB, Action.INSERT),
            check=lambda: _check_stub(None),
        ),
        _case(
            "ingest-no-admin-metadata-all-primary",
            "R4",
            "A bulk-loaded record has URI Works and no adminMetadata anywhere",
            "Every Work is inserted as a full description, none marked incomplete",
            "When a record gives no administrative information at all, we "
            "can't tell which Works are placeholders, so all of them are "
            "treated as real descriptions.",
            _ingest_no_admin_metadata_all_primary,
            answer=(Kind.WORK, Role.PRIMARY, Action.INSERT),
            check=_check_no_admin_metadata,
        ),
        _case(
            "ingest-other-new-inserted",
            "R5",
            "A bulk load cites a described Other Resource we don't hold",
            "The Other Resource is inserted with the payload's description",
            "A new agent, subject or other authority described in a loaded "
            "record is added with the label the record gives it.",
            _ingest_other_new_inserted,
            answer=(Kind.OTHER, Role.REFERENCE, Action.INSERT),
            check=lambda: _check_ingest_other(False, None),
        ),
        _case(
            "ingest-other-held-updated",
            "R5",
            "A bulk load describes an Other Resource we hold, and updating Other Resources is on",
            "The stored description is replaced by the new one",
            "By default, a loaded record's description of an agent or "
            "subject replaces the one we hold.",
            _ingest_other_held_updated,
            answer=(Kind.OTHER, Role.PRIMARY, Action.UPDATE),
            check=lambda: _check_ingest_other(True, ANY),
        ),
        _case(
            "ingest-other-held-kept",
            "R5",
            "A bulk load describes an Other Resource we hold, and updating Other Resources is off",
            "The stored description is unchanged",
            "When the loader is told not to update agents and subjects, "
            "the ones we hold keep their descriptions.",
            _ingest_other_held_kept,
            answer=(Kind.OTHER, Role.REFERENCE, Action.KEEP),
            check=lambda: _check_ingest_other(False, ANY),
        ),
        _case(
            "write-root-blank-inserted",
            "R6",
            "A save through the API has no URI on the resource being created",
            "A new record is created for it and its URI is reported as created",
            "When you create a new resource in the editor, it is given a "
            "new identifier and saved.",
            _write_root_blank_inserted,
            answer=(Kind.WORK, Role.PRIMARY, Action.INSERT),
            check=_check_root_blank,
        ),
        _case(
            "write-uri-new-inserted",
            "R7",
            "A save through the API names a resource of the saved kind by a new Bluecore URI",
            "A record is created at that URI",
            "Saving a Work under a Blue Core identifier we don't hold yet "
            "creates it under that identifier.",
            _write_uri_new_inserted,
            answer=(Kind.WORK, Role.PRIMARY, Action.INSERT),
            check=lambda: _check_write_uri(None),
        ),
        _case(
            "write-uri-held-updated",
            "R7",
            "A save through the API names a resource of the saved kind that we hold",
            "The stored description is replaced by the saved one",
            "Saving a resource from the editor replaces what we hold.",
            _write_uri_held_updated,
            answer=(Kind.WORK, Role.PRIMARY, Action.UPDATE),
            check=lambda: _check_write_uri(FULL),
        ),
        _case(
            "write-other-blank-inline",
            "R8",
            "A save through the API has a nested Work with no URI",
            "No record is made for it; it stays inside its parent's data",
            "A description inside another one, with no identifier, stays "
            "part of the resource that holds it.",
            _write_other_blank_inline,
            answer=(Kind.WORK, Role.INLINE, None),
            check=_check_nested_blank_inline,
        ),
        _case(
            "write-reference-new-inserted",
            "R9",
            "A save through the API refers to a resource of another kind that we don't hold",
            "A record is created for it from what the payload says about it",
            "If you link to a resource we don't have yet, it is created "
            "with whatever the payload says about it.",
            _write_reference_new_inserted,
            answer=(Kind.INSTANCE, Role.REFERENCE, Action.INSERT),
            check=_check_reference_new,
        ),
        _case(
            "write-reference-held-kept",
            "R9",
            "A save through the API refers to a resource of another kind that we hold",
            "The stored description is unchanged",
            "Linking to an existing resource from another one never "
            "changes the linked resource's description.",
            _write_reference_held_kept,
            answer=(Kind.WORK, Role.REFERENCE, Action.KEEP),
            check=_check_reference_held,
        ),
        _case(
            "write-reference-keeps-links",
            "R9",
            "A save through the API refers to a held resource that has Other Resource links",
            "The referenced resource's links are unchanged",
            "Saving an Instance does not remove the agents and subjects "
            "linked to its Work.",
            _write_reference_keeps_links,
            answer=(Kind.WORK, Role.REFERENCE, Action.KEEP),
            check=_check_reference_held,
        ),
        _case(
            "write-other-reference-kept",
            "R10",
            "A save through the API cites an Other Resource that we hold",
            "The stored description is unchanged",
            "Citing an agent or subject from a Work never shrinks the "
            "fuller description we already hold.",
            _write_other_reference_kept,
            answer=(Kind.OTHER, Role.REFERENCE, Action.KEEP),
            check=lambda: _check_write_other(Kind.WORK),
        ),
        _case(
            "write-other-primary-updated",
            "R10",
            "A save through the API writes an Other Resource as the thing being saved",
            "The stored description is replaced by the saved one",
            "Saving an agent or subject on its own replaces what we hold for it.",
            _write_other_primary_updated,
            answer=(Kind.OTHER, Role.PRIMARY, Action.UPDATE),
            check=lambda: _check_write_other(Kind.OTHER),
        ),
        _case(
            "write-clears-stub-marker",
            "R11",
            "A save through the API writes a resource held as an incomplete stub",
            "The incomplete marker is removed and the normal status is set",
            "Once you save a placeholder from the editor, it counts as a "
            "real description and is no longer marked incomplete.",
            _write_clears_stub_marker,
            answer=(Kind.WORK, Role.PRIMARY, Action.UPDATE),
            check=_check_write_stub,
        ),
    ]
)


# --- which cited URIs become links (L1-L8, X1, X2) ----------------------------


def _heading() -> str:
    return f"http://id.loc.gov/authorities/subjects/sh{uuid.uuid4().hex}"


def _citing(uri: str, class_: URIRef, heading: str, describes: bool) -> Graph:
    """A resource citing a heading, either describing it or by URI alone."""
    g = Graph()
    g.add((URIRef(uri), RDF.type, class_))
    g.add((URIRef(uri), BF.subject, URIRef(heading)))
    if describes:
        _describe(g, heading, BF.Topic, "Physicians")
    return g


def _seed_held(session_maker: sessionmaker, uri: str) -> None:
    """Hold a URI as an Other Resource, the way an earlier save would have."""
    seed = bc_uri("works")
    g = Graph()
    g.add((URIRef(seed), RDF.type, BF.Work))
    g.add((URIRef(seed), BF.subject, URIRef(uri)))
    g.add((URIRef(uri), RDFS.label, RdfLiteral("held")))
    save_graph(session_maker, g, primary_class=BF.Work)
    with session_maker() as session:
        assert row(session, OtherResource, uri) is not None


def _link_described_in_payload(session_maker: sessionmaker) -> None:
    heading, work = _heading(), bc_uri("works")
    save_graph(
        session_maker, _citing(work, BF.Work, heading, True), primary_class=BF.Work
    )

    with session_maker() as session:
        assert row(session, OtherResource, heading) is not None
        assert linked(session, row(session, Work, work)) == [heading]


def _link_held_by_uri(session_maker: sessionmaker) -> None:
    heading, first, second = _heading(), bc_uri("works"), bc_uri("works")
    save_graph(
        session_maker, _citing(first, BF.Work, heading, True), primary_class=BF.Work
    )
    with session_maker() as session:
        assert row(session, OtherResource, heading) is not None

    save_graph(
        session_maker, _citing(second, BF.Work, heading, False), primary_class=BF.Work
    )

    with session_maker() as session:
        assert linked(session, row(session, Work, second)) == [heading]


def _link_unknown_uri_bare_reference(session_maker: sessionmaker) -> None:
    unknown, work = _heading(), bc_uri("works")
    save_graph(
        session_maker, _citing(work, BF.Work, unknown, False), primary_class=BF.Work
    )

    with session_maker() as session:
        assert row(session, OtherResource, unknown) is None
        assert row(session, Work, work) is not None
        assert linked(session, row(session, Work, work)) == []


def _link_vocabulary_term(session_maker: sessionmaker) -> None:
    term = str(BF.Text)
    with session_maker() as session:
        if row(session, OtherResource, term) is None:
            session.add(OtherResource(uri=term, data={"@id": term}))
            session.commit()
        assert row(session, OtherResource, term) is not None

    work = bc_uri("works")
    g = Graph()
    g.add((URIRef(work), RDF.type, BF.Work))
    g.add((URIRef(work), BF.content, BF.Text))
    save_graph(session_maker, g, primary_class=BF.Work)

    with session_maker() as session:
        assert row(session, Work, work) is not None
        assert linked(session, row(session, Work, work)) == []


def _link_record_typed_in_payload(session_maker: sessionmaker) -> None:
    cited = f"http://example.org/works/{uuid.uuid4().hex}"
    _seed_held(session_maker, cited)

    work = bc_uri("works")
    g = Graph()
    g.add((URIRef(work), RDF.type, BF.Work))
    g.add((URIRef(work), BF.subject, URIRef(cited)))
    g.add((URIRef(cited), RDF.type, BF.Work))
    save_graph(session_maker, g, primary_class=BF.Work)

    with session_maker() as session:
        assert row(session, Work, work) is not None
        assert linked(session, row(session, Work, work)) == []


def _excluded_predicate(predicate: URIRef, subject_class: URIRef) -> Any:
    """A case: a held untyped URI cited through the predicate is not linked."""

    def run(session_maker: sessionmaker) -> None:
        held = f"http://example.org/records/{uuid.uuid4().hex}"
        _seed_held(session_maker, held)

        subject = bc_uri("works" if subject_class == BF.Work else "instances")
        g = Graph()
        g.add((URIRef(subject), RDF.type, subject_class))
        g.add((URIRef(subject), predicate, URIRef(held)))
        save_graph(session_maker, g, primary_class=subject_class)

        with session_maker() as session:
            model = Work if subject_class == BF.Work else Instance
            assert row(session, model, subject) is not None
            assert linked(session, row(session, model, subject)) == []

    return run


def _inferred_record_predicate(
    predicate: URIRef, subject_class: URIRef, inferred_class: URIRef
) -> Any:
    """
    A case for the two predicates whose object the save types as a record: the
    held URI is replaced by a newly minted record of that kind, and the held
    Other Resource is neither linked nor changed.
    """

    def run(session_maker: sessionmaker) -> None:
        held = f"http://example.org/records/{uuid.uuid4().hex}"
        _seed_held(session_maker, held)
        with session_maker() as session:
            before = _stored_other(session, held)

        subject = bc_uri("works" if subject_class == BF.Work else "instances")
        g = Graph()
        g.add((URIRef(subject), RDF.type, subject_class))
        g.add((URIRef(subject), predicate, URIRef(held)))
        out = save_graph(session_maker, g, primary_class=subject_class)
        record = minted(out, held)

        with session_maker() as session:
            model = Work if subject_class == BF.Work else Instance
            resource = row(session, model, subject)
            assert held not in json.dumps(resource.data)
            assert record in json.dumps(resource.data)
            assert row(session, Work if inferred_class == BF.Work else Instance, record)
            assert linked(session, resource) == []
            assert _stored_other(session, held) == before

    return run


def _link_lc_resources_prefix(session_maker: sessionmaker) -> None:
    held = ext_uri("hubs")
    _seed_held(session_maker, held)

    work = bc_uri("works")
    g = Graph()
    g.add((URIRef(work), RDF.type, BF.Work))
    g.add((URIRef(work), BF.subject, URIRef(held)))
    save_graph(session_maker, g, primary_class=BF.Work)

    with session_maker() as session:
        assert row(session, Work, work) is not None
        assert linked(session, row(session, Work, work)) == []


def _admin_metadata_work(status: str, describe: bool) -> tuple[Graph, str]:
    work = bc_uri("works")
    g = Graph()
    admin_metadata = BNode()
    g.add((URIRef(work), RDF.type, BF.Work))
    g.add((URIRef(work), BF.adminMetadata, admin_metadata))
    g.add((admin_metadata, RDF.type, BF.AdminMetadata))
    g.add((admin_metadata, BF.status, URIRef(status)))
    if describe:
        g.add((URIRef(status), RDFS.label, RdfLiteral("described term")))
    return g, work


def _link_admin_metadata_only_held(session_maker: sessionmaker) -> None:
    status = f"http://id.loc.gov/vocabulary/mstatus/{uuid.uuid4().hex}"
    _seed_held(session_maker, status)

    g, work = _admin_metadata_work(status, describe=False)
    save_graph(session_maker, g, primary_class=BF.Work)

    with session_maker() as session:
        assert row(session, Work, work) is not None
        assert linked(session, row(session, Work, work)) == []


def _link_admin_metadata_described(session_maker: sessionmaker) -> None:
    status = f"http://id.loc.gov/vocabulary/mstatus/{uuid.uuid4().hex}"
    _seed_held(session_maker, status)

    g, work = _admin_metadata_work(status, describe=True)
    save_graph(session_maker, g, primary_class=BF.Work)

    with session_maker() as session:
        assert linked(session, row(session, Work, work)) == [status]


def _link_lc_item(session_maker: sessionmaker) -> None:
    item = ext_uri("items")
    instance = bc_uri("instances")
    g = Graph()
    g.add((URIRef(instance), RDF.type, BF.Instance))
    g.add((URIRef(instance), BF.hasItem, URIRef(item)))
    _describe(g, item, BF.Item, "An LC Item")
    save_graph(session_maker, g, primary_class=BF.Instance)

    with session_maker() as session:
        assert "An LC Item" in _stored_other(session, item)
        assert linked(session, row(session, Instance, instance)) == []


def _hub_link_described(session_maker: sessionmaker) -> None:
    work, hub = bc_uri("works"), bc_uri("hubs")
    g = Graph()
    g.add((URIRef(work), RDF.type, BF.Work))
    g.add((URIRef(work), BF.expressionOf, URIRef(hub)))
    g.add((URIRef(hub), RDF.type, BF.Hub))
    save_graph(session_maker, g)

    with session_maker() as session:
        stored_hub = row(session, Hub, hub)
        assert stored_hub is not None
        assert row(session, Work, work).hub_id == stored_hub.id


def _hub_link_not_described(session_maker: sessionmaker) -> None:
    hub = bc_uri("hubs")
    held = Graph()
    held.add((URIRef(hub), RDF.type, BF.Hub))
    save_graph(session_maker, held, primary_class=BF.Hub)
    with session_maker() as session:
        assert row(session, Hub, hub) is not None

    work = bc_uri("works")
    g = Graph()
    g.add((URIRef(work), RDF.type, BF.Work))
    g.add((URIRef(work), BF.expressionOf, URIRef(hub)))
    # The Hub is cited but not typed, so the save takes it for an Other Resource
    # and tries to insert one at the Hub's URI, which the Hub row already holds.
    with pytest.raises(IntegrityError) as excinfo:
        save_graph(session_maker, g)
    assert isinstance(excinfo.value.orig, psycopg.errors.UniqueViolation)
    assert excinfo.value.orig.diag.constraint_name == "resource_base_uri_key"

    with session_maker() as session:
        assert row(session, Work, work) is None
        assert row(session, OtherResource, hub) is None
        assert row(session, Hub, hub) is not None


def _hub_link_hub_not_stored(session_maker: sessionmaker) -> None:
    hub, work = bc_uri("hubs"), bc_uri("works")
    with session_maker() as session:
        assert row(session, Hub, hub) is None
        assert row(session, OtherResource, hub) is None

    g = Graph()
    g.add((URIRef(work), RDF.type, BF.Work))
    g.add((URIRef(work), BF.expressionOf, URIRef(hub)))
    save_graph(session_maker, g, primary_class=BF.Work)

    with session_maker() as session:
        assert row(session, Work, work).hub_id is None
        assert row(session, Hub, hub) is None
        assert row(session, OtherResource, hub) is not None


def _lc_work(source: str, subject: str) -> Graph:
    """An LC record the way an ingest sees one: a Work citing a described subject."""
    g = _record(source, "Cited An LC Subject")
    g.add((URIRef(source), BF.subject, URIRef(subject)))
    _describe(g, subject, BF.Topic, "LC Subject")
    return g


def _reingest_links_follow_incoming(session_maker: sessionmaker) -> None:
    source = ext_uri("works")
    first, second = _heading(), _heading()
    # the cataloger's subject is one we already hold
    _seed_held(session_maker, second)

    out = save_graph(session_maker, _lc_work(source, first))
    uri = minted(out, source)
    with session_maker() as session:
        assert linked(session, row(session, Work, uri)) == [first]
        edited = dict(row(session, Work, uri).data)

    edited["subject"] = [{"@id": second}]
    save_graph(session_maker, load_jsonld(edited), primary_class=BF.Work)
    with session_maker() as session:
        assert second in _work_data(session, uri)
        assert first not in _work_data(session, uri)
        assert linked(session, row(session, Work, uri)) == [second]

    save_graph(session_maker, _lc_work(source, first))

    with session_maker() as session:
        data = _work_data(session, uri)
        assert second in data
        assert first not in data
        assert linked(session, row(session, Work, uri)) == [first]


# What each link case's cited uri is, with no database.


def _check_heading(describes: bool) -> Answer:
    heading, work = _heading(), bc_uri("works")
    g = _citing(work, BF.Work, heading, describes)
    return citation_answer(g, work, BF.subject, heading)


def _check_vocabulary_term() -> Answer:
    work = bc_uri("works")
    g = Graph()
    g.add((URIRef(work), RDF.type, BF.Work))
    g.add((URIRef(work), BF.content, BF.Text))
    return citation_answer(g, work, BF.content, BF.Text)


def _check_record_typed_in_payload() -> Answer:
    cited, work = f"http://example.org/works/{uuid.uuid4().hex}", bc_uri("works")
    g = Graph()
    g.add((URIRef(work), RDF.type, BF.Work))
    g.add((URIRef(work), BF.subject, URIRef(cited)))
    g.add((URIRef(cited), RDF.type, BF.Work))
    return citation_answer(g, work, BF.subject, cited)


def _check_lc_resources_prefix() -> Answer:
    held, work = ext_uri("hubs"), bc_uri("works")
    g = Graph()
    g.add((URIRef(work), RDF.type, BF.Work))
    g.add((URIRef(work), BF.subject, URIRef(held)))
    return citation_answer(g, work, BF.subject, held)


def _check_admin_metadata(describe: bool) -> Answer:
    status = f"http://id.loc.gov/vocabulary/mstatus/{uuid.uuid4().hex}"
    g, work = _admin_metadata_work(status, describe)
    admin_metadata = next(g.objects(URIRef(work), BF.adminMetadata))
    return citation_answer(g, admin_metadata, BF.status, status)


def _check_lc_item() -> Answer:
    item, instance = ext_uri("items"), bc_uri("instances")
    g = Graph()
    g.add((URIRef(instance), RDF.type, BF.Instance))
    g.add((URIRef(instance), BF.hasItem, URIRef(item)))
    _describe(g, item, BF.Item, "An LC Item")
    return citation_answer(g, instance, BF.hasItem, item)


def _check_hub(typed: bool) -> Answer:
    work, hub = bc_uri("works"), bc_uri("hubs")
    g = Graph()
    g.add((URIRef(work), RDF.type, BF.Work))
    g.add((URIRef(work), BF.expressionOf, URIRef(hub)))
    if typed:
        g.add((URIRef(hub), RDF.type, BF.Hub))
    return citation_answer(g, work, BF.expressionOf, hub)


def _check_reingest() -> Answer:
    source = ext_uri("works")
    g = _lc_work(source, _heading())
    return resource_answer(g, INGEST, source, Kind.WORK, FULL)


def _check_excluded_predicate(predicate: URIRef, subject_class: URIRef) -> Any:
    """What a held, untyped uri cited through predicate is, with no database."""

    def check() -> Answer:
        held = f"http://example.org/records/{uuid.uuid4().hex}"
        subject = bc_uri("works" if subject_class == BF.Work else "instances")
        g = Graph()
        g.add((URIRef(subject), RDF.type, subject_class))
        g.add((URIRef(subject), predicate, URIRef(held)))
        return citation_answer(g, subject, predicate, held)

    return check


CASES.extend(
    [
        _case(
            "link-described-in-payload",
            "L1",
            "A Work cites an authority or agent and the payload describes it",
            "The Work is linked to that Other Resource",
            "A subject or agent you pick in the editor is linked to the record.",
            _link_described_in_payload,
            answer=Citation.OTHER,
            check=lambda: _check_heading(True),
        ),
        _case(
            "link-held-by-uri",
            "L1",
            "A Work cites an authority by URI alone, and we hold it from an earlier save",
            "The Work is linked to the held Other Resource",
            "Citing a subject or agent we already hold links it, even when "
            "the editor sends only its identifier.",
            _link_held_by_uri,
            answer=Citation.OTHER,
            check=lambda: _check_heading(False),
        ),
        _case(
            "link-unknown-uri-bare-reference",
            "L2",
            "A Work cites a URI by identifier alone, and we hold nothing about it",
            "No Other Resource is made and no link is saved; the URI stays in the Work's data",
            "A subject or agent we know nothing about is kept as a plain "
            "reference, with no label and no link.",
            _link_unknown_uri_bare_reference,
            answer=Citation.OTHER,
            check=lambda: _check_heading(False),
            decision="inherited",
            follow_up="#169 stub rows",
        ),
        _case(
            "link-vocabulary-term",
            "L3",
            "A Work cites a BIBFRAME vocabulary term, which is also held as an Other Resource",
            "No link is saved",
            "Terms from the BIBFRAME vocabulary, such as a content type, "
            "are never linked as Other Resources.",
            _link_vocabulary_term,
            answer=Citation.VOCABULARY,
            check=_check_vocabulary_term,
        ),
        _case(
            "link-record-typed-in-payload",
            "L4",
            "A Work cites a URI the payload describes as a Work, and we hold it as an Other Resource",
            "No link is saved",
            "A resource that the record says is a Work is a record of its "
            "own, never an agent or subject link.",
            _link_record_typed_in_payload,
            answer=Citation.RECORD,
            check=_check_record_typed_in_payload,
        ),
        _case(
            "link-lc-resources-prefix",
            "L6",
            "A Work cites, as a subject, a held Other Resource under id.loc.gov/resources/",
            "No link is saved",
            "LC's Works, Instances, Hubs and Items are never linked as "
            "agents or subjects, however they are cited.",
            _link_lc_resources_prefix,
            answer=Citation.EXCLUDED,
            check=_check_lc_resources_prefix,
        ),
        _case(
            "link-admin-metadata-only-held",
            "L7",
            "A Work cites a held vocabulary term only inside its adminMetadata",
            "No link is saved",
            "The status and other terms in a record's administrative "
            "information are not linked, or every record would link to them.",
            _link_admin_metadata_only_held,
            answer=Citation.ADMIN_ONLY,
            check=lambda: _check_admin_metadata(False),
        ),
        _case(
            "link-admin-metadata-described",
            "L7",
            "A Work cites a vocabulary term inside its adminMetadata, and the payload describes the term",
            "The Work is linked to the term",
            "An administrative term is linked only when the record "
            "describes it itself.",
            _link_admin_metadata_described,
            answer=Citation.ADMIN_ONLY,
            check=lambda: _check_admin_metadata(True),
        ),
        _case(
            "link-lc-item",
            "L8",
            "An Instance cites an LC Item that the payload describes",
            "The Item is stored as an Other Resource but the Instance is not linked to it",
            "An LC Item is kept, but is not linked to its Instance.",
            _link_lc_item,
            answer=Citation.ITEM,
            check=_check_lc_item,
            decision="inherited",
            follow_up="Items, decided in Phase 3",
        ),
        _case(
            "hub-link-described",
            "X1",
            "A Work is an expressionOf a Hub that the payload describes",
            "The Work is linked to the Hub",
            "A Work is attached to its Hub when the record describes that Hub.",
            _hub_link_described,
            answer=Citation.RECORD,
            check=lambda: _check_hub(typed=True),
        ),
        _case(
            "hub-link-not-described",
            "X1",
            "A Work is saved as an expressionOf a Hub we hold, but the payload does not describe or type it",
            "The save fails with a duplicate-URI error and nothing is written; the Work is not linked to the Hub",
            "Pointing a Work at a Hub we already have fails to save unless "
            "the record also says that it is a Hub.",
            _hub_link_not_described,
            answer=Citation.EXCLUDED,
            check=lambda: _check_hub(typed=False),
            decision="inherited",
            follow_up="Hub labels and links",
        ),
        _case(
            "hub-link-hub-not-stored",
            "X1",
            "A Work is saved as an expressionOf a Hub we don't hold, and the payload does not describe or type it",
            "The Work is not linked to a Hub; an Other Resource is created at the Hub's URI",
            "Pointing a Work at a Hub we don't have, without describing it, "
            "saves the Hub's identifier as if it were an agent or subject, "
            "and doesn't attach the Work to it.",
            _hub_link_hub_not_stored,
            answer=Citation.EXCLUDED,
            check=lambda: _check_hub(typed=False),
            decision="inherited",
            follow_up="Hub labels and links",
        ),
        _case(
            "reingest-links-follow-incoming",
            "X2",
            "A bulk load brings again an LC record a cataloger has edited, and its subject differs",
            "The edited description is kept, and its Other Resource links are rebuilt from the incoming record",
            "LC sends a record again after you've changed its subject: your "
            "description keeps your subject, but the subject link goes back "
            "to LC's.",
            _reingest_links_follow_incoming,
            answer=(Kind.WORK, Role.PRIMARY, Action.KEEP),
            check=_check_reingest,
            decision="inherited",
            follow_up="LC overlay policy",
        ),
    ]
)

L5_SENTENCES = {
    "instanceOf": "Naming an existing agent or subject as the Work an Instance "
    "belongs to creates a new Work record for it, rather than linking the one "
    "we hold.",
    "hasInstance": "Naming an existing agent or subject as an Instance of a Work "
    "creates a new Instance record for it, rather than linking the one we hold.",
    "expressionOf": "A resource cited as the Hub a Work expresses is never linked "
    "as an agent or subject, even if we hold it that way.",
    "hasExpression": "A Work that names a resource as one of its expressions "
    "never links it as an agent or subject, even if we hold it that way.",
    "relatedTo": "A resource cited as a related Work is never linked as an agent "
    "or subject, even if we hold it that way.",
    "hasItem": "A resource cited as an Item an Instance holds is never linked as "
    "an agent or subject, even if we hold it that way.",
    "itemOf": "An Instance that names a resource as the Instance it is an item "
    "of never links it as an agent or subject, even if we hold it that way.",
    "bf-associatedResource": "A resource cited as an associated resource "
    "(BIBFRAME's property) is never linked as an agent or subject, even if we "
    "hold it that way.",
    "bflc-associatedResource": "A resource cited as an associated resource "
    "(the LC extension's property) is never linked as an agent or subject, "
    "even if we hold it that way.",
}

INFERRED = {"instanceOf": BF.Work, "hasInstance": BF.Instance}

for _predicate, _name, _class in (
    (BF.instanceOf, "instanceOf", BF.Instance),
    (BF.hasInstance, "hasInstance", BF.Work),
    (BF.expressionOf, "expressionOf", BF.Work),
    (BF.hasExpression, "hasExpression", BF.Work),
    (BF.relatedTo, "relatedTo", BF.Work),
    (BF.hasItem, "hasItem", BF.Instance),
    (BF.itemOf, "itemOf", BF.Instance),
    (BF.associatedResource, "bf-associatedResource", BF.Work),
    (BFLC.associatedResource, "bflc-associatedResource", BF.Work),
):
    if _name in INFERRED:
        _situation = (
            f"A resource cites a held, untyped URI through {_name}, which the "
            "save takes to be a record"
        )
        _outcome = (
            "The URI is replaced by a newly minted record of the inferred kind; "
            "the held Other Resource is not linked and is unchanged"
        )
        run = _inferred_record_predicate(_predicate, _class, INFERRED[_name])
    else:
        _situation = f"A resource cites a held, untyped URI through {_name}"
        _outcome = "No link is saved"
        run = _excluded_predicate(_predicate, _class)
    CASES.append(
        _case(
            f"link-excluded-{_name}",
            "L5",
            _situation,
            _outcome,
            L5_SENTENCES[_name],
            run,
            # the two the save types as records are minted, the rest excluded
            answer=Citation.RECORD if _name in INFERRED else Citation.EXCLUDED,
            check=_check_excluded_predicate(_predicate, _class),
        )
    )


INTRODUCTION = (
    "This is how `save_graph` treats each resource and each cited URI today. "
    "It is generated from `tests/policy_cases.py` by "
    "`uv run python tests/policy_cases.py > docs/save-policy.md`; don't edit it "
    "by hand. A case marked *inherited* is what the code already did; nobody has "
    "chosen it yet."
)


def _sentence(text: str) -> str:
    """text on one line, ending with a full stop."""
    text = " ".join(text.split())
    return text if text.endswith(".") else text + "."


def _in_the_code(answer: Answer) -> str:
    if isinstance(answer, Citation):
        names = [f"`Citation.{answer.name}`"]
    else:
        kind, role, action = answer
        names = [f"`Kind.{kind.name}`", f"`Role.{role.name}`"]
        if action is not None:
            names.append(f"`Action.{action.name}`")
    return "In the code: " + ", ".join(names) + "."


def _paragraphs(case: PolicyCase) -> list[str]:
    today = _sentence(case.outcome)
    decision = f"*{case.decision}*"
    if case.decision == "inherited" and case.follow_up:
        decision += f", {case.follow_up}"
    return [
        f"**{_sentence(case.situation)}** {_sentence(case.sentence)}",
        f"Today: {today[0].lower()}{today[1:]}",
        _sentence(f"Decision: {decision}"),
        _in_the_code(case.answer),
    ]


def render_document(cases: list[PolicyCase]) -> str:
    """
    The cases as a document: a section for each row of `ROWS`, in order, with
    the row's cases under it in the order given.
    """
    blocks = ["# What a save does", INTRODUCTION]
    for row_id, row_text in ROWS.items():
        in_row = [case for case in cases if case.row == row_id]
        if not in_row:
            continue
        blocks.append(f"## {row_id}: {row_text}")
        for case in in_row:
            blocks.extend(_paragraphs(case))
    return "\n\n".join(blocks) + "\n"


if __name__ == "__main__":
    sys.stdout.write(render_document(CASES))
