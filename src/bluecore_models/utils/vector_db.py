import logging
import os


from pymilvus import model, MilvusClient

from bluecore_models.utils.graph import init_graph
from bluecore_models.models import Version

logger = logging.getLogger(__name__)

MILVUS_URI = os.environ.get("MILVUS_URI", "http://localhost:19530")


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
