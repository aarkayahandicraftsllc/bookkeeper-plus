"""Packages the desktop app into a single Windows executable.

    python build_exe.py

Produces BookkeeperPlus.exe in this folder, BESIDE data/ — copy the two together
anywhere and double-click. Python does not need to be installed on the machine
that runs it.

**Why beside data/ and not in dist/.** The books live in a data folder next to
the executable, so putting the exe here means `python desktop.py` and the
packaged app read the SAME books; built into dist/ they would have had a set
each, and vouchers entered through one would be missing from the other. Point
BOOKS_DIR elsewhere (OneDrive, a shared folder) to override.

**The executable it replaces is kept.** The outgoing exe is moved to
previous/BookkeeperPlus-<when it was built>.exe first, the newest KEEP_PREVIOUS are
retained, and if PyInstaller fails the last good one is put straight back, so
a bad build never leaves you without a runnable app.
"""

import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
PREVIOUS = HERE / "previous"
EXE_NAME = "BookkeeperPlus.exe"
EXE_PATH = HERE / EXE_NAME

# How many superseded builds to keep. Two rather than one: if you rebuild twice
# before noticing a problem, one would already have overwritten your only good
# copy. Each is ~144 MB, so this caps the cost at roughly 290 MB.
KEEP_PREVIOUS = 2


def archive_current() -> Path | None:
    """Move the exe that is about to be replaced into previous/.

    Named for when that build was MADE, not when it was archived, so the
    filenames line up with what you remember building. Returns the archive
    path, to put back if the new build fails.

    Safe to do while the app is open: Windows permits renaming a running
    executable, and the open copy keeps running from its file handle. It
    cannot be DELETED until that process exits, which is why prune_previous()
    tolerates a refusal.
    """
    current = EXE_PATH
    if not current.exists():
        return None

    PREVIOUS.mkdir(exist_ok=True)
    stamp = datetime.fromtimestamp(current.stat().st_mtime).strftime(
        "%Y%m%d-%H%M%S")
    kept = PREVIOUS / f"BookkeeperPlus-{stamp}.exe"
    if kept.exists():          # same build archived twice - leave the first
        current.unlink()
        print(f"Already kept {kept.name}")
        return kept
    shutil.move(str(current), str(kept))
    size = kept.stat().st_size / (1024 * 1024)
    print(f"Kept the previous build: {kept.relative_to(HERE)}  ({size:.0f} MB)")
    return kept


def prune_previous() -> None:
    """Keep the newest KEEP_PREVIOUS archives, drop the rest.

    The timestamp in the name sorts chronologically, so sorting by name is
    enough and does not depend on the files' own mtimes surviving a copy.

    A failure here must never stop the build. An archive the user is still
    RUNNING cannot be deleted on Windows - the app they launched from dist/ is
    this very file, moved - and an unhandled PermissionError here once wiped
    dist/ and left them with no executable at all. Skip it and say so; it will
    be prunable next time.
    """
    if not PREVIOUS.exists():
        return
    archives = sorted(PREVIOUS.glob("BookkeeperPlus-*.exe"))
    for stale in archives[:-KEEP_PREVIOUS] if KEEP_PREVIOUS else archives:
        try:
            stale.unlink()
            print(f"Dropped older build: {stale.name}")
        except OSError as error:
            print(f"Could not drop {stale.name} ({error.strerror or error}) - "
                  f"it is probably still running. Left in place.")


def main() -> int:
    # Keeping the old build is a convenience; failing to keep it must not cost
    # you the new one. Anything unexpected here is reported and stepped over.
    kept = None
    try:
        kept = archive_current()
        prune_previous()
    except OSError as error:
        print(f"Could not archive the previous build ({error}) - "
              f"continuing with the build.")

    # build/ only, never this folder: data/ lives here, and so does the exe,
    # which archive_current() has already moved aside.
    shutil.rmtree(HERE / "build", ignore_errors=True)

    command = [
        sys.executable, "-m", "PyInstaller",
        "--name", "BookkeeperPlus",
        "--onefile",
        "--windowed",                 # no console window
        "--noconfirm",
        "--clean",
        # the data layer and reports are plain modules, pulled in explicitly
        "--hidden-import", "db",
        "--hidden-import", "accounting",
        "--paths", str(HERE),
        # Beside data/, not in dist/, so the packaged app and `python
        # desktop.py` read the same books.
        "--distpath", str(HERE),
        str(HERE / "desktop.py"),
    ]
    print(" ".join(command), "\n")
    result = subprocess.run(command, cwd=HERE)
    if result.returncode:
        # Put the last good exe back, so a failed build does not leave you
        # without a runnable app. It stays in previous/ as well.
        if kept is not None and kept.exists():
            shutil.copy2(str(kept), str(EXE_PATH))
            print(f"\nBuild failed - restored {kept.name} as {EXE_PATH.name}")
        return result.returncode

    built = EXE_PATH
    if built.exists():
        size = built.stat().st_size / (1024 * 1024)
        print(f"\nBuilt {built}  ({size:.0f} MB)")
    archives = sorted(PREVIOUS.glob("BookkeeperPlus-*.exe")) if PREVIOUS.exists() else []
    if archives:
        print(f"Previous build{'s' if len(archives) > 1 else ''} kept in "
              f"previous/: {', '.join(a.name for a in archives)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
