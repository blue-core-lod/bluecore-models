"""Which context a document is read and written with.

The context used to be an eight-entry literal in `utils/graph.py`, and
`set_jsonld` stripped `@context` on the way into the database. A stored row
therefore could not say what framed it, and anything reading one had to assume.
These cover the replacement: the context comes from bibframe-json, the row names
it by URL, and the URL resolves out of the installed package rather than over
the network.
"""

import contextlib
import json
import pathlib
import socket
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import bibframe_json
import pytest
from pyld import jsonld
from rdflib import Literal, URIRef
from rdflib.compare import to_isomorphic

from bluecore_models.namespaces import BF, BFLC
from bluecore_models.utils.graph import (
    CONTEXT,
    CONTEXT_URL,
    LEGACY_CONTEXT,
    _as_arrays,
    frame_jsonld,
    framed_for_storage,
    load_jsonld,
    terms_for,
    validate_jsonld,
)

WORK = "https://bcld.info/works/1234"

# descriptionLevel is the term the two contexts disagree about: bibframe-json
# declares it @type: @id and the legacy literal did not.
LEVEL = URIRef("http://id.loc.gov/ontologies/bibframe/descriptionLevel")


@contextlib.contextmanager
def no_network():
    """Name resolution fails, so a context fetch cannot quietly succeed.

    Counting requests to a local server is not enough on its own: a fetch of a
    bibframe-json.org URL would never reach it, so the count stays at zero
    whether or not anything was fetched.
    """

    def refuse(*args, **kwargs):
        raise AssertionError("tried to resolve a hostname")

    original = socket.getaddrinfo
    socket.getaddrinfo = refuse
    try:
        yield
    finally:
        socket.getaddrinfo = original


def a_work(**extra):
    return {
        "@id": WORK,
        "@type": ["Work"],
        "title": [{"@type": ["Title"], "mainTitle": ["The Elements of Style"]}],
        **extra,
    }


def test_the_context_is_bibframe_jsons():
    """Not a literal maintained here. One definition of the shape, not two."""
    assert CONTEXT == bibframe_json.context()["@context"]
    assert CONTEXT_URL == bibframe_json.CONTEXT_URL
    # and it is the generated one, not the eight-entry hand-written one
    assert len(CONTEXT) > 200
    assert CONTEXT is not LEGACY_CONTEXT


def test_a_document_with_no_context_is_read_as_legacy():
    """The absent case is the only guess, and it guesses something declared.

    Defaulting to CONTEXT would mean an unmarked row silently acquiring whatever
    terms happen to be current, which gets worse with every version shipped.
    """
    assert terms_for(a_work()) is LEGACY_CONTEXT


def test_a_published_context_url_resolves_without_the_network():
    """The reason the URL can stay in the row at all.

    DNS is broken for the duration, so a fetch cannot quietly succeed and make
    this pass for the wrong reason.
    """

    with no_network():
        terms = terms_for(a_work(**{"@context": CONTEXT_URL}))

    assert terms == CONTEXT


def test_an_unresolvable_context_url_raises():
    """Rather than falling back to the network, or to CONTEXT.

    Either fallback would read the document with terms it does not name, and the
    symptom would be a changed object type rather than an error.
    """
    with pytest.raises(ValueError, match="must be absent or a URL"):
        terms_for(a_work(**{"@context": "https://example.org/v9/context.jsonld"}))


def test_an_inline_context_is_refused():
    """Taking a document at its word is the thing we stopped doing.

    An inline context can reach the network in ways that are not visible in
    the value -- `@import`, a scoped `@context` on a term -- and rdflib
    follows them. Refusing the form closes those without having to enumerate
    them.
    """
    with pytest.raises(ValueError, match="must be absent or a URL"):
        terms_for(a_work(**{"@context": {"@vocab": "http://example.org/"}}))


def test_load_jsonld_does_not_modify_its_argument():
    """It used to assign @context into the caller's dict.

    Reading a stored row therefore left the row's own data holding a context it
    had not arrived with, which is a surprising thing for a read to do.
    """
    document = a_work()
    before = dict(document)
    load_jsonld(document)
    assert document == before
    assert "@context" not in document


def test_framing_records_the_context_it_used():
    """So the row can be read back without anyone having to assume.

    The URL rather than the terms: inlined, the context is around 12,000 bytes
    of vocabulary in front of the description it is about.
    """
    framed = frame_jsonld(WORK, a_work(**{"@context": CONTEXT}))
    assert framed["@context"] == CONTEXT_URL


def test_a_framed_document_round_trips_through_the_context_it_names():
    """Frame, then read back, and get the same graph.

    This is the whole point of keeping the URL: load_jsonld resolves what the
    document names, so the write and the read cannot disagree.
    """
    framed = frame_jsonld(WORK, a_work(**{"@context": CONTEXT}))
    graph = load_jsonld(framed)
    titles = set(
        graph.objects(None, URIRef("http://id.loc.gov/ontologies/bibframe/mainTitle"))
    )
    assert titles == {Literal("The Elements of Style")}


def test_the_contexts_agree_on_the_stored_form_of_description_level():
    """Why the two halves of this change were safe to ship together.

    bibframe-json declares descriptionLevel @type: @id and the legacy context
    did not, so in principle the same compact JSON yields a URIRef under one and
    a Literal under the other. In practice every stored value is {"@id": ...},
    which both read as a URIRef, so no row changes meaning.
    """
    level = "http://id.loc.gov/ontologies/bibframe-2-6-0/"
    wrapped = {
        "adminMetadata": [
            {"@type": ["AdminMetadata"], "descriptionLevel": [{"@id": level}]}
        ]
    }
    # a row written now names the context; one written before carries none
    for document in (a_work(**{"@context": CONTEXT_URL}, **wrapped), a_work(**wrapped)):
        graph = load_jsonld(document)
        assert set(graph.objects(None, LEVEL)) == {URIRef(level)}


def test_a_bare_description_level_is_the_form_that_would_diverge():
    """The case the stored data does not contain, pinned so it stays known.

    If a bare string ever reaches storage, this is what it costs: a URIRef under
    one context and a Literal under the other, with no error either way.
    """
    level = "http://id.loc.gov/ontologies/bibframe-2-6-0/"
    bare = {
        "adminMetadata": [{"@type": ["AdminMetadata"], "descriptionLevel": [level]}]
    }

    current = load_jsonld(a_work(**{"@context": CONTEXT_URL}, **bare))
    legacy = load_jsonld(a_work(**bare))

    assert set(current.objects(None, LEVEL)) == {URIRef(level)}
    assert set(legacy.objects(None, LEVEL)) == {Literal(level)}


@pytest.mark.parametrize(
    "fixture,uri",
    [
        (
            "blue-core-work.jsonld",
            "https://bluecore.info/works/23db8603-1932-4c3f-968c-ae584ef1b4bb",
        ),
        (
            "blue-core-instance.jsonld",
            "https://bluecore.info/instances/75d831b9-e0d6-40f0-abb3-e9130622eb8a",
        ),
        (
            "blue-core-hub.jsonld",
            "http://id.loc.gov/resources/hubs/62a26d82-4e65-c696-afed-b12d215a35b1",
        ),
    ],
)
def test_rewriting_a_stored_row_adds_and_removes_no_triples(fixture, uri):
    """The claim worth proving about swapping the context under stored data.

    A row written before this change carries no `@context`, so set_jsonld
    expands it with LEGACY_CONTEXT and reframes it under bibframe-json's. That
    is a migration, and it has to be shown to say the same thing afterwards.

    Compared up to blank-node renaming, because rdflib mints fresh labels on
    every parse: a triple-set comparison reports a difference for every document
    containing one, which is most of them. Over 500 real stage rows this held
    for all 500.
    """
    raw = json.loads((pathlib.Path("tests/data") / fixture).read_text())

    # What the database held before this change: compacted against the context
    # of the day, with @context stripped on the way in. Framing it with
    # LEGACY_CONTEXT rather than the current one matters, because the two
    # compact some IRIs differently -- see
    # test_a_legacy_read_cannot_parse_a_bf_prefixed_value.
    stored = _as_arrays(
        jsonld.compact(
            jsonld.frame(raw, {"@id": uri, "@embed": "@always"}), LEGACY_CONTEXT
        )
    )
    del stored["@context"]

    written = frame_jsonld(uri, {**stored, "@context": LEGACY_CONTEXT})

    assert written["@context"] == CONTEXT_URL
    assert to_isomorphic(load_jsonld(stored)) == to_isomorphic(load_jsonld(written))


def test_a_legacy_read_cannot_parse_a_bf_prefixed_value():
    """A sharp edge the fixtures found, recorded so it stays found.

    bibframe-json's context declares a `bf` prefix and the legacy one did not,
    so an id-valued IRI in the BIBFRAME namespace now compacts to `bf:hasSeries`
    where it used to stay a full URI. Expanding that under LEGACY_CONTEXT, which
    has no `bf`, reads it as an absolute IRI with scheme `bf`, and compaction
    then raises JSON-LD 1.1's "IRI confused with prefix" guard.

    It raises rather than corrupting, and it only arises for a document
    compacted against the current context whose `@context` has been removed --
    which frame_jsonld no longer does. 92 of 1,035,468 stage rows hold such a
    value, all of them `relationship`, so the combination is worth knowing about
    even though nothing produces it today.
    """
    document = {
        "@type": ["Relation"],
        "relationship": [{"@id": "bf:hasSeries"}],
    }
    with pytest.raises(jsonld.JsonLdError):
        frame_jsonld(WORK, {**document, "@id": WORK, "@context": LEGACY_CONTEXT})

    # named properly, the same value round trips
    full = {"@id": "http://id.loc.gov/ontologies/bibframe/hasSeries"}
    written = frame_jsonld(
        WORK,
        {
            "@id": WORK,
            "@type": ["Relation"],
            "relationship": [full],
            "@context": CONTEXT,
        },
    )
    assert written["relationship"] == [{"@id": "bf:hasSeries"}]


def test_validation_reports_and_does_not_refuse():
    """Monitoring, not a gate.

    A write that refused a non-conforming document would refuse the record
    hardest to recover: the one framing has just broken.
    """
    conforming = frame_jsonld(WORK, a_work(**{"@context": CONTEXT}))
    assert validate_jsonld(conforming, WORK) == []

    # a blank node carrying an @id, which is the one class of finding stage
    # data still produces
    broken = dict(conforming, relation=[{"@id": "_:b0", "@type": ["Relation"]}])
    assert validate_jsonld(broken, WORK) != []


def test_validation_survives_a_document_it_cannot_read():
    """Monitoring must not be the thing that breaks a write."""
    assert validate_jsonld({"@context": "nonsense", "@id": WORK}, WORK) == []


def test_the_write_path_is_one_function():
    """set_jsonld and the reframe DAG both go through framed_for_storage.

    The DAG used to reproduce the logic rather than call it, and when @context
    started being stored it was still stripping it -- which would have undone
    the change for every row it touched. One function, so there is nothing to
    drift from.
    """
    legacy = a_work()
    assert "@context" not in legacy

    stored = framed_for_storage(WORK, legacy)
    assert stored["@context"] == CONTEXT_URL

    # and a second pass is a no-op, which the DAG relies on for its own check
    assert framed_for_storage(WORK, stored) == stored


# --- a list is its own document per member -----------------------------------
#
# JSON-LD 1.1 §4.1 allows a document to be an array of node objects each
# carrying its own @context, and Example 19 in the spec has two members naming
# two different remote contexts. load_jsonld passed no context for any of them
# until this was fixed, so none of the four cases below was covered.


@pytest.fixture
def context_server():
    """An HTTP context server that counts what it is asked for.

    The failure this guards is slow and silent rather than loud: fetching a
    remote context per document works, so nothing fails, it just asks a
    third-party server once a row. Counting requests is the only way to see it.
    """
    hits: list[str] = []
    body = json.dumps({"@context": {"name": "http://xmlns.com/foaf/0.1/name"}}).encode()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            hits.append(self.path)
            self.send_response(200)
            self.send_header("Content-Type", "application/ld+json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/ctx.jsonld", hits
    finally:
        server.shutdown()


def test_a_list_member_naming_a_shipped_context_resolves_offline():
    with no_network():
        graph = load_jsonld([dict(a_work(), **{"@context": CONTEXT_URL})])

    assert set(
        graph.objects(None, URIRef("http://id.loc.gov/ontologies/bibframe/mainTitle"))
    ) == {Literal("The Elements of Style")}


def test_a_list_member_naming_an_unshipped_context_raises(context_server):
    """Rather than fetching it, which is what used to happen.

    `profile_refs` catches ValueError and logs, and `_names_a_remote_context`
    refuses first, so a profile is unaffected. The policy is that this library
    never fetches a context: it runs inside ORM flushes and across a sweep of a
    million rows, and neither can afford a request per document.
    """
    url, hits = context_server
    with pytest.raises(ValueError, match="must be absent or a URL"):
        load_jsonld([{"@context": url, "@id": "https://x/1", "name": "n"}])
    assert hits == [], "it refused without asking the server"


def test_a_list_member_with_no_context_keeps_its_terms():
    """It used to lose them, and invent one triple out of the loss.

    With no context `title` and `mainTitle` are unknown terms and get dropped,
    while `@type: ["Work"]` resolves "Work" as a relative IRI against the
    process's working directory -- a file:// triple that varies by where the
    process started.
    """
    graph = load_jsonld([a_work()])

    assert set(
        graph.objects(None, URIRef("http://id.loc.gov/ontologies/bibframe/mainTitle"))
    ) == {Literal("The Elements of Style")}
    assert not any(str(o).startswith("file://") for _, _, o in graph)
    # the same document as a dict gives the same graph
    assert to_isomorphic(graph) == to_isomorphic(load_jsonld(a_work()))


def test_a_list_member_is_held_to_the_same_rule():
    """A member may name our context or none, and nothing else.

    JSON-LD 1.1 §4.1 lets each node object in an array carry its own context,
    so each is checked on its own rather than the array being waved through.
    """
    ours = dict(a_work(), **{"@context": CONTEXT_URL})
    assert len(load_jsonld([ours, a_work()])) > 0

    with pytest.raises(ValueError, match="must be absent or a URL"):
        load_jsonld([ours, {"@context": {"x": "http://e.org/x"}, "@id": "https://x/2"}])


@pytest.mark.parametrize("shape", ["dict", "list"])
def test_parsing_with_a_context_keeps_the_prefix_bindings(shape):
    """rdflib replaces the store's prefixes with the supplied context's.

    LEGACY_CONTEXT reaches BIBFRAME through `@vocab` and declares no `bf`
    prefix, so handing it to the parser dropped the binding init_graph had just
    made and changed how every Turtle serialisation of the result reads. The
    triples were unaffected, which is why only a test asserting the bindings
    finds it.
    """
    document = a_work()
    graph = load_jsonld([document] if shape == "list" else document)

    assert graph.namespace_manager.store.namespace("bf") == URIRef(BF)
    assert graph.namespace_manager.store.namespace("bflc") == URIRef(BFLC)
    assert "bf:" in graph.serialize(format="turtle")


@pytest.mark.parametrize(
    "shape,accepted",
    [
        ("absent", True),
        ("managed-url", True),
        ("unmanaged-url", False),
        ("inline", False),
        ("list-of-managed-and-inline", False),
        ("import-inside-inline", False),
        ("scoped-context-on-a-term", False),
    ],
)
def test_only_two_shapes_of_context_are_accepted(shape, accepted, context_server):
    """The allowlist, and the forms it closes without naming them.

    The last two are why the rule is an allowlist rather than a check. Both put
    a URL inside an inline context, rdflib follows both, and neither is
    visible unless you know to look. Refusing inline contexts refuses them
    too, along with whatever else JSON-LD grows.
    """
    url, hits = context_server
    inline = {"name": "http://xmlns.com/foaf/0.1/name"}
    contexts = {
        "absent": None,
        "managed-url": CONTEXT_URL,
        "unmanaged-url": url,
        "inline": inline,
        "list-of-managed-and-inline": [CONTEXT_URL, inline],
        "import-inside-inline": {"@version": 1.1, "@import": url},
        "scoped-context-on-a-term": {
            "@version": 1.1,
            "outer": {"@id": "http://e.org/o", "@context": url},
        },
    }
    document = {"@id": "https://x/1", "name": "n"}
    if contexts[shape] is not None:
        document["@context"] = contexts[shape]

    if accepted:
        load_jsonld(document)
    else:
        with pytest.raises(ValueError):
            load_jsonld(document)

    assert hits == [], "a context was fetched"


def test_framing_refuses_an_unshipped_context_on_purpose():
    """And not because `requests` happens to be absent.

    `bibframe_json.document_loader()` falls through to pyld's default for a URL
    it does not ship, and pyld's default is whichever of these it settled on at
    import:

        try:    _default_document_loader = requests_document_loader()
        except ImportError:
            _default_document_loader = dummy_document_loader

    With `requests` absent that default raises, so the write path refused by
    luck. Anything pulling `requests` in transitively would have turned it into
    a real HTTP loader and framing would have begun fetching inside a flush,
    with nothing in this repo changing.

    The installed fallback raises ValueError; pyld's dummy raises JsonLdError.
    Asserting the type is what distinguishes ours being wired in from the
    accident that preceded it.
    """
    loader = jsonld.get_document_loader()

    with pytest.raises(ValueError, match="fetching one is not something"):
        loader("https://example.org/v1/context.jsonld", {})

    # and the shipped one still resolves through the same loader
    assert "@context" in loader(CONTEXT_URL, {})["document"]


def test_framing_and_reading_refuse_the_same_contexts(context_server):
    """One rule, both directions. Neither reaches the server."""
    url, hits = context_server
    document = {"@context": url, "@id": WORK, "@type": ["Work"]}

    with pytest.raises(ValueError, match="must be absent or a URL"):
        load_jsonld(dict(document))
    with pytest.raises(jsonld.JsonLdError):
        frame_jsonld(WORK, dict(document))

    assert hits == []


def test_a_list_member_that_is_not_a_node_object_is_refused(context_server):
    """The last way a list could reach the network.

    A non-dict member used to be handed to rdflib untouched, on the reasoning
    that rdflib would reject it. It does not: a string is read as a document
    in its own right, so a string holding JSON-LD with a remote `@context` was
    fetched, straight past the rule every dict member is held to.

    An array of anything but node objects is not JSON-LD anyway, so this is a
    refusal rather than a special case.
    """
    url, hits = context_server
    document = json.dumps({"@context": url, "@id": "https://x/1", "n": "v"})

    with pytest.raises(TypeError, match="array holds node objects"):
        load_jsonld([document])

    assert hits == [], "the string member was read as a document and fetched"
