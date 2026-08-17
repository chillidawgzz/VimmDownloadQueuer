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
  URL per row (e.g. `https://vimm.net/vault/32`). `title` can be left blank;
  before a run starts, anything missing a title gets looked up from the
  site itself and the file is rewritten in place with the discovered names,
  so future runs (and diffs of the file) show real titles instead of bare
  URLs.
- The currently-downloading item's title is shown live in the terminal
  alongside its progress bar.
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

Use a virtualenv so nothing gets installed globally:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
pip install -e .          # installs the `vimm-queuer` command

# Downloads a headless Chromium build Playwright manages itself
playwright install chromium

# Installs the Linux system libraries Chromium needs (sudo, one-time)
sudo .venv/bin/playwright install-deps
```

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
,https://vimm.net/vault/3
```

Blank titles are filled in automatically (and saved back to this same file)
the next time you run the tool.

### Display

While running, the terminal shows a permanent line for each item as it
finishes (✓ completed, ✗ failed with the reason, or ✓ skipped if already
downloaded) — labeled by title — plus a single in-place progress line
(updated via a plain carriage return, no fancy terminal takeover) for
whatever's currently downloading, with its title and percentage.

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
