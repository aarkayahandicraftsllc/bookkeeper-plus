"""Grab PNGs of the desktop screens, for judging how the app LOOKS.

A look is not something you can assert in a test - ``test_desktop.py`` can
prove a scheme is readable (it measures contrast) but not that it is handsome.
This renders the real widgets and writes them to ``shots/`` so a change to
``theme.py`` can be compared against the build before it.

    python screenshot.py                     # every scheme, three screens
    python screenshot.py --scheme dark       # just one
    python screenshot.py --screens 0 3       # Voucher Entry and Trial Balance

It copies the real books into a scratch folder and pins ``BOOKS_DB`` to the
copy, so taking a screenshot can never write to ``data/``. Widgets are grabbed
with ``QWidget.grab()`` and the window is never shown, so nothing flashes up on
screen - and it runs on the NATIVE platform rather than offscreen on purpose,
because the Light scheme keeps the native widget style and offscreen would
render it as Fusion, i.e. not the thing being judged.
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent

# Default set: an input screen, a tabular report, a two-column statement.
DEFAULT_SCREENS = (0, 3, 5)


def _books() -> Path:
    """A throwaway copy of the real books, so the shots have real content."""
    scratch = Path(tempfile.mkdtemp(prefix="bookkeeper-shots-"))
    source = HERE / "data" / "books.db"
    target = scratch / "books.db"
    if source.exists():
        shutil.copy2(source, target)
    return target


def settle(app) -> None:
    """Let Qt catch up, INCLUDING the deletions.

    `processEvents()` does not deliver DeferredDelete at the same event-loop
    level, so a widget retired with `deleteLater()` is still on screen when
    the picture is taken. The totals bars are rebuilt on every refresh that
    way, so a shot showed two of them - a defect in the screenshot, not in
    the app, which is the worst kind to go looking for.
    """
    # Imported here, not at the top: Qt must not load before BOOKS_DB is
    # pinned, which is the whole reason main() imports it late too.
    from PySide6 import QtCore

    app.processEvents()
    app.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
    app.processEvents()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scheme", action="append", default=None,
                        help="scheme key; repeatable. Default: all of them.")
    parser.add_argument("--screens", type=int, nargs="+",
                        default=list(DEFAULT_SCREENS),
                        help="screen indexes, as in Ctrl+1..8 minus one.")
    parser.add_argument("--out", default=str(HERE / "shots"),
                        help="folder to write the PNGs into.")
    parser.add_argument("--tag", default="",
                        help="suffix for the file names, e.g. --tag before.")
    args = parser.parse_args()

    # Before desktop/db are imported: db reads BOOKS_DB at import time to pin
    # the file, and pinning is what keeps data/ out of reach.
    os.environ["BOOKS_DB"] = str(_books())

    from PySide6 import QtWidgets

    import db
    import desktop
    import theme

    schemes = args.scheme or [k for k in theme.ORDER if k != theme.AUTO]
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(sys.argv)
    theme.remember_native_style(app)
    theme.remember_base_font(app)

    # Closing a window offers a backup in a modal dialog, and there is nobody
    # here to answer it - this would hang waiting for a click. Taking a
    # picture is not a session worth backing up, and these are a throwaway
    # copy of the books anyway.
    desktop.MainWindow.ask_backup_on_close = lambda self: "no"

    written = []
    for key in schemes:
        # The window reads its scheme from the books it opens, so set it there
        # rather than re-applying afterwards - that is the real code path.
        db.set_setting(theme.SETTING_KEY, key)
        window = desktop.MainWindow()
        # Give the window its on-screen size without showing it: grab() of an
        # unshown widget uses its current geometry, and layouts have not run.
        window.resize(window.size())
        for index in args.screens:
            window.show_screen(index)
            settle(app)
            label = window.screens[index][0].replace("&&", "&")
            slug = label.lower().replace(" ", "-").replace("&", "and")
            tag = f"-{args.tag}" if args.tag else ""
            path = out / f"{key}-{slug}{tag}.png"
            window.grab().save(str(path))
            written.append(path)
        window.close()
        window.deleteLater()
        app.processEvents()

    for path in written:
        print(f"wrote {path.relative_to(HERE) if path.is_relative_to(HERE) else path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
