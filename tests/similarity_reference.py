"""CPU reference used only by isolated scoring tests, never by the service."""

import numpy as np


class ReferenceReranker:
    def re_rank(self, query_emb, db_embeddings):
        query = query_emb / (np.linalg.norm(query_emb) + 1e-8)
        matrix = db_embeddings / (np.linalg.norm(db_embeddings, axis=1, keepdims=True) + 1e-8)
        return (matrix @ query).astype(np.float32)
