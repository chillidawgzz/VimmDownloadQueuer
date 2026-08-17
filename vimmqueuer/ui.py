"""Live terminal UI: the full queue, in order, updating in place."""

from __future__ import annotations

import time

from rich.console import Console, Group
from rich.live import Live
from rich.progress_bar import ProgressBar
from rich.table import Table
from rich.text import Text

from .queue_file import QueueItem, Status

_ICON = {
    Status.QUEUED: "•",
    Status.DOWNLOADING: "⬇",  # ⬇
    Status.COMPLETED: "✓",  # ✓
    Status.FAILED: "✗",  # ✗
    Status.SKIPPED: "✓",  # ✓
}

_STYLE = {
    Status.QUEUED: "dim",
    Status.DOWNLOADING: "bold cyan",
    Status.COMPLETED: "bold green",
    Status.FAILED: "bold red",
    Status.SKIPPED: "green",
}

PAUSE_ICON = "⏸"
PAUSE_STYLE = "bold yellow"

MIN_REFRESH_INTERVAL_S = 0.1  # cap redraws at ~10Hz - plenty smooth, avoids spamming the terminal


def _human_bytes(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}TB"


def _format_duration(seconds: float) -> str:
    seconds = max(0, int(seconds))
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


class QueueDisplay:
    """Usage: `with QueueDisplay(items, source) as display: ...` then call
    `display.update(item)` every time an item's status/progress changes."""

    def __init__(self, items: list[QueueItem], source_name: str):
        self.items = items
        self.source_name = source_name
        self.console = Console()
        self._last_refresh = 0.0
        self.live = Live(console=self.console, auto_refresh=False, transient=False)

    def __enter__(self) -> "QueueDisplay":
        self.live.__enter__()
        self._refresh(force=True)
        return self

    def __exit__(self, *exc) -> None:
        self._refresh(force=True)
        self.live.__exit__(*exc)

    def update(self, item: QueueItem) -> None:
        force = item.status in (Status.COMPLETED, Status.FAILED, Status.SKIPPED)
        self._refresh(force=force)

    def _refresh(self, force: bool = False) -> None:
        now = time.monotonic()
        if not force and (now - self._last_refresh) < MIN_REFRESH_INTERVAL_S:
            return
        self._last_refresh = now
        self.live.update(self._render(), refresh=True)

    # -- rendering ---------------------------------------------------

    def _ordered_items(self) -> list[QueueItem]:
        """Plain row_no order - which the watcher keeps in sync with the
        queue file's current order, so edits (add/remove/reorder) show up
        here live. Whatever's downloading stays in its own list position
        instead of jumping to the top, so you watch it progress in place."""
        return sorted(self.items, key=lambda i: i.row_no)

    def _row_text(self, item: QueueItem) -> Text:
        label = item.label
        style = _STYLE[item.status]
        if item.status == Status.FAILED:
            return Text(f"{label} — {item.error}", style=style)
        if item.status == Status.SKIPPED:
            return Text(f"{label} (already downloaded)", style=style)
        return Text(label, style=style)

    def _progress_text(self, item: QueueItem) -> str:
        if item.total_bytes:
            pct = item.received_bytes / item.total_bytes * 100
            size = f"{_human_bytes(item.received_bytes)}/{_human_bytes(item.total_bytes)}"
            amount = f"{size}  {pct:5.1f}%"
        else:
            amount = _human_bytes(item.received_bytes)
        parts = [amount]
        if item.started_at is not None:
            elapsed = time.monotonic() - item.started_at
            rate = item.received_bytes / elapsed if elapsed > 0 else 0.0
            if rate > 0:
                parts.append(f"{_human_bytes(rate)}/s")
            parts.append(_format_duration(elapsed))
            if item.total_bytes and rate > 0:
                remaining = item.total_bytes - item.received_bytes
                parts.append(f"eta {_format_duration(remaining / rate)}")
        return "  ".join(parts)

    def _render(self) -> Group:
        ordered = self._ordered_items()
        number_width = max(2, len(str(len(ordered))) + 1)

        table = Table.grid(padding=(0, 1, 0, 0))
        table.add_column(width=number_width, justify="right")
        table.add_column(width=2)
        table.add_column(ratio=1)
        table.add_column(width=50, justify="right")

        for position, item in enumerate(ordered, start=1):
            if item.is_pause:
                number = Text(f"{position}.", style=PAUSE_STYLE)
                icon = Text(PAUSE_ICON, style=PAUSE_STYLE)
                label = Text(f"── {item.label} ──", style=PAUSE_STYLE)
                table.add_row(number, icon, label, "")
                continue

            number = Text(f"{position}.", style=_STYLE[item.status])
            icon = Text(_ICON[item.status], style=_STYLE[item.status])
            if item.status == Status.DOWNLOADING:
                pct = (item.received_bytes / item.total_bytes * 100) if item.total_bytes else 0.0
                bar = ProgressBar(total=100, completed=min(pct, 100.0), width=32)
                table.add_row(number, icon, Group(self._row_text(item), bar), self._progress_text(item))
            else:
                table.add_row(number, icon, self._row_text(item), "")

        title = f"Queue: {self.source_name}  ({self._counts()})"
        if self._is_paused():
            title += f"  {PAUSE_ICON} paused"
        header = Text(title, style="bold")
        return Group(header, table)

    def _counts(self) -> str:
        done = sum(1 for i in self.items if i.status in (Status.COMPLETED, Status.SKIPPED))
        failed = sum(1 for i in self.items if i.status == Status.FAILED)
        total = sum(1 for i in self.items if not i.is_pause)
        suffix = f", {failed} failed" if failed else ""
        return f"{done}/{total} done{suffix}"

    def _is_paused(self) -> bool:
        """True if a pause marker is currently holding back queued items -
        i.e. nothing's downloading, and something queued sits behind the
        earliest pause marker's position."""
        pause_positions = [i.row_no for i in self.items if i.is_pause]
        if not pause_positions:
            return False
        if any(i.status == Status.DOWNLOADING for i in self.items):
            return False
        boundary = min(pause_positions)
        return any(
            i.status == Status.QUEUED and not i.is_pause and i.row_no > boundary
            for i in self.items
        )
