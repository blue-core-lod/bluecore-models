from pathlib import Path
from typing import Literal

import pytest
from policy_cases import CASES, ROWS, Answer, PolicyCase, render_document
from rdflib import BNode, URIRef
from sqlalchemy.orm import sessionmaker

from bluecore_models.bluecore_graph import (
    Action,
    BluecoreGraph,
    Citation,
    Kind,
    Role,
    _admin_nodes,
    _citation,
)


def _noop(session_maker: sessionmaker) -> None:
    return None


def _case(
    case_id: str,
    row: str,
    sentence: str,
    decision: Literal["chosen", "inherited", "undecided"],
    answer: Answer = Citation.OTHER,
) -> PolicyCase:
    return PolicyCase(
        id=case_id,
        row=row,
        situation=f"situation of {case_id}",
        outcome=f"outcome of {case_id}",
        sentence=sentence,
        decision=decision,
        follow_up="#169" if decision == "inherited" else None,
        run=_noop,
        answer=answer,
        check=lambda: answer,
    )


def test_document_puts_each_case_under_its_row():
    later = _case("a-resource", "R2", "A described resource is saved.", "chosen")
    earlier = _case("a-link", "L1", "A described citation is linked.", "inherited")

    doc = render_document([later, earlier])

    # R rows come before L rows in ROWS, whatever order the cases are given in
    assert list(ROWS).index("R2") < list(ROWS).index("L1")
    assert (
        doc.index("## R2: ") < doc.index("A described resource") < doc.index("## L1: ")
    )
    assert doc.index("## L1: ") < doc.index("A described citation")
    assert "**situation of a-resource.** A described resource is saved." in doc
    assert "Today: outcome of a-resource." in doc
    assert "Decision: *inherited*, #169." in doc
    assert "Decision: *chosen*." in doc
    assert doc.endswith(".\n") and not doc.endswith("\n\n")


def test_document_says_what_the_code_does():
    resource = _case(
        "a-stub",
        "R3",
        "A stub is saved.",
        "chosen",
        answer=(Kind.WORK, Role.STUB, Action.INSERT),
    )
    inline = _case(
        "a-blank", "R1", "It stays inline.", "chosen", (Kind.WORK, Role.INLINE, None)
    )
    link = _case("a-link", "L1", "It is linked.", "chosen", Citation.OTHER)

    doc = render_document([resource, inline, link])

    assert "In the code: `Kind.WORK`, `Role.STUB`, `Action.INSERT`." in doc
    assert "In the code: `Kind.WORK`, `Role.INLINE`." in doc
    assert "In the code: `Citation.OTHER`." in doc


def test_published_document_matches_the_cases():
    published = (Path(__file__).parent.parent / "docs" / "save-policy.md").read_text()
    assert published == render_document(CASES), (
        "docs/save-policy.md is out of date: run "
        "uv run python tests/policy_cases.py > docs/save-policy.md"
    )


def test_case_ids_are_unique():
    ids = [case.id for case in CASES]
    assert len(ids) == len(set(ids))


def test_every_table_row_has_a_case():
    assert {case.row for case in CASES} == set(ROWS)


def test_every_case_is_marked():
    assert [case.id for case in CASES if case.decision == "undecided"] == []


@pytest.mark.parametrize("case", CASES, ids=[c.id for c in CASES])
def test_policy_answer(case: PolicyCase):
    """What the code decides for the case, worked out with no database."""
    assert case.check() == case.answer


def _agrees(case: PolicyCase, answer: Answer, last: BluecoreGraph) -> bool:
    """
    Whether the last real save of a case decided what answer says: for a
    citation, of every uri its Works and Instances cite through the case's
    predicate; for a resource, of every resource of its kind with its role.
    """
    if isinstance(answer, Citation):
        citations = [
            _citation(last.graph, s, p, o, _admin_nodes(entry.graph))
            for kind in (Kind.WORK, Kind.INSTANCE)
            for entry in last._entries[kind]
            for s, p, o in entry.graph
            if p == case.via and isinstance(o, URIRef)
        ]
        return bool(citations) and all(c == answer for c in citations)
    kind, role, action = answer
    if role == Role.INLINE:
        # it got no entry: the only blank node with one is the resource a
        # write is creating
        return any(
            isinstance(node, BNode) and k == kind and r == Role.INLINE
            for (node, k), r in last._roles.items()
        ) and all(
            entry.role == Role.PRIMARY
            for entry in last._entries[kind]
            if isinstance(entry.original, BNode)
        )
    actions = [
        last._actions.get(str(entry.subject))
        for entry in last._entries[kind]
        if entry.role == role
    ]
    return bool(actions) and all(a == action for a in actions)


def _last_save(case: PolicyCase, pg_session, monkeypatch) -> BluecoreGraph:
    """Run the case and return the BluecoreGraph its last save used."""
    saved: list[BluecoreGraph] = []
    save = BluecoreGraph.save

    def recording_save(self, *args, **kwargs):
        saved.append(self)
        return save(self, *args, **kwargs)

    monkeypatch.setattr(BluecoreGraph, "save", recording_save)
    case.run(pg_session)
    return saved[-1]


@pytest.mark.parametrize("case", CASES, ids=[c.id for c in CASES])
def test_policy_case(case: PolicyCase, pg_session, monkeypatch):
    last = _last_save(case, pg_session, monkeypatch)

    # the real save decided what the case's answer says
    assert _agrees(case, case.answer, last)


def _by_id(case_id: str) -> PolicyCase:
    return next(case for case in CASES if case.id == case_id)


def test_the_real_save_check_rejects_a_wrong_citation(pg_session, monkeypatch):
    # every saved Work cites bf:Work through rdf:type, a vocabulary term
    case = _by_id("link-held-by-uri")
    last = _last_save(case, pg_session, monkeypatch)

    assert not _agrees(case, Citation.VOCABULARY, last)


def test_every_citation_case_names_its_predicate():
    assert [
        case.id
        for case in CASES
        if isinstance(case.answer, Citation) and case.via is None
    ] == []
