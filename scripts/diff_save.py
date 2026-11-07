#!/usr/bin/env python3
"""Differential harness for ``save_graph``.

Loads the same records with two versions of the code, takes a canonical
snapshot of each database, and compares them. Later phases of the
BluecoreGraph refactor (#215) must not change what ``save_graph`` writes; this
is how they show it.

Write the shared sample once (it's committed as tests/data/harness-sample.tar.gz).
It holds the first 200 members of each batch, and its bytes are reproducible.
The SHA-256 of the committed file is

    49b95a6e87043b083bf025a83fe909d545c10f5a3d775655276773f07ccd2687

and the command that regenerates it (it needs the 1.4 GB uploads/ corpus, and
prints the SHA-256 it wrote, which should match the line above) is:

    uv run python scripts/diff_save.py sample --uploads uploads \\
        --per-batch 200 --out tests/data/harness-sample.tar.gz

Run each side against its own empty, throwaway Postgres database:

    docker run -d --rm --name diff-a -p 55433:5432 -e POSTGRES_PASSWORD=diff postgres:16
    docker run -d --rm --name diff-b -p 55434:5432 -e POSTGRES_PASSWORD=diff postgres:16
    docker exec diff-a pg_isready   # wait for "accepting connections"

The baseline run puts the 7f0064c code first on PYTHONPATH, and the script
prints which ``bluecore_models`` it imported, so check that line:

    git worktree add ../bluecore-models-7f0064c 7f0064c
    PYTHONPATH=../bluecore-models-7f0064c/src uv run python scripts/diff_save.py run \\
        --database-url postgresql+psycopg://postgres:diff@localhost:55433/postgres \\
        --corpus tests/data/harness-sample.tar.gz --sequences --out harness-out/a.json

Then run the branch against the other database, with no PYTHONPATH, and compare:

    uv run python scripts/diff_save.py run \\
        --database-url postgresql+psycopg://postgres:diff@localhost:55434/postgres \\
        --corpus tests/data/harness-sample.tar.gz --sequences --out harness-out/branch.json
    uv run python scripts/diff_save.py compare harness-out/a.json harness-out/branch.json

To check that the harness is deterministic, run the baseline twice, each time
against a fresh database, and compare with ``compare harness-out/a.json harness-out/b.json --same-code``.

A full run loads 7,000 records twice and takes about 45-50 minutes per side,
whether or not two run in parallel. While working, use the quick check, which
takes the first 25 members of each batch. It needs both sides, a baseline
snapshot and a branch snapshot, and ``compare`` refuses snapshots whose
selections differ, so give both the same ``--per-batch``:

    PYTHONPATH=../bluecore-models-7f0064c/src uv run python scripts/diff_save.py run \\
        --per-batch 25 \\
        --database-url postgresql+psycopg://postgres:diff@localhost:55433/postgres \\
        --corpus tests/data/harness-sample.tar.gz --sequences \\
        --out harness-out/quick-a.json
    uv run python scripts/diff_save.py run --per-batch 25 \\
        --database-url postgresql+psycopg://postgres:diff@localhost:55434/postgres \\
        --corpus tests/data/harness-sample.tar.gz --sequences \\
        --out harness-out/quick.json
    uv run python scripts/diff_save.py compare \\
        harness-out/quick-a.json harness-out/quick.json

(Use a fresh database for each run.) The snapshots are large, about 60 MB each
for a full run, and are never committed: harness-out/ is in .gitignore. Make it
first with ``mkdir harness-out``.

Clean up when you are done:

    docker stop diff-a diff-b
    git worktree remove ../bluecore-models-7f0064c

The script uses only the public API that exists at 7f0064c, so the same file
runs against both.

Bluecore URIs are minted from uuid4, and the saves stamp the time on the
AdminMetadata they generate, so neither is comparable between runs. A snapshot
names each Hub, Work and Instance by the URI it was derived from
(``minted:<uri>``), or by the step that created it (``created:<step>``), and
masks the generated dates.

What the harness can't see:

- JSON-LD array order outside ``@list`` isn't compared.
- ``load`` always passes ``update_other_resources=False``, so the default
  ingest path that re-describes Other Resources isn't exercised; only the
  ``primary_class=OtherResource`` steps write them authoritatively.
- ``created:`` labels replace whole URIs in returned-graph digests (rows keep
  their shape).
- If both sides hit ``DigestError`` with the same message, the returned graph's
  content isn't compared; the run prints the count.
"""

import argparse
import gzip
import hashlib
import io
import json
import logging
import re
import sys
import tarfile
import urllib.parse
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable, Iterator
from pathlib import Path
from typing import Any

from rdflib import RDF, RDFS, BNode, Graph, Literal, URIRef
from rdflib.compare import to_canonical_graph
from sqlalchemy import create_engine, func, inspect, text
from sqlalchemy.orm import Session, sessionmaker

import bluecore_models
import bluecore_models.bluecore_graph
from bluecore_models.bluecore_graph import BluecoreGraph
from bluecore_models.models import (
    Base,
    BibframeClass,
    BibframeOtherResources,
    Hub,
    Instance,
    OtherResource,
    ResourceBase,
    ResourceBibframeClass,
    Version,
    Work,
)
from bluecore_models.models.pg_ext_func import PG_EXT_FUNC
from bluecore_models.namespaces import BF, MADS
from bluecore_models.utils.graph import load_jsonld

TEST_DATA = Path(__file__).resolve().parent.parent / "tests" / "data"
MANIFEST = "MANIFEST.txt"

logger = logging.getLogger("diff_save")

Step = tuple[str, Callable[[sessionmaker], Graph], object | None]
Outcome = dict[str, Any]

# A URI-ish token: a scheme, then anything up to a delimiter. mask() looks
# each one up in the names, so it costs one dict lookup per URI rather than one
# string scan per name.
URI_TOKEN = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*:[^\s\"'<>\\\[\](){},|^`]+")
TRAILING = ".;:!?"
# The namespace load() saves under (BluecoreGraph's default), and the shape
# every URI it mints has: <namespace><hubs|works|instances>/<uuid4>.
NAMESPACE = "https://bcld.info/"
UUID4 = r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}"
UUID4_TOKEN = re.compile(UUID4)
# A canonical name for a resource whose adminMetadata a save generated.
MINTED_NAME = re.compile(r"(^|\{)minted:")
# The two forms _generate_admin_metadata writes bf:date in: the xsd:dateTime
# isoformat of a UTC now(), and a plain YYYY-MM-DD.
GENERATED_DATE = re.compile(
    r"\d{4}-\d{2}-\d{2}(T\d{2}:\d{2}:\d{2}(\.\d{1,6})?\+00:00)?"
)
# a blank node label, as pyld writes one into framed JSON-LD
BNODE_LABEL = re.compile(r"_:\S+")
# What's left in an error message once the names are masked that still
# differs from run to run: uuid4s (which minted URIs end in; LC's ids aren't
# version 4), rdflib blank node labels, timestamps and object addresses.
UNSTABLE = [
    (
        UUID4_TOKEN,
        "<uuid>",
    ),
    (re.compile(r"\bN[0-9a-f]{32}\b"), "<bnode>"),
    (
        re.compile(
            r"\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(\.\d+)?([+-]\d{2}:?\d{2}|Z)?"
        ),
        "<datetime>",
    ),
    (re.compile(r"datetime\.datetime\([^)]*\)"), "<datetime>"),
    (re.compile(r"\b0x[0-9a-f]+\b"), "<address>"),
]


# ---------------------------------------------------------------------------
# The corpus
# ---------------------------------------------------------------------------


def select_members(uploads: Path, per_batch: int | None) -> Iterator[tuple[str, bytes]]:
    """
    The first per_batch .rdf members of each batch_*.tar.gz, sorted by batch
    then member name, or all of them when per_batch is None.
    """
    for tarball in sorted(uploads.glob("batch_*.tar.gz")):
        stem = tarball.name.removesuffix(".tar.gz")
        with tarfile.open(tarball) as tar:
            members = sorted(
                (m for m in tar.getmembers() if m.isfile() and m.name.endswith(".rdf")),
                key=lambda m: m.name,
            )
            if per_batch is not None:
                members = members[:per_batch]
            for member in members:
                fileobj = tar.extractfile(member)
                if fileobj is None:
                    raise ValueError(f"can't read {tarball.name}:{member.name}")
                yield f"{stem}/{member.name}", fileobj.read()


def _tar_member(tar: tarfile.TarFile, name: str, data: bytes) -> None:
    info = tarfile.TarInfo(name)
    info.size = len(data)
    info.mtime = 0
    info.uid = info.gid = 0
    info.uname = info.gname = ""
    info.mode = 0o644
    tar.addfile(info, io.BytesIO(data))


def write_sample(uploads: Path, per_batch: int, out: Path) -> str:
    """
    Write the selected members to one gzipped tarball, after a MANIFEST.txt
    listing them in order, and return its SHA-256. Writing it twice gives the
    same bytes.
    """
    members = list(select_members(uploads, per_batch))
    if not members:
        raise ValueError(f"no batch_*.tar.gz members found in {uploads}")
    manifest = "".join(f"{name}\n" for name, _ in members).encode()
    with (
        out.open("wb") as raw,
        gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as gz,
        tarfile.open(fileobj=gz, mode="w", format=tarfile.USTAR_FORMAT) as tar,
    ):
        _tar_member(tar, MANIFEST, manifest)
        for name, data in members:
            _tar_member(tar, name, data)
    return hashlib.sha256(out.read_bytes()).hexdigest()


def _rdfxml(data: bytes) -> Callable[[sessionmaker], Graph]:
    # Parsed inside the step, so a record that fails to parse is an outcome.
    def build(_: sessionmaker) -> Graph:
        g = Graph()
        g.parse(data=data, format="xml")
        return g

    return build


def _read_sample(sample: Path, per_batch: int | None) -> list[tuple[str, bytes]]:
    with tarfile.open(sample) as tar:
        contents: dict[str, bytes] = {}
        for member in tar:
            fileobj = tar.extractfile(member) if member.isfile() else None
            if fileobj is not None:
                contents[member.name] = fileobj.read()
    if MANIFEST not in contents:
        raise ValueError(f"{sample} has no {MANIFEST}")
    names = [n for n in contents[MANIFEST].decode().splitlines() if n.endswith(".rdf")]
    if per_batch is not None:
        taken: Counter[str] = Counter()
        kept = []
        for name in names:
            batch = name.split("/", 1)[0]
            if taken[batch] < per_batch:
                taken[batch] += 1
                kept.append(name)
        names = kept
    missing = [n for n in names if n not in contents]
    if missing:
        raise ValueError(f"{sample} lists members it doesn't hold: {missing[:5]}")
    return [(name, contents[name]) for name in names]


def iter_corpus(corpus: Path, per_batch: int | None) -> Iterator[Step]:
    """
    Ingest steps for an uploads/ directory or a sample tarball, in a stable
    order: the sample's MANIFEST.txt order, keeping the first per_batch of
    each batch when per_batch is given.
    """
    if corpus.is_dir():
        members: Iterable[tuple[str, bytes]] = select_members(corpus, per_batch)
    else:
        members = _read_sample(corpus, per_batch)
    for name, data in members:
        yield name, _rdfxml(data), None


# ---------------------------------------------------------------------------
# Sequences the corpus can't produce
# ---------------------------------------------------------------------------

LOC_WORKS = "http://id.loc.gov/resources/works/"
STUB_DESCRIBED = LOC_WORKS + "diff-save-stub-described"
STUB_STUB = LOC_WORKS + "diff-save-stub"
EDIT_SOURCE = LOC_WORKS + "diff-save-api-edit"
HEADING = "http://id.loc.gov/authorities/subjects/sh-diff-save-242"
HEADING_TITLE = "Diff Save sinopia_editor#242"


def _parse_file(name: str) -> Callable[[sessionmaker], Graph]:
    def build(_: sessionmaker) -> Graph:
        g = Graph()
        g.parse(TEST_DATA / name, format="json-ld")
        return g

    return build


def _record(described: str, title: str, stub: str | None = None) -> Graph:
    """
    A record the way an ingest sees one: a Work it describes, which carries
    AdminMetadata of its own, and optionally a stub Work it only points at.
    """
    g = Graph()
    work = URIRef(described)
    g.add((work, RDF.type, BF.Work))
    title_node = BNode()
    g.add((work, BF.title, title_node))
    g.add((title_node, RDF.type, BF.Title))
    g.add((title_node, BF.mainTitle, Literal(title)))
    admin_metadata = BNode()
    g.add((work, BF.adminMetadata, admin_metadata))
    g.add((admin_metadata, RDF.type, BF.AdminMetadata))
    g.add((admin_metadata, BF.status, URIRef("http://id.loc.gov/vocabulary/mstatus/n")))
    if stub is not None:
        stub_work = URIRef(stub)
        g.add((work, BF.relatedTo, stub_work))
        g.add((stub_work, RDF.type, BF.Work))
        g.add((stub_work, RDFS.label, Literal("stub label")))
    return g


def _derived_from_ids(data: Any) -> list[str]:
    """Every bf:derivedFrom @id in a resource's top-level adminMetadata."""
    blocks = data.get("adminMetadata", []) if isinstance(data, dict) else []
    found = []
    for block in blocks if isinstance(blocks, list) else [blocks]:
        if not isinstance(block, dict):
            continue
        values = block.get("derivedFrom", [])
        for value in values if isinstance(values, list) else [values]:
            if isinstance(value, dict) and "@id" in value:
                found.append(str(value["@id"]))
            elif isinstance(value, str):
                found.append(value)
    return found


def _stored_work(
    session_maker: sessionmaker, match: Callable[[dict], bool]
) -> tuple[str, dict]:
    """The newest stored Work whose data matches."""
    with session_maker() as session:
        rows = session.query(Work.id, Work.uri, Work.data).order_by(Work.id.desc())
        for _, uri, data in rows:
            if match(data):
                return uri, dict(data)
    raise LookupError("no stored Work matches")


def _api_edit(session_maker: sessionmaker) -> Graph:
    _, data = _stored_work(session_maker, lambda d: EDIT_SOURCE in _derived_from_ids(d))
    data["title"] = {"@type": "Title", "mainTitle": "Cataloger Edit"}
    return load_jsonld(data)


def _unconnected_works(_: sessionmaker) -> Graph:
    """Two Works that nothing connects: a save can't tell which is the record."""
    g = Graph()
    g.add((BNode(), RDF.type, BF.Work))
    g.add((BNode(), RDF.type, BF.Work))
    return g


def _titled_242(data: dict) -> bool:
    return HEADING_TITLE in json.dumps(data)


def _create_242(_: sessionmaker) -> Graph:
    g = Graph()
    work = BNode()
    title = BNode()
    g.add((work, RDF.type, BF.Work))
    g.add((work, BF.title, title))
    g.add((title, RDF.type, BF.Title))
    g.add((title, BF.mainTitle, Literal(HEADING_TITLE)))
    return g


def _cite_inline_242(session_maker: sessionmaker) -> Graph:
    uri, data = _stored_work(session_maker, _titled_242)
    g = load_jsonld(data)
    g.add((URIRef(uri), BF.subject, URIRef(HEADING)))
    g.add((URIRef(HEADING), RDF.type, MADS.Topic))
    g.add((URIRef(HEADING), RDFS.label, Literal("Physicians")))
    return g


def _resave_242(session_maker: sessionmaker) -> Graph:
    # the stored data cites the heading by uri alone
    _, data = _stored_work(session_maker, _titled_242)
    return load_jsonld(data)


def sequences() -> list[Step]:
    """
    Saves the corpus can't produce, flattened in order and named
    <sequence>/<index>. One is refused on purpose, so an error outcome is part
    of every real comparison: refused/0 saves two unconnected blank-node Works
    with primary_class=BF.Work, which raises BluecoreGraphError.
    """
    runs: dict[str, list[tuple[Callable[[sessionmaker], Graph], object | None]]] = {}

    # 1. each fixture under each primary_class
    runs["primary-class"] = [
        (_parse_file(name), primary_class)
        for name in (
            "blue-core-work.jsonld",
            "blue-core-instance.jsonld",
            "blue-core-hub.jsonld",
        )
        for primary_class in (BF.Work, BF.Instance, BF.Hub, OtherResource, None)
    ]

    # 2. a stub, then its full description
    runs["stub"] = [
        (lambda _: _record(STUB_DESCRIBED, "Described Work", stub=STUB_STUB), None),
        (lambda _: _record(STUB_STUB, "Now Fully Described"), None),
    ]

    # 3. an API edit, then a re-ingest of the same record
    runs["api-edit"] = [
        (lambda _: _record(EDIT_SOURCE, "REINGESTED 1"), None),
        (_api_edit, BF.Work),
        (lambda _: _record(EDIT_SOURCE, "REINGESTED 1"), None),
    ]

    # 4. sinopia_editor#242: a Work is created, saved citing a heading it
    # describes inline, then saved again from its stored data, which cites the
    # heading by uri alone
    runs["sinopia-242"] = [
        (_create_242, BF.Work),
        (_cite_inline_242, BF.Work),
        (_resave_242, BF.Work),
    ]

    # 5. a save that is refused: two Works nothing connects, one of them
    # primary
    runs["refused"] = [(_unconnected_works, BF.Work)]

    return [
        (f"{sequence}/{index}", build, primary_class)
        for sequence, steps in runs.items()
        for index, (build, primary_class) in enumerate(steps)
    ]


# ---------------------------------------------------------------------------
# Masking and digests
# ---------------------------------------------------------------------------


def mask(text: str, names: dict[str, str]) -> str:
    """Replace each URI in names with its canonical name."""

    def replace(match: re.Match) -> str:
        token = match.group(0)
        # a URI at the end of a sentence carries its full stop with it
        end = len(token)
        while end and token[:end] not in names and token[end - 1] in TRAILING:
            end -= 1
        if token[:end] in names:
            return names[token[:end]] + token[end:]
        return token

    return URI_TOKEN.sub(replace, text)


def _mask_message(text: str, names: dict[str, str]) -> str:
    text = mask(text, names)
    for pattern, replacement in UNSTABLE:
        text = pattern.sub(replacement, text)
    return text


def _blank_date(value: Any) -> Any:
    """
    Mask a date value in one of the generated forms, keeping its shape and
    datatype. Any other value is left as it is.
    """
    if isinstance(value, list):
        return [_blank_date(v) for v in value]
    if isinstance(value, dict):
        return {k: _blank_date(v) if k == "@value" else v for k, v in value.items()}
    if isinstance(value, str) and GENERATED_DATE.fullmatch(value):
        return "<date>"
    return value


def _mask_admin_metadata(blocks: Any) -> Any:
    if isinstance(blocks, list):
        return [_mask_admin_metadata(b) for b in blocks]
    if isinstance(blocks, dict) and "date" in blocks:
        return {**blocks, "date": _blank_date(blocks["date"])}
    return blocks


def _regenerated(names: dict[str, str]) -> set[str]:
    """The URIs whose adminMetadata a save generated: the minted ones."""
    return {uri for uri, name in names.items() if MINTED_NAME.search(name)}


def _mask_dates(node: Any, regenerated: set[str]) -> Any:
    """
    Mask the generated dates in the adminMetadata of each node a save minted,
    wherever it is nested. Dates on any other node are real data.
    """
    if isinstance(node, list):
        return [_mask_dates(v, regenerated) for v in node]
    if not isinstance(node, dict):
        return node
    masked = {k: _mask_dates(v, regenerated) for k, v in node.items()}
    if node.get("@id") in regenerated and "adminMetadata" in masked:
        masked["adminMetadata"] = _mask_admin_metadata(masked["adminMetadata"])
    return masked


def _is_label(value: Any) -> bool:
    return isinstance(value, str) and BNODE_LABEL.fullmatch(value) is not None


def _without_labels(node: Any) -> Any:
    if isinstance(node, dict):
        return {k: _without_labels(v) for k, v in node.items()}
    if isinstance(node, list):
        return [_without_labels(v) for v in node]
    return "_:" if _is_label(node) else node


def _sort_arrays(node: Any, ordered: bool = False) -> Any:
    """
    Sort every JSON-LD array except an @list. The baseline doesn't store a
    resource's values in the same order from one run to the next (they follow
    rdflib's iteration order, which follows blank node labels), and in RDF a
    property's values have no order. Sorting keeps every value, duplicates too.
    The order ignores blank node labels, which _relabel then renumbers.
    """
    if isinstance(node, dict):
        return {k: _sort_arrays(v, ordered=k == "@list") for k, v in node.items()}
    if isinstance(node, list):
        items = [_sort_arrays(v) for v in node]
        if ordered:
            return items
        return sorted(
            items, key=lambda item: json.dumps(_without_labels(item), sort_keys=True)
        )
    return node


def _relabel(node: Any, labels: dict[str, str]) -> Any:
    """
    Renumber blank node labels in order of first appearance. pyld labels a
    blank node it has to refer to twice (_:b20 in one run, _:b24 in the next);
    which nodes share a label is data, the label itself isn't.
    """
    if isinstance(node, dict):
        return {k: _relabel(node[k], labels) for k in sorted(node)}
    if isinstance(node, list):
        return [_relabel(v, labels) for v in node]
    if _is_label(node):
        return labels.setdefault(node, f"_:b{len(labels)}")
    return node


def mask_data(data: dict, names: dict[str, str]) -> str:
    """
    Stored JSON-LD as stable text: the generated dates in minted resources'
    adminMetadata masked, then the minted URIs, then the arrays sorted and the
    blank node labels renumbered. Dates anywhere else are real data. The
    arrays are sorted after masking, since a minted URI's place in the order
    would otherwise change from run to run.
    """
    dated = _mask_dates(data, _regenerated(names))
    masked = mask(json.dumps(dated, sort_keys=True), names)
    return json.dumps(_relabel(_sort_arrays(json.loads(masked)), {}), sort_keys=True)


def _shaped(uri: str, name: str) -> str:
    """
    A URI with its uuid4 replaced by a name, so what's left of it -- the
    namespace and the kind of resource -- is still compared:
    https://bcld.info/works/{minted:http://id.loc.gov/resources/works/9}. A
    URI with no uuid4 in it is stable already, and kept as it is.
    """
    return UUID4_TOKEN.sub(lambda _: "{" + name + "}", uri)


def graph_names(g: Graph) -> dict[str, str]:
    """
    Each resource with a bf:derivedFrom in its adminMetadata, named for that
    URI: its uuid replaced by {minted:<derivedFrom>}.
    """
    sources: dict[str, set[str]] = defaultdict(set)
    for admin_metadata, source in g.subject_objects(BF.derivedFrom):
        for subject in g.subjects(BF.adminMetadata, admin_metadata):
            if isinstance(subject, URIRef):
                sources[str(subject)].add(str(source))
    return {
        uri: _shaped(uri, "minted:" + "|".join(sorted(s))) for uri, s in sources.items()
    }


# Characters a URI may keep as they are when _safe_iri percent-encodes it.
# "%" isn't one, so the encoding can be undone and two URIs never meet.
IRI_SAFE = "!#$&'()*+,/:;=?@[]~"


def _safe_iri(value: str) -> URIRef:
    """
    A URIRef rdflib will hash and canonicalise. The corpus holds URIs rdflib
    refuses to write (" https://portal.dnb.de/..." with its leading space),
    and so do the canonical names, with their braces. Percent-encoding
    everything else, "%" included, keeps distinct URIs distinct.
    """
    return URIRef(urllib.parse.quote(value, safe=IRI_SAFE))


def _escaped(value: str, specials: str) -> str:
    """Backslash-escape value: the backslash itself first, then specials."""
    value = value.replace("\\", "\\\\")
    for char in specials:
        value = value.replace(char, "\\" + char)
    return value.replace("\n", "\\n").replace("\r", "\\r")


def _term_line(term: Any) -> str:
    """
    A term written out without rdflib's serialiser, which validates URIs. The
    escaping is injective, so distinct terms are written differently.
    """
    if isinstance(term, URIRef):
        return "<" + _escaped(str(term), ">") + ">"
    if isinstance(term, BNode):
        return "_:" + str(term)
    if isinstance(term, Literal):
        line = '"' + _escaped(str(term), '"') + '"'
        if term.language is not None:
            line += "@" + term.language
        if term.datatype is not None:
            line += "^^" + _term_line(term.datatype)
        return line
    raise TypeError(f"unexpected term {term!r}")


def graph_digest(g: Graph, names: dict[str, str]) -> str:
    """
    SHA-256 of the canonical N-Triples of g, with the URIs in names replaced
    and the generated dates on minted resources' AdminMetadata masked.
    """
    regenerated = _regenerated(names)
    admin_nodes = {
        admin_metadata
        for subject, admin_metadata in g.subject_objects(BF.adminMetadata)
        if str(subject) in regenerated
    }

    def rename(term: Any) -> Any:
        if isinstance(term, URIRef):
            return _safe_iri(names.get(str(term), str(term)))
        if isinstance(term, Literal) and term.datatype is not None:
            datatype = _safe_iri(str(term.datatype))
            if datatype != term.datatype:
                return Literal(str(term), datatype=datatype)
        return term

    def masked_date(o: Any) -> Any:
        if not isinstance(o, Literal) or not GENERATED_DATE.fullmatch(str(o)):
            return rename(o)
        if o.datatype is not None:
            return Literal(f"<date> {o.datatype}")
        return Literal("<date>", lang=o.language)

    out = Graph()
    for s, p, o in g:
        if p == BF.date and s in admin_nodes:
            out.add((rename(s), rename(p), masked_date(o)))
        else:
            out.add((rename(s), rename(p), rename(o)))
    lines = sorted(
        " ".join(_term_line(term) for term in triple)
        for triple in to_canonical_graph(out)
    )
    return hashlib.sha256("\n".join(lines).encode()).hexdigest()


# ---------------------------------------------------------------------------
# Loading and snapshots
# ---------------------------------------------------------------------------


def load(
    session_maker: sessionmaker, steps: Iterable[Step], passes: int = 2
) -> tuple[list[Outcome], dict[str, str]]:
    """
    Save every step, once per pass, recording an Outcome for each save. A
    failure is an outcome, not the end of the run. Also returns created: the
    URI each API-style save created, named after the step that created it.
    """
    steps = list(steps)
    outcomes: list[Outcome] = []
    created: dict[str, str] = {}
    for pass_ in range(1, passes + 1):
        for name, build, primary_class in steps:
            # the same step creates a new resource on every pass
            label = f"created:{name}" if pass_ == 1 else f"created:{name}#{pass_}"
            bg: BluecoreGraph | None = None
            try:
                bg = BluecoreGraph(build(session_maker))
                bg.save(
                    session_maker,
                    primary_class=primary_class,
                    update_other_resources=False,
                )
            except Exception as exc:
                # the outcome is what's compared; the traceback is for whoever
                # has to work out why
                logger.exception(f"{name} (pass {pass_}) failed")
                names = _failure_names(bg, label, created)
                outcomes.append(
                    {
                        "name": name,
                        "pass": pass_,
                        "status": type(exc).__name__,
                        "message": _mask_message(str(exc), names),
                        "graph": None,
                    }
                )
                continue
            # the save happened, so what it created is named whatever follows
            if bg.created_uri is not None:
                created[str(bg.created_uri)] = label
            outcomes.append(_saved(name, pass_, bg, created))
    return outcomes, created


def _failure_names(
    bg: BluecoreGraph | None, label: str, created: dict[str, str]
) -> dict[str, str]:
    """The names to mask a failed save's message with, as far as they're known."""
    names: dict[str, str] = {}
    if bg is not None:
        try:
            names = graph_names(bg.graph)
        except Exception:
            logger.exception("can't name the resources in a failed save's graph")
        if bg.created_uri is not None:
            names[str(bg.created_uri)] = label
    names.update(created)
    return names


def _saved(
    name: str, pass_: int, bg: BluecoreGraph, created: dict[str, str]
) -> Outcome:
    """
    The outcome of a save that went through. If its graph can't be digested,
    that's a DigestError outcome rather than the end of the run.
    """
    names = dict(created)
    try:
        names = graph_names(bg.graph) | created
        digest = graph_digest(bg.graph, names)
    except Exception as exc:
        logger.exception(
            f"{name} (pass {pass_}) saved, but its graph can't be digested"
        )
        return {
            "name": name,
            "pass": pass_,
            "status": "DigestError",
            "message": _mask_message(f"{type(exc).__name__}: {exc}", names),
            "graph": None,
        }
    return {"name": name, "pass": pass_, "status": "ok", "message": "", "graph": digest}


# a stored Hub, Work or Instance: (type, id, uri, data)
NamedRow = tuple[str, int, str, Any]


def _name_rows(rows: Iterable[NamedRow], created: dict[str, str]) -> dict[str, str]:
    """
    Name each row for the step that created it, or for the URI it was derived
    from, with the name in place of its uuid (see _shaped). Rows that would
    share a name are told apart by the order they were inserted in.
    """
    by_name: dict[str, list[tuple[int, str]]] = defaultdict(list)
    for _, id_, uri, data in rows:
        sources = sorted(set(_derived_from_ids(data)))
        if uri in created:
            by_name[created[uri]].append((id_, uri))
        elif sources:
            by_name["minted:" + "|".join(sources)].append((id_, uri))
        else:
            raise ValueError(
                f"{uri} has no derivedFrom and no step created it, "
                "so it has no name that is stable between runs"
            )
    names = {}
    for name, named in by_name.items():
        if len(named) == 1:
            names[named[0][1]] = _shaped(named[0][1], name)
        else:
            for rank, (_, uri) in enumerate(sorted(named), start=1):
                names[uri] = _shaped(uri, f"{name}#{rank}")
    return names


def _violations(rows: Iterable[NamedRow], names: dict[str, str]) -> list[list[str]]:
    """
    Rows whose URI isn't <namespace><type>/<uuid4>, the shape a save mints.
    Recorded rather than raised, so they compare as differences.
    """
    return [
        [type_, names[uri], f"not {NAMESPACE}{type_}/<uuid4>"]
        for type_, _, uri, _ in rows
        if not re.fullmatch(re.escape(NAMESPACE + type_ + "/") + UUID4, uri)
    ]


def _bibframe_rows(session: Session) -> list[NamedRow]:
    return [
        (cls.__mapper__.polymorphic_identity, id_, uri, data)
        for cls in (Hub, Work, Instance)
        for id_, uri, data in session.query(cls.id, cls.uri, cls.data)
    ]


def canonical_names(session: Session, created: dict[str, str]) -> dict[str, str]:
    """
    A stable name for every Hub, Work and Instance row: its URI with the uuid
    replaced by the step that created it, or by minted:<the uri it was derived
    from>.
    """
    return _name_rows(_bibframe_rows(session), created)


def code_sha256(directory: Path) -> str:
    """SHA-256 over the sorted .py files under directory: relative path, then bytes."""
    digest = hashlib.sha256()
    for path in sorted(directory.rglob("*.py")):
        content = path.read_bytes()
        digest.update(path.relative_to(directory).as_posix().encode() + b"\0")
        digest.update(str(len(content)).encode() + b"\0" + content)
    return digest.hexdigest()


def _uuid_state(uuid: Any, uri: str) -> str | None:
    """How a row's uuid relates to its URI: a save sets it to the last segment."""
    if uuid is None:
        return None
    return "last-segment" if str(uuid) == uri.rsplit("/", 1)[-1] else "other"


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _sorted(items: list) -> list:
    return sorted(items, key=lambda item: json.dumps(item, sort_keys=True))


def snapshot(
    session_maker: sessionmaker,
    outcomes: list[Outcome],
    created: dict[str, str],
    code_path: str,
) -> dict:
    """The database and the outcomes, in a form two runs can compare."""
    with session_maker() as session:
        bibframe_rows = _bibframe_rows(session)
        names = _name_rows(bibframe_rows, created)
        violations = _violations(bibframe_rows, names)
        hubs: dict[int, str] = {}
        works: dict[int, str] = {}
        uris: dict[int, str] = {}
        rows = []
        for hub in session.query(Hub):
            hubs[hub.id] = uris[hub.id] = names[hub.uri]
            rows.append(
                [
                    hub.type,
                    names[hub.uri],
                    None,
                    None,
                    hub.data,
                    _uuid_state(hub.uuid, hub.uri),
                ]
            )
        for work in session.query(Work):
            works[work.id] = uris[work.id] = names[work.uri]
            hub_uri = hubs[work.hub_id] if work.hub_id is not None else None
            rows.append(
                [
                    work.type,
                    names[work.uri],
                    hub_uri,
                    None,
                    work.data,
                    _uuid_state(work.uuid, work.uri),
                ]
            )
        for instance in session.query(Instance):
            uris[instance.id] = names[instance.uri]
            work_uri = works[instance.work_id] if instance.work_id is not None else None
            rows.append(
                [
                    instance.type,
                    names[instance.uri],
                    None,
                    work_uri,
                    instance.data,
                    _uuid_state(instance.uuid, instance.uri),
                ]
            )
        for other in session.query(OtherResource):
            # Other Resource URIs are external, so they're stable as they are
            uris[other.id] = other.uri
            rows.append(
                [
                    other.type,
                    other.uri,
                    None,
                    None,
                    other.data,
                    _uuid_state(other.uuid, other.uri),
                ]
            )
        links = [
            [uris[link.bibframe_resource_id], uris[link.other_resource_id]]
            for link in session.query(BibframeOtherResources)
        ]
        # every version each resource has, in the order they were written: a
        # list, so a save that writes one more or one fewer shows. created_at
        # and the keycloak fields say when and by whom, not what was saved.
        version_data: dict[str, list[Any]] = defaultdict(list)
        for resource_id, data in session.query(
            Version.resource_id, Version.data
        ).order_by(Version.id):
            version_data[uris[resource_id]].append(data)
        # rows of any type, so one a save shouldn't write, or an orphan
        # resource_base row, still shows
        types = [
            [type_, count]
            for type_, count in session.query(
                ResourceBase.type, func.count(ResourceBase.id)
            ).group_by(ResourceBase.type)
        ]
        bf_classes = [
            [uris[resource_id], uri]
            for resource_id, uri in session.query(
                ResourceBibframeClass.resource_id, BibframeClass.uri
            ).join(BibframeClass, ResourceBibframeClass.bf_class_id == BibframeClass.id)
        ]
    # a row is [type, uri, hub, work, sha256 of its data, its uuid's state]
    for row in rows:
        row[4] = _sha256(mask_data(row[4], names))
    versions = [
        [name, [_sha256(mask_data(data, names)) for data in datas]]
        for name, datas in version_data.items()
    ]
    return {
        "code": code_path,
        "rows": _sorted(rows),
        "links": _sorted(links),
        "records": _sorted(outcomes),
        "versions": _sorted(versions),
        "bf_classes": _sorted(bf_classes),
        "types": _sorted(types),
        "violations": _sorted(violations),
    }


# ---------------------------------------------------------------------------
# Comparing snapshots
# ---------------------------------------------------------------------------


def _grouped(items: list, key: Callable[[Any], Any]) -> dict[str, list[str]]:
    groups: dict[str, list[str]] = defaultdict(list)
    for item in items:
        groups[json.dumps(key(item))].append(json.dumps(item, sort_keys=True))
    return {k: sorted(v) for k, v in groups.items()}


SECTIONS = {
    "rows",
    "links",
    "records",
    "versions",
    "bf_classes",
    "types",
    "violations",
}


def compare(a: dict, b: dict, same_code: bool = False) -> list[str]:
    """
    Human-readable differences between two snapshots; empty when they match.
    Snapshots of the same code compare equal however wrong the code is, so
    comparing two is refused unless same_code says it's meant.
    """
    # the code's hash, when the snapshot has one; its path otherwise
    same = a.get("code_sha256", a["code"]) == b.get("code_sha256", b["code"])
    if same and not same_code:
        raise ValueError(
            f"both snapshots come from the same code ({a['code']} and "
            f"{b['code']}): pass same_code to compare them"
        )
    if a.get("selection") != b.get("selection"):
        raise ValueError(
            f"the snapshots load different records: {a.get('selection')} "
            f"and {b.get('selection')}"
        )
    differences = []
    for key in sorted(
        (set(a) | set(b)) - (SECTIONS | {"code", "code_sha256", "selection"})
    ):
        if a.get(key) != b.get(key):
            differences.append(f"{key}: {a.get(key)!r} != {b.get(key)!r}")
    sections: list[tuple[str, Callable[[Any], Any]]] = [
        ("rows", lambda row: row[1]),
        ("links", lambda link: link),
        ("records", lambda record: [record["name"], record["pass"]]),
        ("versions", lambda version: version[0]),
        ("bf_classes", lambda pair: pair),
        ("types", lambda count: count[0]),
        ("violations", lambda violation: violation),
    ]
    for section, key in sections:
        groups_a = _grouped(a.get(section, []), key)
        groups_b = _grouped(b.get(section, []), key)
        for group in sorted(set(groups_a) | set(groups_b)):
            in_a, in_b = groups_a.get(group, []), groups_b.get(group, [])
            if in_a == in_b:
                continue
            label = f"{section} {json.loads(group)}"
            if not in_b:
                differences.append(f"{label}: only in A: {in_a}")
            elif not in_a:
                differences.append(f"{label}: only in B: {in_b}")
            else:
                differences.append(f"{label}: A {in_a} != B {in_b}")
    return differences


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def ensure_schema(engine: Any) -> None:
    """Create the custom functions and the tables, as the benchmark does."""
    with engine.begin() as conn:
        for stmt in PG_EXT_FUNC:
            conn.execute(text(stmt))
    Base.metadata.create_all(engine)


def _positive(value: str) -> int:
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError(f"must be at least 1, got {number}")
    return number


def _run(args: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    corpus = Path(args.corpus)
    if not corpus.exists():
        parser.error(f"{corpus} doesn't exist")
    if corpus.is_dir() and args.per_batch is None and not args.all:
        parser.error("a directory needs --per-batch N or --all")
    # bluecore_models is a namespace package, so it has no __file__ of its
    # own; bluecore_graph's says which code this run is saving with
    code_file = bluecore_models.bluecore_graph.__file__
    if code_file is None:
        parser.error("can't tell where bluecore_models.bluecore_graph came from")
    code = str(Path(code_file).resolve().parent)
    print(f"bluecore_models: {code_file} (path {list(bluecore_models.__path__)})")
    log = f"{args.out}.log"
    logging.basicConfig(filename=log, level=logging.WARNING)
    print(f"failed steps are logged to {log}", flush=True)

    engine = create_engine(args.database_url)
    tables = inspect(engine).get_table_names()
    if tables:
        parser.error(f"the database isn't empty: it has tables {sorted(tables)}")
    ensure_schema(engine)
    session_maker = sessionmaker(bind=engine)

    steps = list(iter_corpus(corpus, args.per_batch))
    if args.sequences:
        steps += sequences()
    passes = 2
    print(f"loading {len(steps)} steps, {passes} passes", flush=True)
    outcomes, created = load(session_maker, steps, passes=passes)

    snap = snapshot(session_maker, outcomes, created, code)
    snap["code_sha256"] = code_sha256(Path(code))
    snap["selection"] = {
        "corpus": corpus.name,
        # two samples can share a name, so a sample's bytes are part of it too
        "sha256": (
            None if corpus.is_dir() else hashlib.sha256(corpus.read_bytes()).hexdigest()
        ),
        "per_batch": args.per_batch,
        "sequences": args.sequences,
        "passes": passes,
    }
    Path(args.out).write_text(json.dumps(snap, sort_keys=True, indent=1) + "\n")
    statuses = Counter(record["status"] for record in outcomes)
    print(
        f"{len(snap['rows'])} rows, {len(snap['links'])} links, "
        f"outcomes {dict(sorted(statuses.items()))} -> {args.out}"
    )
    return 0


def _compare(args: argparse.Namespace) -> int:
    a = json.loads(Path(args.a).read_text())
    b = json.loads(Path(args.b).read_text())
    try:
        differences = compare(a, b, same_code=args.same_code)
    except ValueError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2
    for difference in differences[:50]:
        print(difference)
    if differences:
        print(f"{len(differences)} difference(s)")
        return 1
    print("no differences")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    commands = parser.add_subparsers(dest="command", required=True)

    sample = commands.add_parser("sample", help="write the shared sample")
    sample.add_argument("--uploads", required=True)
    sample.add_argument("--per-batch", type=_positive, required=True)
    sample.add_argument("--out", required=True)

    run = commands.add_parser("run", help="load a corpus and snapshot the database")
    run.add_argument("--database-url", required=True)
    run.add_argument("--corpus", required=True, help="the sample, or an uploads/ dir")
    selection = run.add_mutually_exclusive_group()
    selection.add_argument("--per-batch", type=_positive)
    selection.add_argument("--all", action="store_true")
    run.add_argument("--sequences", action="store_true")
    run.add_argument("--out", required=True)

    compare_ = commands.add_parser("compare", help="compare two snapshots")
    compare_.add_argument("a")
    compare_.add_argument("b")
    compare_.add_argument("--same-code", action="store_true")

    args = parser.parse_args(argv)
    if args.command == "sample":
        print(write_sample(Path(args.uploads), args.per_batch, Path(args.out)))
        return 0
    if args.command == "run":
        return _run(args, run)
    return _compare(args)


if __name__ == "__main__":
    sys.exit(main())
