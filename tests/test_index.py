import pytest  # noqa

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from bluecore_models.models import ResourceBase, Work


def test_regular_search(pg_session: sessionmaker[Session]) -> None:
    # For regular search, use 'english' configuration and boolean operators (&, |) to construct the search query.
    with pg_session() as session:
        search_query = func.to_tsquery(
            "english", func.unaccent("Renewable & policy | Korea")
        )
        stmt = select(ResourceBase).where(
            search_query.op("@@")(ResourceBase.data_vector)
        )
        results = session.execute(stmt).scalars().all()
        assert len(results) == 2


def test_index_phrase(pg_session: sessionmaker[Session]) -> None:
    """
    For phrase search, use 'simple' configuration and <-> operator to construct the search query.
    If you use 'english' configuration, exact phrase search won't work as intended.
    """
    with pg_session() as session:
        search_query = func.to_tsquery("simple", "Renewable <-> energy")
        stmt = select(Work).where(search_query.op("@@")(Work.data_vector))
        results = session.execute(stmt).scalars().all()
        assert len(results) == 1
        assert (
            results[0].uri
            == "https://bluecore.info/works/23db8603-1932-4c3f-968c-ae584ef1b4bb"
        )


def test_index_diacritics(pg_session: sessionmaker[Session]) -> None:
    """
    The data_vector is indexed with unaccented version of the data, and search query must be unaccented as well.
    Rest of the tests will skip this step for simplicity, but production search api must use func.unaccent at all times.
    """
    with pg_session() as session:
        search_query = func.to_tsquery("simple", func.unaccent("Chaesaeng <-> enŏji"))
        stmt = select(Work).where(search_query.op("@@")(Work.data_vector))
        results = session.execute(stmt).scalars().all()
        assert len(results) == 1
        assert (
            results[0].uri
            == "https://bluecore.info/works/23db8603-1932-4c3f-968c-ae584ef1b4bb"
        )


def test_index_phrase_incomplete_words(pg_session: sessionmaker[Session]) -> None:
    # search with incomplete words, should not match.
    with pg_session() as session:
        search_query = func.to_tsquery(
            "simple", "Rene <-> ener <-> poli <-> in <-> Kor"
        )
        stmt = select(Work).where(search_query.op("@@")(Work.data_vector))
        results = session.execute(stmt).scalars().all()
        assert len(results) == 0


def test_index_phrase_with_wildcard(pg_session: sessionmaker[Session]) -> None:
    # For wildcard search, use :* operator at the end of a term to match any suffix.
    with pg_session() as session:
        search_query = func.to_tsquery(
            "simple",
            "Rene:* <-> ener:* <-> poli:* <-> in <-> Kor:*",
        )
        stmt = select(Work).where(search_query.op("@@")(Work.data_vector))
        results = session.execute(stmt).scalars().all()
        assert len(results) == 1
        assert (
            results[0].uri
            == "https://bluecore.info/works/23db8603-1932-4c3f-968c-ae584ef1b4bb"
        )


def test_index_exact_phrase_with_ranking(pg_session: sessionmaker[Session]) -> None:
    """
    The data_vector is indexed with weights.
    The search doesn't automatically boost the results based on the weights, however.
    To use it, the search must calcualte the rank and order the results by relevance.
    In this test, Work should come up first as it contains the query in mainTitle.
    Instance contains the query in subtitle and should come up second.
    """
    with pg_session() as session:
        search_query = func.to_tsquery(
            "simple", "Renewable <-> energy <-> policy <-> in <-> Korea"
        )
        stmt = (
            select(ResourceBase)
            .where(search_query.op("@@")(ResourceBase.data_vector))
            .order_by(func.ts_rank(ResourceBase.data_vector, search_query).desc())
        )
        results = session.execute(stmt).scalars().all()
        assert len(results) == 2
        assert (
            results[0].uri
            == "https://bluecore.info/works/23db8603-1932-4c3f-968c-ae584ef1b4bb"
        )


def test_index_uri(pg_session: sessionmaker[Session]) -> None:
    """
    For URI search, we need to escape the colon in http[s]:// to avoid treating it as a tsquery operator.
    We cannot use routines like func.quote_literal as it will escape the operators like :*, & and treat them as text instead.
    """
    with pg_session() as session:
        search_query = func.to_tsquery(
            "english",
            "https\\://bluecore.info/works/23db8603-1932-4c3f-968c-ae584ef1b4bb",
        )
        stmt = select(ResourceBase).where(search_query.op("@@")(Work.data_vector))
        results = session.execute(stmt).scalars().all()
        assert len(results) == 2


def _add_work(session, uri: str, main_title: str) -> None:
    """Insert a Work so data_vector is computed by Postgres from its data.

    The data needs @id and @type: ResourceBase frames the JSON-LD on insert,
    and a fragment without them frames down to an empty object.
    """
    session.add(
        Work(
            uri=uri,
            data={
                "@id": uri,
                "@type": ["Text", "Work", "Monograph"],
                "title": {"@type": "Title", "mainTitle": main_title},
            },
        )
    )
    session.commit()


def _matches(session, query: str, uri: str) -> bool:
    stmt = select(ResourceBase).where(
        func.to_tsquery("simple", query).op("@@")(ResourceBase.data_vector),
        ResourceBase.uri == uri,
    )
    return session.execute(stmt).scalars().first() is not None


def test_index_distinguishes_flat_from_sharp(
    pg_session: sessionmaker[Session],
) -> None:
    """The bug in bluecore_api#203: the parser drops the musical signs, so a
    search for one returns records containing the other."""
    uri = "https://bluecore.info/works/symbol-flat"
    with pg_session() as session:
        _add_work(session, uri, "Nocturne in D♭ major")
        assert _matches(session, "d & bcsymflat & major", uri)
        assert not _matches(session, "d & bcsymsharp & major", uri)
        # Searching without the symbol must keep working.
        assert _matches(session, "d & major", uri)


def test_index_strips_romanization_marks(pg_session: sessionmaker[Session]) -> None:
    """unaccent turns the ayn into an apostrophe, which the parser splits on,
    so "Sadi" finds nothing until the mark is deleted first."""
    uri = "https://bluecore.info/works/symbol-romanization"
    with pg_session() as session:
        _add_work(session, uri, "Saʻdī. Gulistān")
        assert _matches(session, "sadi", uri)
        assert _matches(session, "gulistan", uri)


def test_index_folds_subscripts(pg_session: sessionmaker[Session]) -> None:
    """Subscripts act as separators, so C₆H₁₂O₆ indexes as bare c, h, o."""
    uri = "https://bluecore.info/works/symbol-subscript"
    with pg_session() as session:
        _add_work(session, uri, "The chemistry of H₂O and C₆H₁₂O₆")
        assert _matches(session, "h2o", uri)
        assert _matches(session, "c6h12o6", uri)


def test_index_deletes_ligature_halves(pg_session: sessionmaker[Session]) -> None:
    uri = "https://bluecore.info/works/symbol-ligature"
    with pg_session() as session:
        _add_work(session, uri, "O nekotorykh aktualʹnykh problemakh transformat︠s︡ii")
        assert _matches(session, "transformatsii", uri)
        assert _matches(session, "aktualnykh", uri)
