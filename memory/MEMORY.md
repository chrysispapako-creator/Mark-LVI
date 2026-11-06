# Shared memory

One local memory for the whole assistant: facts about the person, and a
journal of conversations, tasks, decisions, actions and results. It is not
a trading journal and it is not tied to one session.

Nothing here calls a paid API. Compaction does not call a model at all.
The only dependency added is the SQLite that ships with CPython, including
FTS5. There is no new line in `requirements.txt`.

## Where it lives

| What | File | Written by |
|---|---|---|
| Facts (identity, preferences, projects, relationships, wishes, notes) and the one-line briefing summaries | `memory/long_term.json` | `memory/memory_manager.py` |
| Monitored topics (not facts, not shown as memories) | the `monitors` key in that same file | `actions/background_monitor.py`, through `update_raw` |
| Conversations, tasks, decisions, actions, results, summaries | `memory/journal.sqlite` (plus `-wal` / `-shm` while open) | `memory/store.py` |

Both files stay on this machine. They are gitignored. The updater refuses
to overwrite them.

`memory/config_manager.py` is settings and API keys. It is not memory.

## How a turn becomes a memory

1. At startup the voice session builds one `session_id` and one
   `SharedMemory`. Every tool and plugin receives that object as
   `session_memory` (it used to be `None`).
2. When a user line or an assistant line is logged — microphone, typed
   command, or the phone dashboard — it is appended to the journal as a
   `conversation` row. The same line is still kept in RAM for the live
   session.
3. When a tool returns, one `action` row stores the name, a truncated copy
   of the arguments, and a truncated copy of the result. `recall_memory`,
   `manage_memory` and `system_status` are not stored: the first two would
   copy the journal into itself, and the third is a meter.
4. `save_memory` still writes the fact file, and also a pinned `decision`
   row so the fact can be found when it did not fit in the prompt.
5. A background-monitor alert is stored as a `result`.
6. On disconnect, if the live log has at least three lines, an extractive
   summary is written locally and copied into `long_term.json` for the next
   morning briefing. The briefing still pops that one line so it is not
   repeated. The journal copy stays.

The system prompt does not receive the journal. It receives the existing
fact block (identity, a few recent facts, an index of the other fact keys)
plus a separate slice of at most 700 characters of recent journal lines.
Older detail is loaded only when `recall_memory` asks for it.

## Retrieval

`recall_memory` and `search_memory` run one fact scan and one FTS5 query,
then pack both into 1800 characters. FTS5 is an index: the table is not
read into the prompt. Words of four characters or more match as prefixes
(`flower` finds `flowerpot`). Ranking is SQLite's BM25.

If this Python's SQLite was built without FTS5, recall falls back to a
scan of the newest 500 active rows and says so in the memory panel. A hit
that exists only as an older raw row is missed on that fallback.
Compaction is what keeps an old topic inside reach, by leaving a summary
row, and summaries are indexed.

## Budgets and compaction

| Budget | Value | What it limits |
|---|---|---|
| Fact value | 380 characters | One stored fact |
| Fact prompt | 900 characters, 6 per category, plus a 420-character key index | What rides in the system prompt |
| Journal body | 2000 characters | One stored episode |
| Recent prompt slice | 700 characters, at most 12 rows considered | Extra system-prompt block |
| Recall reply | 1800 characters | One `recall_memory` result |
| Active journal | 400 rows or 350,000 characters | When compaction starts |
| Summary | 900 characters (280 for the briefing line) | One folded stretch |
| Compacted rows kept | 1500 | Then the oldest folded rows are deleted |

Compaction folds the oldest raw conversation, task, action and result rows.
It always keeps the newest 40 active rows verbatim, and it never folds a
pinned row (a correction, or a decision from `save_memory`). The summary
keeps the first thing the user said in that stretch, the last thing, the
tool names, the last reply, and as many middle user lines as the summary
budget has room for.

That is lossy. A sentence in the middle of a long stretch can disappear
from recall. The memory panel's count of folded rows is the visible sign
that it happened. Deleting a summary does not restore the lines that were
folded into it.

## Review, correct, delete

The MEMORY panel lists facts and the newest journal rows. ✎ replaces the
text. ✕ deletes it. FIND uses the journal index, so a match older than the
rows on screen is still found, without loading the table into the window.

By voice, `manage_memory` does the same three things: `list`, `correct`,
`forget`. A fact is addressed by category and key. A journal row is
addressed by the id from `list`.

Deleting a fact does not delete journal lines that mention it, and deleting
a journal line does not change the fact. Correcting a fact does not rewrite
older conversation lines that quoted the old value.

## Concurrency

Fact updates hold one lock for the whole read-modify-write and replace the
file with `os.replace` after an `fsync`. Two tools saving different keys
no longer drop one of them. A fact file that exists but cannot be parsed
is not replaced.

The journal is one SQLite connection per process, WAL mode,
`synchronous=FULL`, `busy_timeout` of five seconds, and a lock around every
operation. Threads in this process share it. A second process writing the
same file is serialised by SQLite; if it cannot get the lock in five
seconds the write fails and is logged, and the file is not half-written.

## Secrets

Before a fact or a journal row is stored, `memory/redact.py` replaces text
that matches a list of shapes: private-key blocks, a few well-known token
prefixes (`sk-`, `AIza`, `AKIA`, `ghp_`, `github_pat_`, Slack `xox`,
`Bearer`, JWTs), assignments such as `password=` / `api_key=` when the
value is at least 8 characters, and `user:password@host` URLs.

The same pass runs on the way out, and once per redaction revision the
journal is rewritten so an older row that now matches is masked at rest.
The first load of the fact file does the same for facts.

This is not a guarantee.

* A secret written as an ordinary sentence, split across two messages, or
  in a shape the list does not name is stored in full.
* The files are not encrypted. Anyone who can read them can read what was
  stored.
* The live model still saw the text in the current session. Redaction does
  not reach into a provider's context, the activity log, or a backup.
* `PRAGMA secure_delete` is on, and a delete or correction vacuums the
  journal so the old bytes are not left in that file's free pages. Copies
  made before that — a backup, a copied folder, the Windows shadow copy —
  are untouched.
* A string that merely looks like a key is removed too.

Do not describe this as "secrets are protected." The accurate statement is
that common key-shaped strings are masked before they are written, and
other secrets are not.

## What was already here

`long_term.json`, `save_memory`, `recall_memory`, the morning-briefing pop,
and the memory panel all stay. The panel can now edit as well as delete,
and recall searches the journal as well as the facts. Session summaries
used to ask Gemini. They are extractive now, so a session with no API key
still leaves a memory, and this path does not spend quota.
