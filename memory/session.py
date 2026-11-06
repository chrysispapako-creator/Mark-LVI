"""The object tools already accept as ``session_memory``.

It used to be passed as ``None``. Weather (and anything else that grew up
checking ``if session_memory``) can now leave a result in the same journal
the voice session writes to. There is one journal per machine, not one per
tool — ``session_id`` only marks which conversation the row belongs to.
"""
from __future__ import annotations

from memory import store


class SharedMemory:
    def __init__(self, session_id: str):
        self.session_id = session_id or ""

    def set_last_search(self, query: str = "", response: str = "") -> None:
        """Weather and any other search-style tool already call this."""
        try:
            store.record(
                "result",
                response or "",
                role="tool",
                source="session_memory",
                title=query or "search",
                session_id=self.session_id,
                tags="search",
            )
        except Exception as exc:
            print(f"[Memory] set_last_search failed: {exc}")

    def note(self, text: str, kind: str = "note") -> None:
        """A plugin's own observation. ``kind`` must be one the journal knows."""
        try:
            store.record(
                kind or "note",
                text or "",
                role="tool",
                source="session_memory",
                session_id=self.session_id,
            )
        except Exception as exc:
            print(f"[Memory] note failed: {exc}")

    def recall(self, query: str) -> str:
        try:
            from memory.memory_manager import search_memory
            return search_memory(query)
        except Exception as exc:
            print(f"[Memory] recall failed: {exc}")
            return ""
