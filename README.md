# homelab-newspaper

Your own printed morning newspaper. Every day it pulls news from the sections you pick, lays it out as a classic three-column broadsheet, and sends it straight to your network printer. It runs in Docker on your homelab. There's no AI, no API keys and no subscriptions: it just reads public RSS feeds.

<img src="docs/sample.png" width="520" alt="Sample front page">

## Features

- **Pick your sections**: 24 ready-made ones (World, US, Canada, UK, India, Tech, AI for developers, Frontend, Security, Self-hosting, Science, Sports and more), in the order you choose. You can also add any RSS feed.
- **Never the same story twice**: anything printed in the last 7 days is skipped, even when another outlet rewords it.
- **Fills every page**: pages are packed top to bottom, and leftover space gets extra stories from your top sections.
- **A QR code on every story**: scan it with your phone to read the full article.
- **Quote of the day and developer tip of the day**: neither repeats.
- **Prints with no drivers**: it talks to IPP Everywhere / AirPrint printers directly. That covers most network printers made in the last ten years, including ones that only accept raster images. No CUPS needed.
- **Or no printer at all**: you still get a dated PDF every morning in `editions/`.

## Install

You need a Linux machine that stays on (a homelab server, NAS or Raspberry Pi 4 or newer) with Docker and Python 3.

```bash
git clone https://github.com/99arya/homelab-newspaper.git
cd homelab-newspaper
./setup.sh
```

The wizard asks a few questions; press Enter to accept the default for each:

1. The name of your paper and its tagline
2. Your sections, most important first
3. Topics to boost (for example `AI, Toronto, React`)
4. Quote and tip of the day on or off
5. Your printer: it finds printers on your network automatically, or choose "No printer" for PDF only
6. Time zone and the time to make it each day

It then builds the Docker image, schedules the paper with a systemd user timer, and offers to make a preview. Run `./setup.sh` again any time to change your answers.

## Everyday use

```bash
./run.sh              # make today's paper and print it
./run.sh --no-print   # preview only: editions/preview.pdf
systemctl --user list-timers homelab-newspaper.timer   # when it runs next
journalctl --user -u homelab-newspaper -n 30           # logs from the last run
```

## Customizing

Everything lives in `config.json`, which the wizard writes. You can edit it directly; no rebuild is needed.

| Setting | What it does |
|---|---|
| `sections[]` | `name`, `stories` (how many), `feeds` (RSS/Atom URLs). Optional: `max_age_hours`, `boost_keywords`, `exclude_keywords`, `require_keywords` (a story must mention one of these) and `summary_words` |
| `boost_keywords` / `exclude_keywords` | Rank stories up or drop them, across every section |
| `no_repeat_days` | How long a printed story stays blocked (default 7) |
| `two_sided` | Double-sided printing (the paper fills the back of the last sheet) |
| `max_age_hours`, `summary_words`, `keep_editions` | Freshness, summary length, and how many daily PDFs to keep |
| `print_time`, `timezone` | Re-run `./setup.sh` after changing these, so the timer is updated |

Want a section that isn't in the list? Add any RSS feed to a section in `config.json`. Many sites have one at `/feed`, `/rss` or `/rss.xml`. Avoid Google News feeds: their links are very long, so their QR codes come out huge.

To add your own tips, edit `tips.json`.

## How it works

`newsbot.py` fetches the feeds in parallel and ranks stories by freshness and your keywords. It removes duplicates across outlets and across previous days, then builds an HTML page and renders it to PDF with WeasyPrint. It renders the paper more than once, adding spare stories until the last page is full. `ipp.py` is a small, dependency-free IPP client. `raster.py` converts the PDF to PWG raster for printers that can't print PDF. `discover.py` finds printers with mDNS.

## Troubleshooting

- **Printer not found during setup**: type its address instead, for example `ipp://192.168.1.50/ipp/print`. Check that it's on and on the same network. You can test it with `python3 ipp.py info ipp://<address>/ipp/print`.
- **Nothing printed at the scheduled time**: check `journalctl --user -u homelab-newspaper`. If it only runs while you're logged in, run `sudo loginctl enable-linger $USER` once.
- **A feed fails** (timeouts, 429 or 404 errors): one broken feed never stops the paper; it's skipped and logged. Replace it in `config.json` if it keeps failing.

## License

MIT
