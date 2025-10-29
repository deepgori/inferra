"""
embeddings.py — Pluggable Embedding Backend for RAG

Provides vector embedding support for semantic code search. Architecture:

1. EmbeddingBackend (ABC) — interface for any embedding provider
2. LocalEmbedding — lightweight numpy-based embeddings using bag-of-words + SVD
   (works offline, no API keys, good for demo/testing)
3. SentenceTransformerEmbedding — production-quality embeddings via HuggingFace
   (local model, no API key needed, requires sentence-transformers install)
4. OpenAIEmbedding — cloud-based embeddings via OpenAI API
   (highest quality, requires OPENAI_API_KEY)

The CodeIndexer auto-selects the best available backend:
    sentence-transformers installed → SentenceTransformerEmbedding
    numpy installed → LocalEmbedding (SVD-based, surprisingly good for code)
    neither → falls back to TF-IDF only
"""

import math
import re
from abc import ABC, abstractmethod
from collections import defaultdict
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

import numpy as np


# ── Embedding Backend Interface ───────────────────────────────────────────────


class EmbeddingBackend(ABC):
    """Abstract interface for embedding providers."""

    @abstractmethod
    def encode(self, texts: List[str]) -> "np.ndarray":
        """Encode a list of texts into embedding vectors.
        Returns shape (n_texts, embedding_dim).
        """
        ...

    @abstractmethod
    def encode_query(self, query: str) -> "np.ndarray":
        """Encode a single query. Returns shape (embedding_dim,)."""
        ...

    @property
    @abstractmethod
    def dimension(self) -> int:
        """Embedding dimension."""
        ...

    @property
    def name(self) -> str:
        return self.__class__.__name__


# ── Local SVD-Based Embeddings ────────────────────────────────────────────────


class LocalEmbedding(EmbeddingBackend):
    """
    Lightweight embedding using TF-IDF matrix + truncated SVD.

    This creates dense vector representations by:
    1. Building a TF-IDF matrix from the corpus
    2. Applying SVD to reduce to `n_components` dimensions
    3. Each code unit and query gets a dense vector for cosine similarity

    Surprisingly effective for code search because:
    - Code has very distinct vocabularies per function
    - SVD captures latent semantic structure (e.g., "timeout" and "connection"
      co-occur, so they become similar in embedding space)

    No external API or model download needed.
    """

    def __init__(self, n_components: int = 64):
        self._n_components = n_components
        self._vocabulary: Dict[str, int] = {}
        self._idf: Dict[str, float] = {}
        self._svd_components: Optional["np.ndarray"] = None  # (n_components, vocab_size)
        self._fitted = False

    @property
    def dimension(self) -> int:
        return self._n_components

    def fit(self, documents: List[List[str]]):
        """
        Build the SVD embedding space from tokenized documents.

        Args:
            documents: List of tokenized documents (each is a list of tokens)
        """
        if not documents:
            return

        # Build vocabulary
        df = defaultdict(int)
        for doc in documents:
            for token in set(doc):
                df[token] += 1

        # Filter: keep tokens appearing in at least 1 doc but not more than 90%
        n_docs = len(documents)
        max_df = int(n_docs * 0.9)
        self._vocabulary = {}
        idx = 0
        for token, freq in sorted(df.items()):
            if 1 <= freq <= max(max_df, 2):
                self._vocabulary[token] = idx
                idx += 1

        vocab_size = len(self._vocabulary)
        if vocab_size == 0:
            return

        # Compute IDF
        for token, freq in df.items():
            if token in self._vocabulary:
                self._idf[token] = math.log(n_docs / (1 + freq)) + 1.0

        # Build TF-IDF matrix (n_docs × vocab_size)
        tfidf_matrix = np.zeros((n_docs, vocab_size), dtype=np.float32)
        for i, doc in enumerate(documents):
            tf = defaultdict(int)
            for token in doc:
                tf[token] += 1
            max_tf = max(tf.values()) if tf else 1

            for token, count in tf.items():
                if token in self._vocabulary:
                    j = self._vocabulary[token]
                    tfidf_matrix[i, j] = (count / max_tf) * self._idf.get(token, 1.0)

        # Row-normalize
        norms = np.linalg.norm(tfidf_matrix, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        tfidf_matrix /= norms

        # SVD — reduce to n_components dimensions
        n_components = min(self._n_components, min(tfidf_matrix.shape) - 1)
        if n_components <= 0:
            n_components = 1

        try:
            U, S, Vt = np.linalg.svd(tfidf_matrix, full_matrices=False)
            self._svd_components = Vt[:n_components]  # (n_components, vocab_size)
            self._n_components = n_components
            self._fitted = True
        except np.linalg.LinAlgError:
            # SVD failed — fall back to raw TF-IDF
            self._fitted = False

    def _text_to_tfidf(self, tokens: List[str]) -> "np.ndarray":
        """Convert tokenized text to a TF-IDF vector."""
        vocab_size = len(self._vocabulary)
        vec = np.zeros(vocab_size, dtype=np.float32)

        tf = defaultdict(int)
        for token in tokens:
            tf[token] += 1
        max_tf = max(tf.values()) if tf else 1

        for token, count in tf.items():
            if token in self._vocabulary:
                j = self._vocabulary[token]
                vec[j] = (count / max_tf) * self._idf.get(token, 1.0)

        norm = np.linalg.norm(vec)
        if norm > 0:
            vec /= norm

        return vec

    def encode(self, texts: List[str]) -> "np.ndarray":
        """Encode texts by tokenizing, computing TF-IDF, then projecting via SVD."""
        if not self._fitted:
            raise RuntimeError("Must call fit() before encode()")

        tokenized = [self._tokenize(t) for t in texts]
        tfidf_vecs = np.array([self._text_to_tfidf(tokens) for tokens in tokenized])

        # Project through SVD components
        embeddings = tfidf_vecs @ self._svd_components.T  # (n_texts, n_components)

        # Normalize
        norms = np.linalg.norm(embeddings, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        embeddings /= norms

        return embeddings

    def encode_query(self, query: str) -> "np.ndarray":
        """Encode a single query."""
        return self.encode([query])[0]

    def _tokenize(self, text: str) -> List[str]:
        """Tokenize for embedding."""
        text = text.lower()
        tokens = re.findall(r'[a-z_][a-z0-9_]*', text)
        stopwords = {"self", "none", "true", "false", "return", "import", "from",
                      "def", "class", "and", "or", "not", "the", "is", "in",
                      "for", "if", "else", "with", "as"}
        return [t for t in tokens if len(t) > 2 and t not in stopwords]


# ── Sentence Transformer Embeddings ──────────────────────────────────────────


class SentenceTransformerEmbedding(EmbeddingBackend):
    """
    Production-quality embeddings via HuggingFace sentence-transformers.

    Uses all-MiniLM-L6-v2 by default (fast, 384-dim, good for code).
    Runs locally — no API key needed, but requires:
        pip install sentence-transformers

    First run downloads the model (~80MB).
    """

    def __init__(self, model_name: str = "all-MiniLM-L6-v2"):
        try:
            from sentence_transformers import SentenceTransformer
            self._model = SentenceTransformer(model_name)
            self._dim = self._model.get_sentence_embedding_dimension()
        except ImportError:
            raise ImportError(
                "sentence-transformers not installed. "
                "Install with: pip install sentence-transformers"
            )

    @property
    def dimension(self) -> int:
        return self._dim

    def encode(self, texts: List[str]) -> "np.ndarray":
        return self._model.encode(texts, normalize_embeddings=True)

    def encode_query(self, query: str) -> "np.ndarray":
        return self._model.encode(query, normalize_embeddings=True)


# ── OpenAI Embeddings ────────────────────────────────────────────────────────


class OpenAIEmbedding(EmbeddingBackend):
    """
    Cloud-based embeddings via OpenAI's text-embedding-3-small.

    Requires OPENAI_API_KEY environment variable.
    Highest quality but adds latency and cost.
    """

    def __init__(self, model: str = "text-embedding-3-small"):
        import os
        try:
            import openai
            self._client = openai.OpenAI(api_key=os.environ.get("OPENAI_API_KEY"))
            self._model = model
            self._dim = 1536
        except ImportError:
            raise ImportError("openai not installed. Install with: pip install openai")

    @property
    def dimension(self) -> int:
        return self._dim

    def encode(self, texts: List[str]) -> "np.ndarray":
        response = self._client.embeddings.create(input=texts, model=self._model)
        return np.array([d.embedding for d in response.data], dtype=np.float32)

    def encode_query(self, query: str) -> "np.ndarray":
        return self.encode([query])[0]


# ── Vector Store ─────────────────────────────────────────────────────────────


class VectorStore:
    """
    Simple in-memory vector store with cosine similarity search.

    Stores embedding vectors alongside metadata (code unit indices).
    Uses numpy for fast batch cosine similarity.
    """

    def __init__(self):
        self._vectors: Optional["np.ndarray"] = None  # (n, dim)
        self._metadata: List[int] = []  # index into CodeUnit list

    def add(self, vectors: "np.ndarray", indices: List[int]):
        """Add vectors with their CodeUnit indices."""
        if self._vectors is None:
            self._vectors = vectors
        else:
            self._vectors = np.vstack([self._vectors, vectors])
        self._metadata.extend(indices)

    def search(self, query_vec: "np.ndarray", top_k: int = 5) -> List[Tuple[int, float]]:
        """
        Find the top-k most similar vectors via cosine similarity.

        Returns: List of (code_unit_index, similarity_score) tuples.
        """
        if self._vectors is None or len(self._metadata) == 0:
            return []

        # Cosine similarity (vectors are already normalized)
        similarities = self._vectors @ query_vec

        # Get top-k indices
        top_k = min(top_k, len(self._metadata))
        top_indices = np.argsort(similarities)[-top_k:][::-1]

        return [
            (self._metadata[i], float(similarities[i]))
            for i in top_indices
            if similarities[i] > 0
        ]

    @property
    def size(self) -> int:
        return len(self._metadata)


# ── Factory Function ─────────────────────────────────────────────────────────


def get_best_backend() -> Optional[EmbeddingBackend]:
    """
    Auto-detect and return the best available embedding backend.

    Priority:
    1. sentence-transformers (local, high quality)
    2. LocalEmbedding (numpy SVD, good enough)
    3. None (fall back to TF-IDF only)
    """
    try:
        return SentenceTransformerEmbedding()
    except (ImportError, Exception):
        pass

    try:
        return LocalEmbedding(n_components=64)
    except Exception:
        pass

    return None
