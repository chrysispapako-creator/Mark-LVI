"""Memory behaviour that has to stay true.

Run from the repository root:

    python -m unittest memory._tests

No network, no Qt, no API key. Each test uses a temporary directory and
points the fact file and the journal at it, so a developer's
``memory/long_term.json`` is never opened.
"""
from __future__ import annotations

import json
import tempfile
import threading
import unittest
from pathlib import Path

from memory import memory_manager, store
from memory.redact import redact_text
from memory.session import SharedMemory
from memory.store import Journal
from memory.update_apply import UpdateError, apply_update, is_protected, rollback


OPENAI = "sk-testsecretkey0123456789abcd"
GOOGLE = "AIza" + "B" * 35
AWS = "AKIAIOSFODNN7EXAMPLE"
GITHUB = "ghp_" + "a" * 36
PASSWORD = "password=SuperSecretValue99"
PEM = (
    "-----BEGIN RSA PRIVATE KEY-----\n"
    "MIIEsecretmaterialhere\n"
    "-----END RSA PRIVATE KEY-----"
)
BEARER = "Bearer abcdefghijklmnopqrstuvwxyz"
JWT = (
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9."
    "eyJzdWIiOiIxMjM0NTY3ODkwIn0."
    "SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"
)
URL_SECRET = "supersecretpassword"
SECRETS = (OPENAI, GOOGLE, AWS, GITHUB, "SuperSecretValue99",
           "MIIEsecretmaterialhere", BEARER.split()[1], JWT, URL_SECRET)


class IsolatedMemory(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.facts = self.root / "long_term.json"
        self.journal_path = self.root / "journal.sqlite"
        self._old_facts = memory_manager.MEMORY_PATH
        memory_manager.MEMORY_PATH = self.facts
        memory_manager._facts_scrubbed = False
        self.journal = Journal(self.journal_path)
        self._old_journal = store._default
        store.set_default_journal(self.journal)

    def tearDown(self):
        store.set_default_journal(self._old_journal)
        self.journal.close()
        memory_manager.MEMORY_PATH = self._old_facts
        memory_manager._facts_scrubbed = False
        self._tmp.cleanup()

    def test_redactor_keeps_ordinary_speech(self):
        text = "the spare key is under the blue flowerpot"
        cleaned, n = redact_text(text)
        self.assertEqual(cleaned, text)
        self.assertEqual(n, 0)

    def test_redactor_masks_common_shapes(self):
        raw = " ".join([OPENAI, GOOGLE, AWS, GITHUB, PASSWORD, PEM, BEARER, JWT,
                        f"https://user:{URL_SECRET}@example.com/x"])
        cleaned, n = redact_text(raw)
        self.assertGreater(n, 0)
        for secret in SECRETS:
            self.assertNotIn(secret, cleaned)

    def test_persistence_across_reopen(self):
        self.journal.record(
            "conversation", "the spare key is under the blue flowerpot",
            role="user", session_id="s1",
        )
        self.journal.close()
        reopened = Journal(self.journal_path)
        try:
            found = reopened.recall("flowerpot")
            self.assertIn("flowerpot", found)
        finally:
            reopened.close()
        # tearDown closes self.journal. It was closed above; close() is safe
        # to call twice. Rebind so a later line in this test is not required.
        self.journal = Journal(self.journal_path)

    def test_facts_persist_across_loads(self):
        memory_manager.remember("favorite_tea", "jasmine", "preferences")
        memory_manager._facts_scrubbed = False
        memory_manager.MEMORY_PATH = self.facts
        again = memory_manager.load_memory()
        self.assertEqual(
            again["preferences"]["favorite_tea"]["value"], "jasmine"
        )

    def test_relevance_does_not_dump_unrelated_history(self):
        filler = "weather report for the harbour " * 40
        for i in range(12):
            self.journal.record(
                "conversation",
                f"{filler} UNIQUE_WEATHER_{i}",
                role="user",
                session_id="s",
            )
        self.journal.record(
            "conversation",
            "the spare key is under the blue flowerpot",
            role="user",
            session_id="s",
        )
        found = self.journal.recall("flowerpot", char_budget=500)
        self.assertIn("flowerpot", found)
        self.assertLessEqual(len(found), 500)
        self.assertNotIn("UNIQUE_WEATHER_0", found)
        # The fact side of the same question stays inside the shared budget.
        memory_manager.remember("desk", "the blue flowerpot", "notes")
        combined = memory_manager.search_memory("flowerpot")
        self.assertIn("flowerpot", combined)
        self.assertLessEqual(len(combined), memory_manager.RECALL_CHAR_BUDGET)
        self.assertNotIn("UNIQUE_WEATHER_3", combined)

    def test_context_budgets(self):
        for i in range(30):
            self.journal.record(
                "conversation",
                f"TOKEN_{i:02d} " + ("padding " * 200),
                role="user",
                session_id="s",
            )
        recent = self.journal.recent_for_prompt(500)
        self.assertLessEqual(len(recent), 500)
        self.assertNotIn("TOKEN_00", recent)
        self.assertIn("TOKEN_29", recent)
        packed = self.journal.recall("", char_budget=400)
        self.assertLessEqual(len(packed), 400)
        # An empty journal adds nothing, so the prompt budget is the caller's.
        empty = Journal(self.root / "empty.sqlite")
        try:
            self.assertEqual(empty.recent_for_prompt(500), "")
        finally:
            empty.close()

    def test_concurrency(self):
        errors = []
        barrier = threading.Barrier(8)

        def worker(index: int):
            try:
                barrier.wait(timeout=5)
                for n in range(25):
                    token = f"thread-{index}-row-{n}-marker"
                    got = self.journal.record(
                        "note", token, role="tool", session_id=f"t{index}",
                        title=token,
                    )
                    if not got:
                        errors.append(f"missing id {token}")
            except Exception as exc:  # noqa: BLE001 - the assertion reports it
                errors.append(repr(exc))

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
        self.assertEqual(errors, [])
        self.assertEqual(self.journal.stats()["rows"], 200)
        self.assertIn("thread-3-row-7-marker", self.journal.recall("thread-3-row-7-marker"))

    def test_concurrent_fact_updates_do_not_drop_keys(self):
        errors = []

        def worker(index: int):
            try:
                for n in range(10):
                    memory_manager.update_memory(
                        {"notes": {f"k{index}_{n}": {"value": f"v{index}_{n}"}}}
                    )
            except Exception as exc:  # noqa: BLE001
                errors.append(repr(exc))

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
        self.assertEqual(errors, [])
        data = json.loads(self.facts.read_text(encoding="utf-8"))
        for index in range(8):
            for n in range(10):
                self.assertEqual(data["notes"][f"k{index}_{n}"]["value"], f"v{index}_{n}")

    def test_deletion_removes_the_row_and_the_bytes(self):
        marker = "flowerpot-marker-9f3a-unique"
        episode = self.journal.record("note", marker, session_id="s")
        self.assertIn(marker, self.journal.recall(marker))
        message = self.journal.forget(episode)
        self.assertIn("Deleted", message)
        # The empty reply quotes the query, so "not found" is a prefix check.
        self.assertTrue(self.journal.recall(marker).startswith("Nothing in the journal"))
        self.assertFalse(self.journal.contains_bytes(marker))
        self.assertFalse(any(row["id"] == episode for row in self.journal.list_entries()))

    def test_correction_replaces_visible_text(self):
        episode = self.journal.record(
            "note", "favorite city is Paris", session_id="s",
        )
        message = self.journal.correct(episode, "favorite city is Lyon")
        self.assertIn("Updated", message)
        found = self.journal.recall("city")
        self.assertIn("Lyon", found)
        self.assertNotIn("Paris", found)
        # A correction is pinned: compaction must not fold it away.
        for i in range(6):
            self.journal.record("conversation", f"chatter {i} " * 30, role="user", session_id="c")
        self.journal.compact_if_needed(active_cap=2, keep_recent=1, batch=20, summary_chars=200)
        self.assertIn("Lyon", self.journal.recall("Lyon"))

    def test_secrets_are_not_written(self):
        body = (
            f"note {OPENAI} {GOOGLE} {AWS} {GITHUB} {PASSWORD} {PEM} "
            f"{BEARER} {JWT} https://user:{URL_SECRET}@example.com/x"
        )
        self.journal.record("note", body, session_id="s", title=OPENAI)
        self.journal.close()
        raw = self.journal_path.read_bytes()
        wal = Path(str(self.journal_path) + "-wal")
        if wal.exists():
            raw += wal.read_bytes()
        for secret in SECRETS:
            self.assertNotIn(secret.encode("utf-8"), raw, secret)
        self.journal = Journal(self.journal_path)
        store.set_default_journal(self.journal)
        visible = self.journal.recall("note")
        for secret in SECRETS:
            self.assertNotIn(secret, visible)
        self.assertIn("[redacted]", visible)

    def test_secret_injected_before_the_filter_is_scrubbed(self):
        self.journal.close()
        raw_db = Journal(self.journal_path)
        raw_db._conn.execute(
            "INSERT INTO episodes (created, kind, role, source, session_id, title, body, "
            "tags, importance, compacted, deleted, summarized, redactions, char_len) "
            "VALUES ('2020-01-01 00:00:00', 'note', '', '', 's', '', ?, '', 0, 0, 0, 0, 0, ?)",
            (OPENAI, len(OPENAI)),
        )
        raw_db._conn.execute("DELETE FROM meta WHERE key='redact_revision'")
        raw_db.close()
        scrubbed = Journal(self.journal_path)
        try:
            self.assertFalse(scrubbed.contains_bytes(OPENAI))
            self.assertIn("[redacted]", scrubbed.recall("redacted") + scrubbed.list_entries()[0]["body"])
        finally:
            scrubbed.close()
        self.journal = Journal(self.journal_path)

    def test_fact_secrets_are_not_written(self):
        memory_manager.update_memory(
            {"notes": {"login": {"value": f"the login is {PASSWORD}"}}}
        )
        raw = self.facts.read_text(encoding="utf-8")
        self.assertNotIn("SuperSecretValue99", raw)
        self.assertIn("[redacted]", raw)
        # A fact written by an older version is scrubbed on load, not left for the prompt.
        self.facts.write_text(json.dumps({
            "identity": {},
            "preferences": {},
            "projects": {},
            "relationships": {},
            "wishes": {},
            "notes": {"old": {"value": PASSWORD, "updated": "2020-01-01"}},
        }), encoding="utf-8")
        memory_manager._facts_scrubbed = False
        loaded = memory_manager.load_memory()
        self.assertNotIn("SuperSecretValue99", loaded["notes"]["old"]["value"])
        self.assertNotIn("SuperSecretValue99", self.facts.read_text(encoding="utf-8"))

    def test_corrupt_fact_file_is_not_overwritten(self):
        self.facts.write_text("{", encoding="utf-8")
        memory_manager._facts_scrubbed = False
        self.assertFalse(memory_manager.update_raw(lambda memory: memory.update({"notes": {}})))
        self.assertEqual(self.facts.read_text(encoding="utf-8"), "{")

    def test_compaction_keeps_the_ends_and_drops_the_middle(self):
        self.journal.record(
            "conversation", "ALPHA_TOKEN_START " + ("alpha " * 40),
            role="user", session_id="old",
        )
        for i in range(4):
            self.journal.record(
                "conversation",
                f"MIDDLE_TOKEN_{i} " + ("middle " * 80),
                role="user", session_id="old",
            )
        self.journal.record(
            "conversation", "OMEGA_TOKEN_END " + ("omega " * 40),
            role="user", session_id="old",
        )
        self.journal.record("conversation", "RECENT_KEEP_ONE", role="user", session_id="new")
        self.journal.record("conversation", "RECENT_KEEP_TWO", role="user", session_id="new")
        report = self.journal.compact_if_needed(
            active_cap=2, keep_recent=2, batch=20, summary_chars=220,
        )
        self.assertGreaterEqual(report["folded"], 6)
        self.assertIn("ALPHA_TOKEN_START", self.journal.recall("ALPHA_TOKEN_START"))
        self.assertIn("OMEGA_TOKEN_END", self.journal.recall("OMEGA_TOKEN_END"))
        self.assertIn("RECENT_KEEP_ONE", self.journal.recall("RECENT_KEEP_ONE"))
        # The middle of a folded stretch is not guaranteed. This asserts the
        # loss instead of hiding it. The "nothing stored" reply quotes the
        # query, so the check is the stored summary and the active rows.
        summaries = [
            row for row in self.journal.list_entries(limit=20) if row["kind"] == "summary"
        ]
        self.assertTrue(summaries)
        self.assertLessEqual(len(summaries[0]["body"]), 220)
        self.assertIn("ALPHA_TOKEN_START", summaries[0]["body"])
        self.assertIn("OMEGA_TOKEN_END", summaries[0]["body"])
        self.assertNotIn("MIDDLE_TOKEN_2", summaries[0]["body"])
        active_bodies = " ".join(row["body"] for row in self.journal.list_entries(limit=20))
        self.assertNotIn("MIDDLE_TOKEN_2", active_bodies)

    def test_session_summary_is_local_and_not_repeated(self):
        self.journal.record("conversation", "let us plan the greenhouse", role="user", session_id="day")
        self.journal.record("action", "result: opened the notes", role="tool", title="file_controller", session_id="day")
        self.journal.record("conversation", "the greenhouse needs glass", role="assistant", session_id="day")
        text = self.journal.session_summary("day")
        self.assertIn("greenhouse", text.lower())
        self.assertEqual(self.journal.session_summary("day"), "")

    def test_shared_memory_object_writes_the_journal(self):
        shared = SharedMemory("voice-session")
        shared.set_last_search(query="weather in Split", response="Showing the weather for Split")
        found = memory_manager.search_memory("Split")
        self.assertIn("Split", found)

    def test_manage_correct_and_forget(self):
        memory_manager.remember("favorite_food", "pizza", "preferences")
        corrected = memory_manager.manage_memory(
            "correct", category="preferences", key="favorite_food", value="soup",
        )
        self.assertIn("Updated", corrected)
        self.assertIn("soup", memory_manager.search_memory("food"))
        self.assertNotIn("pizza", memory_manager.search_memory("favorite_food"))
        forgotten = memory_manager.manage_memory(
            "forget", category="preferences", key="favorite_food",
        )
        self.assertIn("Forgotten", forgotten)
        self.assertNotIn("soup", memory_manager.search_memory("favorite_food"))

        episode = self.journal.record("task", "buy glass for the greenhouse", session_id="s")
        memory_manager.manage_memory("correct", episode_id=episode, value="buy timber for the greenhouse")
        self.assertIn("timber", self.journal.recall("greenhouse"))
        memory_manager.manage_memory("forget", episode_id=episode)
        self.assertNotIn("timber", self.journal.recall("greenhouse"))

    def test_duplicate_line_is_not_stored_twice(self):
        first = self.journal.record("conversation", "hello there", role="user", session_id="s")
        second = self.journal.record("conversation", "hello there", role="user", session_id="s")
        self.assertIsNotNone(first)
        self.assertIsNone(second)
        self.assertEqual(self.journal.stats()["rows"], 1)

    def test_monitors_added_together_all_survive(self):
        from actions.background_monitor import add_monitor, list_monitors
        errors = []

        def worker(index: int):
            try:
                add_monitor(f"space topic {index}")
            except Exception as exc:  # noqa: BLE001
                errors.append(repr(exc))

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
        self.assertEqual(errors, [])
        topics = list_monitors()
        for index in range(8):
            self.assertIn(f"space topic {index}", topics)
        # The fact written beside the monitors is still there.
        memory_manager.remember("name", "Ada", "identity")
        add_monitor("gardening")
        data = json.loads(self.facts.read_text(encoding="utf-8"))
        self.assertEqual(data["identity"]["name"]["value"], "Ada")
        self.assertIn("gardening", data["monitors"]["gardening"]["topic"])


class UpdateApplyTests(unittest.TestCase):
    def _install(self, root: Path) -> Path:
        install = root / "install"
        (install / "memory").mkdir(parents=True)
        (install / "core").mkdir()
        (install / "plugins").mkdir()
        (install / "config").mkdir()
        (install / "main.py").write_text("OLD MAIN\n", encoding="utf-8")
        (install / "ui.py").write_text("ui\n", encoding="utf-8")
        (install / "memory" / "memory_manager.py").write_text("old memory\n", encoding="utf-8")
        (install / "core" / "prompt.txt").write_text("prompt\n", encoding="utf-8")
        (install / "config" / "api_keys.json").write_text('{"gemini_api_key": "SECRET-KEY"}\n', encoding="utf-8")
        (install / "memory" / "long_term.json").write_text('{"notes": {"keep": "yes"}}\n', encoding="utf-8")
        (install / "voice_bridge.py").write_text("CUSTOM VOICE BRIDGE\n", encoding="utf-8")
        (install / "plugins" / "grok_voice_bridge.py").write_text("CUSTOM GROK BRIDGE\n", encoding="utf-8")
        return install

    def _source(self, root: Path) -> Path:
        source = root / "source"
        (source / "memory").mkdir(parents=True)
        (source / "main.py").write_text("NEW MAIN\n", encoding="utf-8")
        (source / "memory" / "store.py").write_text("NEW STORE\n", encoding="utf-8")
        # A decoy that must never be copied, even though it sits in the source.
        (source / "voice_bridge.py").write_text("SHOULD NOT COPY\n", encoding="utf-8")
        (source / "config").mkdir()
        (source / "config" / "api_keys.json").write_text('{"gemini_api_key": "OTHER"}\n', encoding="utf-8")
        return source

    def test_protected_paths_are_recognised(self):
        self.assertTrue(is_protected("config/api_keys.json"))
        self.assertTrue(is_protected("voice_bridge.py"))
        self.assertTrue(is_protected("plugins/grok_voice_bridge.py"))
        self.assertTrue(is_protected("memory/long_term.json"))
        self.assertTrue(is_protected("memory/journal.sqlite"))
        self.assertTrue(is_protected("../outside.py"))
        self.assertFalse(is_protected("memory/store.py"))
        self.assertFalse(is_protected("main.py"))

    def test_apply_preserves_custom_files_and_rollback_restores_code(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            install = self._install(root)
            source = self._source(root)
            manifest = ["main.py", "memory/store.py"]
            backups = root / "backups"

            refused = apply_update(
                source, install, yes=False, manifest=manifest,
                backup_root=backups, python_version="3.12.10",
            )
            self.assertFalse(refused["applied"])
            self.assertEqual((install / "main.py").read_text(encoding="utf-8"), "OLD MAIN\n")

            report = apply_update(
                source, install, yes=True, manifest=manifest,
                backup_root=backups, python_version="3.12.3", stamp="t1",
            )
            self.assertTrue(report["applied"])
            self.assertEqual(report["python_status"], "warn")
            self.assertEqual((install / "main.py").read_text(encoding="utf-8"), "NEW MAIN\n")
            self.assertEqual((install / "memory" / "store.py").read_text(encoding="utf-8"), "NEW STORE\n")
            self.assertEqual((install / "voice_bridge.py").read_text(encoding="utf-8"), "CUSTOM VOICE BRIDGE\n")
            self.assertEqual(
                (install / "plugins" / "grok_voice_bridge.py").read_text(encoding="utf-8"),
                "CUSTOM GROK BRIDGE\n",
            )
            self.assertIn("SECRET-KEY", (install / "config" / "api_keys.json").read_text(encoding="utf-8"))
            self.assertIn("keep", (install / "memory" / "long_term.json").read_text(encoding="utf-8"))

            # Memories and keys written after the update must survive rollback.
            (install / "config" / "api_keys.json").write_text('{"gemini_api_key": "NEWER"}\n', encoding="utf-8")
            (install / "memory" / "long_term.json").write_text('{"notes": {"after": "update"}}\n', encoding="utf-8")
            rolled = rollback(Path(report["backup"]), install)
            self.assertEqual((install / "main.py").read_text(encoding="utf-8"), "OLD MAIN\n")
            self.assertFalse((install / "memory" / "store.py").exists())
            self.assertEqual((install / "voice_bridge.py").read_text(encoding="utf-8"), "CUSTOM VOICE BRIDGE\n")
            self.assertIn("NEWER", (install / "config" / "api_keys.json").read_text(encoding="utf-8"))
            self.assertIn("after", (install / "memory" / "long_term.json").read_text(encoding="utf-8"))
            self.assertEqual(rolled["data_restored"], [])

            # The snapshot can be put back only when asked.
            data = rollback(Path(report["backup"]), install, include_data=True)
            self.assertIn("config/api_keys.json", data["data_restored"])
            self.assertIn("SECRET-KEY", (install / "config" / "api_keys.json").read_text(encoding="utf-8"))

    def test_wrong_python_and_protected_manifest_write_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            install = self._install(root)
            source = self._source(root)
            before = (install / "main.py").read_text(encoding="utf-8")
            with self.assertRaises(UpdateError):
                apply_update(
                    source, install, yes=True, manifest=["main.py"],
                    python_version="3.11.9", backup_root=root / "backups",
                )
            self.assertEqual((install / "main.py").read_text(encoding="utf-8"), before)
            self.assertFalse((root / "backups").exists())
            with self.assertRaises(UpdateError):
                apply_update(
                    source, install, yes=True,
                    manifest=["voice_bridge.py"],
                    python_version="3.12.10", backup_root=root / "backups",
                )
            self.assertEqual((install / "voice_bridge.py").read_text(encoding="utf-8"), "CUSTOM VOICE BRIDGE\n")

    def test_repo_manifest_matches_this_tree_and_skips_protected_files(self):
        from memory.update_apply import default_manifest_path, load_manifest, validate_manifest
        files = load_manifest(default_manifest_path())
        validate_manifest(files)
        root = Path(__file__).resolve().parent.parent
        missing = [rel for rel in files if not (root / rel).is_file()]
        self.assertEqual(missing, [])
        for rel in files:
            self.assertFalse(is_protected(rel), rel)


if __name__ == "__main__":
    unittest.main()
