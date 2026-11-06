import json
import os
import re
from datetime import datetime
from threading import RLock
from pathlib import Path
import sys

from memory.redact import redact_text


def get_base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


BASE_DIR         = get_base_dir()
MEMORY_PATH      = BASE_DIR / "memory" / "long_term.json"
# RLock: a read-modify-write holds it, and a notifier or a nested load from
# the same thread must not deadlock. Threads that only share the lock
# (the voice loop, a tool worker, the monitor) queue instead of interleaving
# two updates and dropping one of them — that was the old load-then-save race.
_lock            = RLock()
MAX_VALUE_LENGTH = 380

# ── Why there are two very different numbers here ────────────────────────────
#
# There used to be one: MEMORY_MAX_CHARS = 2200, applied to the whole store. It
# was a *storage* limit, and it existed only because the entire memory was
# pasted into the system prompt on every connect — so growing the memory grew
# every single request. When it filled, _trim_to_limit() deleted the oldest
# entries and printed one line to a console nobody reads. A memory described as
# "deeply remembers projects, preferences and personal context" was in practice
# two pages long, and quietly forgot your sister's name after a few weeks.
#
# Storage and prompt budget are now separate concerns:
#
#   MEMORY_MAX_CHARS  — a runaway guard, not a feature limit. Nothing normal
#                       reaches it; a bug writing in a loop does.
#   PROMPT_CORE_CHARS — what actually rides in the system prompt every session.
#                       Smaller than the old whole-memory dump, so sessions
#                       start *faster* than before, not slower.
#
# Everything above the core stays on disk and is fetched on demand by the
# recall_memory tool — see search_memory() and format_memory_for_prompt().
MEMORY_MAX_CHARS  = 200_000
PROMPT_CORE_CHARS = 900
PROMPT_INDEX_CHARS = 420
# Most entries any one category may contribute to the core block, so a person
# with forty stored preferences still gets their sister into the prompt.
PROMPT_MAX_PER_CATEGORY = 6

def _empty_memory() -> dict:
    return {
        "identity":      {},
        "preferences":   {},
        "projects":      {},
        "relationships": {},
        "wishes":        {},
        "notes":         {},
    }

class MemoryReadError(Exception):
    """The fact file exists and could not be parsed. Writers must not replace it."""


def _normalize(data: dict) -> dict:
    # Fill categories the rest of the app assumes are dicts. Leave every other
    # key alone — `sessions` is a list and `monitors` is its own dict, and
    # both are user data this function must not reshape.
    base = _empty_memory()
    for key in base:
        if key not in data:
            data[key] = {}
    return data


def _load_unlocked() -> dict:
    """Caller holds ``_lock``. Raises MemoryReadError instead of returning empty
    when the file is present but unreadable — an empty dict written back would
    erase the user's facts."""
    if not MEMORY_PATH.exists():
        return _empty_memory()
    try:
        data = json.loads(MEMORY_PATH.read_text(encoding="utf-8"))
    except Exception as exc:
        print(f"[Memory] ⚠️ Load error: {exc}")
        raise MemoryReadError(str(exc)) from exc
    if not isinstance(data, dict):
        raise MemoryReadError("memory file is not a JSON object")
    return _normalize(data)


def load_memory() -> dict:
    """Facts for the prompt and the panel. A corrupt file reads as empty and
    is left on disk untouched.

    The first successful load scrubs secret-shaped fact values onto disk.
    Later loads in this process do not rewrite the file again.
    """
    global _facts_scrubbed
    with _lock:
        try:
            data = _load_unlocked()
        except MemoryReadError:
            return _empty_memory()
        if not _facts_scrubbed:
            removed = _redact_inplace(data)
            _facts_scrubbed = True
            if removed:
                _save_unlocked(data)
                print(f"[Memory] Redacted {removed} secret-shaped span(s) in {MEMORY_PATH.name}")
        return data


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


def update_raw(mutator) -> bool:
    """Locked read-modify-write of the whole fact file.

    ``mutator`` receives the dict and edits it in place. Returns False when
    the file could not be read, in which case nothing is written. Returns
    True after a successful replace. The journal is a different file; this
    only covers ``long_term.json``.
    """
    with _lock:
        try:
            memory = _load_unlocked()
        except MemoryReadError as exc:
            print(f"[Memory] refusing to write over unreadable {MEMORY_PATH}: {exc}")
            return False
        mutator(memory)
        _save_unlocked(memory)
        return True


def _save_unlocked(memory: dict) -> None:
    memory = _trim_to_limit(memory)
    _atomic_write(
        MEMORY_PATH,
        json.dumps(memory, indent=2, ensure_ascii=False),
    )

def _all_entries(memory: dict) -> list[tuple]:
    entries = []
    for cat, items in memory.items():
        if not isinstance(items, dict):
            continue
        for key, entry in items.items():
            if isinstance(entry, dict) and "value" in entry:
                entries.append((cat, key, entry))
    return entries


# Set by main.py so a trim can reach the activity log. Deleting something a
# person told you and mentioning it only on stdout is how a memory loses trust.
_trim_notifier = None


def set_trim_notifier(fn) -> None:
    """Register a callable(str) that surfaces trims to the user."""
    global _trim_notifier
    _trim_notifier = fn


def _trim_to_limit(memory: dict) -> dict:
    if len(json.dumps(memory, ensure_ascii=False)) <= MEMORY_MAX_CHARS:
        return memory
    entries = _all_entries(memory)
    entries.sort(key=lambda t: t[2].get("updated", "0000-00-00"))
    dropped = []
    for cat, key, _ in entries:
        if len(json.dumps(memory, ensure_ascii=False)) <= MEMORY_MAX_CHARS:
            break
        del memory[cat][key]
        dropped.append(f"{cat}/{key}")
        print(f"[Memory] 🗑️  Trimmed {cat}/{key}")
    if dropped and _trim_notifier:
        try:
            _trim_notifier(
                f"SYS: Memory full — forgot {len(dropped)} oldest entries "
                f"({', '.join(dropped[:3])}{'…' if len(dropped) > 3 else ''})"
            )
        except Exception:
            pass
    return memory

def save_memory(memory: dict) -> None:
    """Replace the fact file. Prefer ``update_raw`` when you are editing the
    copy you just loaded — two of those in parallel drop a key."""
    if not isinstance(memory, dict):
        return
    with _lock:
        _save_unlocked(memory)


def _truncate_value(val: str) -> str:
    if isinstance(val, str) and len(val) > MAX_VALUE_LENGTH:
        return val[:MAX_VALUE_LENGTH].rstrip() + "…"
    return val


def _recursive_update(target: dict, updates: dict) -> bool:
    changed = False
    for key, value in updates.items():
        if value is None:
            continue
        if isinstance(value, str) and not value.strip():
            continue
        if isinstance(value, dict) and "value" not in value:
            if key not in target or not isinstance(target[key], dict):
                target[key] = {}
                changed = True
            if _recursive_update(target[key], value):
                changed = True
        else:
            raw = str(value["value"] if isinstance(value, dict) else value)
            raw, _redactions = redact_text(raw)
            new_val  = _truncate_value(raw)
            entry    = {"value": new_val, "updated": datetime.now().strftime("%Y-%m-%d")}
            existing = target.get(key, {})
            if not isinstance(existing, dict) or existing.get("value") != new_val:
                target[key] = entry
                changed = True
    return changed


def update_memory(memory_update: dict) -> dict:
    if not isinstance(memory_update, dict) or not memory_update:
        return load_memory()
    holder: dict = {}

    def _mut(memory: dict) -> None:
        if _recursive_update(memory, memory_update):
            holder["changed"] = True
            print(f"[Memory] 💾 Saved: {list(memory_update.keys())}")
        holder["memory"] = memory

    if not update_raw(_mut):
        return load_memory()
    return holder.get("memory") or load_memory()

def _entry_value(entry) -> str:
    """Accept both the {'value': ..., 'updated': ...} shape and a bare string,
    because early versions of the store wrote plain strings.

    Redacted on the way out as well as on the way in, so a fact written
    before a pattern existed is not pasted into the prompt verbatim. The
    file itself is rewritten by ``scrub_facts`` on the first successful load.
    """
    if isinstance(entry, dict):
        raw = str(entry.get("value", "") or "").strip()
    else:
        raw = str(entry or "").strip()
    cleaned, _n = redact_text(raw)
    return cleaned.strip()


def _redact_inplace(memory: dict) -> int:
    """Rewrite fact values the redactor recognises. Returns spans removed."""
    removed = 0
    categories = set(_empty_memory())
    for cat, items in list(memory.items()):
        if cat == "sessions" and isinstance(items, list):
            for entry in items:
                if isinstance(entry, dict) and isinstance(entry.get("summary"), str):
                    cleaned, n = redact_text(entry["summary"])
                    if n:
                        entry["summary"] = cleaned
                        removed += n
            continue
        if cat not in categories or not isinstance(items, dict):
            continue
        for key, entry in list(items.items()):
            if isinstance(entry, dict) and "value" in entry:
                cleaned, n = redact_text(str(entry.get("value") or ""))
                if n:
                    entry["value"] = _truncate_value(cleaned)
                    removed += n
            elif isinstance(entry, str):
                cleaned, n = redact_text(entry)
                if n:
                    items[key] = {
                        "value": _truncate_value(cleaned),
                        "updated": datetime.now().strftime("%Y-%m-%d"),
                    }
                    removed += n
    return removed


_facts_scrubbed = False


def scrub_facts() -> int:
    """Rewrite ``long_term.json`` once if any stored fact matches the redactor.

    A miss stays a miss. This does not encrypt the file.
    """
    global _facts_scrubbed
    with _lock:
        try:
            memory = _load_unlocked()
        except MemoryReadError:
            return 0
        removed = _redact_inplace(memory)
        if removed:
            _save_unlocked(memory)
        _facts_scrubbed = True
        return removed


def _pretty(key: str) -> str:
    return key.replace("_", " ").strip()


# Identity is always in the prompt; these categories compete for the remaining
# budget by recency.
_CATEGORY_LABELS = {
    "preferences":   "Preferences",
    "projects":      "Active projects / goals",
    "relationships": "People in their life",
    "wishes":        "Wishes / plans",
    "notes":         "Notes",
}

_IDENTITY_FIELDS = ["name", "age", "birthday", "city", "job",
                    "language", "school", "nationality"]


def format_memory_for_prompt(memory: dict | None) -> str:
    """Build the memory block that goes into the system prompt.

    This used to dump everything. It now sends three things:

      1. IDENTITY  - always, in full. It is small, and it is wrong for the
         assistant to have to look up your name.
      2. RECENT    - the most recently updated entries from every other
         category, up to PROMPT_CORE_CHARS. Recency is the cheapest useful
         relevance signal available without embeddings.
      3. AN INDEX  - the *keys* of everything else, values omitted.

    Point 3 is what makes recall work at all. A model cannot decide to look
    something up if it does not know the thing exists: with only points 1 and 2,
    "who is Ayse?" would get "I don't know" while ayse_sister sat on disk
    unread. The index costs a few hundred characters and turns recall from a
    gamble into a lookup.

    Net effect on latency: this block is SMALLER than the old full dump, so
    every session connects with fewer tokens. Occasionally the model spends one
    extra round trip on recall_memory - covered by the acknowledgment it
    already speaks before any slow step."""
    if not memory:
        return ""

    core_lines: list[str] = []

    # 1. Identity - always, in full
    identity = memory.get("identity", {}) or {}
    for field in _IDENTITY_FIELDS:
        val = _entry_value(identity.get(field))
        if not val:
            continue
        if field == "language":
            # Labelled as an observation, not a setting. A bare "Language:
            # English" line written months ago reads like a standing order and
            # was one of the reasons a Turkish question came back in English.
            core_lines.append(
                f"Has spoken to you in: {val} (an observation about the past — "
                f"always answer in the language of their CURRENT message)")
        else:
            core_lines.append(f"{field.title()}: {val}")
    for key, entry in identity.items():
        if key in _IDENTITY_FIELDS:
            continue
        val = _entry_value(entry)
        if val:
            core_lines.append(f"{_pretty(key).title()}: {val}")

    # 2. Everything else, most recently updated first
    rest: list[tuple[str, str, str, str]] = []   # (updated, cat, key, value)
    for cat in _CATEGORY_LABELS:
        for key, entry in (memory.get(cat, {}) or {}).items():
            val = _entry_value(entry)
            if not val:
                continue
            updated = (entry.get("updated", "") if isinstance(entry, dict) else "") or "0000-00-00"
            rest.append((updated, cat, key, val))
    rest.sort(key=lambda t: t[0], reverse=True)

    used    = sum(len(l) + 1 for l in core_lines)
    shown: dict[str, list[str]] = {}
    overflow: dict[str, list[str]] = {}

    # Recency decides order, but no single category may take the whole budget.
    # Without the cap, someone with forty stored preferences gets a prompt that
    # is forty preferences and not one person's name — the categories that
    # matter most in conversation are also the ones that change least often, so
    # pure recency systematically buries them.
    per_cat_used: dict[str, int] = {}
    for _updated, cat, key, val in rest:
        line = f"  - {_pretty(key).title()}: {val}"
        if (per_cat_used.get(cat, 0) < PROMPT_MAX_PER_CATEGORY
                and used + len(line) + 1 <= PROMPT_CORE_CHARS):
            shown.setdefault(cat, []).append(line)
            per_cat_used[cat] = per_cat_used.get(cat, 0) + 1
            used += len(line) + 1
        else:
            overflow.setdefault(cat, []).append(_pretty(key))

    # The index is a table of contents, so it is interleaved across categories
    # rather than continuing in recency order. Sorted by recency it would list
    # twenty-four preferences before the first relationship, and the one entry
    # the index exists for — the old fact the model has no other way to know
    # about — would fall off the end.
    indexed: list[str] = []
    if overflow:
        cats  = [c for c in _CATEGORY_LABELS if overflow.get(c)]
        cursor = {c: 0 for c in cats}
        while cats:
            for cat in list(cats):
                i = cursor[cat]
                if i >= len(overflow[cat]):
                    cats.remove(cat)
                    continue
                indexed.append(overflow[cat][i])
                cursor[cat] = i + 1

    for cat, label in _CATEGORY_LABELS.items():
        if shown.get(cat):
            core_lines.append("")
            core_lines.append(f"{label}:")
            core_lines.extend(shown[cat])

    if not core_lines and not indexed:
        return ""

    out = [
        "[WHAT YOU KNOW ABOUT THIS PERSON — use naturally, never recite like a list]",
        *core_lines,
    ]

    # 3. The index of what is on disk but not in this prompt
    if indexed:
        budget, names = PROMPT_INDEX_CHARS, []
        for n in indexed:
            if budget - len(n) - 2 < 0:
                break
            names.append(n)
            budget -= len(n) + 2
        if names:
            out.append("")
            out.append(
                "[ALSO REMEMBERED — values not shown here. Call recall_memory "
                "with a keyword to read any of these before saying you do not know]"
            )
            out.append(", ".join(names)
                       + (f" (+{len(indexed) - len(names)} more)"
                          if len(indexed) > len(names) else ""))

    return "\n".join(out) + "\n"


# ── Recall ────────────────────────────────────────────────────────────────────

def _score(query_words: list[str], cat: str, key: str, value: str) -> int:
    """Cheap lexical relevance. No embeddings, no network, no model call - this
    runs in well under a millisecond, which is the entire point: recall must
    cost one model round trip, never two."""
    hay_key = _pretty(key).lower()
    hay_val = value.lower()
    score   = 0
    for w in query_words:
        if not w:
            continue
        if w == hay_key:
            score += 10
        elif w in hay_key:
            score += 6
        if w in hay_val:
            score += 3
        if w in cat:
            score += 1
    return score


# What recall_memory is allowed to hand back in one turn. Facts and journal
# share it. The prompt has its own, smaller, budgets in format_memory_for_prompt
# and store.recent_for_prompt.
RECALL_CHAR_BUDGET = 1_800


def _search_facts(query: str, limit: int) -> str:
    memory = load_memory()
    words  = [w for w in re.split(r"[^\w]+", (query or "").lower()) if len(w) > 1]

    rows: list[tuple[int, str, str, str]] = []
    for cat, items in memory.items():
        if not isinstance(items, dict):
            continue                     # skip 'sessions', which is a list
        for key, entry in items.items():
            val = _entry_value(entry)
            if not val:
                continue
            s = _score(words, cat, key, val) if words else 1
            if s > 0:
                rows.append((s, cat, key, val))

    if not rows:
        return ""

    rows.sort(key=lambda r: (-r[0], r[2]))
    lines = [f"{cat}/{_pretty(key)}: {val}" for _s, cat, key, val in rows[:max(1, limit)]]
    head  = (f"Stored facts matching '{query}':" if query
             else "Everything currently stored:")
    more  = (f"\n(+{len(rows) - len(lines)} more — search with a narrower keyword)"
             if len(rows) > len(lines) else "")
    return head + "\n" + "\n".join(lines) + more


def _bound(text: str, budget: int) -> str:
    if len(text) <= budget:
        return text
    return text[: budget - 1].rstrip() + "…"


def search_memory(query: str, limit: int = 8) -> str:
    """Facts plus the journal, packed into one bounded reply.

    Backs the recall_memory tool. An empty query lists what is stored, still
    capped — it is the answer to "what do you remember about me?", not a
    dump of every turn. The journal is read through its index. A failure
    there still returns the facts.
    """
    facts = _search_facts(query, limit)
    journal = ""
    try:
        from memory import store
        remaining = RECALL_CHAR_BUDGET - (len(facts) + 2 if facts else 0)
        if remaining > 80:
            journal = store.recall(query, limit=limit, char_budget=remaining)
    except Exception as exc:
        print(f"[Memory] journal recall failed: {exc}")

    if facts and journal:
        text = facts + "\n\n" + journal
    elif facts:
        text = facts
    elif journal:
        text = journal
    elif query:
        text = f"Nothing stored about '{query}'."
    else:
        text = "I have not stored anything about this person yet."
    return _bound(text, RECALL_CHAR_BUDGET)


def manage_memory(action: str, *, query: str = "", category: str = "",
                  key: str = "", episode_id=None, value: str = "") -> str:
    """Review, correct, or delete. Used by the manage_memory tool and the panel.

    Never raises. Deleting a fact does not delete journal lines that mention
    it, and the reverse is also true — say which one changed.
    """
    action = (action or "").strip().lower()
    try:
        if action in ("list", "review", "show"):
            return _manage_list(query)
        if action in ("correct", "update", "edit"):
            return _manage_correct(category, key, episode_id, value)
        if action in ("forget", "delete", "remove"):
            return _manage_forget(category, key, episode_id)
        return "Use list, correct, or forget."
    except Exception as exc:
        return f"Memory update failed: {exc}"


def _manage_list(query: str) -> str:
    facts = all_entries_for_ui()
    lines = ["Facts:"]
    if not facts:
        lines.append("(none)")
    for row in facts[:12]:
        lines.append(f"- {row['category']}/{row['key']} = {row['value']}")
    if len(facts) > 12:
        lines.append(f"(+{len(facts) - 12} more facts)")
    try:
        from memory import store
        episodes = store.list_entries(limit=12)
    except Exception as exc:
        episodes = []
        lines.append(f"(journal unavailable: {exc})")
    lines.append("Journal:")
    if not episodes:
        lines.append("(none)")
    for row in episodes:
        snippet = (row.get("body") or "").replace("\n", " ")[:80]
        lines.append(f"- #{row['id']} {row['kind']} {snippet}")
    text = "\n".join(lines)
    if query:
        # A filtered view still has to come from the index, not from a
        # client-side scan of a list we already truncated.
        found = search_memory(query, limit=6)
        text = text + "\n\n" + found
    return _bound(text, RECALL_CHAR_BUDGET)


def _manage_correct(category: str, key: str, episode_id, value: str) -> str:
    if episode_id not in (None, ""):
        from memory import store
        return store.correct(episode_id, value or "")
    if key:
        return correct(key, value or "", category or "notes")
    return "Say which fact (category and key) or which journal id to correct."


def _manage_forget(category: str, key: str, episode_id) -> str:
    if episode_id not in (None, ""):
        from memory import store
        return store.forget(episode_id)
    if key:
        return forget(key, category or "notes")
    return "Say which fact (category and key) or which journal id to delete."


def all_entries_for_ui() -> list[dict]:
    """Flat list for the memory panel: what JARVIS knows, and when it learned it.
    Sorted newest first so the panel opens on what changed most recently."""
    memory = load_memory()
    rows = []
    for cat, items in memory.items():
        if not isinstance(items, dict):
            continue
        for key, entry in items.items():
            val = _entry_value(entry)
            if not val:
                continue
            rows.append({
                "category": cat,
                "key":      key,
                "value":    val,
                "updated":  (entry.get("updated", "") if isinstance(entry, dict) else ""),
            })
    rows.sort(key=lambda r: (r["updated"] or "0000-00-00"), reverse=True)
    return rows

def remember(key: str, value: str, category: str = "notes") -> str:
    valid = {"identity", "preferences", "projects", "relationships", "wishes", "notes"}
    if category not in valid:
        category = "notes"
    update_memory({category: {key: {"value": value}}})
    return f"Remembered: {category}/{key} = {value}"


def forget(key: str, category: str = "notes") -> str:
    found = {"yes": False}

    def _mut(memory: dict) -> None:
        cat = memory.get(category, {})
        if isinstance(cat, dict) and key in cat:
            del cat[key]
            memory[category] = cat
            found["yes"] = True

    if not update_raw(_mut):
        return f"Could not update memory — {MEMORY_PATH.name} could not be read."
    if found["yes"]:
        return f"Forgotten: {category}/{key}"
    return f"Not found: {category}/{key}"


def correct(key: str, value: str, category: str = "notes") -> str:
    """Replace one fact. The new text goes through the same redactor as a save.

    This does not rewrite older journal lines that quoted the old value.
    Those stay until they are deleted or compacted. The fact itself is what
    the prompt prefers.
    """
    valid = {"identity", "preferences", "projects", "relationships", "wishes", "notes"}
    if category not in valid:
        category = "notes"
    cleaned, _n = redact_text(value or "")
    cleaned = _truncate_value(cleaned.strip())
    if not cleaned:
        return "Nothing to store."
    found = {"yes": False}

    def _mut(memory: dict) -> None:
        cat = memory.get(category)
        if not isinstance(cat, dict) or key not in cat:
            return
        cat[key] = {"value": cleaned, "updated": datetime.now().strftime("%Y-%m-%d")}
        memory[category] = cat
        found["yes"] = True

    if not update_raw(_mut):
        return f"Could not update memory — {MEMORY_PATH.name} could not be read."
    if not found["yes"]:
        return f"Not found: {category}/{key}"
    return f"Updated: {category}/{key}"


forget_memory = forget


# ── Session memory ─────────────────────────────────────────────────────────────

_SESSION_MAX = 3   # safety cap — in practice 0-1 entries after pop


def save_session_summary(summary: str, language: str = "") -> None:
    """Append a 1-2 sentence session summary to long_term.json['sessions'].

    The journal keeps its own copy. This list is only the line the next
    morning briefing speaks once and then pops. It is still capped at
    ``_SESSION_MAX`` so a crash before the pop cannot grow it without bound.
    """
    summary, _n = redact_text((summary or "").strip())
    if not summary:
        return
    entry: dict = {
        "date":    datetime.now().strftime("%Y-%m-%d"),
        "summary": summary[:280],
    }
    if language:
        entry["language"] = language

    def _mut(memory: dict) -> None:
        sessions = memory.get("sessions", [])
        if not isinstance(sessions, list):
            sessions = []
        sessions.append(entry)
        memory["sessions"] = sessions[-_SESSION_MAX:]

    if update_raw(_mut):
        print(f"[Memory] 📝 Session saved ({entry['date']}): {summary[:60]}…")


def pop_last_session() -> dict | None:
    """
    Return AND remove the most recent session entry.
    Calling this consumes the entry so it is never repeated in future briefings.
    The journal copy is not removed — recall can still find it.
    """
    found: dict = {}

    def _mut(memory: dict) -> None:
        sessions = memory.get("sessions", [])
        if not isinstance(sessions, list) or not sessions:
            return
        found["entry"] = sessions.pop()
        memory["sessions"] = sessions

    try:
        if not update_raw(_mut):
            return None
    except Exception as exc:
        print(f"[Memory] ⚠️ pop_last_session error: {exc}")
        return None
    return found.get("entry")