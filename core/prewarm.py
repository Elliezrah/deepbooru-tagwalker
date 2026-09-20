"""
core/prewarm.py

Loads the bundled reference data early, in small pieces, so that no
single button press pays for all of it.

FIELD REPORT: "the program softlocks until data is fully loaded."
Measured, the first use of each dataset blocked the UI thread for:

    tag snapshots (4 CSVs)   ~1300 ms
    tag database classify     ~550 ms
    co-occurrence table       ~310 ms
    CLIP vocabulary           ~290 ms

Around two and a half seconds of frozen window, charged to whichever
feature happened to need it first — so it landed on a Tag Referencer
lookup or the first Token Counter run, and read as that feature being
slow rather than a one-off cost.

WHY THIS IS NOT A BACKGROUND THREAD. It was, briefly. Parsing megabytes
of CSV on a worker thread reliably segfaulted the test suite with
"QObject::killTimer: Timers cannot be stopped from another thread" —
a Qt object being destroyed off the main thread. Suspending cyclic
garbage collection did not fix it, and a daemon thread abandoned at
interpreter shutdown is unsafe with Qt regardless. A slow first click
is a nuisance; a segfault in someone's tagging session is not, so the
thread is gone.

Instead the work is split into steps and driven by a timer on the main
thread. Each step is one file, a few hundred milliseconds at most, and
Qt processes events between them. Nothing runs off the main thread and
nothing blocks for more than one step at a time.
"""
from __future__ import annotations

from typing import Callable, Optional

_STARTED = False
_timer = None


def _steps() -> list[tuple[str, Callable[[], object]]]:
    """The work, smallest useful units first.

    Ordered by what the user is likeliest to reach first: the tag
    database backs the tree that appears as soon as a folder loads,
    while the CLIP vocabulary is not needed until the Token Counter
    is opened.
    """
    def tag_db():
        from core import tag_database
        return tag_database.bucket_for("1girl")

    def snapshots():
        from core import tag_reference
        return tag_reference.ensure_loaded()

    def cooccurrence():
        from core import cooccurrence as co
        return co.get_db().get_cooccurring("1girl", limit=1)

    def clip_vocab():
        from core import clip_token_counter
        return clip_token_counter.ensure_loaded()

    return [("tag database", tag_db),
            ("tag snapshots", snapshots),
            ("co-occurrence", cooccurrence),
            ("CLIP vocabulary", clip_vocab)]


def start(parent=None, interval_ms: int = 120) -> bool:
    """Begin loading. Safe to call more than once; only the first call
    starts anything. Returns True for that call.

    `parent` should be a QObject (the main window) so the timer dies
    with it rather than outliving the application.
    """
    global _STARTED, _timer
    if _STARTED:
        return False
    _STARTED = True
    try:
        from PySide6.QtCore import QTimer
    except Exception:
        # No Qt available (a core-only test run): just do the work.
        run_all()
        return True

    pending = _steps()

    def tick() -> None:
        if not pending:
            if _timer is not None:
                _timer.stop()
            return
        _label, work = pending.pop(0)
        try:
            work()
        except Exception:
            # Whatever fails here will fail again, visibly, when the
            # feature that needs it is used. Startup is not the place
            # to report it.
            pass

    _timer = QTimer(parent)
    _timer.setInterval(interval_ms)
    _timer.timeout.connect(tick)
    _timer.start()
    return True


def run_all() -> None:
    """Do every step immediately, on the calling thread. Used when Qt
    is unavailable, and by tests that want the data ready."""
    for _label, work in _steps():
        try:
            work()
        except Exception:
            pass


def started() -> bool:
    return _STARTED


def reset_for_tests() -> None:
    global _STARTED, _timer
    _STARTED = False
    _timer = None
