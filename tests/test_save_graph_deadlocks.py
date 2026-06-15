import random
import threading

from psycopg2 import errors as pg_errors
from rdflib import Graph
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from bluecore_models.bluecore_graph import BluecoreGraph, save_graph
from bluecore_models.models import Instance, OtherResource, Work, BibframeOtherResources
from bluecore_models.namespaces import BF
from bluecore_models.utils.graph import load_jsonld


jsonld_context = {
    "@vocab": "http://id.loc.gov/ontologies/bibframe/",
    "bflc": "http://id.loc.gov/ontologies/bflc/",
    "mads": "http://www.loc.gov/mads/rdf/v1#",
    "rdfs": "http://www.w3.org/2000/01/rdf-schema#",
}


def _remove_fixtures(pg_session):
    with pg_session() as session:
        session.query(Instance).delete()
        session.query(Work).delete()
        session.query(BibframeOtherResources).delete()
        session.query(OtherResource).delete()
        session.commit()


# ---------------------------------------------------------------------------
# #1 Deterministic write ordering
# ---------------------------------------------------------------------------
def test_save_writes_other_resources_in_sorted_order(pg_session, monkeypatch):
    """
    Resources must be written in a stable (sorted-by-URI)
    order so concurrent transactions acquire the hot Other Resource rows in the
    same order and serialize instead of deadlocking.

    We spy on Session.add and assert the Other Resources are added in
    ascending-URI order. Currently they are added in arbitrary rdflib graph
    order, so this fails.
    """
    _remove_fixtures(pg_session)

    added_other_uris: list[str] = []
    original_add = Session.add

    def spy_add(self, obj, *args, **kwargs):
        if isinstance(obj, OtherResource):
            added_other_uris.append(obj.uri)
        return original_add(self, obj, *args, **kwargs)

    monkeypatch.setattr(Session, "add", spy_add)

    g = Graph()
    g.parse("tests/23807141.ttl")
    BluecoreGraph(g).save(pg_session)

    assert len(added_other_uris) > 1, "sanity: multiple Other Resources were written"
    assert added_other_uris == sorted(added_other_uris), (
        "Other Resources should be written in deterministic sorted-by-URI order "
        f"but were written as: {added_other_uris}"
    )


# ---------------------------------------------------------------------------
# #5 Built-in serialization-failure retry
# ---------------------------------------------------------------------------
class _FlakySessionMaker:
    """
    Wraps a real sessionmaker. For the first `fail_times` sessions it produces,
    the session's commit() raises an OperationalError wrapping a Postgres
    DeadlockDetected (as SQLAlchemy surfaces a real deadlock). Subsequent
    sessions commit normally.
    """

    def __init__(self, real_maker, fail_times=1):
        self._real = real_maker
        self._fail_times = fail_times
        self.calls = 0

    def __call__(self, *args, **kwargs):
        self.calls += 1
        session = self._real(*args, **kwargs)
        if self.calls <= self._fail_times:
            def boom():
                raise OperationalError(
                    "UPDATE resource_base SET data=%(data)s::JSONB ...",
                    {},
                    pg_errors.DeadlockDetected("deadlock detected"),
                )

            session.commit = boom
        return session


def test_save_retries_on_deadlock(pg_session):
    """
    A DeadlockDetected/SerializationFailure should trigger an
    automatic re-run of the whole per-graph save, since it is self-contained.

    With a session whose first commit() raises a deadlock, save() should retry
    on a fresh session and ultimately persist the Work. The current code has no
    retry, so the OperationalError propagates and nothing is saved.
    """
    _remove_fixtures(pg_session)

    flaky = _FlakySessionMaker(pg_session, fail_times=1)

    g = Graph()
    g.parse("tests/23807141.ttl")

    # should NOT raise: the deadlock on the first attempt is retried
    BluecoreGraph(g).save(flaky)

    assert flaky.calls >= 2, "save() should have retried after the deadlock"

    with pg_session() as session:
        assert session.query(Work).count() == 2, "Works persisted after retry"


# ---------------------------------------------------------------------------
# #3 Atomic upsert instead of SELECT-then-INSERT (get-or-create race)
# ---------------------------------------------------------------------------
SHARED_OTHER_URI = "http://id.loc.gov/test/shared-authority"


def _work_referencing_shared_other(work_uuid: str, other_uri: str) -> Graph:
    """A minimal Work graph that references the given shared Other Resource."""
    return load_jsonld(
        {
            "@context": jsonld_context,
            "@id": f"https://bcld.info/works/{work_uuid}",
            "@type": BF.Work,
            "title": {"@type": "Title", "mainTitle": "Gravity's Rainbow"},
            "subject": {
                "@id": other_uri,
                "@type": "Topic",
                "rdfs:label": "Shared Authority",
            },
        }
    )


def test_concurrent_first_time_create_of_same_uri(pg_session):
    """
    Acceptance criterion #3: concurrent first-time creation of the same Other
    Resource uri must not raise a unique-constraint error.

    Several concurrent writers each save a distinct Work that references the
    SAME brand-new Other Resource. The current code does
    `session.query(...).where(uri == ...).first()` then INSERTs on a miss, so
    multiple writers miss and all INSERT the same uri -> a unique-constraint
    IntegrityError. A barrier lines the writers up so they all run their
    get-or-create before any commits, which makes the race fire reliably
    against the current code.

    Each round uses a fresh, never-before-seen authority uri so that every
    round is a genuine *first-time* creation (the acceptance scenario), run
    several times to make the race reliably reproducible.
    """
    _remove_fixtures(pg_session)

    writers = 5
    rounds = 5
    failures: list[tuple[str, str]] = []
    failures_lock = threading.Lock()

    for round_num in range(rounds):
        other_uri = f"{SHARED_OTHER_URI}/{round_num}"

        barrier = threading.Barrier(writers)

        def writer(idx: int, other_uri: str = other_uri) -> None:
            work_uuid = f"{round_num:02d}{idx:02d}0000-0000-0000-0000-000000000000"
            graph = _work_referencing_shared_other(work_uuid, other_uri)
            barrier.wait()  # all writers do get-or-create before anyone commits
            try:
                save_graph(pg_session, graph)
            except Exception as exc:  # noqa: BLE001
                orig = getattr(exc, "orig", None)
                with failures_lock:
                    failures.append((type(exc).__name__, type(orig).__name__))

        threads = [
            threading.Thread(target=writer, args=(idx,)) for idx in range(writers)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

    assert failures == [], (
        "concurrent first-time creation of the same uri should not raise, "
        f"but {len(failures)} writer(s) failed: {failures[:5]}"
    )

    # every round's authority should have been created exactly once
    with pg_session() as session:
        for round_num in range(rounds):
            others = (
                session.query(OtherResource)
                .where(OtherResource.uri == f"{SHARED_OTHER_URI}/{round_num}")
                .all()
            )
            assert len(others) == 1, (
                "concurrent first-time creation should yield exactly one row for "
                f"{SHARED_OTHER_URI}/{round_num}, found {len(others)}"
            )


# ---------------------------------------------------------------------------
# Acceptance: concurrent writers sharing authorities complete without deadlock
# ---------------------------------------------------------------------------
_NUM_SHARED_AUTHORITIES = 10
_NUM_CONCURRENT_WRITERS = 5
_NUM_ROUNDS = 5

_SHARED_AUTHORITIES = [
    f"http://id.loc.gov/test/auth/{i:02d}" for i in range(_NUM_SHARED_AUTHORITIES)
]


def _seed_authorities(pg_session) -> None:
    """Pre-create the shared authority rows so every save UPDATEs (locks) them."""
    with pg_session() as session:
        for i, uri in enumerate(_SHARED_AUTHORITIES):
            session.add(
                OtherResource(
                    uri=uri,
                    data={"seed": i},
                    type="other_resources",
                    is_profile=False,
                )
            )
        session.commit()


def _work_referencing_all_authorities(work_uuid: str, order: list[str], salt: str) -> Graph:
    """
    A Work that references every shared authority (as a subject), in the given
    order, with labels that differ from the seed so a real UPDATE is forced on
    each hot row.
    """
    return load_jsonld(
        {
            "@context": jsonld_context,
            "@id": f"https://bcld.info/works/{work_uuid}",
            "@type": BF.Work,
            "title": {"@type": "Title", "mainTitle": f"Work {salt}"},
            "subject": [
                {"@id": uri, "@type": "Topic", "rdfs:label": f"label-{uri}-{salt}"}
                for uri in order
            ],
        }
    )


def test_concurrent_writers_sharing_authorities_no_deadlock(pg_session):
    """
    Two or more concurrent save_graph() writers loading records that 
    share authorities must complete without DeadlockDetected.

    Each round launches several writers concurrently. Every writer saves a
    distinct Work that references the SAME set of pre-seeded shared authorities,
    but in a different (shuffled) order and with distinct label data so each
    save takes a write lock on every hot row. Because the current code locks
    those rows in arbitrary graph order, concurrent writers acquire them in
    conflicting orders and Postgres raises DeadlockDetected (verified to occur
    in every round against the current implementation).

    """
    _remove_fixtures(pg_session)
    _seed_authorities(pg_session)

    failures: list[tuple[str, str]] = []
    failures_lock = threading.Lock()

    for round_num in range(_NUM_ROUNDS):

        def writer(idx: int) -> None:
            order = _SHARED_AUTHORITIES[:]
            random.Random(round_num * 1000 + idx).shuffle(order)
            work_uuid = f"{round_num:02d}{idx:02d}0000-0000-0000-0000-000000000000"
            graph = _work_referencing_all_authorities(
                work_uuid, order, salt=f"{round_num}-{idx}"
            )
            try:
                save_graph(pg_session, graph)
            except Exception as exc:  # noqa: BLE001 - record anything that surfaces
                orig = getattr(exc, "orig", None)
                with failures_lock:
                    failures.append((type(exc).__name__, type(orig).__name__))

        threads = [
            threading.Thread(target=writer, args=(idx,))
            for idx in range(_NUM_CONCURRENT_WRITERS)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

    assert failures == [], (
        "concurrent writers sharing authorities should complete without "
        f"DeadlockDetected, but {len(failures)} writer(s) failed: {failures[:5]}"
    )

    # sanity: the shared authorities still exist exactly once each
    with pg_session() as session:
        assert (
            session.query(OtherResource)
            .where(OtherResource.uri.in_(_SHARED_AUTHORITIES))
            .count()
            == _NUM_SHARED_AUTHORITIES
        )
