# SPDX-License-Identifier: MIT
# ChromaDB removed — all vector storage is in pgvector (document_embeddings, HNSW index).
# This stub exists only to prevent ImportError in any legacy code still importing it.
# chroma_client is None; callers that check for None will skip gracefully.

chroma_client = None


def get_chroma_client():
    return None
