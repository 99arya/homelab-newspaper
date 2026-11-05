# Instructions for AI assistants

A user has asked you to set up this newspaper bot for them. Follow these steps.

## 1. Check requirements
- Linux with Docker (`docker info` works without sudo) and `python3`.
- If Docker needs sudo, tell the user to run `sudo usermod -aG docker $USER` and log in again. Don't do it for them.

## 2. Ask the user (offer these defaults)
- Paper name (default: "The <their first name> Times") and tagline.
- Sections, most important first. Pick from `catalog.json` (`id` and `description`). Default: world, tech, ai-dev, science.
- Topics to boost (`boost_keywords`), for example their city, employer's tech stack or hobbies.
- City for the weather box.
- Tip mix: `["tech", "life"]` (default), `["tech"]` or `["life"]`.
- Printer: run `python3 discover.py` and let them choose, or use none (PDF only).
- Time zone and daily time (default 07:00).

## 3. Write config.json
- Copy `config.example.json` to `config.json`.
- Set `sections` to the chosen catalog entries' `section` objects, in the user's order.
- Set `printer` (IPP URI from discover.py), `printer_fallback_ip` (its IP) and `print: true`. Use `print: false` with no printer.
- Set `weather` to `{"name", "lat", "lon", "units"}`. Look up coordinates with `https://geocoding-api.open-meteo.com/v1/search?count=1&name=<city>`.
- Set `timezone` (IANA, for example `America/Toronto`) and `print_time` ("HH:MM").

## 4. Install
```bash
./setup.sh --defaults   # keeps config.json as written: builds the image and schedules the daily run
./run.sh --no-print     # makes editions/preview.pdf; show it to the user
```

## Rules
- `./run.sh` without `--no-print` PRINTS PAPER. Only run it when the user asks for a test print.
- Never commit `config.json`, `history.json`, `used.json` or `editions/`; they're the user's personal data.
- To check the printer: `python3 ipp.py info <ipp-uri>`. It needs `image/pwg-raster` or `application/pdf` in `document-format-supported`.
