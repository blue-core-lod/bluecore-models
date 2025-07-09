import pytest  # noqa

from pymilvus import MilvusClient

from bluecore_models.models import Version
from bluecore_models.utils.vector_db import create_embeddings, init_collections


def test_init_collections(tmp_path):
    test_db_path = tmp_path / "vector-test.db"

    test_client = MilvusClient(str(test_db_path))

    assert not test_client.has_collection("works")

    init_collections(test_client)

    assert test_client.has_collection("works")
    assert test_client.has_collection("instances")


def test_init_collections_existing(tmp_path, caplog):
    test_db_path = tmp_path / "vector-test.db"

    test_client = MilvusClient(str(test_db_path))
    test_client.create_collection(
        collection_name="works",
        dimension=768,
    )
    test_client.create_collection(collection_name="instances", dimension=768)

    init_collections(test_client)

    assert "Creating works collection" not in caplog.text


def test_create_embeddings(pg_session, tmp_path):
    test_db_path = tmp_path / "vector-test.db"
    test_client = MilvusClient(str(test_db_path))

    with pg_session() as session:
        version = session.query(Version).where(Version.id == 1).first()
        create_embeddings(version, "works", test_client)

    collection_stats = test_client.get_collection_stats("works")
    assert collection_stats["row_count"] == 79
