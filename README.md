# vimm-queuer

A small CLI that downloads a queue of [Vimm's Lair](https://vimm.net) vault
links, **one at a time, in order**, with a live in-place progress view.

It drives a real Chromium tab (via [Playwright](https://playwright.dev/python/))
through each vault page's own "Download" button — this is a plain browser
navigation, not a scraped/replayed HTTP request, so it behaves exactly like
a person clicking the button and doesn't trip the site's bot detection.
Progress percentages come from Chrome DevTools Protocol download events on
that same browser tab, so there's no second, separate request needed to
track progress.

Runs headless by default — no window pops up. Pass `--headful` to watch it
work in an actual browser window instead (needs a display, e.g. WSLg).

## How it works

- The queue file is a CSV with columns `title,url` — one Vimm's Lair vault
  URL per row (e.g. `https://vimm.net/vault/32`). Both `title` and the
  comma are optional: a bare `https://vimm.net/vault/32` line works fine.
  Anything missing a title gets looked up from the site itself and the
  file is rewritten in place with the discovered name, so future runs (and
  diffs of the file) show real titles instead of bare URLs.
- The full queue stays visible for the whole run, in order, updating in
  place — each row shows its position number, status, and (while
  downloading) bytes downloaded/total size, percentage, elapsed time, and
  an ETA.
- **The queue file can be edited live while a run is in progress.** A
  background watcher polls it for changes and reflects them immediately:
  add a row and it joins the queue, delete a row (before it's started
  downloading) and it drops out, reorder rows and the still-pending part
  of the queue reorders to match. See "Live editing" below for pause
  markers.
- Items are downloaded **sequentially**, using **one persistent browser
  session** for the whole run (matching Vimm's Lair's single-session
  policy), with a short randomized pause between items.
- If an item fails (network hiccup, page changed, temporarily blocked), it's
  retried a few times with backoff before being marked failed; the run
  continues on to the rest of the queue and prints a summary of any
  failures at the end.
- Re-running the tool on the same queue file skips anything already present
  in the output folder (tracked via a small manifest file), so it's safe to
  resume after an interruption.

## Setup (WSL / Linux)

### Quick start (any machine, any directory)

Clone the repo anywhere, then run the `vimm-queuer` launcher script by its
path. On first run it builds a private virtualenv next to the script, installs
the package, and downloads the Chromium build — after that it just runs:

```bash
/path/to/VimmDownloadQueuer/vimm-queuer queue.csv --out-dir downloads
```

The launcher finds its own location, so your current working directory doesn't
matter — the queue file and `--out-dir` are resolved relative to wherever you
run it from. Put the script on your `PATH` (e.g. symlink it into `~/.local/bin`)
to just type `vimm-queuer`. Set `PYTHON=python3.12` to pick a specific
interpreter for the venv.

The one-time system-library step still needs sudo:

```bash
sudo /path/to/VimmDownloadQueuer/.venv/bin/playwright install-deps
```

### Manual setup

If you'd rather manage the virtualenv yourself:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e .          # installs the `vimm-queuer` command

# Downloads a headless Chromium build Playwright manages itself
playwright install chromium

# Installs the Linux system libraries Chromium needs (sudo, one-time)
sudo .venv/bin/playwright install-deps
```

A virtualenv is tied to the absolute path it was created at — if you move or
re-clone the repo, delete `.venv` and let the launcher rebuild it (or re-run
`pip install -e .`).

`playwright install-deps` needs `sudo apt` access. If you don't have that,
ask whoever administers the box to run it once, or install the equivalent
packages manually — see [Playwright's system requirements](https://playwright.dev/python/docs/browsers#install-system-dependencies).

`--headful` needs a display — WSLg provides this out of the box on modern
WSL2/Ubuntu (`DISPLAY` and a working X11 socket are already set up; nothing
extra to install).

## Usage

```bash
source .venv/bin/activate  # if not already active

cp queue.example.csv queue.csv
# edit queue.csv with your real vault URLs, one per row - title can be left blank

vimm-queuer queue.csv --out-dir downloads
```

Options:

| Flag | Description |
| --- | --- |
| `-o, --out-dir DIR` | Where to save downloads (default: `./downloads`). Relative paths are fine — resolved to absolute before use |
| `--headful` | Show the actual browser window instead of headless (needs a display, e.g. WSLg) — useful for debugging |
| `--no-resume` | Ignore the manifest and re-download everything, even files already present |

### Queue file format

```csv
title,url
Adventure Island 4 (NES),https://vimm.net/vault/32
https://vimm.net/vault/3
```

Blank titles are filled in automatically (and saved back to this same file)
the next time you run the tool.

### Live editing

While a run is in progress, you can edit the queue file directly (in any
editor) and it takes effect within a couple of seconds, no restart needed:

- **Add** a row (bare URL or `title,url`) — it joins the end of the queue.
- **Delete** a row — if it hasn't started downloading yet, it drops out.
- **Reorder** rows — the still-pending part of the queue reorders to match.
- **Pause** — add a line whose url is the literal word `PAUSE` (any case),
  optionally with a label: `Taking a break,PAUSE`. The queue stops at that
  position and waits — whatever's already downloading finishes, but nothing
  past the pause line starts. Delete the line to let it continue.

### Display

The full queue stays on screen for the whole run, in original order,
updating in place: a position number, a status icon (⬇ downloading, ✓
completed, ✗ failed, • still queued, ⏸ pause marker), the title, and for
the item currently downloading — bytes downloaded/total size, percentage,
elapsed time, and an ETA, e.g. `420.0MB/1.4GB   28.0%  0:37  eta 1:35`.

## Notes / etiquette

This tool intentionally downloads one file at a time through a single
browser session, the same way a person using the site normally would — it
does not parallelize downloads, bypass Vimm's Lair's download limits, or
run through a proxy. Please keep it that way; Vimm's Lair is a
volunteer-run preservation archive and relies on ad revenue and reasonable
usage to stay online.

## Troubleshooting

- **`playwright install-deps` fails / no sudo** — see Setup above; install
  the listed system packages manually instead.
- **"You, or someone on your network, is currently downloading X"** — only
  one download is allowed at a time per network on Vimm's Lair. This
  includes your own manual browser downloads, not just other `vimm-queuer`
  runs. Wait for whatever's currently downloading to finish.
- **Downloads immediately fail with a blocked/bot-detection-looking error**
  — you're likely running many queue files back-to-back very quickly, or
  from a flagged IP/VPN. Wait a bit and try a smaller queue.
- **A title shows "not currently available in the vault"** — that upload is
  genuinely missing from the vault right now; there's nothing the tool can
  do about that specific title.
- **Anything else / digging deeper** — every run writes a detailed
  trace log (page navigation, every CDP download-progress event, console
  errors, network responses) to `/tmp/vimmqueuer_trace.log`, overwritten
  each run.
