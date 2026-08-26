import pytest  # noqa

from sqlalchemy import func, select, text
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


def test_title_vector_searches_only_requested_title_values(
    pg_session: sessionmaker[Session],
) -> None:
    """The title vector finds each requested title field but ignores other data."""
    uri = "https://bcld.info/works/title-vector-test"
    with pg_session() as session:
        session.add(
            Work(
                uri=uri,
                data={
                    "@id": uri,
                    "@type": "Work",
                    "title": [
                        {
                            "@type": "Title",
                            "mainTitle": "primaryscope",
                            "subtitle": "subtitlescope",
                        },
                        {"@type": "VariantTitle", "mainTitle": "variantscope"},
                        {"@type": "ParallelTitle", "mainTitle": "parallelscope"},
                    ],
                    "note": {"@type": "Note", "label": "notescope"},
                },
            )
        )
        session.flush()

        for term in (
            "primaryscope",
            "variantscope",
            "parallelscope",
            "subtitlescope",
        ):
            search_query = func.to_tsquery("english", term)
            results = session.scalars(
                select(Work).where(search_query.op("@@")(Work.title_vector))
            ).all()
            assert [result.uri for result in results] == [uri]

        search_query = func.to_tsquery("english", "notescope")
        title_results = session.scalars(
            select(Work).where(search_query.op("@@")(Work.title_vector))
        ).all()
        broad_results = session.scalars(
            select(Work).where(search_query.op("@@")(Work.data_vector))
        ).all()
        assert title_results == []
        assert [result.uri for result in broad_results] == [uri]


def test_title_vector_has_a_gin_index(pg_session: sessionmaker[Session]) -> None:
    """The database creates and uses a GIN index for title matching."""
    with pg_session() as session:
        index_definition = session.scalar(
            text(
                "SELECT indexdef FROM pg_indexes "
                "WHERE tablename = 'resource_base' "
                "AND indexname = 'index_resource_base_on_title_vector'"
            )
        )
        session.execute(text("SET LOCAL enable_seqscan = off"))
        query_plan = session.scalars(
            text(
                "EXPLAIN SELECT id FROM resource_base "
                "WHERE to_tsquery('english', 'Renewable') @@ title_vector"
            )
        ).all()

    assert index_definition is not None
    assert "USING gin (title_vector)" in index_definition
    assert any("index_resource_base_on_title_vector" in line for line in query_plan)
