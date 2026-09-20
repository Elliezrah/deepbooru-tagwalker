"""
ui/danbooru_fetcher.py

The Tag Reference's transport layer: a thin async wrapper over
QNetworkAccessManager. This is the ONLY networking code in TagWalker,
and it exists behind the master opt-in (Settings \u2192 Danbooru
lookups); with the switch off it is never even constructed.

Design (DESIGN_TAG_REFERENCE.md):
- fully asynchronous — a dead connection can never freeze the walk;
- identifies the app via User-Agent (core.danbooru_api.USER_AGENT);
- 15 s transfer timeout;
- callbacks receive (payload_bytes | None, http_status); transport
  errors surface as (None, 0) and HTTP errors as (None, status), so
  the orchestration in the window can distinguish 404 (similarity
  path) from 429 (back off) from everything else (degrade offline).

Tests never touch this class: the window takes an injectable fetcher,
and the suite supplies a canned fake (spec's testing approach).
"""
from __future__ import annotations

from typing import Callable, Optional

from PySide6.QtCore import QObject, QUrl
from PySide6.QtNetwork import (
    QNetworkAccessManager,
    QNetworkReply,
    QNetworkRequest,
)

from PySide6.QtNetwork import QNetworkProxy, QNetworkProxyFactory

from core.danbooru_api import USER_AGENT

TIMEOUT_MS = 15_000

# Requests allowed to run at once. Qt itself caps concurrent
# connections per host at six; matching that here keeps the queue
# visible to this class so it can be abandoned when a new search
# supersedes it.
MAX_IN_FLIGHT = 6

Callback = Callable[[Optional[bytes], int], None]


class DanbooruFetcher(QObject):
    def __init__(self, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self._nam = QNetworkAccessManager(self)

        # FIELD REPORT: "the program softlocks while posts load."
        #
        # The requests themselves are asynchronous, and decoding the
        # thumbnails costs about 70 ms for a full page — neither is
        # enough to freeze a window for seconds. The proxy lookup is.
        #
        # With no proxy set, Qt asks the operating system what proxy
        # to use, and on Windows that triggers WPAD auto-discovery: a
        # DNS and HTTP probe for a wpad host, performed SYNCHRONOUSLY
        # on the calling thread. On a network with no WPAD server it
        # waits for the lookup to time out. That is a frozen UI on the
        # first request of a session, and again whenever the cached
        # result expires.
        #
        # This program talks to exactly one public host over HTTPS and
        # has no business going through a corporate proxy, so the
        # lookup is declined outright.
        QNetworkProxyFactory.setUseSystemConfiguration(False)
        self._nam.setProxy(QNetworkProxy(
            QNetworkProxy.ProxyType.NoProxy))

        self._live: set[QNetworkReply] = set()   # keep replies alive
        self._queue: list = []                   # not yet started

    def get(self, url: str, callback: Callback) -> None:
        """Start a request.

        REVERTED: this briefly held its own queue, six at a time, on
        the theory that a burst of twenty was part of the softlock. It
        was not — the cache eviction was — and throttling made loading
        visibly slower and stretched the freeze out, which is exactly
        what the next field report said.

        Qt already limits connections per host and pipelines the rest.
        Requests are handed straight to it.
        """
        self._start(url, callback)

    def abandon_pending(self) -> None:
        """Kept for callers; there is no queue of our own to drop
        now that requests go straight to Qt."""
        self._queue.clear()

    def _start(self, url: str, callback: Callback) -> None:
        request = QNetworkRequest(QUrl(url))
        request.setHeader(
            QNetworkRequest.KnownHeaders.UserAgentHeader, USER_AGENT)
        request.setTransferTimeout(TIMEOUT_MS)
        reply = self._nam.get(request)
        self._live.add(reply)

        def _finished() -> None:
            self._live.discard(reply)
            status = reply.attribute(
                QNetworkRequest.Attribute.HttpStatusCodeAttribute)
            status = int(status) if status is not None else 0
            if reply.error() == QNetworkReply.NetworkError.NoError:
                payload: Optional[bytes] = bytes(reply.readAll().data())
            else:
                payload = None
            reply.deleteLater()
            try:
                callback(payload, status)
            except Exception:
                # A rendering bug must never take down the network
                # stack; the crashlog hook still records it.
                raise

        reply.finished.connect(_finished)
