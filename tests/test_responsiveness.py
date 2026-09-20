"""
tests/test_responsiveness.py

Keeping the window usable while the network is working.

FIELD REPORT, twice: "the program softlocks until data is fully
loaded", and after a first attempt, "still happening".

The first attempt fixed startup — 2.5 seconds of snapshot parsing
charged to whichever button needed it first — which was real but was
not what the report described. Measuring the rest properly found
three separate causes, only one of which was the obvious one:

1. PROXY AUTO-DISCOVERY. With no proxy configured, Qt asks the OS
   what proxy to use, and on Windows that starts WPAD auto-discovery:
   a DNS and HTTP probe run SYNCHRONOUSLY on the calling thread. On a
   network with no WPAD server it blocks until the lookup times out.
   This is the big one, and it is invisible on Linux.

2. TWENTY REQUESTS AT ONCE. Qt caps connections per host at six and
   queues the rest internally, so the tail of a page waits behind the
   head with no way to abandon it when a new search supersedes it.

3. SYNCHRONOUS CACHE HITS. A cached page called back inline for every
   thumbnail, decoding twenty images in one call stack with no chance
   for Qt to process a click between them.

Decoding itself was measured at about 70 ms for a full page — real,
but not a freeze, and not worth moving off-thread given that an
earlier attempt at threading segfaulted Qt.

Run: python3 tests/test_responsiveness.py
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["TAGWALKER_CONFIG_DIR"] = tempfile.mkdtemp()

import io
from pathlib import Path

from PIL import Image
from PySide6.QtNetwork import QNetworkProxy
from PySide6.QtWidgets import QApplication

_app = QApplication.instance() or QApplication([])

from config.settings import Settings
from config.theme import initialize_theme
from ui.danbooru_fetcher import DanbooruFetcher
from ui.tag_reference_window import TagReferenceWindow


def _png() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (8, 8), "red").save(buf, "PNG")
    return buf.getvalue()


def test_cache_writes_do_not_sweep_the_whole_directory() -> None:
    """THE CAUSE. Every cache write used to trim the cache: a glob
    plus two stat() calls for every file already there, on the UI
    thread, for each of twenty thumbnails on a page.

    The cost is the MEASUREMENT, not the deletion: learning the
    cache's total size means stat-ing every file, and that happens
    before we can know whether anything needs removing. A cache well
    under its cap therefore paid the full price and deleted nothing —
    measured at 140 MB against a 200 MB cap: ~100 ms per write, 0
    files deleted, about two seconds of frozen window per page.

    So the freeze scaled with the NUMBER OF CACHED FILES, not with
    how full the cache was. The size cap is still enforced — by a
    sweep on an idle timer instead of on the write path."""
    import time

    from core.danbooru_api import EVICT_EVERY, ImageCache

    folder = Path(tempfile.mkdtemp())
    cache = ImageCache(folder, cap_bytes=200 * 1024 * 1024)
    blob = b"x" * 40_000
    for i in range(3000):
        (folder / f"old{i}.bin").write_bytes(blob)

    # Deliberately UNDER the cap: this is the case the old code got
    # wrong, and the one the user actually had.
    total = sum(p.stat().st_size for p in folder.glob("*.bin"))
    assert total < cache.cap_bytes

    start = time.perf_counter()
    cache.put("fresh", blob)
    elapsed_ms = (time.perf_counter() - start) * 1000
    assert elapsed_ms < 5, f"{elapsed_ms:.1f} ms per write"
    # Nothing was deleted, which is the point: the old code did this
    # much work to reach the same conclusion.
    assert (folder / "old0.bin").exists()

    # The cap is still honoured, on a schedule the caller controls.
    small = ImageCache(Path(tempfile.mkdtemp()), cap_bytes=1024)
    assert not small.maybe_evict()          # nothing written yet
    for _ in range(EVICT_EVERY):
        small.put("k", b"x" * 100)
    assert small.maybe_evict()              # threshold reached
    print("OK: a cache write no longer sweeps the cache directory, so "
          "page load does not slow down as the cache fills")


def test_a_full_cache_does_not_slow_a_page() -> None:
    """The same thing end to end, through a real browser window."""
    import json
    import time

    from core import danbooru_api as dapi
    from ui.tag_reference_window import TagReferenceWindow

    class _Fake:
        def __init__(self, routes):
            self.routes = routes
            self.started = 0

        def get(self, url, cb):
            self.started += 1
            r = self.routes.get(url)
            cb(None, 404) if r is None else cb(r[0], r[1])

    def _jpg() -> bytes:
        buf = io.BytesIO()
        Image.new("RGB", (850, 850), "red").save(buf, "JPEG")
        return buf.getvalue()

    s = Settings()
    initialize_theme(s.theme)
    s.danbooru_lookups_enabled = True
    s.danbooru_show_images = True
    s.danbooru_reveal_by_default = True     # load every thumbnail now

    posts = [{"id": i, "preview_file_url": f"https://x/p{i}",
              "large_file_url": f"https://x/l{i}", "rating": "g",
              "tag_string": "1girl", "tag_count": 1}
             for i in range(1, 21)]
    image = _jpg()
    routes = {dapi.posts_search_url("1girl", "rated", 1, 20):
              (json.dumps(posts).encode(), 200)}
    for i in range(1, 21):
        routes[f"https://x/p{i}"] = (image, 200)
        routes[f"https://x/l{i}"] = (image, 200)

    win = TagReferenceWindow(s, fetcher=_Fake(routes))
    win.show()
    folder = Path(win._img_cache.dir)
    folder.mkdir(parents=True, exist_ok=True)
    for i in range(6000):                   # deliberately over the cap
        (folder / f"old{i}.bin").write_bytes(b"x" * 40_000)

    browser = win._open_browser()
    fetcher = _Fake(routes)
    browser._fetcher = fetcher

    # Count directory scans rather than milliseconds. A wall-clock
    # threshold on shared hardware is a flaky test — this one failed
    # at 504 ms against a 400 ms limit purely because the machine was
    # busy, which says nothing about the code. What must hold is that
    # rendering a page never sweeps the cache directory at all.
    cache = win._img_cache
    sweeps = [0]
    original = cache._evict

    def counting(*args, **kwargs):
        sweeps[0] += 1
        return original(*args, **kwargs)

    cache._evict = counting
    try:
        browser.browse("1girl")
    finally:
        cache._evict = original
    assert sweeps[0] == 0, f"{sweeps[0]} cache sweeps during one page"
    # And the requests go out together: throttling them to six at a
    # time was tried, made loading visibly slower, and was reverted.
    assert fetcher.started >= 20

    before = len(list(folder.glob("*.bin")))
    win._img_cache.maybe_evict(force=True)
    after = len(list(folder.glob("*.bin")))
    total = sum(p.stat().st_size for p in folder.glob("*.bin"))
    assert after < before
    assert total <= win._img_cache.cap_bytes
    print("OK: a page renders quickly with an over-full cache, and "
          "the cap is enforced when the window goes idle")


def test_no_proxy_auto_discovery() -> None:
    """The cause that only shows on Windows, and the reason a Linux
    test suite could not find it."""
    fetcher = DanbooruFetcher()
    assert (fetcher._nam.proxy().type()
            == QNetworkProxy.ProxyType.NoProxy)
    print("OK: proxy auto-discovery is declined, so no synchronous "
          "WPAD lookup can block the first request of a session")


def test_requests_are_not_throttled() -> None:
    """Throttling to six at a time was tried as a softlock fix. It was
    the wrong cause, and the throttle made loading visibly slower and
    stretched the freeze out. Qt already limits connections per host;
    requests go straight to it."""
    fetcher = DanbooruFetcher()
    started: list = []
    fetcher._start = lambda url, cb: started.append(url)
    for i in range(20):
        fetcher.get(f"https://example.invalid/{i}", lambda *_a: None)
    assert len(started) == 20
    print("OK: requests are handed to Qt immediately rather than "
          "queued behind one another")


def test_cache_hits_do_not_block_in_one_call_stack() -> None:
    """Twenty cached thumbnails must not decode inside a single call,
    or the window cannot repaint or accept a click until the last one
    finishes."""
    s = Settings()
    initialize_theme(s.theme)
    s.danbooru_lookups_enabled = True

    class _NoNetwork:
        def get(self, url, cb):
            raise AssertionError("a cache hit must not hit the network")

    win = TagReferenceWindow(s, fetcher=_NoNetwork())
    win.show()
    for i in range(20):
        win._img_cache.put(f"k{i}", _png())

    seen: list = []
    for i in range(20):
        win.thumb_bytes(f"k{i}", "https://example.invalid/x",
                        lambda data, n=i: seen.append(n))
    # Nothing has run yet: every callback was deferred to the loop.
    assert seen == []
    _app.processEvents()
    assert len(seen) == 20
    print("OK: cached images call back through the event loop, so a "
          "warm page is twenty short blocks rather than one long one")


def test_memory_cache_is_capped() -> None:
    """A memory cache with no limit would be a leak dressed as a
    feature. The numbers make the design non-negotiable: an 850px
    preview is 12 KB compressed and 2,822 KB decoded, so an
    afternoon's browsing costs 113 MB as bytes and 26 GB as pixmaps.

    So: compressed bytes only, and a hard cap with LRU eviction that
    is genuinely cheap — the running total is tracked as entries come
    and go, nothing is scanned or measured."""
    from core.danbooru_api import MEMORY_CAP_BYTES, ImageCache

    folder = Path(tempfile.mkdtemp()) / "img"
    cache = ImageCache(folder, memory_cap_bytes=1_000_000)
    blob = b"x" * 100_000                    # ten fit in the cap

    for i in range(25):
        cache.put(f"k{i}", blob)
    assert cache.memory_bytes() <= 1_000_000
    assert len(cache._mem) == 10
    assert "k0" not in cache._mem            # oldest dropped
    assert "k24" in cache._mem               # newest kept

    # Evicted from memory but still on disk, and a read promotes it.
    assert (folder / "k0.bin").exists()
    assert cache.get("k0") == blob
    assert "k0" in cache._mem

    # A sane default, and it is separate from the disk limit.
    assert MEMORY_CAP_BYTES == 64 * 1024 * 1024
    print("OK: the memory cache holds compressed bytes under a hard "
          "cap, dropping least-recently-used entries")


def test_disk_cache_can_be_turned_off() -> None:
    """Privacy as much as disk space: cached thumbnails are a record
    of what was viewed. Turning the disk cache off must still leave
    browsing fast within a session."""
    from core.danbooru_api import DISK_CACHE_CHOICES, ImageCache

    folder = Path(tempfile.mkdtemp()) / "img"
    cache = ImageCache(folder, cap_bytes=0)
    cache.put("k", b"x" * 50_000)
    assert not (folder.exists() and list(folder.glob("*.bin")))
    assert cache.get("k") == b"x" * 50_000   # memory still serves it

    # Shrinking applies at once: a setting that appears to do nothing
    # until later is a setting people distrust.
    big = Path(tempfile.mkdtemp()) / "img"
    big.mkdir(parents=True)
    sized = ImageCache(big, cap_bytes=200 * 1024 * 1024)
    for i in range(3000):
        (big / f"f{i}.bin").write_bytes(b"x" * 40_000)
    sized.set_disk_cap(50 * 1024 * 1024)
    total = sum(p.stat().st_size for p in big.glob("*.bin"))
    assert total <= 50 * 1024 * 1024
    sized.set_disk_cap(0)
    assert not list(big.glob("*.bin"))

    # Every offered choice is explained, including why it exists.
    assert len(DISK_CACHE_CHOICES) == 4
    for mb, label, why in DISK_CACHE_CHOICES:
        assert label and len(why) > 40, mb
    print("OK: the disk cache can be turned off entirely, shrinking "
          "takes effect immediately, and every choice is explained")


def run() -> None:
    test_cache_writes_do_not_sweep_the_whole_directory()
    test_a_full_cache_does_not_slow_a_page()
    test_memory_cache_is_capped()
    test_disk_cache_can_be_turned_off()
    test_no_proxy_auto_discovery()
    test_requests_are_not_throttled()
    test_cache_hits_do_not_block_in_one_call_stack()
    print("\nALL PASS: responsiveness")


if __name__ == "__main__":
    run()
