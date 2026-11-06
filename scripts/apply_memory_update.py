"""Apply or roll back the memory update against an existing install.

Run this with the installation's own interpreter, from a copy of this
update that is NOT the installation folder:

    .venv\\Scripts\\python.exe path\\to\\update\\scripts\\apply_memory_update.py ^
        --install C:\\Users\\Chris\\Downloads\\Mark-LVI-main\\Mark-LVI-main ^
        --source path\\to\\update --dry-run

    .venv\\Scripts\\python.exe path\\to\\update\\scripts\\apply_memory_update.py ^
        --install C:\\Users\\Chris\\Downloads\\Mark-LVI-main\\Mark-LVI-main ^
        --source path\\to\\update --yes

Nothing is written unless --yes is present. See scripts/WINDOWS_UPDATE.md.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from memory.update_apply import main

if __name__ == "__main__":
    raise SystemExit(main())
