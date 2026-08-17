"""Entry point: `vimm-queuer QUEUE_FILE [--out-dir DIR]`."""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from .downloader import VimmDownloader
from .queue_file import Status, parse_queue_file
from .ui import QueueDisplay


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="vimm-queuer",
        description=(
            "Download a queue of Vimm's Lair vault links, one at a time, "
            "with a live front-to-back progress view."
        ),
    )
    parser.add_argument(
        "queue_file",
        type=Path,
        help="CSV file with 'title,url' columns, one Vimm's Lair vault URL per row",
    )
    parser.add_argument(
        "-o",
        "--out-dir",
        type=Path,
        default=Path("downloads"),
        help="Directory to save downloads into (default: ./downloads)",
    )
    parser.add_argument(
        "--headful",
        action="store_true",
        help="Show the browser window instead of running headless (needs a display, e.g. WSLg) — useful for debugging",
    )
    parser.add_argument(
        "--no-resume",
        action="store_true",
        help="Don't skip files that were already downloaded in a previous run",
    )
    return parser


async def _run(args: argparse.Namespace) -> int:
    items = parse_queue_file(args.queue_file)

    async with VimmDownloader(out_dir=args.out_dir, headless=not args.headful) as downloader:
        with QueueDisplay(items, source_name=str(args.queue_file)) as display:
            await downloader.run_queue(
                items,
                queue_path=args.queue_file,
                on_update=display.update,
                skip_existing=not args.no_resume,
            )

    failed = [i for i in items if i.status == Status.FAILED]
    if failed:
        print(f"\n{len(failed)} of {len(items)} download(s) failed:", file=sys.stderr)
        for item in failed:
            print(f"  row {item.row_no}: {item.label} — {item.error}", file=sys.stderr)
        return 1
    return 0


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    try:
        exit_code = asyncio.run(_run(args))
    except (ValueError, FileNotFoundError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        sys.exit(2)
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        sys.exit(130)

    sys.exit(exit_code)


if __name__ == "__main__":
    main()
