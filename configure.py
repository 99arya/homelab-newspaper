"""Setup wizard: asks a few questions (Enter keeps the default), writes config.json,
builds the Docker image and schedules the paper with a systemd user timer.

    ./setup.sh             # interactive
    ./setup.sh --defaults  # accept every default (PDF only, no printer)
Run it again any time to change your answers; your current settings become the defaults.
"""
import getpass
import json
import os
import re
import shutil
import socket
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
CONFIG = HERE / "config.json"
UNITS = Path.home() / ".config/systemd/user"
DEFAULTS = "--defaults" in sys.argv
B, D, G, R, X = "\033[1m", "\033[2m", "\033[32m", "\033[31m", "\033[0m"
if not sys.stdout.isatty():
    B = D = G = R = X = ""


def ask(question, default="", check=None):
    while True:
        if DEFAULTS:
            return default
        hint = f" {D}[{default}]{X}" if default != "" else ""
        try:
            ans = input(f"{B}?{X} {question}{hint}: ").strip() or default
        except EOFError:
            ans = default
        if not check or check(ans):
            return ans
        print(f"  {R}That doesn't look right, try again.{X}")


def yes(question, default=True):
    ans = ask(question + " (y/n)", "y" if default else "n", lambda a: a.lower()[:1] in ("y", "n"))
    return ans.lower().startswith("y")


def step(title):
    print(f"\n{B}── {title} ──{X}")


def detect_timezone():
    try:
        tz = subprocess.run(["timedatectl", "show", "-p", "Timezone", "--value"],
                            capture_output=True, text=True, timeout=5).stdout.strip()
        if tz:
            return tz
    except (OSError, subprocess.SubprocessError):
        pass
    try:
        return Path("/etc/timezone").read_text().strip() or "UTC"
    except OSError:
        link = os.path.realpath("/etc/localtime")
        return link.split("zoneinfo/")[1] if "zoneinfo/" in link else "UTC"


def choose_sections(current):
    catalog = json.loads((HERE / "catalog.json").read_text())
    by_name = {c["section"]["name"]: i + 1 for i, c in enumerate(catalog)}
    print("Pick the sections you want, most important first (they're printed in that order).")
    for i, c in enumerate(catalog, 1):
        print(f"  {i:2}. {c['description']}")
    custom = [s for s in current if s["name"] not in by_name]  # sections you added by hand
    picked = [str(by_name[s["name"]]) for s in current if s["name"] in by_name]
    default = ",".join(picked) or "1,12,13,20"
    ans = ask("Section numbers, comma separated", default,
              lambda a: all(x.strip().isdigit() and 1 <= int(x) <= len(catalog) for x in a.split(",") if x.strip()))
    keep = {s["name"]: s for s in current}  # keep any feeds/keywords you edited
    chosen = []
    for x in ans.split(","):
        if x.strip():
            sec = catalog[int(x) - 1]["section"]
            chosen.append(keep.get(sec["name"], sec))
    if custom and yes(f"Keep your {len(custom)} custom section(s) ({', '.join(s['name'] for s in custom)})?"):
        chosen += custom
    return chosen


def choose_printer(cfg):
    sys.path.insert(0, str(HERE))
    import discover
    import ipp
    current = cfg.get("printer", "")
    print("Looking for printers on your network…")
    found = [] if DEFAULTS else discover.find_printers()
    for i, p in enumerate(found, 1):
        print(f"  {i}. {p['name']}  {D}{p['model']}  {p['uri']}{X}")
    print(f"  0. No printer — just save a PDF every day")
    print(f"  {D}…or type a printer address like ipp://192.168.1.50/ipp/print{X}")
    default = "0"
    if current:
        default = next((str(i) for i, p in enumerate(found, 1) if p["uri"] == current), current)
    elif found:
        default = "1"
    ans = ask("Printer", default)
    if ans == "0" or not ans:
        return "", "", False
    if ans.isdigit() and 1 <= int(ans) <= len(found):
        p = found[int(ans) - 1]
        uri, ip = p["uri"], p["ip"]
    else:
        uri = ans if "://" in ans else f"ipp://{ans}/ipp/print"
        host = uri.split("//", 1)[1].split("/", 1)[0].split(":")[0]
        try:
            ip = socket.gethostbyname(host)
        except OSError:
            ip = cfg.get("printer_fallback_ip", "")
    try:
        test = uri if not ip else uri.replace(uri.split("//", 1)[1].split("/", 1)[0].split(":")[0], ip, 1)
        info = ipp.printer_info(test)
        fmts = info.get("document-format-supported", [])
        model = (info.get("printer-make-and-model") or ["printer"])[0]
        ok = "application/pdf" in fmts or "image/pwg-raster" in fmts
        print(f"  {G if ok else R}{model}: {'ready' if ok else 'supports neither PDF nor PWG raster, printing may fail'}{X}")
        duplex = "two-sided-long-edge" in info.get("sides-supported", [])
    except Exception as e:
        print(f"  {R}Couldn't reach that printer right now ({e}). Saving it anyway.{X}")
        duplex = True
    return uri, ip, duplex


def choose_weather(current):
    """City name → coordinates with Open-Meteo's free geocoder."""
    import urllib.parse
    import urllib.request
    name = ask("City for the weather box (or 'none')", current.get("name", "none") if current else "none")
    if name.lower() == "none":
        return {}
    if current and name == current.get("name"):
        return current
    try:
        url = "https://geocoding-api.open-meteo.com/v1/search?count=1&name=" + urllib.parse.quote(name)
        hit = json.loads(urllib.request.urlopen(url, timeout=15).read())["results"][0]
    except Exception:
        print(f"  {R}Couldn't find '{name}'. Skipping the weather box; run setup again to retry.{X}")
        return {}
    print(f"  {G}{hit['name']}, {hit.get('admin1', '')} {hit.get('country', '')}{X}")
    units = "fahrenheit" if hit.get("country_code") in ("US", "LR", "MM") else "celsius"
    return {"name": hit["name"], "lat": round(hit["latitude"], 3), "lon": round(hit["longitude"], 3), "units": units}


def geocode(place):
    import urllib.parse
    import urllib.request
    url = "https://nominatim.openstreetmap.org/search?format=json&limit=1&q=" + urllib.parse.quote(place)
    req = urllib.request.Request(url, headers={"User-Agent": "homelab-newspaper-setup/1.0"})
    hit = json.loads(urllib.request.urlopen(req, timeout=20).read())[0]
    return [round(float(hit["lat"]), 4), round(float(hit["lon"]), 4)], hit["display_name"]


def choose_commute(current):
    current = current or {}
    start = ask("Commute: where do you start (address or area, or 'none')", current.get("from", "none"))
    if start.lower() == "none":
        return {}
    end = ask("Commute: where to (address)", current.get("to", ""))
    if current.get("from") == start and current.get("to") == end:
        return current
    try:
        a, an = geocode(start)
        b, bn = geocode(end)
    except Exception:
        print(f"  {R}Couldn't find one of those places. Skipping the commute box.{X}")
        return {}
    print(f"  {G}{an[:70]}\n  → {bn[:70]}{X}")
    label = ask("Short label for the box", f"{start.split(',')[0]} → {end.split(',')[0]}")
    key = ask("TomTom API key for live traffic (free at developer.tomtom.com, or 'none')",
              current.get("tomtom_key") or "none")
    return {"label": label, "from": start, "to": end, "from_coords": a, "to_coords": b,
            "tomtom_key": "" if key.lower() == "none" else key}


def write_units(cfg):
    UNITS.mkdir(parents=True, exist_ok=True)
    name = cfg["paper_name"]
    (UNITS / "homelab-newspaper.service").write_text(f"""[Unit]
Description={name} - daily personal newspaper
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
ExecStart={HERE}/run.sh
""")
    (UNITS / "homelab-newspaper.timer").write_text(f"""[Unit]
Description=Make {name} every day at {cfg['print_time']} ({cfg['timezone']})

[Timer]
OnCalendar=*-*-* {cfg['print_time']}:00 {cfg['timezone']}
Persistent=true

[Install]
WantedBy=timers.target
""")
    run = lambda *a: subprocess.run(["systemctl", "--user", *a], capture_output=True, text=True)
    run("daemon-reload")
    r = run("enable", "--now", "homelab-newspaper.timer")
    if r.returncode:
        print(f"  {R}Couldn't enable the timer: {r.stderr.strip()}{X}")
        return
    print(f"  {G}Scheduled daily at {cfg['print_time']} {cfg['timezone']}.{X}")
    linger = subprocess.run(["loginctl", "show-user", getpass.getuser(), "-p", "Linger", "--value"],
                            capture_output=True, text=True).stdout.strip()
    if linger != "yes":
        r = subprocess.run(["loginctl", "enable-linger"], capture_output=True, text=True)
        if r.returncode:
            print(f"  {R}So it runs while you're logged out, run once: sudo loginctl enable-linger {getpass.getuser()}{X}")


def main():
    if CONFIG.exists():
        cfg = json.loads(CONFIG.read_text())
        print(f"Updating your existing settings in {CONFIG.name}. Press Enter to keep each one.")
    else:
        cfg = json.loads((HERE / "config.example.json").read_text())
        cfg["paper_name"] = f"The {getpass.getuser().split('.')[0].title()} Times"
        cfg["timezone"] = detect_timezone()
        print(f"{B}Welcome! Let's set up your daily newspaper.{X} Press Enter to accept the [default].")

    step("Your paper")
    cfg["paper_name"] = ask("Name of your paper", cfg["paper_name"])
    cfg["tagline"] = ask("Tagline under the name", cfg.get("tagline", ""))

    step("Sections")
    cfg["sections"] = choose_sections(cfg.get("sections", []))
    boost = ask("Topics you care about most, boosted everywhere (comma separated, or 'none')",
                ", ".join(cfg.get("boost_keywords", [])) or "none")
    cfg["boost_keywords"] = [] if boost.lower() == "none" else [k.strip() for k in boost.split(",") if k.strip()]

    step("Extras")
    cfg["quote_of_the_day"] = yes("Quote of the day?", cfg.get("quote_of_the_day", True))
    cfg["tip_of_the_day"] = yes("Tip of the day?", cfg.get("tip_of_the_day", True))
    if cfg["tip_of_the_day"]:
        mix = {"both": ["tech", "life"], "tech": ["tech"], "life": ["life"]}
        now = next((k for k, v in mix.items() if v == cfg.get("tip_kinds", ["tech", "life"])), "both")
        cfg["tip_kinds"] = mix[ask("Tips about tech (coding, AI), life (habits, money, health), or both",
                                   now, lambda a: a in mix)]
    cfg["weather"] = choose_weather(cfg.get("weather"))
    cfg["commute"] = choose_commute(cfg.get("commute"))
    fx = cfg.get("exchange_rate") or {}
    pair = ask("Exchange rate to show, like CAD-INR or USD-EUR (or 'none')",
               f"{fx['from']}-{fx['to']}" if fx else "none",
               lambda a: a.lower() == "none" or re.fullmatch(r"[A-Za-z]{3}-[A-Za-z]{3}", a) is not None)
    cfg["exchange_rate"] = {} if pair.lower() == "none" else dict(zip(("from", "to"), pair.upper().split("-")))
    cfg["express_entry"] = yes("Canadian Express Entry draw results (for people immigrating to Canada)?",
                               cfg.get("express_entry", False))

    step("Printer")
    uri, ip, duplex = choose_printer(cfg)
    cfg["printer"], cfg["printer_fallback_ip"], cfg["print"] = uri, ip, bool(uri)
    if uri:
        cfg["two_sided"] = yes("Print on both sides of the paper?", cfg.get("two_sided", duplex) and duplex)

    step("Schedule")
    cfg["timezone"] = ask("Time zone", cfg.get("timezone") or detect_timezone(),
                          lambda a: Path("/usr/share/zoneinfo", a).exists() or a == "UTC")
    cfg["print_time"] = ask("Time to make it every day (24h HH:MM)", cfg.get("print_time", "07:00"),
                            lambda a: re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", a) is not None)

    step("Monitoring (optional)")
    print(f"  {D}Paste an Uptime Kuma push URL or a healthchecks.io URL to be alerted if a paper fails.{X}")
    hc = ask("Health check URL (or 'none')", cfg.get("healthcheck_url") or "none")
    cfg["healthcheck_url"] = "" if hc.lower() == "none" else hc

    CONFIG.write_text(json.dumps(cfg, indent=2, ensure_ascii=False) + "\n")
    print(f"\n{G}Saved {CONFIG.name}.{X} Edit it any time for finer control (feeds, keywords, story counts).")

    step("Building")
    if subprocess.run(["docker", "build", "-q", "-t", "homelab-newspaper", str(HERE)],
                      stdout=subprocess.DEVNULL).returncode:
        sys.exit(f"{R}Docker build failed.{X}")
    print(f"  {G}Docker image ready.{X}")
    if shutil.which("systemctl") and yes("Schedule it to run every day?", True):
        write_units(cfg)

    if yes("Make a preview PDF now (no printing)?", not DEFAULTS):
        subprocess.run([str(HERE / "run.sh"), "--no-print"])
        print(f"  Preview saved to {HERE / 'editions/preview.pdf'}")
    if cfg["print"] and yes("Print a test copy now?", False):
        subprocess.run([str(HERE / "run.sh")])
    print(f"\n{B}All set.{X} Make one now with ./run.sh (prints) or ./run.sh --no-print (PDF only).")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nCancelled; nothing else was changed.")
