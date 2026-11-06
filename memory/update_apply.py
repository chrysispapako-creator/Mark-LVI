"""Safe file update for an existing Mark LVI install.

The Windows machine this was written for is not contacted by this module.
A person runs it there, with that machine's virtualenv, pointing ``--source``
at a copy of this tree and ``--install`` at the existing installation.

What it will not do
    * It will not merge, start the app, or call any network API.
    * It will not overwrite API keys, ``memory/long_term.json``, the journal,
      ``.venv``, ``voice_bridge.py`` or ``plugins/grok_voice_bridge.py``.
    * It will not copy a file that is not listed in the manifest.
    * It will not copy anything until ``--yes`` is passed. Without that it
      prints the plan and stops.

Rollback restores the code files from the backup. It leaves keys, memory and
local customisations where they are, including memories created after the
update. ``--include-data`` is a separate, explicit choice that puts the
pre-update snapshot of those files back.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import platform
import shutil
import sys
from datetime import datetime
from pathlib import Path

EXPECTED_PYTHON = "3.12.10"
PYTHON_PREFIX = "3.12."

PROTECTED_EXACT = {
    "config/api_keys.json",
    "memory/long_term.json",
    "voice_bridge.py",
    "plugins/grok_voice_bridge.py",
}

# Prefixes are matched against the slash-separated relative path.
PROTECTED_PREFIXES = (
    ".venv/",
    "venv/",
    "config/certs/",
    "config/whatsapp_web/",
    "memory/journal.sqlite",
)

INSTALL_MARKERS = (
    "main.py",
    "ui.py",
    "memory/memory_manager.py",
    "core/prompt.txt",
)

SNAPSHOT_NAMES = (
    "config/api_keys.json",
    "memory/long_term.json",
    "memory/journal.sqlite",
    "memory/journal.sqlite-wal",
    "memory/journal.sqlite-shm",
    "voice_bridge.py",
    "plugins/grok_voice_bridge.py",
)


class UpdateError(Exception):
    """The update did not change the install. The message says why."""


def repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def default_manifest_path() -> Path:
    return repo_root() / "scripts" / "memory_update_files.txt"


def _rel(path: str) -> str:
    return path.replace("\\", "/").lstrip("/")


def is_protected(rel: str) -> bool:
    rel = _rel(rel)
    if not rel or rel.startswith("/") or Path(rel).is_absolute():
        return True
    parts = Path(rel).parts
    if ".." in parts:
        return True
    if rel in PROTECTED_EXACT:
        return True
    for prefix in PROTECTED_PREFIXES:
        if rel.startswith(prefix):
            return True
    name = parts[-1] if parts else ""
    if name.startswith("long_term") or name.endswith(".sqlite") or name.endswith(".sqlite-wal"):
        return True
    if "credential" in name.lower() or name.startswith("token"):
        return True
    return False


def check_python(version: str | None = None) -> tuple[str, str]:
    """``ok``, ``warn`` or ``abort``, plus a sentence a person can read.

    3.12.10 is the interpreter in the installation this update was written
    for. Any other 3.12 is close enough that the standard-library sqlite
    journal still works, and the message says so. Anything else stops.
    """
    found = version or platform.python_version()
    if not found.startswith(PYTHON_PREFIX):
        return (
            "abort",
            f"Python {PYTHON_PREFIX}x is required (expected {EXPECTED_PYTHON}). Found {found}.",
        )
    if found != EXPECTED_PYTHON:
        return (
            "warn",
            f"Expected Python {EXPECTED_PYTHON}. Found {found}, which is still "
            f"{PYTHON_PREFIX}x, so the update can continue.",
        )
    return "ok", f"Python {found}"


def load_manifest(path: Path) -> list[str]:
    files = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if line:
            files.append(_rel(line))
    if not files:
        raise UpdateError(f"Manifest is empty: {path}")
    return files


def validate_manifest(files: list[str]) -> None:
    bad = [rel for rel in files if is_protected(rel)]
    if bad:
        raise UpdateError(
            "Manifest lists protected paths and will not be applied: "
            + ", ".join(bad)
        )


def looks_like_install(install: Path) -> None:
    missing = [rel for rel in INSTALL_MARKERS if not (install / rel).is_file()]
    if missing:
        raise UpdateError(
            f"{install} does not look like a Mark LVI install. Missing: "
            + ", ".join(missing)
        )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _same_path(a: Path, b: Path) -> bool:
    try:
        return a.resolve() == b.resolve()
    except OSError:
        return False


def apply_update(
    source: Path,
    install: Path,
    *,
    yes: bool = False,
    dry_run: bool = False,
    manifest: list[str] | None = None,
    manifest_path: Path | None = None,
    backup_root: Path | None = None,
    python_version: str | None = None,
    stamp: str | None = None,
) -> dict:
    """Copy manifest files from ``source`` onto ``install``.

    Returns a report. Raises ``UpdateError`` before any install file is
    changed when the request is refused. A failure during the copy restores
    the files already replaced and then raises.
    """
    source = Path(source)
    install = Path(install)
    status, python_message = check_python(python_version)
    if status == "abort":
        raise UpdateError(python_message)
    if _same_path(source, install):
        raise UpdateError(
            "Source and install are the same folder. Unpack the update "
            "somewhere else and pass that path as --source. Do not copy "
            "the update on top of the installation yourself."
        )
    looks_like_install(install)
    if manifest is None:
        manifest = load_manifest(manifest_path or default_manifest_path())
    validate_manifest(manifest)

    missing = [rel for rel in manifest if not (source / rel).is_file()]
    if missing:
        raise UpdateError(
            "Source is missing files the manifest promises: " + ", ".join(missing)
        )

    protected_present = [rel for rel in SNAPSHOT_NAMES if (install / rel).is_file()]
    plan = {
        "python": python_message,
        "python_status": status,
        "source": str(source),
        "install": str(install),
        "files": list(manifest),
        "left_untouched": protected_present,
        "dry_run": dry_run or not yes,
    }
    if dry_run or not yes:
        plan["applied"] = False
        plan["message"] = "Dry run. Nothing was written. Re-run with --yes to apply."
        return plan

    stamp = stamp or datetime.now().strftime("%Y%m%d-%H%M%S")
    root = Path(backup_root) if backup_root else (install.parent / "Mark-LVI-backups")
    backup = root / stamp
    code_dir = backup / "code"
    snap_dir = backup / "snapshot"
    code_dir.mkdir(parents=True, exist_ok=False)
    snap_dir.mkdir(parents=True, exist_ok=True)

    entries = []
    for rel in manifest:
        dest = install / rel
        entry = {"rel": rel, "existed": dest.is_file()}
        if dest.is_file():
            target = code_dir / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(dest, target)
            entry["sha256_before"] = _sha256(dest)
        entries.append(entry)

    for rel in protected_present:
        target = snap_dir / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(install / rel, target)

    hashes_before = {rel: _sha256(install / rel) for rel in protected_present}
    written: list[str] = []
    try:
        for rel in manifest:
            src = source / rel
            dest = install / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dest)
            if _sha256(dest) != _sha256(src):
                raise UpdateError(f"Copy check failed for {rel}")
            written.append(rel)
        for rel, digest in hashes_before.items():
            if _sha256(install / rel) != digest:
                raise UpdateError(
                    f"A protected file changed during the update: {rel}. "
                    "Restoring code files."
                )
    except Exception:
        _restore_entries(install, code_dir, entries, only=written)
        raise

    manifest_body = {
        "created": stamp,
        "source": str(source),
        "install": str(install),
        "python": python_message,
        "files": entries,
        "snapshot": protected_present,
        "memory_version_file": "memory/store.py",
    }
    (backup / "manifest.json").write_text(
        json.dumps(manifest_body, indent=2), encoding="utf-8"
    )
    (backup / "README.txt").write_text(
        "Code backup for the memory update.\n"
        "Default rollback restores only the files listed in manifest.json "
        "under 'files'. It does not put API keys or memory back.\n"
        "The snapshot/ folder is the pre-update copy of keys, memory and "
        "local custom files, for a manual restore or --include-data.\n",
        encoding="utf-8",
    )
    plan.update({
        "applied": True,
        "backup": str(backup),
        "message": f"Updated {len(manifest)} files. Backup: {backup}",
    })
    return plan


def _restore_entries(install: Path, code_dir: Path, entries: list[dict],
                     only: list[str] | None = None) -> list[str]:
    """Put manifest files back. ``only`` limits the set (used when a copy fails halfway).

    Protected paths are skipped even if a bad manifest named them.
    """
    restored = []
    chosen = set(only) if only is not None else None
    for entry in entries:
        rel = entry["rel"]
        if is_protected(rel):
            continue
        if chosen is not None and rel not in chosen:
            continue
        dest = install / rel
        if entry.get("existed"):
            src = code_dir / rel
            if not src.is_file():
                raise UpdateError(f"Backup is missing {rel}")
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dest)
            restored.append(rel)
        elif dest.is_file():
            dest.unlink()
            restored.append(rel)
    return restored


def rollback(backup: Path, install: Path, *, include_data: bool = False) -> dict:
    """Put code files back. Keys and memory stay unless ``include_data``."""
    backup = Path(backup)
    install = Path(install)
    manifest_path = backup / "manifest.json"
    if not manifest_path.is_file():
        raise UpdateError(f"No manifest.json in {backup}")
    body = json.loads(manifest_path.read_text(encoding="utf-8"))
    entries = body.get("files") or []
    code_dir = backup / "code"
    restored = _restore_entries(install, code_dir, entries)
    data_restored: list[str] = []
    if include_data:
        snap = backup / "snapshot"
        for rel in body.get("snapshot") or []:
            if not is_protected(rel) and rel not in PROTECTED_EXACT:
                # Snapshot is allowed to hold protected data. Anything else
                # in that list would be a surprise; skip it.
                if not rel.startswith("memory/journal.sqlite"):
                    continue
            src = snap / rel
            if not src.is_file():
                continue
            dest = install / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dest)
            data_restored.append(rel)
    return {
        "restored": restored,
        "data_restored": data_restored,
        "include_data": include_data,
        "install": str(install),
        "backup": str(backup),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Update a Mark LVI install in place, leaving keys and memory alone.")
    parser.add_argument("--install", required=True, help="Existing installation folder (the one that contains main.py).")
    parser.add_argument("--source", default=str(repo_root()), help="Unpacked update. Defaults to this repository.")
    parser.add_argument("--yes", action="store_true", help="Apply the copy. Without this flag nothing is written.")
    parser.add_argument("--dry-run", action="store_true", help="Print the plan and stop.")
    parser.add_argument("--rollback", default="", help="Backup folder to restore code from.")
    parser.add_argument("--include-data", action="store_true",
                        help="With --rollback, also restore the pre-update snapshot of keys and memory.")
    parser.add_argument("--backup-root", default="", help="Where to create the backup. Defaults to a sibling Mark-LVI-backups folder.")
    parser.add_argument(
        "--manifest", default="",
        help="File list to copy. Defaults to scripts/memory_update_files.txt. "
             "A smaller list (scripts/fancy_dashboard_update_files.txt) adds "
             "only the dashboard tool on a machine that already has the memory update.",
    )
    args = parser.parse_args(argv)

    try:
        if args.rollback:
            report = rollback(
                Path(args.rollback), Path(args.install), include_data=args.include_data,
            )
        else:
            report = apply_update(
                Path(args.source),
                Path(args.install),
                yes=args.yes,
                dry_run=args.dry_run,
                manifest_path=Path(args.manifest) if args.manifest else None,
                backup_root=Path(args.backup_root) if args.backup_root else None,
            )
    except UpdateError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
