"""
BackgroundMonitor — user-configured topic watching.
Checks DDG news once per day per topic; alerts JARVIS when a new headline appears.
No crypto, no finance, no uninvited tracking.
"""
import hashlib
import re
from datetime import datetime
from pathlib import Path


# ── Blocked categories (never monitor regardless of what user says) ────────────

_BLOCKED = {
    # Brand / asset names — spelled the same in every language
    "bitcoin", "ethereum", "dogecoin", "solana", "binance",
    "nft", "blockchain", "defi", "altcoin", "memecoin", "coin", "token",
    # spellings of the "crypto" root across different languages
    "crypto", "kripto", "cripto", "krypto", "крипто", "仮想通貨", "暗号資産",
    "cryptocurrency",
}

def _is_blocked(topic: str) -> bool:
    t = topic.lower()
    return any(word in t for word in _BLOCKED)


# ── Slug / hash helpers ────────────────────────────────────────────────────────

def _slug(topic: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", topic.lower().strip())[:40].strip("_")

def _title_hash(title: str) -> str:
    return hashlib.md5(title.encode("utf-8", errors="ignore")).hexdigest()[:12]


# ── Memory I/O ─────────────────────────────────────────────────────────────────

def _load() -> dict:
    from memory.memory_manager import load_memory
    data = load_memory().get("monitors", {})
    return data if isinstance(data, dict) else {}

def _save(monitors: dict) -> None:
    """Write check results without dropping a topic added while news was fetching.

    Only ``last_check`` / ``last_hash`` are copied onto the rows still
    present. A topic removed during the fetch stays removed. A topic added
    during the fetch stays added. Facts outside ``monitors`` are untouched
    because the write goes through ``update_raw``.
    """
    from memory.memory_manager import update_raw

    def _mut(memory: dict) -> None:
        current = memory.get("monitors")
        if not isinstance(current, dict):
            current = {}
        for slug, data in monitors.items():
            if slug not in current or not isinstance(data, dict):
                continue
            live = current[slug]
            if not isinstance(live, dict):
                continue
            for field in ("last_check", "last_hash"):
                if data.get(field):
                    live[field] = data[field]
        memory["monitors"] = current

    if not update_raw(_mut):
        print("[Monitor] ⚠️ Could not save topics — memory file was not readable.")


# ── Public API ─────────────────────────────────────────────────────────────────

def add_monitor(topic: str) -> str:
    topic = topic.strip()
    if not topic:
        return "Please specify a topic to monitor."
    if _is_blocked(topic):
        return "I don't monitor crypto or financial topics."
    from memory.memory_manager import update_raw
    outcome = {"msg": ""}

    def _mut(memory: dict) -> None:
        monitors = memory.get("monitors")
        if not isinstance(monitors, dict):
            monitors = {}
        slug = _slug(topic)
        if slug in monitors:
            outcome["msg"] = f"Already monitoring: {monitors[slug]['topic']}"
            memory["monitors"] = monitors
            return
        monitors[slug] = {
            "topic":      topic,
            "added":      datetime.now().strftime("%Y-%m-%d"),
            "last_check": "",
            "last_hash":  "",
        }
        memory["monitors"] = monitors
        outcome["msg"] = f"Now monitoring: {topic}"

    if not update_raw(_mut):
        return "Could not save that topic — the memory file was not readable."
    if outcome["msg"].startswith("Now monitoring"):
        print(f"[Monitor] ➕ Added: {topic}")
    return outcome["msg"]


def remove_monitor(topic: str) -> str:
    from memory.memory_manager import update_raw
    topic = topic.strip().lower()
    outcome = {"msg": f"Not found in monitored topics: {topic}"}

    def _mut(memory: dict) -> None:
        monitors = memory.get("monitors")
        if not isinstance(monitors, dict):
            monitors = {}
        slug = _slug(topic)
        if slug in monitors:
            label = monitors.pop(slug)["topic"]
            memory["monitors"] = monitors
            outcome["msg"] = f"Stopped monitoring: {label}"
            return
        for key, val in list(monitors.items()):
            if topic in str(val.get("topic", "")).lower():
                label = monitors.pop(key)["topic"]
                memory["monitors"] = monitors
                outcome["msg"] = f"Stopped monitoring: {label}"
                return
        memory["monitors"] = monitors

    if not update_raw(_mut):
        return "Could not update topics — the memory file was not readable."
    return outcome["msg"]


def list_monitors() -> list[str]:
    return [v.get("topic", k) for k, v in _load().items()]


def check_all() -> list[str]:
    """
    Run all pending topic checks (once per day per topic).
    Returns a list of [MONITOR_ALERT] strings — empty if nothing new.
    """
    from actions.web_search import _ddg_news

    monitors = _load()
    if not monitors:
        return []

    today   = datetime.now().strftime("%Y-%m-%d")
    alerts  = []
    changed = False

    for slug, data in monitors.items():
        if data.get("last_check") == today:
            continue                     # already checked today

        topic = data.get("topic", slug)
        try:
            results = _ddg_news(topic, max_results=5)
            if not results:
                monitors[slug]["last_check"] = today
                changed = True
                continue

            top   = results[0]
            title = top.get("title", "").strip()
            if not title:
                continue

            h = _title_hash(title)
            monitors[slug]["last_check"] = today
            changed = True

            if h == data.get("last_hash"):
                continue                 # same headline as last check — no alert

            monitors[slug]["last_hash"] = h

            snippet = top.get("snippet", "")[:150]
            source  = top.get("source", "")
            parts   = [f"[MONITOR_ALERT] {topic}", f"Headline: {title}"]
            if snippet:
                parts.append(snippet)
            if source:
                parts.append(f"Source: {source}")
            alerts.append("\n".join(parts))
            print(f"[Monitor] 🔔 New headline for '{topic}': {title[:60]}")

        except Exception as e:
            print(f"[Monitor] ⚠️ Check failed for '{topic}': {e}")

    if changed:
        _save(monitors)

    return alerts
