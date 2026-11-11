import gzip
import io
import re
import subprocess
import tarfile
from pathlib import Path

import diff_save
import pytest
from diff_save import (
    Step,
    _name_rows,
    _relabel,
    _sort_arrays,
    _violations,
    compare,
    graph_digest,
    iter_corpus,
    load,
    mask,
    mask_data,
    select_members,
    sequences,
    snapshot,
    write_sample,
)
from rdflib import RDF, RDFS, BNode, Graph, Literal, URIRef
from sqlalchemy import text
from sqlalchemy.orm import sessionmaker

from bluecore_models.bluecore_graph import Resolution
from bluecore_models.namespaces import BF as BF_NS

BF = "http://id.loc.gov/ontologies/bibframe/"
DATA = Path(__file__).parent / "data"


def _snapshot(code: str, rows: list) -> dict:
    return {"code": code, "rows": rows, "links": [], "records": []}


def _rdfxml(n: int) -> bytes:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"'
        ' xmlns:bf="http://id.loc.gov/ontologies/bibframe/">'
        f'<bf:Work rdf:about="http://id.loc.gov/resources/works/{n}"/>'
        "</rdf:RDF>\n"
    ).encode()


@pytest.fixture
def uploads(tmp_path: Path) -> Path:
    """Two batches of three one-triple records, the shape of uploads/."""
    for batch in (1, 2):
        with tarfile.open(tmp_path / f"batch_0000{batch}.tar.gz", "w:gz") as tar:
            # added out of order, so select_members has to sort them
            for n in (3, 1, 2):
                data = _rdfxml(batch * 10 + n)
                info = tarfile.TarInfo(f"data2/{batch * 10 + n}.cbd.rdf")
                info.size = len(data)
                # what a real tarball carries, which the sample must not
                info.mtime = 1_700_000_000
                info.uid = info.gid = 1000
                info.uname = info.gname = "cataloger"
                info.mode = 0o600
                tar.addfile(info, io.BytesIO(data))
    return tmp_path


def test_mask_data_replaces_minted_uris_and_admin_metadata_dates():
    masked = mask_data(
        {
            "@id": "https://bcld.info/works/123",
            "adminMetadata": [{"date": "2026-11-06T10:00:00+00:00"}],
            "originDate": "1999-01-01",
        },
        {"https://bcld.info/works/123": "minted:http://id.loc.gov/resources/works/9"},
    )
    assert "minted:http://id.loc.gov/resources/works/9" in masked
    assert "<date>" in masked
    assert "1999-01-01" in masked
    assert "bcld.info/works/123" not in masked
    assert "2026-11-06" not in masked


def test_graph_digest_ignores_blank_node_labels():
    def graph(title: BNode) -> Graph:
        g = Graph()
        work = URIRef("http://id.loc.gov/resources/works/1")
        g.add((work, URIRef(BF + "title"), title))
        g.add((title, URIRef(BF + "mainTitle"), Literal("A Title")))
        return g

    assert graph_digest(graph(BNode()), {}) == graph_digest(graph(BNode()), {})


def test_graph_digest_sees_a_changed_literal():
    def graph(title: str) -> Graph:
        g = Graph()
        node = BNode()
        g.add(
            (URIRef("http://id.loc.gov/resources/works/1"), URIRef(BF + "title"), node)
        )
        g.add((node, URIRef(BF + "mainTitle"), Literal(title)))
        return g

    assert graph_digest(graph("A Title"), {}) != graph_digest(graph("B Title"), {})


def _citing(uri: str) -> Graph:
    g = Graph()
    g.add(
        (
            URIRef("http://id.loc.gov/resources/works/1"),
            URIRef(BF + "subject"),
            URIRef(uri),
        )
    )
    return g


def test_graph_digest_takes_a_uri_rdflib_will_not_write():
    dnb = "https://portal.dnb.de/opac.htm?method=simpleSearch&query=idn=004071727"

    assert graph_digest(_citing(" " + dnb), {}) != graph_digest(_citing(dnb), {})


def test_graph_digest_tells_an_encoded_uri_from_its_raw_form():
    assert graph_digest(_citing("http://x/a b"), {}) != graph_digest(
        _citing("http://x/a%20b"), {}
    )


def test_compare_reports_a_changed_row():
    uri = "minted:http://id.loc.gov/resources/works/9"
    a = _snapshot("baseline", [["works", uri, None, None, "aaa"]])
    b = _snapshot("branch", [["works", uri, None, None, "bbb"]])

    differences = compare(a, b)

    assert len(differences) == 1
    assert uri in differences[0]


def test_compare_reports_a_changed_version_count():
    uri = "minted:http://id.loc.gov/resources/works/9"
    a = _snapshot("baseline", []) | {"versions": [[uri, ["aaa"]]]}
    b = _snapshot("branch", []) | {"versions": [[uri, ["aaa", "aaa"]]]}

    differences = compare(a, b)

    assert len(differences) == 1
    assert uri in differences[0]


def test_sample_is_byte_identical_when_written_twice(uploads: Path, tmp_path: Path):
    a = tmp_path / "a.tar.gz"
    b = tmp_path / "b.tar.gz"

    assert write_sample(uploads, 2, a) == write_sample(uploads, 2, b)
    assert a.read_bytes() == b.read_bytes()

    # the gzip header's MTIME field (bytes 4-8) is zero, not the time of writing
    assert a.read_bytes()[4:8] == b"\0\0\0\0"
    with tarfile.open(a) as tar:
        members = tar.getmembers()
    assert members[0].name == "MANIFEST.txt"
    for member in members:
        assert (member.mtime, member.uid, member.gid) == (0, 0, 0), member.name
        assert (member.uname, member.gname) == ("", ""), member.name
        assert member.mode == 0o644, member.name
    assert gzip.decompress(a.read_bytes())


def test_iter_corpus_reads_the_sample_in_manifest_order(uploads: Path, tmp_path: Path):
    sample = tmp_path / "sample.tar.gz"
    write_sample(uploads, 2, sample)

    assert [name for name, _, _ in iter_corpus(sample, None)] == [
        name for name, _ in select_members(uploads, 2)
    ]
    assert [name for name, _, _ in iter_corpus(sample, 1)] == [
        name for name, _ in select_members(uploads, 1)
    ]


def test_compare_refuses_snapshots_from_the_same_code():
    a = _snapshot("same", [])
    b = _snapshot("same", [])

    with pytest.raises(ValueError):
        compare(a, b)
    assert compare(a, b, same_code=True) == []


def test_compare_refuses_the_same_code_hash_from_different_paths():
    a = _snapshot("/baseline/src", []) | {"code_sha256": "abc"}
    b = _snapshot("/branch/src", []) | {"code_sha256": "abc"}

    with pytest.raises(ValueError):
        compare(a, b)
    assert compare(a, b, same_code=True) == []


def test_compare_refuses_different_selections():
    a = _snapshot("baseline", []) | {"selection": {"corpus": "a", "per_batch": 25}}
    b = _snapshot("branch", []) | {"selection": {"corpus": "a", "per_batch": None}}

    with pytest.raises(ValueError):
        compare(a, b)


def test_compare_sees_a_minted_uri_under_another_prefix():
    name = "{minted:http://id.loc.gov/resources/works/9}"
    a = _snapshot("baseline", [["works", f"https://bcld.info/works/{name}"]])
    b = _snapshot("branch", [["works", f"https://bcld.info/instances/{name}"]])

    assert compare(a, b)


WORK_UUID = "0b9f9e3c-2f5e-4c4b-9a43-1d2e3f4a5b6c"
WORK = f"https://bcld.info/works/{WORK_UUID}"


def _derived(source: str) -> dict:
    return {"adminMetadata": [{"derivedFrom": [{"@id": source}]}]}


def test_canonical_names_keep_the_shape_of_a_minted_uri():
    source = "http://id.loc.gov/resources/works/9"
    names = _name_rows([("works", 1, WORK, _derived(source))], {})

    assert names == {WORK: f"https://bcld.info/works/{{minted:{source}}}"}


def test_canonical_names_tell_colliding_rows_apart():
    source = "http://id.loc.gov/resources/works/9"
    other = "https://bcld.info/works/7c9e6679-7425-40de-944b-e07fc1f90ae7"
    names = _name_rows(
        [("works", 2, WORK, _derived(source)), ("works", 1, other, _derived(source))],
        {},
    )

    assert names[other].endswith(f"{{minted:{source}#1}}")
    assert names[WORK].endswith(f"{{minted:{source}#2}}")


def test_canonical_names_refuse_a_row_with_no_stable_name():
    with pytest.raises(ValueError, match=WORK):
        _name_rows([("works", 1, WORK, {"adminMetadata": []})], {})


def test_a_uri_a_save_would_not_mint_is_a_violation():
    source = "http://id.loc.gov/resources/works/9"
    rows = [
        ("works", 1, WORK, _derived(source)),
        # filed under the wrong kind
        ("works", 2, f"https://bcld.info/instances/{WORK_UUID[:-1]}d", _derived("a")),
        # not minted at all
        ("works", 3, source + "0", _derived("b")),
    ]
    names = _name_rows(rows, {})

    assert [v[1] for v in _violations(rows, names)] == [names[r[2]] for r in rows[1:]]


def test_mask_leaves_unnamed_uris_and_longer_ones_alone():
    names = {WORK: "minted:x"}
    text = f"{WORK} {WORK}/extra https://bcld.info/works/other {WORK}."

    assert mask(text, names) == (
        f"minted:x {WORK}/extra https://bcld.info/works/other minted:x."
    )


def test_mask_data_leaves_dates_on_resources_it_did_not_mint():
    data = {
        "@id": "http://id.loc.gov/resources/works/9",
        "adminMetadata": [{"date": ["2026-11-06"]}],
    }

    assert "2026-11-06" in mask_data(data, {})


def test_mask_data_leaves_dates_not_in_a_generated_form():
    data = {"@id": WORK, "adminMetadata": [{"date": ["2026-11-06", "1999"]}]}

    masked = mask_data(data, {WORK: "minted:x"})

    assert "2026-11-06" not in masked
    assert '"1999"' in masked


def test_sort_arrays_keeps_duplicates_and_leaves_lists_alone():
    data = {"note": ["b", "a", "b"], "parts": {"@list": ["b", "a", "b"]}}

    assert _sort_arrays(data) == {
        "note": ["a", "b", "b"],
        "parts": {"@list": ["b", "a", "b"]},
    }


def test_relabel_keeps_which_nodes_share_a_label():
    shared = {"a": {"@id": "_:b7"}, "b": {"@id": "_:b7"}, "c": {"@id": "_:b3"}}

    assert _relabel(shared, {}) == {
        "a": {"@id": "_:b0"},
        "b": {"@id": "_:b0"},
        "c": {"@id": "_:b1"},
    }


def _parsed(name: str, format: str) -> Step:
    def build(_: sessionmaker) -> Graph:
        g = Graph()
        g.parse(DATA / name, format=format)
        return g

    return f"tests/data/{name}", build, None


def _clear(pg_session: sessionmaker) -> None:
    """
    Remove every resource row, the conftest seeds included. A bulk ORM delete
    of a joined-table subclass leaves its resource_base rows behind, and their
    URIs then collide with the next load's inserts, so truncate the tables.
    """
    with pg_session() as session:
        session.execute(
            text(
                "TRUNCATE TABLE bibframe_other_resources, resource_bibframe_classes, "
                "versions, instances, works, hubs, other_resources, resource_base "
                "RESTART IDENTITY CASCADE"
            )
        )
        session.commit()


def _load_and_snapshot(pg_session: sessionmaker) -> dict:
    _clear(pg_session)
    steps = [
        _parsed("23807141.ttl", "turtle"),
        _parsed("19167709.cbd.jsonld", "json-ld"),
        _parsed("marc2bibframe2-dlc.rdf", "xml"),
        *sequences(),
    ]
    outcomes, created = load(pg_session, steps, passes=2)
    return snapshot(pg_session, outcomes, created, "this code")


def test_snapshot_is_identical_across_two_runs(pg_session):
    a = _load_and_snapshot(pg_session)
    b = _load_and_snapshot(pg_session)

    # sanity: the runs wrote something worth comparing
    assert {row[0] for row in a["rows"]} >= {"works", "instances", "hubs"}
    assert a["links"]
    assert a["versions"]
    assert a["bf_classes"]
    # every minted uri has the shape a save mints, and a uuid to match it
    assert a["violations"] == []
    assert {row[5] for row in a["rows"] if row[0] != "other_resources"} == {
        "last-segment"
    }
    assert compare(a, b, same_code=True) == []


def test_compare_sees_a_changed_save_end_to_end(pg_session, monkeypatch):
    steps = [
        _parsed("23807141.ttl", "turtle"),
        _parsed("19167709.cbd.jsonld", "json-ld"),
    ]

    def load_and_snapshot() -> dict:
        _clear(pg_session)
        outcomes, created = load(pg_session, steps, passes=1)
        return snapshot(pg_session, outcomes, created, "this code")

    a = load_and_snapshot()
    assert compare(a, load_and_snapshot(), same_code=True) == []

    original = Resolution._generate_admin_metadata

    def with_a_note(self, bluecore_uri, *args, **kwargs):
        original(self, bluecore_uri, *args, **kwargs)
        self.graph.add((bluecore_uri, RDFS.comment, Literal("an extra triple")))

    monkeypatch.setattr(Resolution, "_generate_admin_metadata", with_a_note)
    b = load_and_snapshot()

    differences = compare(a, b, same_code=True)
    assert any(d.startswith("rows ") for d in differences), differences
    assert any(d.startswith("records ") for d in differences), differences


def test_failed_record_is_recorded_as_an_outcome(pg_session):
    def unconnected_works(_: sessionmaker) -> Graph:
        g = Graph()
        g.add((BNode(), RDF.type, BF_NS.Work))
        g.add((BNode(), RDF.type, BF_NS.Work))
        return g

    outcomes, _ = load(pg_session, [("unconnected", unconnected_works, BF_NS.Work)])

    assert [o["status"] for o in outcomes] == ["BluecoreGraphError"] * 2
    assert all(o["graph"] is None for o in outcomes)


def test_a_graph_that_cannot_be_digested_is_an_outcome(pg_session, monkeypatch):
    def fail(g: Graph, names: dict[str, str]) -> str:
        raise ValueError("can't digest this")

    monkeypatch.setattr(diff_save, "graph_digest", fail)
    outcomes, _ = load(pg_session, sequences()[:1], passes=1)

    assert [(o["status"], o["graph"]) for o in outcomes] == [("DigestError", None)]
    assert "can't digest this" in outcomes[0]["message"]


def test_sequences_run_without_errors_escaping(pg_session):
    outcomes, _ = load(pg_session, sequences())

    assert len(outcomes) == 2 * len(sequences())
    for outcome in outcomes:
        status = outcome["status"]
        assert status == "ok" or status.isidentifier(), outcome


def test_a_refused_step_is_part_of_the_sequences(pg_session):
    outcomes, _ = load(pg_session, sequences(), passes=1)

    refused = [o for o in outcomes if o["name"].startswith("refused/")]
    assert [o["status"] for o in refused] == ["BluecoreGraphError"]


# --- run --docker ----------------------------------------------------------


class _FakeDocker:
    """Stands in for subprocess.run, recording each docker command."""

    def __init__(self, missing: bool = False):
        self.calls: list[list[str]] = []
        self.missing = missing

    def __call__(self, cmd, **kwargs):
        self.calls.append(list(cmd))
        if self.missing:
            raise FileNotFoundError("docker")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    def verbs(self) -> list[str]:
        return [call[1] for call in self.calls]


@pytest.fixture
def fake_docker(monkeypatch):
    fake = _FakeDocker()
    monkeypatch.setattr(diff_save.subprocess, "run", fake)
    monkeypatch.setattr(diff_save, "_wait_ready", lambda url, timeout: None)
    return fake


def test_docker_starts_a_named_published_removable_container(fake_docker):
    with diff_save.throwaway_postgres() as url:
        started = next(c for c in fake_docker.calls if c[1] == "run")
        assert "--rm" in started
        name = started[started.index("--name") + 1]
        assert re.fullmatch(r"diff-save-[0-9a-f]+", name)
        published = started[started.index("-p") + 1]
        port, container_port = published.split(":")[1:]
        assert container_port == "5432" and int(port) > 0
        assert "postgres:16" in started
        assert url.endswith(f"localhost:{port}/postgres")


def test_docker_containers_get_unique_names(fake_docker):
    names = []
    for _ in range(2):
        with diff_save.throwaway_postgres():
            pass
    for call in fake_docker.calls:
        if call[1] == "run":
            names.append(call[call.index("--name") + 1])
    assert len(set(names)) == 2


def test_docker_removes_the_container_after_success(fake_docker):
    with diff_save.throwaway_postgres():
        pass
    started = next(c for c in fake_docker.calls if c[1] == "run")
    name = started[started.index("--name") + 1]
    assert fake_docker.calls[-1][1:] == ["rm", "-f", name]


def test_docker_removes_the_container_when_the_run_raises(fake_docker):
    with pytest.raises(RuntimeError, match="boom"), diff_save.throwaway_postgres():
        raise RuntimeError("boom")
    assert fake_docker.calls[-1][1:2] == ["rm"]


def test_docker_removes_the_container_on_ctrl_c(fake_docker):
    with pytest.raises(KeyboardInterrupt), diff_save.throwaway_postgres():
        raise KeyboardInterrupt
    assert fake_docker.calls[-1][1:2] == ["rm"]


def test_docker_removes_the_container_when_it_never_becomes_ready(
    fake_docker, monkeypatch
):
    def never(url, timeout):
        raise diff_save.DockerError("postgres didn't accept connections")

    monkeypatch.setattr(diff_save, "_wait_ready", never)
    with pytest.raises(diff_save.DockerError), diff_save.throwaway_postgres():
        pytest.fail("the body must not run")
    assert fake_docker.calls[-1][1:2] == ["rm"]


def test_a_missing_docker_gives_a_one_line_error(monkeypatch, capsys, tmp_path):
    fake = _FakeDocker(missing=True)
    monkeypatch.setattr(diff_save.subprocess, "run", fake)
    sample = tmp_path / "s.tar.gz"
    sample.write_bytes(b"x")
    code = diff_save.main(
        ["run", "--docker", "--corpus", str(sample), "--out", str(tmp_path / "o.json")]
    )
    err = capsys.readouterr().err.strip()
    assert code == 2
    assert len(err.splitlines()) == 1 and "docker" in err
    assert fake.verbs() == ["info"]


def test_docker_and_database_url_are_mutually_exclusive():
    with pytest.raises(SystemExit):
        diff_save.main(
            ["run", "--docker", "--database-url", "x", "--corpus", "c", "--out", "o"]
        )


def test_run_needs_docker_or_a_database_url():
    with pytest.raises(SystemExit):
        diff_save.main(["run", "--corpus", "c", "--out", "o"])


def test_database_url_never_touches_docker(monkeypatch, tmp_path):
    fake = _FakeDocker()
    monkeypatch.setattr(diff_save.subprocess, "run", fake)
    monkeypatch.setattr(diff_save, "_snapshot_run", lambda *a, **k: 0)
    sample = tmp_path / "s.tar.gz"
    sample.write_bytes(b"x")
    code = diff_save.main(
        [
            "run",
            "--database-url",
            "postgresql+psycopg://u@localhost/db",
            "--corpus",
            str(sample),
            "--out",
            str(tmp_path / "o.json"),
        ]
    )
    assert code == 0 and fake.calls == []


def test_docker_run_removes_the_container_when_the_load_fails(
    fake_docker, monkeypatch, tmp_path
):
    def fail(*args, **kwargs):
        raise RuntimeError("load failed")

    monkeypatch.setattr(diff_save, "_snapshot_run", fail)
    sample = tmp_path / "s.tar.gz"
    sample.write_bytes(b"x")
    with pytest.raises(RuntimeError, match="load failed"):
        diff_save.main(
            [
                "run",
                "--docker",
                "--corpus",
                str(sample),
                "--out",
                str(tmp_path / "o.json"),
            ]
        )
    assert fake_docker.verbs() == ["info", "run", "rm"]
