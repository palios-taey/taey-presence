"""Retrieval backend interface — the seam that decouples the presence engine
from any specific memory/vector store.

The MEMORY mechanism searches a retrieval backend on partial user input so
relevant context is ready the instant the user submits. The engine depends
ONLY on this Protocol — plug in any store (a vector DB, a BM25 index, a flat
file, nothing at all) by implementing `search`.
"""
from __future__ import annotations

from typing import Protocol, TypedDict, runtime_checkable

import httpx


class RetrievalHit(TypedDict, total=False):
    """One retrieved item. All fields optional except `snippet`."""
    id: str
    title: str
    snippet: str
    score: float


@runtime_checkable
class IRetrievalBackend(Protocol):
    """Implement this to give the presence engine a memory.

    `search` is called with the user's partial input and should return the
    most relevant hits. It must be safe to call frequently (debounced ~500ms)
    and should fail soft (return [] on error) rather than raise — a memory
    miss should never break the typing loop.
    """

    async def search(self, query: str, top_k: int = 5) -> list[RetrievalHit]:
        ...


class NoRetrievalBackend:
    """Default backend: no memory. The engine runs fine without retrieval —
    the MEMORY mechanism simply contributes nothing."""

    async def search(self, query: str, top_k: int = 5) -> list[RetrievalHit]:
        return []


class HTTPRetrievalBackend:
    """Example backend: POST {query, top_k} to an HTTP search endpoint that
    returns a list of hits (or {"results"/"tiles": [...]}).

    No auth — points at a search service on your own trusted network. Set the
    URL via config; there is no credential.
    """

    def __init__(self, search_url: str, timeout: float = 8.0):
        if not search_url:
            raise ValueError(
                "HTTPRetrievalBackend requires a search_url (no default — "
                "point it at your own search endpoint, e.g. http://localhost:8095/search)"
            )
        self._url = search_url
        self._timeout = timeout

    async def search(self, query: str, top_k: int = 5) -> list[RetrievalHit]:
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as http:
                resp = await http.post(self._url, json={"query": query, "top_k": top_k})
                data = resp.json()
            rows = data if isinstance(data, list) else data.get("results", data.get("tiles", []))
            out: list[RetrievalHit] = []
            for t in rows[:top_k]:
                out.append({
                    "id": t.get("id", t.get("content_hash", "")),
                    "title": t.get("title", t.get("document_name", "")),
                    "snippet": (t.get("content", "") or t.get("snippet", ""))[:400],
                    "score": float(t.get("score", t.get("certainty", 0)) or 0),
                })
            return out
        except Exception:
            # fail soft — a memory miss must never break the typing loop
            return []
