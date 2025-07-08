import pytest

from pymilvus import MilvusClient

from bluecore_models.utils.vector_db import init_collections


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

    assert not "Creating works collection" in caplog.text
