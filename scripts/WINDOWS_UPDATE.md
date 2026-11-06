# Updating an existing Windows install

This update was prepared in a git branch. It has not been applied to
`C:\Users\Chris\Downloads\Mark-LVI-main\Mark-LVI-main`, and it cannot be:
that disk is not reachable from the environment that built the branch.
What follows is the copy you run yourself. It does not merge, start the
assistant, or call an API.

The install this was written against uses Python **3.12.10** from its own
`.venv`, started with `main.py`. The script refuses any interpreter that
is not 3.12.x, warns if it is not exactly 3.12.10, and refuses to run
unless it is that `.venv`.

## What is left alone

The updater copies only the files listed in `scripts/memory_update_files.txt`.
It will not overwrite, even if a copy of them is sitting in the update:

* `config\api_keys.json` and anything under `config\certs\` or `config\whatsapp_web\`
* `memory\long_term.json`
* `memory\journal.sqlite` and its `-wal` / `-shm` files
* `.venv\`
* `voice_bridge.py`
* `plugins\grok_voice_bridge.py`

Do not unzip or copy this update on top of the installation. That is the
mistake the script exists to avoid.

## Before you start

1. Quit the assistant. A journal left open by a running process is not
   corrupted by this copy — the copy does not touch it — but you should
   not be talking to it while `main.py` is being replaced.
2. Unpack the update somewhere else, for example
   `C:\Users\Chris\Downloads\mark-lvi-memory-update\`.
   That folder is `--source`. The installation folder is `--install`.

## Dry run

In PowerShell, from anywhere:

```powershell
powershell -ExecutionPolicy Bypass -File C:\Users\Chris\Downloads\mark-lvi-memory-update\scripts\windows_memory_update.ps1 `
    -InstallDir "C:\Users\Chris\Downloads\Mark-LVI-main\Mark-LVI-main" `
    -SourceDir  "C:\Users\Chris\Downloads\mark-lvi-memory-update"
```

This prints the Python version and the plan. It writes nothing. Read the
`left_untouched` list. It should name your key file, your fact file, and
`voice_bridge.py` / `plugins\grok_voice_bridge.py` if those files exist.

The same plan, without PowerShell:

```bat
C:\Users\Chris\Downloads\Mark-LVI-main\Mark-LVI-main\.venv\Scripts\python.exe ^
    C:\Users\Chris\Downloads\mark-lvi-memory-update\scripts\apply_memory_update.py ^
    --install C:\Users\Chris\Downloads\Mark-LVI-main\Mark-LVI-main ^
    --source  C:\Users\Chris\Downloads\mark-lvi-memory-update ^
    --dry-run
```

## Apply

Add `-Yes` to the PowerShell command, or replace `--dry-run` with `--yes`.

The script:

1. Checks Python is 3.12.x (warns if it is not 3.12.10).
2. Checks the install contains `main.py`, `ui.py`, `memory\memory_manager.py`
   and `core\prompt.txt`.
3. Refuses if the manifest names a protected path, or if source and install
   are the same folder.
4. Copies the files it is about to replace, plus a snapshot of keys, memory
   and the two custom bridge files, to:

       C:\Users\Chris\Downloads\Mark-LVI-main\Mark-LVI-backups\<timestamp>\

   The snapshot is not used by a normal rollback. It is there if you want
   the old keys back yourself.
5. Copies the manifest files and checks each hash.
6. Checks the protected files still hash the same. If a copy fails halfway,
   the files already replaced are put back.

No pip install is required. SQLite and FTS5 come with Python 3.12.

After it finishes, start the assistant the way you already do
(`main.py` with that `.venv`). The journal file is created on first use,
next to `memory\long_term.json`. Existing facts are read as they are.

Optional check, from the install folder, with the same interpreter:

```bat
.venv\Scripts\python.exe -m unittest memory._tests
```

That test uses temporary files. It does not read your `long_term.json`
or your API keys.

## Roll back

Code only — keys, memory and the two custom bridge files stay as they are
now, including anything the assistant stored after the update:

```powershell
powershell -ExecutionPolicy Bypass -File C:\Users\Chris\Downloads\mark-lvi-memory-update\scripts\windows_memory_rollback.ps1 `
    -InstallDir "C:\Users\Chris\Downloads\Mark-LVI-main\Mark-LVI-main" `
    -BackupDir  "C:\Users\Chris\Downloads\Mark-LVI-main\Mark-LVI-backups\<timestamp>"
```

`-IncludeData` also copies the pre-update snapshot of keys and memory back
over the current ones. That throws away keys and memories written after
the update. Leave it off unless that is what you want.

A journal created by the new code is not deleted by the default rollback.
The old code ignores `memory\journal.sqlite`. You can delete that file
yourself if you want it gone; do that only after quitting the assistant.

## If the script stops

* "does not look like a Mark LVI install" — `--install` is not the folder
  that contains `main.py`.
* "Source and install are the same folder" — unpack the update elsewhere.
* "Python 3.12.x is required" — the `.venv` is not 3.12. Do not point the
  script at a different Python to get past that.
* "Manifest lists protected paths" — do not edit the manifest to add
  `api_keys.json`, `voice_bridge.py`, or the journal. The refusal is the
  point.
