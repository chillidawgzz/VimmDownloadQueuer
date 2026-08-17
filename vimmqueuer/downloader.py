"""Playwright-driven downloader for Vimm's Lair vault pages.

Each vault page has a plain <form id="dl_form"> whose submit button triggers
the site's download. Rather than replicating that request with a raw HTTP
client (which trips the site's bot detection almost immediately - a plain
curl request with copied cookies/headers gets an HTTP 400 "browser is acting
funny" or a 429), we drive a real headless Chromium tab through Playwright
and let the browser make the request itself.

For progress reporting we open a Chrome DevTools Protocol session on that
same authenticated page and listen to Page.downloadProgress events, so we
get true byte-level progress from Chromium's own network stack without ever
issuing a second, suspicious-looking request.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import random
import time
from pathlib import Path
from typing import Callable
from urllib.parse import urlparse

from playwright.async_api import async_playwright, Browser, BrowserContext

from .queue_file import QueueItem, Status, parse_queue_file, write_queue_file

TRACE_LOG_PATH = Path("/tmp/vimmqueuer_trace.log")
logger = logging.getLogger("vimmqueuer")
logger.setLevel(logging.DEBUG)
if not logger.handlers:
    _handler = logging.FileHandler(TRACE_LOG_PATH, mode="w", encoding="utf-8")
    _handler.setFormatter(
        logging.Formatter("%(asctime)s.%(msecs)03d %(levelname)s %(message)s", datefmt="%H:%M:%S")
    )
    logger.addHandler(_handler)

DOWNLOAD_BUTTON_SELECTOR = "#dl_form button[type=submit]"
UNAVAILABLE_SELECTOR = "#upload-row"
NAV_TIMEOUT_MS = 30_000
START_TIMEOUT_S = 45  # max time to wait for a download to begin at all
IDLE_TIMEOUT_S = 90  # once bytes are flowing, max time between progress updates
POLL_INTERVAL_S = 1.0
RETRIES = 3
RETRY_BACKOFF_S = (3, 8, 20)
POLITE_DELAY_RANGE_S = (3.0, 8.0)
PREFETCH_DELAY_RANGE_S = (1.5, 4.0)
WATCH_POLL_INTERVAL_S = 2.0
WATCH_IDLE_EXIT_S = 4.0  # stop watching this long after the file goes quiet and the queue drains
MANIFEST_NAME = ".vimmqueuer_manifest.json"
TITLE_PREFIX_TO_STRIP = "The Vault: "

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

ProgressCallback = Callable[[QueueItem], None]


class DownloadError(Exception):
    """Raised when a single item fails to download (caller decides on retry)."""


class VimmDownloader:
    """Owns one browser + one persistent context for the whole run.

    Reusing a single context (and therefore a single cookie jar) across all
    items makes the run look like one continuous browsing session rather
    than N unrelated requests, which matters both for politeness and for
    not tripping rate limiting.
    """

    def __init__(self, out_dir: Path, headless: bool = True):
        # Chrome's Page.setDownloadBehavior interprets a relative downloadPath
        # relative to its own process's working directory, not this script's -
        # a relative --out-dir (e.g. the ordinary "../downloads") silently
        # pointed Chrome at the wrong location and it canceled every download
        # almost immediately. Resolving here fixes it for every caller.
        self.out_dir = out_dir.resolve()
        self.headless = headless
        self._playwright = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None

    async def __aenter__(self) -> "VimmDownloader":
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(
            headless=self.headless,
            # channel="chromium" forces the full Chrome for Testing binary
            # rather than Playwright's stripped-down "chromium-headless-shell"
            # (the default for headless=True), and the two flags below are
            # cheap defense-in-depth around Chrome's download-protection /
            # GPU-init behavior. None of these turned out to be the actual
            # cause of downloads getting canceled (that was a relative
            # --out-dir path being passed straight to Chrome's own
            # Page.setDownloadBehavior, fixed via out_dir.resolve() above) -
            # kept here because they're harmless and still reasonable
            # defaults for headless/headful automation in general.
            channel="chromium",
            args=[
                "--disable-blink-features=AutomationControlled",
                "--safebrowsing-disable-download-protection",
                # In headful mode via WSLg, Chrome's GPU process has been
                # observed crashing on initialization ("Exiting GPU process
                # due to errors", "ContextResult::kTransientFailure"),
                # eventually destabilizing and closing the whole browser
                # mid-run. --enable-unsafe-swiftshader (already set by
                # Playwright) only covers WebGL fallback - it doesn't stop
                # Chrome from trying to start a real GPU process first and
                # failing. --disable-gpu skips that entirely.
                "--disable-gpu",
            ],
        )
        self._context = await self._browser.new_context(
            user_agent=DEFAULT_USER_AGENT,
            accept_downloads=True,
        )
        return self

    async def __aexit__(self, *exc) -> None:
        if self._context:
            await self._context.close()
        if self._browser:
            await self._browser.close()
        if self._playwright:
            await self._playwright.stop()

    # -- public API ---------------------------------------------------

    async def run_queue(
        self,
        items: list[QueueItem],
        queue_path: Path,
        on_update: ProgressCallback,
        skip_existing: bool = True,
    ) -> None:
        if skip_existing:
            self._mark_resumable(items)

        if await self._prefetch_titles(items, on_update):
            write_queue_file(queue_path, items)

        for item in items:
            if item.status == Status.SKIPPED:
                on_update(item)

        # `pending` (not yet started) is the part live edits can actually
        # affect: added, removed, or reordered to match the file. Once an
        # item starts downloading it's out of `pending` and there's no
        # clean way to unwind it, so edits to already-downloading/finished
        # rows don't retroactively do anything.
        by_url: dict[str, QueueItem] = {i.url: i for i in items}
        pending: list[QueueItem] = [i for i in items if i.status == Status.QUEUED]
        wake = asyncio.Event()
        stop_requested = asyncio.Event()

        watcher = asyncio.create_task(
            self._watch_queue_file(queue_path, items, by_url, pending, wake, stop_requested, on_update)
        )
        try:
            first = True
            while True:
                if not pending:
                    if stop_requested.is_set():
                        break
                    wake.clear()
                    await wake.wait()
                    continue
                item = pending.pop(0)
                if item.status != Status.QUEUED:
                    continue  # reconciled away between being queued and being popped
                if not first:
                    await asyncio.sleep(random.uniform(*POLITE_DELAY_RANGE_S))
                first = False
                await self._download_with_retries(item, on_update)
        finally:
            watcher.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await watcher

    # -- live queue-file watching -----------------------------------------

    async def _watch_queue_file(
        self,
        queue_path: Path,
        items: list[QueueItem],
        by_url: dict[str, QueueItem],
        pending: list[QueueItem],
        wake: asyncio.Event,
        stop_requested: asyncio.Event,
        on_update: ProgressCallback,
    ) -> None:
        """Poll the queue file for edits made while a run is in progress and
        reconcile `items`/`pending` to match: added rows join the queue,
        removed rows (if not yet started) drop out, and the still-pending
        portion is reordered to match the file's current order. Exits (and
        tells the main loop to stop) once the file has been quiet and
        everything's finished for a little while.
        """
        loop = asyncio.get_running_loop()
        last_mtime = self._safe_mtime(queue_path)
        stable_since = loop.time()

        while True:
            await asyncio.sleep(WATCH_POLL_INTERVAL_S)
            mtime = self._safe_mtime(queue_path)

            if mtime != last_mtime:
                last_mtime = mtime
                stable_since = loop.time()
                await self._reconcile_queue_file(queue_path, items, by_url, pending, on_update)
                wake.set()
                continue

            queue_drained = not pending and all(
                i.status not in (Status.QUEUED, Status.DOWNLOADING) for i in items
            )
            if queue_drained and (loop.time() - stable_since) >= WATCH_IDLE_EXIT_S:
                stop_requested.set()
                wake.set()
                return

    @staticmethod
    def _safe_mtime(path: Path) -> float | None:
        try:
            return path.stat().st_mtime
        except OSError:
            return None

    async def _reconcile_queue_file(
        self,
        queue_path: Path,
        items: list[QueueItem],
        by_url: dict[str, QueueItem],
        pending: list[QueueItem],
        on_update: ProgressCallback,
    ) -> None:
        try:
            rows = parse_queue_file(queue_path)
        except (FileNotFoundError, ValueError, OSError) as exc:
            # Transient: an editor can briefly leave the file empty/mid-write
            # while saving. Just wait for the next poll.
            logger.debug("watcher: skipping unreadable queue file: %s", exc)
            return

        file_order = [row.url for row in rows]
        file_urls = set(file_order)
        file_titles = {row.url: row.title for row in rows}
        changed = False

        for item in [i for i in pending if i.url not in file_urls]:
            pending.remove(item)
            items.remove(item)
            del by_url[item.url]
            changed = True
            logger.debug("watcher: removed row: %s", item.url)

        new_items = []
        for row in rows:
            if row.url not in by_url:
                by_url[row.url] = row
                items.append(row)
                pending.append(row)
                new_items.append(row)
                changed = True
                logger.debug("watcher: added row: %s", row.url)

        # Reorder the still-pending portion to match the file's current
        # order - already-downloading/finished rows keep their place.
        reordered = [by_url[u] for u in file_order if by_url.get(u) in pending]
        if reordered != pending:
            pending[:] = reordered
            changed = True
            logger.debug("watcher: reordered pending queue")

        # Position numbers for display, matching the file's current order.
        for row_no, url in enumerate(file_order, start=1):
            if url in by_url:
                by_url[url].row_no = row_no

        titles_changed = False
        for item in new_items:
            if not item.title:
                title = await self._fetch_title(item.url)
                if title:
                    item.title = title
                    titles_changed = True

        # A manual title edit directly in the file, for a row we already
        # know about, is respected too - but only while it's still pending;
        # once a download starts, item.title has already been used/shown.
        for url, title in file_titles.items():
            item = by_url.get(url)
            if item and item in pending and title and title != item.title:
                item.title = title
                titles_changed = True
                changed = True

        if titles_changed:
            ordered_for_write = [by_url[u] for u in file_order if u in by_url]
            write_queue_file(queue_path, ordered_for_write)

        if changed and items:
            on_update(items[0])

    # -- title prefetch --------------------------------------------------

    async def _prefetch_titles(self, items: list[QueueItem], on_update: ProgressCallback) -> bool:
        """Fill in item.title for any item missing one. Returns True if any changed."""
        missing = [i for i in items if not i.title]
        changed = False
        for index, item in enumerate(missing):
            title = await self._fetch_title(item.url)
            if title:
                item.title = title
                changed = True
                on_update(item)
            if index < len(missing) - 1:
                await asyncio.sleep(random.uniform(*PREFETCH_DELAY_RANGE_S))
        return changed

    async def _fetch_title(self, url: str) -> str | None:
        assert self._context is not None
        page = await self._context.new_page()
        try:
            page.set_default_timeout(NAV_TIMEOUT_MS)
            await page.goto(url, wait_until="domcontentloaded")
            raw = await page.title()
            if raw.startswith(TITLE_PREFIX_TO_STRIP):
                raw = raw[len(TITLE_PREFIX_TO_STRIP):]
            return raw.strip() or None
        except Exception:
            return None
        finally:
            await page.close()

    # -- resume / manifest ---------------------------------------------

    def _manifest_path(self) -> Path:
        return self.out_dir / MANIFEST_NAME

    def _load_manifest(self) -> dict:
        path = self._manifest_path()
        if not path.exists():
            return {}
        try:
            return json.loads(path.read_text())
        except json.JSONDecodeError:
            return {}

    def _record_manifest(self, item: QueueItem) -> None:
        manifest = self._load_manifest()
        manifest[item.url] = {"filename": item.filename, "bytes": item.received_bytes}
        self._manifest_path().write_text(json.dumps(manifest, indent=2))

    def _mark_resumable(self, items: list[QueueItem]) -> None:
        manifest = self._load_manifest()
        for item in items:
            entry = manifest.get(item.url)
            if not entry:
                continue
            filename = entry.get("filename")
            if filename and (self.out_dir / filename).exists():
                item.status = Status.SKIPPED
                item.filename = filename
                item.received_bytes = entry.get("bytes", 0)
                item.total_bytes = item.received_bytes

    # -- per-item download -----------------------------------------------

    async def _download_with_retries(self, item: QueueItem, on_update: ProgressCallback) -> None:
        last_error: Exception | None = None
        for attempt in range(1, RETRIES + 1):
            self._cleanup_partial(item)
            item.attempts = attempt
            item.status = Status.DOWNLOADING
            item.received_bytes = 0
            item.total_bytes = 0
            item.error = None
            item.filename = None
            item.started_at = time.monotonic()
            on_update(item)
            logger.debug("--- item %r attempt %d/%d ---", item.title or item.url, attempt, RETRIES)
            try:
                await self._download_once(item, on_update)
                item.status = Status.COMPLETED
                on_update(item)
                self._record_manifest(item)
                return
            except Exception as exc:  # noqa: BLE001 - we want to retry on anything
                last_error = exc
                logger.debug("attempt %d/%d failed: %s", attempt, RETRIES, exc)
                if attempt < RETRIES:
                    await asyncio.sleep(RETRY_BACKOFF_S[min(attempt - 1, len(RETRY_BACKOFF_S) - 1)])

        item.status = Status.FAILED
        item.error = str(last_error) if last_error else "unknown error"
        on_update(item)

    def _cleanup_partial(self, item: QueueItem) -> None:
        """Remove any leftover file from a previous failed attempt at this
        item, so a retry doesn't pile up as "name (1)", "name (2)", ... -
        Chromium appends that suffix whenever its target filename is taken."""
        if not item.filename:
            return
        for suffix in ("", ".crdownload"):
            path = self.out_dir / f"{item.filename}{suffix}"
            if path.exists():
                try:
                    path.unlink()
                except OSError:
                    pass

    async def _download_once(self, item: QueueItem, on_update: ProgressCallback) -> None:
        assert self._context is not None
        page = await self._context.new_page()
        logger.debug("=== attempt start: %s (%s) ===", item.title or item.url, item.url)

        def _on_console(msg) -> None:
            logger.debug("console[%s]: %s", msg.type, msg.text)

        def _on_pageerror(exc) -> None:
            logger.debug("pageerror: %s", exc)

        def _on_response(resp) -> None:
            host = urlparse(resp.url).hostname or ""
            if host.endswith("vimm.net"):
                logger.debug("response %s %s headers=%s", resp.status, resp.url, dict(resp.headers))

        def _on_requestfailed(req) -> None:
            host = urlparse(req.url).hostname or ""
            if host.endswith("vimm.net"):
                logger.debug("requestfailed %s failure=%s", req.url, req.failure)

        page.on("console", _on_console)
        page.on("pageerror", _on_pageerror)
        page.on("response", _on_response)
        page.on("requestfailed", _on_requestfailed)
        try:
            page.set_default_timeout(NAV_TIMEOUT_MS)
            await page.goto(item.url, wait_until="domcontentloaded")
            logger.debug("page loaded: %s", page.url)

            if await page.locator(UNAVAILABLE_SELECTOR).count() and await page.locator(
                UNAVAILABLE_SELECTOR
            ).first.is_visible():
                logger.debug("title unavailable in vault")
                raise DownloadError("this title isn't currently available in the vault")

            cdp = await self._context.new_cdp_session(page)
            await cdp.send("Page.enable")
            behavior_resp = await cdp.send(
                "Page.setDownloadBehavior",
                {
                    "behavior": "allow",
                    "downloadPath": str(self.out_dir),
                    "eventsEnabled": True,
                },
            )
            logger.debug("setDownloadBehavior -> %r", behavior_resp)

            loop = asyncio.get_running_loop()
            done = asyncio.Event()
            state: dict = {"guid": None, "filename": None, "error": None}
            last_activity = loop.time()

            def on_will_begin(evt: dict) -> None:
                nonlocal last_activity
                logger.debug("downloadWillBegin: %r", evt)
                state["guid"] = evt.get("guid")
                state["filename"] = evt.get("suggestedFilename")
                item.filename = state["filename"]
                last_activity = loop.time()
                on_update(item)

            def on_progress(evt: dict) -> None:
                nonlocal last_activity
                logger.debug("downloadProgress: %r", evt)
                if state["guid"] and evt.get("guid") != state["guid"]:
                    return
                received = evt.get("receivedBytes", 0)
                # Chromium can emit progress pings with an unchanged byte
                # count while a connection sits open with no data flowing
                # (e.g. the server accepted the request but is throttling or
                # queuing it). Only *actual* new bytes count as activity -
                # otherwise a truly stalled download would never trip the
                # idle timeout below, since "an event happened" isn't the
                # same as "it's making progress".
                if received > item.received_bytes:
                    last_activity = loop.time()
                item.received_bytes = received
                item.total_bytes = evt.get("totalBytes", 0)
                on_update(item)
                if evt.get("state") == "canceled":
                    state["error"] = "download canceled"
                    done.set()
                elif evt.get("state") == "completed":
                    done.set()

            cdp.on("Page.downloadWillBegin", on_will_begin)
            cdp.on("Page.downloadProgress", on_progress)

            # A real download never navigates the tab away - Chromium intercepts
            # it at the network layer before it becomes a page. If the site
            # instead rejects the request (rate limited, title unavailable,
            # blocked, ...) it responds with a normal HTML error page and the
            # tab *does* navigate. We race those two outcomes so a rejection
            # surfaces immediately with the site's own explanation instead of
            # us sitting out the full timeout and reporting a generic failure.
            original_url = page.url
            navigated_away = asyncio.Event()

            def on_framenavigated(frame) -> None:
                if frame == page.main_frame and frame.url != original_url:
                    logger.debug("frame navigated away to %s", frame.url)
                    navigated_away.set()

            page.on("framenavigated", on_framenavigated)
            try:
                button = page.locator(DOWNLOAD_BUTTON_SELECTOR).first
                await button.wait_for(state="visible", timeout=NAV_TIMEOUT_MS)
                logger.debug("clicking download button")
                await button.click()
                last_activity = loop.time()

                # Some vault pages (an ad-block soft-gate, seemingly tied to
                # whether a particular ad script has loaded by click time)
                # don't submit the form directly - they pop open a <dialog>
                # with a "Continue" button instead, and the real download
                # only starts once that's clicked too.
                dialog_handled = False

                # Poll rather than wait on one flat timeout: a multi-GB file
                # can legitimately take a long time, so what we actually want
                # to catch is *no progress*, not *not finished yet*. The
                # allowed idle window is short before the download has even
                # begun (should start almost immediately) and longer once
                # bytes are actively flowing.
                while True:
                    if done.is_set():
                        break
                    if navigated_away.is_set():
                        await page.wait_for_load_state("domcontentloaded")
                        body_text = await page.locator("body").inner_text()
                        raise DownloadError(" ".join(body_text.split())[:300])

                    if not dialog_handled and not state["filename"]:
                        dialog_open = await page.evaluate(
                            "() => !!document.querySelector('dialog[open]')"
                        )
                        if dialog_open:
                            logger.debug("confirmation dialog opened - clicking Continue")
                            dialog_handled = True
                            continue_button = page.locator(
                                "dialog[open] form[method=dialog] input[type=submit], "
                                "dialog[open] form[method=dialog] button[type=submit]"
                            ).first
                            await continue_button.click()
                            last_activity = loop.time()

                    idle_limit = IDLE_TIMEOUT_S if state["filename"] else START_TIMEOUT_S
                    if loop.time() - last_activity > idle_limit:
                        if state["filename"]:
                            logger.debug("idle timeout: no new bytes for %.0fs", idle_limit)
                            raise DownloadError(
                                f"download stalled - no progress for {idle_limit:.0f}s"
                            )
                        logger.debug("start timeout: no download after %.0fs", idle_limit)
                        raise DownloadError("timed out waiting for the download to start")

                    await asyncio.sleep(POLL_INTERVAL_S)
            finally:
                page.remove_listener("framenavigated", on_framenavigated)

            if state["error"]:
                logger.debug("attempt ending with state error: %s", state["error"])
                raise DownloadError(state["error"])
            if not state["filename"]:
                logger.debug("attempt ending: no download ever started")
                raise DownloadError("no download started (button missing or request blocked)")
            logger.debug("attempt succeeded: %s (%d bytes)", item.filename, item.received_bytes)
        except Exception as exc:
            logger.debug("attempt raised: %r", exc)
            raise
        finally:
            await page.close()
