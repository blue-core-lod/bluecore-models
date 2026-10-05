"""The identifiers column: cleanup rules, and that Postgres keeps it up to date."""

from collections.abc import Sequence

import pytest
from sqlalchemy import func, literal, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Session, sessionmaker

from bluecore_models.models import ResourceBase, Work


# Runs one value through the same cleanup the column and the search API use
def identifier_values(
    session: Session, scheme: str, raw_value: str
) -> list[str] | None:
    return session.scalar(select(func.bluecore_identifier_values(scheme, raw_value)))


# Runs a whole record's data through the function that fills the column
def identifiers_for(session: Session, data: dict) -> list[str] | None:
    return session.scalar(select(func.bluecore_identifiers(literal(data, JSONB))))


@pytest.mark.parametrize(
    "raw_value, expected",
    [
        ("sn 85009985 ", ["sn85009985"]),
        ("   73094113 ", ["73094113"]),
        ("n78-890351", ["n78890351"]),
        ("85-2", ["85000002"]),
        ("75-425165//r75", ["75425165"]),
        ("85-abc", []),  # not a valid serial number after the hyphen
    ],
)
def test_lccn_follows_library_of_congress_rules(
    pg_session: sessionmaker[Session], raw_value: str, expected: list[str]
):
    with pg_session() as session:
        assert identifier_values(session, "lccn", raw_value) == expected


@pytest.mark.parametrize(
    "raw_value, expected",
    [
        ("0878880690", ["0878880690", "9780878880690"]),
        ("978-0-14-044911-2", ["9780140449112", "0140449116"]),
        ("080442957x", ["080442957X", "9780804429573"]),
        ("0878880690 (pbk.)", ["0878880690", "9780878880690"]),
        ("979-10-90636-07-1", ["9791090636071"]),  # 979 has no ISBN-10 form
        ("0878880691", ["0878880691"]),  # bad check digit: kept as written
        ("08788806901", ["08788806901"]),  # too long to be an ISBN
    ],
)
def test_isbn_keeps_both_forms(
    pg_session: sessionmaker[Session], raw_value: str, expected: list[str]
):
    with pg_session() as session:
        assert identifier_values(session, "isbn", raw_value) == expected


@pytest.mark.parametrize(
    "scheme, raw_value, expected",
    [
        ("issn", "0047-1607", ["00471607"]),
        ("issn", "1234-567x (print)", ["1234567X"]),
        ("issn", "9780140449112", ["9780140449112"]),  # too long to be an ISSN
        ("doi", "https://doi.org/10.1000/ABC", ["10.1000/abc"]),
        ("doi", "doi:10.1000/x:y", ["10.1000/x:y"]),
        ("doi", "10.1000/ABC", ["10.1000/abc"]),
        ("local", "ABC123", []),  # other schemes are not indexed
        ("upc", "012345", []),
        ("isbn", "   ", []),
    ],
)
def test_other_schemes(
    pg_session: sessionmaker[Session], scheme: str, raw_value: str, expected: list[str]
):
    with pg_session() as session:
        assert identifier_values(session, scheme, raw_value) == expected


def test_record_identifiers_are_stored_with_and_without_scheme(
    pg_session: sessionmaker[Session],
):
    data = {
        "identifiedBy": [
            {"@type": "Isbn", "rdf:value": "9780878880690"},
            {"@type": "Lccn", "rdf:value": "   73094113 "},
            {"@type": "Isbn", "rdf:value": "0878880690", "acquisitionTerms": "$4.95"},
        ]
    }
    with pg_session() as session:
        assert identifiers_for(session, data) == [
            "0878880690",
            "73094113",
            "9780878880690",
            "isbn:0878880690",
            "isbn:9780878880690",
            "lccn:73094113",
        ]


def test_record_identifier_shapes(pg_session: sessionmaker[Session]):
    with pg_session() as session:
        # A single object instead of a list, with a list of types in IRI form
        assert identifiers_for(
            session,
            {
                "identifiedBy": {
                    "@type": ["VariantX", "http://id.loc.gov/ontologies/bibframe/Lccn"],
                    "rdf:value": "sn 85009985 ",
                }
            },
        ) == ["lccn:sn85009985", "sn85009985"]

        # Generic "Identifier" uses its source code; prefixed types; tagged
        # values; nodes with no type, an empty value or another scheme are skipped
        assert identifiers_for(
            session,
            {
                "identifiedBy": [
                    {
                        "@type": "Identifier",
                        "source": {"code": "DOI"},
                        "rdf:value": "10.1000/ABC",
                    },
                    {"rdf:value": "no type"},
                    {"@type": "Isbn", "rdf:value": " "},
                    {"@type": "bf:Issn", "rdf:value": {"@value": "0047-1607"}},
                    {"@type": "Local", "rdf:value": "SERIALSET-02254"},
                    {"@type": "Nbn", "rdf:value": "014634473"},
                ]
            },
        ) == ["00471607", "10.1000/abc", "doi:10.1000/abc", "issn:00471607"]

        assert identifiers_for(session, {"title": "no identifiers"}) == []


def test_cancelled_identifiers_are_still_indexed(pg_session: sessionmaker[Session]):
    data = {
        "identifiedBy": [
            {
                "@type": "Lccn",
                "status": {"@id": "http://id.loc.gov/vocabulary/mstatus/cancinv"},
                "rdf:value": "n 2003044804",
            }
        ]
    }
    with pg_session() as session:
        assert identifiers_for(session, data) == ["lccn:n2003044804", "n2003044804"]


def test_nested_identifiers_are_not_indexed(pg_session: sessionmaker[Session]):
    data = {
        "relation": {
            "associatedResource": {
                "identifiedBy": [{"@type": "Isbn", "rdf:value": "0878880690"}]
            }
        }
    }
    with pg_session() as session:
        assert identifiers_for(session, data) == []


def test_column_is_filled_on_insert_and_update(pg_session: sessionmaker[Session]):
    uri = "https://bluecore.info/works/identifiers-test"
    with pg_session() as session:
        work = Work(
            uri=uri,
            data={
                "@id": uri,
                "@type": "Work",
                "identifiedBy": [{"@type": "Lccn", "rdf:value": "sn 85009985 "}],
            },
        )
        session.add(work)
        session.commit()
        session.refresh(work)
        assert "lccn:sn85009985" in work.identifiers

        work.data = {
            "@id": uri,
            "@type": "Work",
            "identifiedBy": [{"@type": "Isbn", "rdf:value": "0878880690"}],
        }
        session.commit()
        session.refresh(work)
        assert "isbn:9780878880690" in work.identifiers
        assert "lccn:sn85009985" not in work.identifiers

        found: Sequence[ResourceBase] = session.scalars(
            select(ResourceBase).where(
                ResourceBase.identifiers.overlap(["isbn:0878880690"])
            )
        ).all()
        assert [resource.uri for resource in found] == [uri]
