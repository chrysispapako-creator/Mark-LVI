"""Shared episodic memory: one local journal for every conversation.

Facts about the person (name, preferences, projects) stay in
``memory_manager`` / ``memory/long_term.json``. This file is the other half
of the same memory: what was said, decided, done and returned, across
sessions, on this machine.

Why SQLite and not another JSON file
    The fact file is small and rewritten as a whole. A journal of every
    turn cannot be. SQLite is in the Python standard library — no package,
    no account, no network. WAL mode lets a reader proceed while one writer
    commits, and a transaction either lands or it does not. FTS5, which
    CPython's SQLite is built with, answers a keyword query from an index
    instead of loading the table into the prompt.

Why there is no embedding model
    A local vector index would be a second download of several hundred
    megabytes and a second thing that behaves differently on each machine.
    Keyword search over a journal that has already been compacted to
    summaries is the strongest option that adds nothing to ``requirements.txt``
    and never calls a paid API. Compaction is extractive for the same
    reason: it has to work when no model is configured at all.

Limits — read these before describing the feature as complete
    * Retrieval is lexical. A question phrased with none of the stored
      words can miss, and FTS stemming is light (``unicode61``, prefix
      match). Paraphrases are not understood.
    * Compaction is lossy. A folded stretch keeps the first and last thing
      the user said, the tool names, and as many of the other user lines as
      fit in the summary budget. Lines that do not fit are dropped from
      recall. The dropped text is deleted from the search index; the row
      stays marked compacted until the compacted-row cap, then those rows
      are deleted too.
    * The prompt never receives the whole journal. It receives a bounded
      recent slice. Older detail is reached only through ``recall``.
    * Secret redaction is the heuristic in ``redact.py``. It is not
      encryption and it is not a guarantee. See that module.
    * ``PRAGMA secure_delete`` overwrites deleted row bytes inside this
      database file. It does not wipe backups, the activity log, the live
      provider context, or free space the OS has already detached.
    * One operating-system process is the intended writer. Threads in that
      process share one connection behind a lock. A second process can read
      the WAL; two writers are serialised by SQLite's lock, with a five
      second busy timeout, and the loser raises rather than corrupting the
      file.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

from memory.redact import REDACT_REVISION, redact_text

MEMORY_VERSION = "1"

# How much of the journal is allowed to stay "raw" before older turns are
# folded into a summary. These are storage budgets, not prompt budgets.
ACTIVE_EPISODE_CAP = 400
ACTIVE_CHAR_CAP = 350_000
KEEP_RECENT = 40
COMPACT_BATCH = 80
SUMMARY_CHARS = 900
MAX_COMPACTED_ROWS = 1500

# A single stored body. The prompt and recall paths snippet further.
BODY_MAX = 2_000

# What a tool result is allowed to contribute. Arguments and results are
# truncated again inside this, so one web page cannot fill the journal.
TOOL_ARGS_MAX = 500
TOOL_RESULT_MAX = 1_200

# Prompt and tool-result budgets. Callers can pass a smaller number.
RECENT_PROMPT_CHARS = 700
RECALL_CHARS = 1_800

# Tools whose results are either the memory system itself (recording them
# copies the store into the store) or a high-frequency meter.
_SKIP_TOOLS = frozenset({"recall_memory", "manage_memory", "system_status"})

_KINDS = frozenset({
    "conversation", "task", "decision", "action", "result", "note", "summary",
})

_COMPACT_KINDS = frozenset({"conversation", "action", "result", "task"})

_STOP = frozenset({
    "the", "a", "an", "of", "to", "and", "or", "for", "in", "on", "at", "is",
    "it", "my", "me", "do", "you", "your", "what", "about", "that", "this",
    "was", "were", "with", "remember", "recall", "from", "have", "has",
})


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S")


def _clip(text: str, limit: int) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def default_path() -> Path:
    """The journal sits next to ``long_term.json``.

    ``get_base_dir`` is what the fact file already uses, including a frozen
    build where ``__file__`` is not the folder the user can see. Falling
    back to the source tree keeps a broken import from writing nowhere.
    """
    try:
        from memory.memory_manager import get_base_dir
        base = get_base_dir()
    except Exception:
        base = Path(__file__).resolve().parent.parent
    return base / "memory" / "journal.sqlite"


def _words(text: str) -> list[str]:
    import re
    found = re.findall(r"\w{2,}", (text or "").lower(), flags=re.UNICODE)
    content = [w for w in found if w not in _STOP]
    return content or found


def _fts_query(text: str) -> str:
    """A safe FTS5 query. User text is never interpolated as FTS syntax."""
    parts = []
    for word in _words(text)[:12]:
        cleaned = word.replace('"', "")
        if not cleaned:
            continue
        # Prefix match so "flower" finds "flowerpot". Short words stay exact:
        # "ai" as a prefix matches half the language.
        if len(cleaned) >= 4:
            parts.append(f'"{cleaned}"*')
        else:
            parts.append(f'"{cleaned}"')
    return " OR ".join(parts)


def _snippet(body: str, words: list[str], limit: int = 240) -> str:
    if not body:
        return ""
    if len(body) <= limit:
        return body
    low = body.lower()
    idx = -1
    for word in words:
        at = low.find(word)
        if at >= 0 and (idx < 0 or at < idx):
            idx = at
    if idx < 0:
        return _clip(body, limit)
    start = max(0, idx - 40)
    return _clip(body[start:], limit)


def _summarize(rows: list[sqlite3.Row], limit: int = SUMMARY_CHARS) -> str:
    """Lossy extractive summary. No model, no network.

    The first user line, the last user line, the tool names and the last
    reply each get an equal share of ``limit``, so the two ends survive
    even when the middle does not. Middle user lines are added only with
    whatever room is left. Anything that does not fit is omitted on
    purpose — see the module docstring.
    """
    users: list[str] = []
    actions: list[str] = []
    last_reply = ""
    for row in rows:
        kind = row["kind"]
        body = (row["body"] or "").replace("\n", " ").strip()
        title = (row["title"] or "").strip()
        if kind == "conversation" and row["role"] == "user" and body:
            users.append(body)
        elif kind == "conversation" and body:
            last_reply = body
        elif kind in ("action", "result", "task"):
            label = title or body[:80]
            if label and label not in actions:
                actions.append(label)

    required: list[tuple[str, str]] = []
    if users:
        required.append(("User: ", users[0]))
    if len(users) > 1:
        required.append(("Later: ", users[-1]))
    if actions:
        required.append(("Actions: ", ", ".join(actions[:8])))
    if last_reply:
        required.append(("Reply: ", last_reply))
    if not required:
        return ""

    each = max(24, (limit // len(required)) - 1)
    lines: list[str] = []
    for prefix, text in required:
        room = max(0, each - len(prefix))
        lines.append(prefix + text[:room])
    used = sum(len(line) + 1 for line in lines)
    for middle in users[1:-1]:
        piece = "Also: " + middle[:120]
        if used + len(piece) + 1 > limit:
            break
        lines.append(piece)
        used += len(piece) + 1
    return "\n".join(lines).strip()[:limit]


class Journal:
    """One SQLite file. Safe for threads in this process. Not a secret store."""

    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(
            str(self.path),
            check_same_thread=False,
            timeout=5.0,
            isolation_level=None,  # we open transactions ourselves
        )
        self._conn.row_factory = sqlite3.Row
        self._fts = False
        self._prepare()

    def _prepare(self) -> None:
        conn = self._conn
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=FULL")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA secure_delete=ON")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS episodes (
                id INTEGER PRIMARY KEY,
                created TEXT NOT NULL,
                kind TEXT NOT NULL,
                role TEXT NOT NULL DEFAULT '',
                source TEXT NOT NULL DEFAULT '',
                session_id TEXT NOT NULL DEFAULT '',
                title TEXT NOT NULL DEFAULT '',
                body TEXT NOT NULL,
                tags TEXT NOT NULL DEFAULT '',
                importance INTEGER NOT NULL DEFAULT 0,
                compacted INTEGER NOT NULL DEFAULT 0,
                deleted INTEGER NOT NULL DEFAULT 0,
                summarized INTEGER NOT NULL DEFAULT 0,
                redactions INTEGER NOT NULL DEFAULT 0,
                char_len INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_episodes_active "
            "ON episodes(deleted, compacted, id)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_episodes_session "
            "ON episodes(session_id, id)"
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
            """
        )
        self._fts = self._ensure_fts()
        self._scrub_if_needed()

    def _ensure_fts(self) -> bool:
        conn = self._conn
        try:
            row = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='episodes_fts'"
            ).fetchone()
            if row:
                return True
            try:
                conn.execute(
                    "CREATE VIRTUAL TABLE episodes_fts USING fts5("
                    "title, body, tags, kind, tokenize='unicode61 remove_diacritics 2')"
                )
            except sqlite3.OperationalError:
                conn.execute(
                    "CREATE VIRTUAL TABLE episodes_fts USING fts5(title, body, tags, kind)"
                )
            # Rebuild from rows that should be searchable. A fresh file has none.
            for episode in conn.execute(
                "SELECT id, title, body, tags, kind FROM episodes "
                "WHERE deleted=0 AND compacted=0"
            ):
                conn.execute(
                    "INSERT INTO episodes_fts(rowid, title, body, tags, kind) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (episode["id"], episode["title"], episode["body"],
                     episode["tags"], episode["kind"]),
                )
            return True
        except sqlite3.OperationalError as exc:
            print(f"[Memory] FTS5 unavailable ({exc}); recall scans recent rows only")
            return False

    def close(self) -> None:
        with self._lock:
            conn = self._conn
            if conn is None:
                return
            self._conn = None
            try:
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            except sqlite3.Error:
                pass
            try:
                conn.close()
            except sqlite3.Error:
                pass

    def _meta(self, key: str) -> str:
        row = self._conn.execute(
            "SELECT value FROM meta WHERE key=?", (key,)
        ).fetchone()
        return row["value"] if row else ""

    def _set_meta(self, key: str, value: str) -> None:
        self._conn.execute(
            "INSERT INTO meta(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )

    def _scrub_if_needed(self) -> int:
        """Rewrite stored text the current redactor now recognises.

        Runs once per ``REDACT_REVISION``. Rows the patterns still miss
        stay as they are — scrubbing is not a scan for every secret.
        """
        if self._meta("redact_revision") == REDACT_REVISION:
            return 0
        changed = 0
        with self._lock:
            self._conn.execute("BEGIN")
            try:
                rows = list(self._conn.execute(
                    "SELECT id, title, body, tags, compacted, deleted FROM episodes"
                ))
                for row in rows:
                    title, n1 = redact_text(row["title"])
                    body, n2 = redact_text(row["body"])
                    tags, n3 = redact_text(row["tags"])
                    if not (n1 or n2 or n3):
                        continue
                    self._conn.execute(
                        "UPDATE episodes SET title=?, body=?, tags=?, "
                        "redactions=redactions+?, char_len=? WHERE id=?",
                        (title, body, tags, n1 + n2 + n3, len(body), row["id"]),
                    )
                    if row["deleted"] == 0 and row["compacted"] == 0 and self._fts:
                        self._reindex(row["id"], title, body, tags, None)
                    changed += 1
                self._set_meta("redact_revision", REDACT_REVISION)
                self._set_meta("memory_version", MEMORY_VERSION)
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise
        if changed:
            # The UPDATE above replaces the cell. VACUUM drops the old page
            # so the previous text is not left in free space of this file.
            self.vacuum()
        return changed

    def _reindex(self, episode_id: int, title: str, body: str, tags: str,
                 kind: str | None) -> None:
        if not self._fts:
            return
        if kind is None:
            row = self._conn.execute(
                "SELECT kind FROM episodes WHERE id=?", (episode_id,)
            ).fetchone()
            kind = row["kind"] if row else ""
        self._conn.execute("DELETE FROM episodes_fts WHERE rowid=?", (episode_id,))
        self._conn.execute(
            "INSERT INTO episodes_fts(rowid, title, body, tags, kind) VALUES (?,?,?,?,?)",
            (episode_id, title, body, tags, kind),
        )

    def record(
        self,
        kind: str,
        body: str,
        *,
        role: str = "",
        source: str = "",
        title: str = "",
        tags: str = "",
        session_id: str = "",
        importance: int = 0,
    ) -> int | None:
        """Insert one episode. Returns the new id, or None if there was nothing to store.

        Never raises for bad input. Database errors propagate so callers on
        the voice path can catch them — the module-level wrappers do.
        """
        if kind not in _KINDS:
            kind = "note"
        title, n_title = redact_text(title or "")
        body, n_body = redact_text(body or "")
        tags, n_tags = redact_text(tags or "")
        body = _clip(body, BODY_MAX)
        title = _clip(title, 180)
        if not body and not title:
            return None
        redactions = n_title + n_body + n_tags
        with self._lock:
            if self._duplicate(kind, role, body, session_id):
                return None
            self._conn.execute("BEGIN")
            try:
                cur = self._conn.execute(
                    """
                    INSERT INTO episodes (
                        created, kind, role, source, session_id, title, body,
                        tags, importance, redactions, char_len
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        _now(), kind, role or "", source or "", session_id or "",
                        title, body, tags, int(importance), redactions, len(body),
                    ),
                )
                episode_id = int(cur.lastrowid)
                if self._fts:
                    self._conn.execute(
                        "INSERT INTO episodes_fts(rowid, title, body, tags, kind) "
                        "VALUES (?, ?, ?, ?, ?)",
                        (episode_id, title, body, tags, kind),
                    )
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise
        return episode_id

    def _duplicate(self, kind: str, role: str, body: str, session_id: str) -> bool:
        """Drop the same line if it was just stored.

        A typed command and a transcript of that command can both arrive.
        Fifteen seconds is enough to collapse that pair and short enough
        that a person repeating themselves a minute later is kept.
        """
        row = self._conn.execute(
            """
            SELECT body, created FROM episodes
            WHERE session_id=? AND kind=? AND role=? AND deleted=0
            ORDER BY id DESC LIMIT 1
            """,
            (session_id or "", kind, role or ""),
        ).fetchone()
        if row is None or row["body"] != body:
            return False
        try:
            then = datetime.strptime(row["created"], "%Y-%m-%d %H:%M:%S")
            age = (datetime.now() - then).total_seconds()
        except Exception:
            return False
        return age >= 0 and age < 15

    def recall(self, query: str, *, limit: int = 8, char_budget: int = RECALL_CHARS) -> str:
        """Relevant episodes, packed into ``char_budget``. Never the whole table."""
        query = (query or "").strip()
        words = _words(query)
        rows = self._search_rows(query, limit=max(limit * 3, limit))
        if not rows:
            if not query:
                return ""
            return f"Nothing in the journal about '{_clip(query, 80)}'."
        if not query:
            header = "Recent memory (newest first, not the full history):"
        else:
            header = f"Journal entries matching '{_clip(query, 80)}':"

        # Newest-first when the user asked "what happened"; relevance order
        # when they asked for something specific. ``_search_rows`` already
        # returns that order.
        lines = [header]
        used = len(header) + 1
        shown = 0
        for row in rows:
            if shown >= limit:
                break
            snippet = _snippet(row["body"] or row["title"] or "", words)
            role = f" {row['role']}" if row["role"] else ""
            line = (
                f"[{row['created']} {row['kind']}{role}] "
                f"{(row['title'] + ': ') if row['title'] else ''}{snippet}"
            )
            if used + len(line) + 1 > char_budget:
                break
            lines.append(line)
            used += len(line) + 1
            shown += 1
        hidden = max(0, len(rows) - shown)
        if hidden and used + 48 <= char_budget:
            lines.append(f"(+{hidden} more — ask with a narrower keyword)")
        text = "\n".join(lines)
        if len(text) > char_budget:
            text = text[: char_budget - 1].rstrip() + "…"
        return text

    def _search_rows(self, query: str, limit: int) -> list[sqlite3.Row]:
        query = (query or "").strip()
        with self._lock:
            if not query:
                return list(self._conn.execute(
                    """
                    SELECT id, created, kind, role, title, body, source
                    FROM episodes
                    WHERE deleted=0 AND compacted=0
                    ORDER BY id DESC
                    LIMIT ?
                    """,
                    (max(1, limit),),
                ))
            if self._fts:
                fts = _fts_query(query)
                if fts:
                    try:
                        return list(self._conn.execute(
                            """
                            SELECT e.id, e.created, e.kind, e.role, e.title, e.body, e.source,
                                   bm25(episodes_fts) AS rank
                            FROM episodes_fts
                            JOIN episodes e ON e.id = episodes_fts.rowid
                            WHERE episodes_fts MATCH ?
                              AND e.deleted=0 AND e.compacted=0
                            ORDER BY rank
                            LIMIT ?
                            """,
                            (fts, max(1, limit)),
                        ))
                    except sqlite3.OperationalError as exc:
                        print(f"[Memory] FTS query failed ({exc}); scanning recent rows")
            return self._lexical_fallback(query, limit)

    def _lexical_fallback(self, query: str, limit: int) -> list[sqlite3.Row]:
        """Used only when FTS5 is missing or the query cannot be compiled.

        Scans the newest 500 active rows, not the whole table. A hit that
        lives only in an older raw row is invisible on this path. Compaction
        is what keeps old topics inside that window, as summaries — and
        summaries are active rows. With FTS5 this path does not run.
        """
        words = _words(query)
        if not words:
            return []
        candidates = list(self._conn.execute(
            """
            SELECT id, created, kind, role, title, body, source
            FROM episodes
            WHERE deleted=0 AND compacted=0
            ORDER BY id DESC
            LIMIT 500
            """
        ))
        scored = []
        for row in candidates:
            hay_t = (row["title"] or "").lower()
            hay_b = (row["body"] or "").lower()
            score = 0
            for word in words:
                if word in hay_t:
                    score += 6
                if word in hay_b:
                    score += 3
                if word in (row["kind"] or ""):
                    score += 1
            if score:
                scored.append((score, row["id"], row))
        scored.sort(key=lambda item: (-item[0], -item[1]))
        return [item[2] for item in scored[: max(1, limit)]]

    def recent_for_prompt(self, char_budget: int = RECENT_PROMPT_CHARS) -> str:
        """A bounded slice for the system prompt. One indexed read, not a dump."""
        header = (
            "[RECENT MEMORY — older detail stays on disk. "
            "Call recall_memory before saying you do not know.]"
        )
        with self._lock:
            rows = list(self._conn.execute(
                """
                SELECT created, kind, role, title, body FROM episodes
                WHERE deleted=0 AND compacted=0
                  AND kind IN ('summary', 'conversation', 'decision', 'note', 'task')
                ORDER BY id DESC
                LIMIT 12
                """
            ))
        if not rows:
            return ""
        rows.reverse()
        lines = [header]
        used = len(header) + 1
        # Pack from the newest end so a long older summary cannot crowd out
        # the last thing that was said. ``rows`` is chronological; walk it
        # backwards and then restore order.
        chosen: list[str] = []
        for row in reversed(rows):
            snippet = _clip((row["body"] or row["title"] or "").replace("\n", " "), 180)
            if not snippet:
                continue
            who = row["role"] or row["kind"]
            line = f"- {who}: {snippet}"
            if used + len(line) + 1 > char_budget:
                continue
            chosen.append(line)
            used += len(line) + 1
        chosen.reverse()
        if not chosen:
            return ""
        text = "\n".join([header, *chosen])
        if len(text) > char_budget:
            text = text[: char_budget - 1].rstrip() + "…"
        return text

    def list_entries(self, *, limit: int = 40, include_compacted: bool = False) -> list[dict]:
        clause = "" if include_compacted else "AND compacted=0"
        with self._lock:
            rows = self._conn.execute(
                f"""
                SELECT id, created, kind, role, title, body, source, importance,
                       compacted, redactions
                FROM episodes
                WHERE deleted=0 {clause}
                ORDER BY id DESC
                LIMIT ?
                """,
                (max(1, min(limit, 200)),),
            ).fetchall()
        out = []
        for row in rows:
            out.append({
                "id": row["id"],
                "created": row["created"],
                "kind": row["kind"],
                "role": row["role"],
                "title": row["title"],
                "body": row["body"],
                "source": row["source"],
                "importance": row["importance"],
                "compacted": row["compacted"],
                "redactions": row["redactions"],
            })
        return out

    def search_entries(self, query: str, *, limit: int = 40) -> list[dict]:
        """Indexed lookup for the memory panel. Returns the same shape as ``list_entries``."""
        rows = self._search_rows((query or "").strip(), limit=max(1, min(limit, 80)))
        out = []
        for row in rows:
            out.append({
                "id": row["id"],
                "created": row["created"],
                "kind": row["kind"],
                "role": row["role"],
                "title": row["title"],
                "body": row["body"],
                "source": row["source"],
                "importance": 0,
                "compacted": 0,
                "redactions": 0,
            })
        return out

    def correct(self, episode_id: int, body: str) -> str:
        """Replace one episode's text and pin it so compaction leaves it alone."""
        try:
            episode_id = int(episode_id)
        except (TypeError, ValueError):
            return "That is not a memory id."
        body, n = redact_text(body or "")
        body = _clip(body, BODY_MAX)
        if not body:
            return "Nothing to store."
        with self._lock:
            row = self._conn.execute(
                "SELECT id, kind, tags, title FROM episodes WHERE id=? AND deleted=0",
                (episode_id,),
            ).fetchone()
            if row is None:
                return f"No memory #{episode_id}."
            self._conn.execute("BEGIN")
            try:
                self._conn.execute(
                    """
                    UPDATE episodes
                    SET body=?, redactions=redactions+?, char_len=?, importance=1,
                        compacted=0
                    WHERE id=?
                    """,
                    (body, n, len(body), episode_id),
                )
                self._reindex(episode_id, row["title"], body, row["tags"], row["kind"])
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise
            try:
                self.vacuum()
            except sqlite3.Error as exc:
                print(f"[Memory] vacuum after correct failed: {exc}")
        return f"Updated memory #{episode_id}."

    def forget(self, episode_id: int) -> str:
        """Delete one episode. The row is removed, not hidden.

        ``secure_delete`` is on, and a checkpoint follows. That overwrites
        the row inside this file. It does not cover backups or other copies.
        """
        try:
            episode_id = int(episode_id)
        except (TypeError, ValueError):
            return "That is not a memory id."
        with self._lock:
            self._conn.execute("BEGIN")
            try:
                if self._fts:
                    self._conn.execute(
                        "DELETE FROM episodes_fts WHERE rowid=?", (episode_id,)
                    )
                cur = self._conn.execute(
                    "DELETE FROM episodes WHERE id=?", (episode_id,)
                )
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise
            removed = cur.rowcount > 0
            if removed:
                self._checkpoint()
                try:
                    # Free pages can keep the deleted text. VACUUM rebuilds
                    # the file. It is not a wipe of backups or other copies.
                    self._conn.execute("VACUUM")
                except sqlite3.Error as exc:
                    print(f"[Memory] vacuum after delete failed: {exc}")
        if not removed:
            return f"No memory #{episode_id}."
        return f"Deleted memory #{episode_id}."

    def _checkpoint(self) -> None:
        try:
            self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except sqlite3.Error as exc:
            print(f"[Memory] checkpoint failed: {exc}")

    def vacuum(self) -> None:
        """Rebuild the file so freed pages do not keep old bytes.

        Requires no other connection. We hold the only one. VACUUM cannot
        run inside a transaction.
        """
        with self._lock:
            self._checkpoint()
            self._conn.execute("VACUUM")

    def compact_if_needed(
        self,
        *,
        active_cap: int = ACTIVE_EPISODE_CAP,
        char_cap: int = ACTIVE_CHAR_CAP,
        keep_recent: int = KEEP_RECENT,
        batch: int = COMPACT_BATCH,
        summary_chars: int = SUMMARY_CHARS,
    ) -> dict:
        """Fold the oldest raw episodes into an extractive summary.

        Pinned rows (importance >= 1) and decisions/notes/summaries are not
        folded. The newest ``keep_recent`` active rows are not folded either,
        so the conversation you are in stays verbatim.
        """
        folded = 0
        passes = 0
        with self._lock:
            # Frozen for this call. Recomputing it each pass lets a summary
            # just inserted become one of the "newest" rows and push a
            # verbatim turn into the next batch.
            keep_ids = [
                row["id"] for row in self._conn.execute(
                    """
                    SELECT id FROM episodes
                    WHERE deleted=0 AND compacted=0
                    ORDER BY id DESC
                    LIMIT ?
                    """,
                    (max(0, keep_recent),),
                )
            ]
            while passes < 6:
                passes += 1
                count, chars = self._active_size()
                if count <= active_cap and chars <= char_cap:
                    break
                params: list = list(keep_ids)
                not_in = ""
                if keep_ids:
                    not_in = " AND id NOT IN (" + ",".join("?" * len(keep_ids)) + ")"
                params.append(max(1, batch))
                rows = list(self._conn.execute(
                    f"""
                    SELECT id, created, kind, role, title, body, session_id
                    FROM episodes
                    WHERE deleted=0 AND compacted=0 AND importance=0
                      AND kind IN ('conversation', 'action', 'result', 'task')
                      {not_in}
                    ORDER BY id ASC
                    LIMIT ?
                    """,
                    params,
                ))
                if not rows:
                    break
                summary = _summarize(rows, limit=summary_chars)
                if not summary:
                    summary = f"{len(rows)} earlier entries were folded."
                self._conn.execute("BEGIN")
                try:
                    created = rows[0]["created"]
                    cur = self._conn.execute(
                        """
                        INSERT INTO episodes (
                            created, kind, role, source, session_id, title, body,
                            tags, importance, redactions, char_len
                        ) VALUES (?, 'summary', '', 'compaction', ?, ?, ?, 'compacted', 0, 0, ?)
                        """,
                        (
                            created,
                            rows[0]["session_id"] or "",
                            f"Folded {len(rows)} entries",
                            summary,
                            len(summary),
                        ),
                    )
                    summary_id = int(cur.lastrowid)
                    if self._fts:
                        self._conn.execute(
                            "INSERT INTO episodes_fts(rowid, title, body, tags, kind) "
                            "VALUES (?, ?, ?, 'compacted', 'summary')",
                            (summary_id, f"Folded {len(rows)} entries", summary),
                        )
                    ids = [row["id"] for row in rows]
                    marks = ",".join("?" * len(ids))
                    self._conn.execute(
                        f"UPDATE episodes SET compacted=1 WHERE id IN ({marks})",
                        ids,
                    )
                    if self._fts:
                        self._conn.execute(
                            f"DELETE FROM episodes_fts WHERE rowid IN ({marks})",
                            ids,
                        )
                    self._conn.execute("COMMIT")
                except Exception:
                    self._conn.execute("ROLLBACK")
                    raise
                folded += len(rows)
            purged = self._purge_compacted()
            count, chars = self._active_size()
        return {"folded": folded, "purged": purged, "active": count, "chars": chars}

    def _active_size(self) -> tuple[int, int]:
        row = self._conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(char_len), 0) FROM episodes "
            "WHERE deleted=0 AND compacted=0"
        ).fetchone()
        return int(row[0]), int(row[1])

    def _purge_compacted(self) -> int:
        row = self._conn.execute(
            "SELECT COUNT(*) FROM episodes WHERE compacted=1 AND deleted=0"
        ).fetchone()
        extra = int(row[0]) - MAX_COMPACTED_ROWS
        if extra <= 0:
            return 0
        doomed = [
            r["id"] for r in self._conn.execute(
                "SELECT id FROM episodes WHERE compacted=1 AND deleted=0 "
                "ORDER BY id ASC LIMIT ?",
                (extra,),
            )
        ]
        if not doomed:
            return 0
        marks = ",".join("?" * len(doomed))
        self._conn.execute("BEGIN")
        try:
            self._conn.execute(
                f"DELETE FROM episodes WHERE id IN ({marks})", doomed
            )
            self._conn.execute("COMMIT")
        except Exception:
            self._conn.execute("ROLLBACK")
            raise
        return len(doomed)

    def session_summary(self, session_id: str, *, minimum_turns: int = 2) -> str:
        """Extractive one-session summary for the next briefing. No model call.

        Marks the covered rows ``summarized`` so a reconnect does not brief
        the same stretch again. The rows stay recallable until compaction.
        Returns '' when there is not enough new conversation.
        """
        session_id = session_id or ""
        with self._lock:
            rows = list(self._conn.execute(
                """
                SELECT id, kind, role, title, body, created, session_id
                FROM episodes
                WHERE session_id=? AND deleted=0 AND summarized=0
                  AND kind IN ('conversation', 'action', 'result', 'task')
                ORDER BY id ASC
                """,
                (session_id,),
            ))
            spoken = [r for r in rows if r["kind"] == "conversation"]
            if len(spoken) < minimum_turns:
                return ""
            text = _clip(_summarize(rows, limit=280).replace("\n", " "), 280)
            if not text:
                return ""
            self._conn.execute("BEGIN")
            try:
                self._conn.execute(
                    """
                    INSERT INTO episodes (
                        created, kind, role, source, session_id, title, body,
                        tags, importance, redactions, char_len, summarized
                    ) VALUES (?, 'summary', '', 'session', ?, 'Session', ?, 'session', 0, 0, ?, 1)
                    """,
                    (_now(), session_id, text, len(text)),
                )
                summary_id = int(self._conn.execute("SELECT last_insert_rowid()").fetchone()[0])
                if self._fts:
                    self._conn.execute(
                        "INSERT INTO episodes_fts(rowid, title, body, tags, kind) "
                        "VALUES (?, 'Session', ?, 'session', 'summary')",
                        (summary_id, text),
                    )
                ids = [r["id"] for r in rows]
                marks = ",".join("?" * len(ids))
                self._conn.execute(
                    f"UPDATE episodes SET summarized=1 WHERE id IN ({marks})",
                    ids,
                )
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise
        return text

    def stats(self) -> dict:
        with self._lock:
            active, chars = self._active_size()
            compacted = self._conn.execute(
                "SELECT COUNT(*) FROM episodes WHERE compacted=1"
            ).fetchone()[0]
            total = self._conn.execute("SELECT COUNT(*) FROM episodes").fetchone()[0]
        return {
            "version": MEMORY_VERSION,
            "path": str(self.path),
            "fts": self._fts,
            "active": active,
            "chars": chars,
            "compacted": int(compacted),
            "rows": int(total),
            "redact_revision": REDACT_REVISION,
        }

    def contains_bytes(self, needle: str) -> bool:
        """True if ``needle`` occurs anywhere in the database file or its WAL.

        Tests use this. A match in a free page still counts — callers that
        just deleted a row should checkpoint (and vacuum, if they need the
        free pages gone) before asking.
        """
        self._checkpoint()
        raw = needle.encode("utf-8")
        for suffix in ("", "-wal", "-shm"):
            candidate = Path(str(self.path) + suffix) if suffix else self.path
            if candidate.exists() and raw in candidate.read_bytes():
                return True
        return False


# ── Process-wide journal ─────────────────────────────────────────────────────
# The voice session, the dashboard, plugins and the memory panel all talk to
# this one file. Tests replace it so they never touch a developer's journal.

_default: Journal | None = None
_default_guard = threading.Lock()


def set_default_journal(journal: Journal | None) -> None:
    global _default
    with _default_guard:
        _default = journal


def default_journal() -> Journal:
    global _default
    with _default_guard:
        if _default is None:
            _default = Journal(default_path())
        return _default


def record(kind: str, body: str, **kwargs) -> int | None:
    try:
        return default_journal().record(kind, body, **kwargs)
    except Exception as exc:
        print(f"[Memory] record failed: {exc}")
        return None


def record_turn(role: str, text: str, session_id: str, source: str = "voice") -> int | None:
    text = (text or "").strip()
    if not text:
        return None
    return record(
        "conversation", text, role=role, source=source, session_id=session_id,
    )


def record_tool(name: str, args, result: str, session_id: str) -> int | None:
    """One row per tool call: what was asked and what came back.

    ``recall_memory`` and ``manage_memory`` are skipped so the journal does
    not contain a copy of itself. ``system_status`` is skipped because it
    fires as a meter, not as something worth remembering.
    """
    if name in _SKIP_TOOLS:
        return None
    try:
        rendered = json.dumps(args, ensure_ascii=False, default=str)
    except Exception:
        rendered = str(args)
    body = "args: " + _clip(rendered, TOOL_ARGS_MAX) + "\nresult: " + _clip(
        str(result or ""), TOOL_RESULT_MAX
    )
    return record(
        "action",
        body,
        role="tool",
        source=f"tool:{name}",
        title=str(name or ""),
        session_id=session_id,
        tags=str(name or ""),
    )


def recall(query: str, **kwargs) -> str:
    try:
        return default_journal().recall(query, **kwargs)
    except Exception as exc:
        print(f"[Memory] recall failed: {exc}")
        return ""


def recent_for_prompt(char_budget: int = RECENT_PROMPT_CHARS) -> str:
    try:
        return default_journal().recent_for_prompt(char_budget)
    except Exception as exc:
        print(f"[Memory] recent context failed: {exc}")
        return ""


def list_entries(**kwargs) -> list[dict]:
    try:
        return default_journal().list_entries(**kwargs)
    except Exception as exc:
        print(f"[Memory] list failed: {exc}")
        return []


def search_entries(query: str, **kwargs) -> list[dict]:
    try:
        return default_journal().search_entries(query, **kwargs)
    except Exception as exc:
        print(f"[Memory] search failed: {exc}")
        return []


def correct(episode_id: int, body: str) -> str:
    try:
        return default_journal().correct(episode_id, body)
    except Exception as exc:
        return f"Could not update that memory: {exc}"


def forget(episode_id: int) -> str:
    try:
        return default_journal().forget(episode_id)
    except Exception as exc:
        return f"Could not delete that memory: {exc}"


def session_summary(session_id: str) -> str:
    try:
        text = default_journal().session_summary(session_id)
        default_journal().compact_if_needed()
        return text
    except Exception as exc:
        print(f"[Memory] session summary failed: {exc}")
        return ""


def stats() -> dict:
    try:
        return default_journal().stats()
    except Exception as exc:
        return {"error": str(exc)}


def self_check() -> dict:
    """Open the journal, confirm FTS, return stats. Used by the updater."""
    info = stats()
    info["ok"] = "error" not in info and bool(info.get("fts"))
    return info
