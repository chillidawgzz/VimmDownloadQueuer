"""Parsing/writing of the queue file: a CSV with columns `title,url`.

`title` may be blank - the downloader fills in anything missing from the
site itself before a run starts and rewrites the file in place.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

FIELDNAMES = ["title", "url"]


class Status(str, Enum):
    QUEUED = "queued"
    DOWNLOADING = "downloading"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"  # already downloaded on a previous run


@dataclass
class QueueItem:
    """One row from the queue file and its live download state."""

    url: str
    row_no: int
    title: str = ""
    status: Status = Status.QUEUED
    filename: str | None = None
    received_bytes: int = 0
    total_bytes: int = 0
    error: str | None = None
    attempts: int = 0
    started_at: float | None = None  # time.monotonic() when the current attempt began

    @property
    def label(self) -> str:
        """Best available human-readable name for display/logging."""
        return self.title or self.filename or self.url


def parse_queue_file(path: Path) -> list[QueueItem]:
    """Read a queue file into an ordered list of QueueItems.

    Each non-blank line is either `title,url` or a bare URL (title left
    blank) - the comma isn't required when you don't have a title yet. An
    optional `title,url` header line is recognized and skipped if present.
    """
    if not path.exists():
        raise FileNotFoundError(f"queue file not found: {path}")

    lines = path.read_text(encoding="utf-8").splitlines()

    start = 0
    if lines:
        header = next(csv.reader([lines[0]]), [])
        if [c.strip().lower() for c in header] == FIELDNAMES:
            start = 1

    items: list[QueueItem] = []
    row_no = 0
    for raw in lines[start:]:
        if not raw.strip():
            continue
        row_no += 1
        fields = next(csv.reader([raw]))
        if len(fields) >= 2:
            title, url = fields[0].strip(), fields[1].strip()
        else:
            title, url = "", fields[0].strip()
        if not url:
            continue
        if not url.lower().startswith(("http://", "https://")):
            raise ValueError(f"{path}: row {row_no}: doesn't look like a URL: {url!r}")
        items.append(QueueItem(url=url, row_no=row_no, title=title))

    if not items:
        raise ValueError(f"{path}: no URLs found")

    return items


def write_queue_file(path: Path, items: list[QueueItem]) -> None:
    """Rewrite the queue file, e.g. after filling in discovered titles."""
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDNAMES)
        writer.writeheader()
        for item in items:
            writer.writerow({"title": item.title, "url": item.url})
