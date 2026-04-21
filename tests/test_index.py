import pytest

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from bluecore_models.models import ResourceBase, Work


def test_index_diacritics(pg_session: sessionmaker[Session]) -> None:
    with pg_session() as session:
        # data contains "Chaesaeng enŏji" only, search with unaccented version
        search_query = func.to_tsquery("simple", "Chaesaeng <-> enoji")
        stmt = select(Work).where(search_query.op("@@")(Work.data_vector))
        results = session.execute(stmt).scalars().all()
        assert len(results) == 1
        assert (
            results[0].uri
            == "https://bluecore.info/works/23db8603-1932-4c3f-968c-ae584ef1b4bb"
        )


def test_index_weights(pg_session: sessionmaker[Session]) -> None:
    with pg_session() as session:
        # work should come up first as it contains the query in mainTitle.
        # Instance contains the query in subtitle and should come up second.
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


if __name__ == "__main__":
    pytest.main()
