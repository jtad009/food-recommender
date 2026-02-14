# embeddings.py
from dataclasses import dataclass
from typing import List, Sequence, Optional
import numpy as np

from embedder.base_encoder import BaseEncoder

@dataclass
class HFEmbedConfig:
    model_name: str = "sentence-transformers/all-MiniLM-L6-v2"
    device: str = "cpu"  # or "cuda" for GPU
    batch_size: int = 64
    normalize: bool = True

class HFEmbedder(BaseEncoder):
    def __init__(self, cfg: Optional[HFEmbedConfig] = None):
        self.cfg = cfg or HFEmbedConfig()
        from sentence_transformers import SentenceTransformer
        self._model = SentenceTransformer(self.cfg.model_name, device=self.cfg.device)


    def encode(self, texts: Sequence[str]) -> List[List[float]]:
        embs = self._model.encode(
            list(texts),
            batch_size=self.cfg.batch_size,
            normalize_embeddings=self.cfg.normalize,
            show_progress_bar=False,
        )
        # Ensure pure python lists (Mongo/JSON friendly)
        if isinstance(embs, np.ndarray):
            embs = embs.tolist()
        else:
            embs = [e.tolist() if hasattr(e, "tolist") else list(e) for e in embs]
        return embs
