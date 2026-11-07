from typing import Literal

import pytest
from policy_cases import CASES, ROWS, PolicyCase, render_markdown
from sqlalchemy.orm import sessionmaker


def _noop(session_maker: sessionmaker) -> None:
    return None


def _case(
    case_id: str,
    row: str,
    sentence: str,
    decision: Literal["chosen", "inherited", "undecided"],
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
    )


def test_render_markdown_lists_each_case_under_its_row():
    later = _case("a-resource", "R2", "A described resource is saved.", "chosen")
    earlier = _case("a-link", "L1", "A described citation is linked.", "inherited")

    table = render_markdown([later, earlier])
    lines = table.splitlines()

    assert lines[0].split("|")[1:-1] == [
        " Row ",
        " Situation ",
        " Today ",
        " For catalogers ",
        " Decision ",
    ]
    assert "A described resource is saved." in table
    assert "A described citation is linked." in table
    # R rows come before L rows in ROWS, whatever order the cases are given in
    assert list(ROWS).index("R2") < list(ROWS).index("L1")
    assert table.index("A described resource") < table.index("A described citation")
    inherited_line = next(line for line in lines if "A described citation" in line)
    assert "inherited" in inherited_line
    assert "#169" in inherited_line
    chosen_line = next(line for line in lines if "A described resource" in line)
    assert "chosen" in chosen_line
    assert "inherited" not in chosen_line


def test_case_ids_are_unique():
    ids = [case.id for case in CASES]
    assert len(ids) == len(set(ids))


def test_every_table_row_has_a_case():
    assert {case.row for case in CASES} == set(ROWS)


@pytest.mark.parametrize("case", CASES, ids=[c.id for c in CASES])
def test_policy_case(case: PolicyCase, pg_session):
    case.run(pg_session)
