# Fancy Dashboard tool — install on top of the memory update

This adds one Jarvis plugin. It does not change the Fancy Dashboard, and it
does not replace `Launch-FancyDashboard.ps1`. That launcher was installed
on its own and stays where it is:

```
C:\Users\Chris\Desktop\JarvisProjects\Fancy_Dashboard\Launch-FancyDashboard.ps1
```

The tool's allowlist is that file name. The default full path is the one
above. Jarvis will not run a different file name, and the model cannot pass
a path or extra arguments.

What the tool actually does:

| You say | Action | What happens |
|---|---|---|
| is Fancy Dashboard running? | status | `GET http://127.0.0.1:8511/_stcore/health` |
| start Fancy Dashboard | start | that health check; the allowlisted launcher only if it is down |
| open Fancy Dashboard | open | the same, then open `http://127.0.0.1:8511` in the browser |

There is no other dashboard API. The page is loopback only. If health is
already 200 ok, the launcher is not started again.

Spoken end-to-end is not part of the tests. Those need the live assistant
on the laptop. The tests cover the tool's decisions with stand-ins for
health, the process, and the browser.

## Requires the memory update already installed

`memory\store.py` in the install must contain `MEMORY_VERSION = "1"`.
The script stops if it does not. This file list does not copy `main.py`,
the memory modules, API keys, the journal, `.venv`, `voice_bridge.py`, or
`plugins\grok_voice_bridge.py`.

## Do not copy this folder onto the install

Unpack the update somewhere else, for example
`C:\Users\Chris\Downloads\mark-lvi-dashboard-tool\`.
Quit Jarvis first.

Dry run (writes nothing):

```powershell
powershell -ExecutionPolicy Bypass -File C:\Users\Chris\Downloads\mark-lvi-dashboard-tool\scripts\windows_fancy_dashboard_update.ps1 `
    -InstallDir "C:\Users\Chris\Downloads\Mark-LVI-main\Mark-LVI-main" `
    -SourceDir  "C:\Users\Chris\Downloads\mark-lvi-dashboard-tool"
```

Apply (add `-Yes`):

```powershell
powershell -ExecutionPolicy Bypass -File C:\Users\Chris\Downloads\mark-lvi-dashboard-tool\scripts\windows_fancy_dashboard_update.ps1 `
    -InstallDir "C:\Users\Chris\Downloads\Mark-LVI-main\Mark-LVI-main" `
    -SourceDir  "C:\Users\Chris\Downloads\mark-lvi-dashboard-tool" -Yes
```

The same thing without PowerShell, using the install's Python 3.12 venv.
The script that runs is the one in the update, so it understands
`--manifest` even if the copy already on the machine does not:

```bat
C:\Users\Chris\Downloads\Mark-LVI-main\Mark-LVI-main\.venv\Scripts\python.exe ^
    C:\Users\Chris\Downloads\mark-lvi-dashboard-tool\scripts\apply_memory_update.py ^
    --install C:\Users\Chris\Downloads\Mark-LVI-main\Mark-LVI-main ^
    --source  C:\Users\Chris\Downloads\mark-lvi-dashboard-tool ^
    --manifest C:\Users\Chris\Downloads\mark-lvi-dashboard-tool\scripts\fancy_dashboard_update_files.txt ^
    --dry-run
```

Replace `--dry-run` with `--yes` to copy. A backup of any replaced file is
written under `C:\Users\Chris\Downloads\Mark-LVI-main\Mark-LVI-backups\<timestamp>\`.
Keys and memory are snapshotted there and are not written back unless you
later roll back with `--include-data`.

Python must be 3.12.x. 3.12.10 is the expected patch; any other 3.12 warns
and continues.

## After it copies

1. Start Jarvis the way you already do. Plugins load at startup, so a
   restart is what makes `fancy_dashboard` appear.
2. Leave the launcher script where it is. If the dashboard ever moves,
   set the new absolute path in ⚙ → PLUGIN SETTINGS → Fancy Dashboard.
   The file still has to be named `Launch-FancyDashboard.ps1`. Saving that
   setting writes `config\api_keys.json` through the app. The installer
   does not.
3. Optional check, from the install, with its venv:

```bat
.venv\Scripts\python.exe -m unittest plugins._fancy_dashboard_test
```

## Roll back

Use the backup folder the apply step printed, with the same updater:

```bat
.venv\Scripts\python.exe path\to\update\scripts\apply_memory_update.py ^
    --install C:\Users\Chris\Downloads\Mark-LVI-main\Mark-LVI-main ^
    --rollback C:\Users\Chris\Downloads\Mark-LVI-main\Mark-LVI-backups\<timestamp>
```

That removes `plugins\fancy_dashboard.py` if it was new, and restores it
if a previous copy was replaced. It does not put API keys or memory back,
and it does not touch the dashboard folder.
