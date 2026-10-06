"""Base de conocimiento para RAG, en memoria y sin servidor de base de datos.

Usa embeddings de Gemini si están disponibles; si no (sin key o sin cuota),
cae automáticamente a búsqueda por palabras clave, así que siempre funciona.
"""

import json
import logging
import math
import re
from collections import Counter
from pathlib import Path

import numpy as np

from core import llm
from core.tools import Tool, tool
from core.tools.files import extract_text

log = logging.getLogger("cognia.memory")


def chunk_text(text: str, size: int = 1200, overlap: int = 200) -> list[str]:
    """Parte el texto en trozos, cortando preferentemente en saltos de párrafo."""
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    chunks, start = [], 0
    while start < len(text):
        end = min(start + size, len(text))
        if end < len(text):
            cut = text.rfind("\n\n", start + size // 2, end)
            end = cut if cut != -1 else end
        chunks.append(text[start:end].strip())
        if end >= len(text):
            break
        start = max(end - overlap, start + 1)
    return [c for c in chunks if c]


def _tokens(text: str) -> list[str]:
    return [t for t in re.findall(r"\w+", text.lower()) if len(t) > 2]


class KnowledgeBase:
    def __init__(self):
        self.chunks: list[dict] = []  # {"text": ..., "source": ...}
        self.vectors: np.ndarray | None = None
        self.use_embeddings = True

    def __len__(self) -> int:
        return len(self.chunks)

    @property
    def sources(self) -> list[str]:
        return sorted({c["source"] for c in self.chunks})

    def add_text(self, text: str, source: str = "texto") -> int:
        new = [{"text": c, "source": source} for c in chunk_text(text)]
        if not new:
            return 0
        if self.use_embeddings:
            try:
                vecs = llm.embed([c["text"] for c in new])
                self.vectors = vecs if self.vectors is None else np.vstack([self.vectors, vecs])
            except Exception as e:  # noqa: BLE001
                log.warning("Embeddings no disponibles (%s); uso búsqueda por palabras clave.", str(e)[:150])
                self.use_embeddings, self.vectors = False, None
        self.chunks += new
        return len(new)

    def add_file(self, path: str | Path) -> int:
        return self.add_text(extract_text(path), source=Path(path).name)

    def search(self, query: str, k: int = 5) -> list[dict]:
        if not self.chunks:
            return []
        if self.use_embeddings and self.vectors is not None:
            scores = self.vectors @ llm.embed([query])[0]
        else:
            scores = self._keyword_scores(query)
        top = np.argsort(scores)[::-1][:k]
        return [{**self.chunks[i], "score": float(scores[i])} for i in top if scores[i] > 0]

    def _keyword_scores(self, query: str) -> np.ndarray:
        docs = [Counter(_tokens(c["text"])) for c in self.chunks]
        n = len(docs)
        scores = np.zeros(n)
        for term in set(_tokens(query)):
            df = sum(1 for d in docs if term in d)
            if df:
                idf = math.log(1 + n / df)
                scores += np.array([math.log1p(d[term]) * idf for d in docs])
        return scores

    def as_tool(self) -> Tool:
        kb = self

        @tool
        def search_documents(query: str, k: int = 5) -> str:
            """Busca información relevante en los documentos que subió el usuario. Úsala antes de responder preguntas sobre ellos.

            Args:
                query: qué información buscar
                k: cuántos fragmentos devolver
            """
            hits = kb.search(query, k)
            if not hits:
                return "No se encontró nada relevante en los documentos."
            return "\n\n---\n\n".join(f"[{h['source']}]\n{h['text']}" for h in hits)

        return search_documents

    def save(self, path: str | Path) -> None:
        path = Path(path)
        path.mkdir(parents=True, exist_ok=True)
        (path / "chunks.json").write_text(json.dumps(self.chunks, ensure_ascii=False), encoding="utf-8")
        if self.vectors is not None:
            np.save(path / "vectors.npy", self.vectors)

    @classmethod
    def load(cls, path: str | Path) -> "KnowledgeBase":
        path, kb = Path(path), cls()
        kb.chunks = json.loads((path / "chunks.json").read_text(encoding="utf-8"))
        vectors = path / "vectors.npy"
        if vectors.exists():
            kb.vectors = np.load(vectors)
        else:
            kb.use_embeddings = False
        return kb
