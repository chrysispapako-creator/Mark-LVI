"""Best-effort redaction of secret-shaped text before it is stored.

This is a heuristic filter, not a security boundary.

What it does
    Replaces common key, token, password-assignment and private-key shapes
    with ``[redacted]`` so those strings are less likely to land in
    ``memory/long_term.json`` or ``memory/journal.sqlite``.

What it does not do
    * It does not understand secrets that lack these shapes: a passphrase
      written as an ordinary sentence, a key split across two messages, a
      code the user invented, or a value this pattern list has never seen.
    * It does not encrypt the files. Anyone who can read the memory files
      can read everything that was stored.
    * It does not clear the live model context, the activity log, a backup,
      or the operating system's free space. SQLite ``secure_delete`` covers
      rows this process deletes; it does not cover copies made elsewhere.
    * A false positive is possible. A string that merely looks like a key
      is removed too.

``REDACT_REVISION`` bumps when the patterns change so an existing journal
can be rewritten once on open. Rewriting only catches text the new
patterns recognise.
"""
from __future__ import annotations

import re

REDACT_REVISION = "1"
REDACTION = "[redacted]"

# Private-key blocks first: they span lines, and a later one-line pass
# must not be what decides whether the block survived.
_PEM = re.compile(
    r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?-----END [A-Z0-9 ]*PRIVATE KEY-----",
    re.DOTALL,
)

# Single-token credentials. Ordered from more specific to less, though
# they do not overlap in practice.
_TOKENS = (
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b"),
    re.compile(r"\bsk-(?:proj-|ant-)?[A-Za-z0-9_\-]{20,}\b"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9\-]{10,}\b"),
    re.compile(r"\bBearer\s+[A-Za-z0-9\-._~+/]{20,}={0,2}\b", re.IGNORECASE),
    re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\b"),
)

# "password = something-long", "api_key: something-long". The value has to
# be long enough that ordinary words ("password is pizza") do not match.
# Bare "token" and "key" are deliberately absent: they show up in normal
# speech ("spare key", "token of appreciation").
_ASSIGN = re.compile(
    r"(?i)\b("
    r"api[_\- ]?key|secret|password|passwd|passcode|"
    r"access[_\- ]?key|private[_\- ]?key|auth[_\- ]?token|"
    r"client[_\- ]?secret"
    r")\b(\s*(?:=|:|is)\s*)(['\"]?)([^\s'\"]{8,})"
)

# user:password@host — the password is the part we remove.
_URL_USERINFO = re.compile(
    r"([a-z][a-z0-9+\-.]*://[^/\s:@]+):([^/\s@]{6,})@",
    re.IGNORECASE,
)


def _subn(pattern: re.Pattern, repl, text: str) -> tuple[str, int]:
    text, n = pattern.subn(repl, text)
    return text, n


def redact_text(text: str) -> tuple[str, int]:
    """Return ``(cleaned, number_of_spans_removed)``.

    ``text`` that is not a string is returned unchanged with a count of 0.
    The count is how many spans matched, not how many distinct secrets a
    person would have counted.
    """
    if not isinstance(text, str) or not text:
        return text if isinstance(text, str) else "", 0

    total = 0
    text, n = _subn(_PEM, REDACTION, text)
    total += n
    for pattern in _TOKENS:
        text, n = _subn(pattern, REDACTION, text)
        total += n

    def _assign(match: re.Match) -> str:
        return f"{match.group(1)}{match.group(2)}{match.group(3)}{REDACTION}"

    text, n = _subn(_ASSIGN, _assign, text)
    total += n

    def _url(match: re.Match) -> str:
        return f"{match.group(1)}:{REDACTION}@"

    text, n = _subn(_URL_USERINFO, _url, text)
    total += n
    return text, total
