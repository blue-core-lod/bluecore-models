import logging
import os

from typing import Union

from pymilvus import model, MilvusClient

from bluecore_models.utils.graph import load_jsonld
from bluecore_models.models import Version

logger = logging.getLogger(__name__)

MILVUS_URI = os.environ.get("MILVUS_URI")


def init_collections(client: MilvusClient):
    if not client.has_collection("works"):
        logger.info("Creating works collection")
        client.create_collection(
            collection_name="works",
            dimension=768,
        )
    if not client.has_collection("instances"):
        logger.info("Creating instances collection")
        client.create_collection(
            collection_name="instances",
            dimension=768,
        )


def create_embeddings(
    version: Version, collection: str, client: Union[MilvusClient, None] = None
):
    if not client:
        client = MilvusClient(uri=MILVUS_URI)

    init_collections(client)

    embedding_func = model.DefaultEmbeddingFunction()

    version_graph = load_jsonld(version.data)
    version_id = version.id

    resource_uri = version.resource.uri
    skolemized_graph = version_graph.skolemize(basepath=f"{resource_uri}#")

    triples = [
        line.rstrip(".")
        for line in skolemized_graph.serialize(format="nt").splitlines()
    ]

    triple_vectors = embedding_func.encode_documents(triples)

    embeddings_data = []

    for i, vector in enumerate(triple_vectors):
        embeddings_data.append(
            {
                "id": i,
                "vector": vector,
                "text": triples[i],
                "uri": resource_uri,
                "version": version_id,
            }
        )

    logging.info(
        f"Creating embeddings for {resource_uri} version {version_id}, total vectors: {len(triple_vectors)}"
    )
    result = client.insert(collection_name=collection, data=embeddings_data)
    logging.info(f"Inserted {result['insert_count']} triple embeddings")
