#!/usr/bin/env python3
"""Personal newspaper bot.

Pulls headlines from RSS/Atom feeds, ranks them by your interests,
lays them out as a newspaper page, saves a PDF, and (optionally) prints it.

Usage:
    python3 newsbot.py            # make today's edition (prints if config says so)
    python3 newsbot.py --no-print # make the PDF only
Edit config.json to change topics, feeds, keywords, and the paper's name.
"""
import concurrent.futures as cf
import datetime as dt
import email.utils
import html
import json
import re
import subprocess
import sys
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

HERE = Path(__file__).resolve().parent
EDITIONS = HERE / "editions"
CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
UA = "Mozilla/5.0 (Macintosh) PersonalNewsBot/1.0"


def log(msg):
    print(f"[{dt.datetime.now():%H:%M:%S}] {msg}", flush=True)


# ---------- fetching & parsing ----------

def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=20) as r:
        return r.read()


def local(tag):
    return tag.rsplit("}", 1)[-1].lower()


def child_text(el, *names):
    for c in el:
        if local(c.tag) in names:
            if (c.text or "").strip():
                return c.text.strip()
            if local(c.tag) == "link" and c.get("href"):
                return c.get("href")
    return ""


def atom_link(el):
    for c in el:
        if local(c.tag) == "link" and c.get("rel", "alternate") == "alternate" and c.get("href"):
            return c.get("href")
    return child_text(el, "link")


def parse_date(s):
    if not s:
        return None
    try:
        d = email.utils.parsedate_to_datetime(s)
    except (TypeError, ValueError):
        try:
            d = dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
        except ValueError:
            return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=dt.timezone.utc)
    return d


def clean(text, words=None):
    text = html.unescape(text or "") if "&lt;" in (text or "") else (text or "")
    text = re.sub(r"(?is)<(ul|ol|aside|figure|script|style)\b.*?</\1>", " ", text)  # promo link lists, images
    text = re.sub(r"(?i)([^.!?:;”\"'\s])\s*</p>", r"\1. ", text)  # end paragraphs with a period
    text = html.unescape(re.sub(r"<[^>]+>", " ", text))
    text = re.sub(r"(?i)(Get our [^.]*?(email|newsletter|podcast)[^.]*?\.?|Sign up for [^.]*?\.|Send us your questions[^.]*)", " ", text)
    text = re.sub(r"\s+", " ", text).strip().lstrip(".,;:-–— ")
    text = re.sub(r"(The post .* appeared first on .*|Continue reading.*|Read more.*)$", "", text).strip()
    m = re.match(r'(?is)description:\s*"?(.*?)"?\s+(tags|cover_image|canonical_url|published|series):', text)
    if m:  # dev.to posts sometimes leak their front matter
        text = m.group(1)
    return whole_sentences(text, words) if words else text


ABBREV = re.compile(r"(?:^|\s)(?:[A-Z]\.){1,3}$|\b(?:Mr|Mrs|Ms|Dr|St|Jr|Sr|No|vs|Inc|Ltd|Co|Gov|Sen|Rep|Gen|Lt|Col|Capt|Sgt|Cpl|Const|Supt|Insp|Det|Cmdr|Adm|Hon|Mt|Ft|Prof|"
                    r"Jan|Feb|Mar|Apr|Aug|Sept?|Oct|Nov|Dec|approx|e\.g|i\.e)\.$")


def sentences(text):
    """Split into sentences, without breaking at U.S., Dr., Oct. and similar."""
    out, start = [], 0
    for m in re.finditer(r"[.!?][”\"’')\]]*\s+(?=[A-Z0-9“\"‘'(])", text):
        if ABBREV.search(text[start:m.start() + 1]):
            continue
        out.append(text[start:m.end()].strip())
        start = m.end()
    out.append(text[start:].strip())
    return [s for s in out if s]


def whole_sentences(text, words):
    """About `words` long, but only complete sentences: never a sentence cut off with '…'."""
    cut = re.search(r"\s*(\[\s*(…|\.\.\.)\s*\]|…|\.{3})\s*$", text)  # feed's own truncation mark
    if cut:
        text = text[:cut.start()]
    done = sentences(text)
    if done and not re.search(r"[.!?][”\"’')\]]*$", done[-1]):
        tail = done.pop()  # no final period: a one-line deck (keep it) or a cut-off sentence (drop it)
        if not cut and not re.search(r"(?i)\b(a|an|the|of|to|in|on|at|for|and|or|but|with|by|from|as|is|was|"
                                     r"are|that|which|who|its|their|his|her)$", tail.rstrip(",;:—– ")):
            done.append(tail.rstrip(",;:—– ") + ".")
    picked, count = [], 0
    for s in done:
        n = len(s.split())
        if picked and count + n > words * 1.35:
            break
        if not picked and n > words * 2.2:
            return ""  # one huge sentence: the headline alone reads better than half of it
        picked.append(s)
        count += n
    return " ".join(picked)


def source_name(url):
    host = re.sub(r"^(www|feeds|rss)\.", "", urllib.request.urlparse(url).netloc)
    names = {"theverge.com": "The Verge", "arstechnica.com": "Ars Technica",
             "techcrunch.com": "TechCrunch", "technologyreview.com": "MIT Tech Review",
             "wired.com": "Wired", "bbci.co.uk": "BBC", "npr.org": "NPR",
             "theguardian.com": "The Guardian", "aljazeera.com": "Al Jazeera",
             "globalnews.ca": "Global News", "thestar.com": "Toronto Star", "insauga.com": "insauga",
             "cicnews.com": "CIC News", "api.io.canada.ca": "IRCC", "thehindu.com": "The Hindu",
             "indianexpress.com": "Indian Express", "feedburner.com": "NDTV",
             "timesofindia.indiatimes.com": "Times of India", "hindustantimes.com": "Hindustan Times",
             "simonwillison.net": "Simon Willison", "openai.com": "OpenAI", "nextjs.org": "Next.js",
             "vercel.com": "Vercel", "smashingmagazine.com": "Smashing Mag", "dev.to": "DEV",
             "medium.com": "Medium", "blog.google": "Google", "github.com": "GitHub",
             "sciencedaily.com": "ScienceDaily", "news.mit.edu": "MIT News",
             "nytimes.com": "New York Times", "abc.net.au": "ABC News", "krebsonsecurity.com": "Krebs",
             "bleepingcomputer.com": "BleepingComputer", "lobste.rs": "Lobsters", "nasa.gov": "NASA",
             "space.com": "Space.com", "espn.com": "ESPN", "selfh.st": "selfh.st", "euronews.com": "Euronews"}
    if host == "feedburner.com" and "TheHackersNews" in url:
        return "The Hacker News"
    return names.get(host, host)


def parse_feed(url):
    root = ET.fromstring(fetch(url))
    items = [e for e in root.iter() if local(e.tag) in ("item", "entry")]
    gnews, hn = "news.google.com" in url, "hnrss.org" in url
    out = []
    for it in items:
        title = clean(child_text(it, "title"))
        if not title:
            continue
        is_atom = local(it.tag) == "entry"
        link = atom_link(it) if is_atom else child_text(it, "link")
        summary = clean(child_text(it, "description", "summary", "encoded", "content"))
        date = parse_date(child_text(it, "pubdate", "published", "updated", "date"))
        source = source_name(url)
        if gnews:  # "Headline - Publisher"; the description just repeats the headline
            if " - " in title:
                title, _, pub = title.rpartition(" - ")
                source = pub.strip() or "Google News"
            summary = ""
        elif hn:  # Hacker News: description is just links and points
            source, summary = source_name(link), ""
        out.append({"title": title, "link": link, "summary": summary, "date": date, "source": source})
    return out


USED = HERE / "used.json"  # quotes and tips already printed: {"quotes": [...], "tips": [...]}


def load_used():
    try:
        u = json.loads(USED.read_text())
    except (OSError, ValueError):
        u = {}
    return {"quotes": u.get("quotes", []), "tips": u.get("tips", [])}


def quote_of_the_day(used):
    """A quote never printed before: picked from a batch of 50 random ones."""
    for api in ("https://zenquotes.io/api/quotes", "https://zenquotes.io/api/today"):
        try:
            for q in json.loads(fetch(api)):
                text = (q.get("q") or "").strip()
                if (text and "too many requests" not in text.lower() and len(text) <= 180
                        and text not in used["quotes"]):
                    return text, q.get("a", "").strip()
        except Exception as e:
            log(f"  quote source failed: {e}")
    return None


def tip_of_the_day(used, kinds=("tech", "life")):
    """Shuffled: every tip once before any repeats, alternating tech and life tips,
    never the same topic twice running, and seasonal tips only in their months."""
    import random
    try:
        tips = json.loads((HERE / "tips.json").read_text())
    except (OSError, ValueError):
        return None
    today = dt.date.today()
    tips = [x for x in tips if x.get("kind", "tech") in kinds
            and today.month in x.get("months", range(1, 13))]
    if not tips:
        return None
    last = next((x for x in tips if used["tips"] and x["title"] == used["tips"][-1]), {})
    fresh = [x for x in tips if x["title"] not in used["tips"]] or tips  # all used: start a new round
    for want in ([x for x in fresh if x.get("kind") != last.get("kind") and x["topic"] != last.get("topic")],
                 [x for x in fresh if x["topic"] != last.get("topic")], fresh):
        if want:
            seasonal = [x for x in want if "months" in x]  # in season now: show those first
            return random.Random(today.toordinal()).choice(seasonal or want)


WEATHER = {0: "Clear", 1: "Mostly clear", 2: "Partly cloudy", 3: "Cloudy", 45: "Fog", 48: "Freezing fog",
           51: "Light drizzle", 53: "Drizzle", 55: "Heavy drizzle", 56: "Freezing drizzle", 57: "Freezing drizzle",
           61: "Light rain", 63: "Rain", 65: "Heavy rain", 66: "Freezing rain", 67: "Freezing rain",
           71: "Light snow", 73: "Snow", 75: "Heavy snow", 77: "Snow grains", 80: "Showers", 81: "Showers",
           82: "Heavy showers", 85: "Snow showers", 86: "Heavy snow showers", 95: "Thunderstorms",
           96: "Thunderstorms with hail", 99: "Thunderstorms with hail"}


def weather_today(place):
    """Today's forecast, sunrise and sunset from Open-Meteo (free, no key)."""
    if not place or "lat" not in place:
        return None
    unit = place.get("units", "celsius")
    url = ("https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}&timezone=auto&forecast_days=1"
           "&daily=weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max,sunrise,sunset"
           f"&temperature_unit={unit}").format(**place)
    try:
        d = json.loads(fetch(url))["daily"]
        t = lambda s: dt.datetime.fromisoformat(s).strftime("%-I:%M %p")
        return {"place": place.get("name", ""), "sky": WEATHER.get(d["weather_code"][0], ""),
                "hi": round(d["temperature_2m_max"][0]), "lo": round(d["temperature_2m_min"][0]),
                "rain": d["precipitation_probability_max"][0], "sunrise": t(d["sunrise"][0]),
                "sunset": t(d["sunset"][0])}
    except Exception as e:
        log(f"  weather skipped: {e}")
        return None


def commute_today(c):
    """Drive time between two places. Live traffic with a TomTom key, otherwise clear-road time (OSRM)."""
    if not c or "from_coords" not in c:
        return None
    (alat, alon), (blat, blon) = c["from_coords"], c["to_coords"]
    try:
        if c.get("tomtom_key"):
            url = (f"https://api.tomtom.com/routing/1/calculateRoute/{alat},{alon}:{blat},{blon}/json"
                   f"?traffic=true&departAt=now&key={urllib.parse.quote(c['tomtom_key'])}")
            s = json.loads(fetch(url))["routes"][0]["summary"]
            mins, km, live = s["travelTimeInSeconds"] / 60, s["lengthInMeters"] / 1000, True
            delay = s.get("trafficDelayInSeconds", 0) / 60
        else:
            url = f"https://router.project-osrm.org/route/v1/driving/{alon},{alat};{blon},{blat}?overview=false"
            r = json.loads(fetch(url))["routes"][0]
            mins, km, live, delay = r["duration"] / 60, r["distance"] / 1000, False, 0
        return {"label": c.get("label", ""), "mins": round(mins), "km": round(km), "live": live, "delay": round(delay)}
    except Exception as e:
        log(f"  commute skipped: {e}")
        return None


CURRENCY = {"INR": "₹", "USD": "US$", "EUR": "€", "GBP": "£", "CAD": "C$", "AUD": "A$", "JPY": "¥", "CNY": "¥",
            "PHP": "₱", "PKR": "Rs ", "NGN": "₦", "MXN": "MX$", "AED": "AED "}


def fx_today(fx):
    """Exchange rate and its change over a week, from Frankfurter (European Central Bank rates, free)."""
    if not fx:
        return None
    base, quote = fx.get("from", "CAD"), fx.get("to", "INR")
    try:
        now = json.loads(fetch(f"https://api.frankfurter.dev/v1/latest?from={base}&to={quote}"))
        week = (dt.date.fromisoformat(now["date"]) - dt.timedelta(days=7)).isoformat()
        then = json.loads(fetch(f"https://api.frankfurter.dev/v1/{week}?from={base}&to={quote}"))
        rate, old = now["rates"][quote], then["rates"][quote]
        return {"base": base, "quote": quote, "rate": rate, "change": rate - old, "date": now["date"],
                "sym": CURRENCY.get(quote, quote + " ")}
    except Exception as e:
        log(f"  exchange rate skipped: {e}")
        return None


def express_entry_today():
    """Latest Express Entry draw, and the latest CRS cutoff, from CIC News's Express Entry feed."""
    try:
        items = parse_feed("https://www.cicnews.com/category/express-entry/feed")
    except Exception as e:
        log(f"  Express Entry skipped: {e}")
        return None
    draws = []
    for s in items:
        text = clean(s["summary"])
        m = re.search(r"issued ([\d,]+) (?:ITAs|invitations)[^.]*? draw for ([^.]+)\.", text)
        if not m:
            continue
        what = re.sub(r"^(?:Express Entry )?candidates (?:eligible under|with|in) (?:the )?", "", m.group(2))
        crs = re.search(r"CRS\)? score of (\d{3})", text)
        draws.append({"date": s["date"], "itas": m.group(1), "what": what.strip(), "crs": crs and crs.group(1)})
    if not draws:
        return None
    with_crs = next((d for d in draws if d["crs"]), None)
    return {"latest": draws[0], "crs": with_crs}


def save_used(used, quote, tip):
    if quote:
        used["quotes"] = (used["quotes"] + [quote[0]])[-1000:]
    if tip:
        used["tips"] = [t for t in used["tips"] if t != tip["title"]] + [tip["title"]]
    USED.write_text(json.dumps(used, indent=0, ensure_ascii=False))


# ---------- ranking ----------

def norm(title):
    return set(w for w in re.findall(r"[a-z0-9]+", title.lower()) if len(w) > 3)


def has_kw(text, kw):
    return re.search(r"\b" + re.escape(kw.lower()) + r"\b", text.lower()) is not None


HISTORY = HERE / "history.json"


def load_history(days):
    """Links and titles of stories printed in the previous `days` days (not today's)."""
    try:
        items = json.loads(HISTORY.read_text())
    except (OSError, ValueError):
        items = []
    today = dt.date.today()
    recent = [i for i in items if 0 < (today - dt.date.fromisoformat(i["date"])).days <= days]
    return {tidy_url(i["link"]) for i in recent if i.get("link")}, [i["title"] for i in recent]


def save_history(sections, keep_days=30):
    """Remember what was just printed so later editions don't repeat it."""
    try:
        items = json.loads(HISTORY.read_text())
    except (OSError, ValueError):
        items = []
    today = dt.date.today()
    items = [i for i in items if i["date"] != today.isoformat()
             and (today - dt.date.fromisoformat(i["date"])).days <= keep_days]
    items += [{"date": today.isoformat(), "link": s["link"], "title": s["title"]}
              for _, stories in sections for s in stories]
    HISTORY.write_text(json.dumps(items, indent=0, ensure_ascii=False))


def same_story(words, others):
    """Loose match, for the same news covered by different outlets on the same day."""
    return any(len(words & w) >= max(3, len(words) // 2) for w in others)


def already_printed(s, history):
    """Strict match against earlier editions: the same link or an almost identical headline.
    Follow-ups (new findings, court proceedings) and series parts have new headlines, so they get through."""
    if tidy_url(s["link"]) in history[0]:
        return True
    words, digits = norm(s["title"]), set(re.findall(r"\d+", s["title"]))
    for old in history[1]:
        w = norm(old)
        if words and w and len(words & w) / len(words | w) >= 0.7 and digits == set(re.findall(r"\d+", old)):
            return True
    return False


def fetch_all(sections):
    """Every feed of every section, fetched in parallel: {url: [stories]}."""
    urls = list(dict.fromkeys(u for sec in sections for u in sec["feeds"]))
    out = {}
    with cf.ThreadPoolExecutor(12) as pool:
        futures = {pool.submit(parse_feed, u): u for u in urls}
        for f in cf.as_completed(futures):
            try:
                out[futures[f]] = f.result()
            except Exception as e:  # one broken feed shouldn't kill the paper
                log(f"  skipped {futures[f]}: {e}")
                out[futures[f]] = []
    return out


def coverage_counter(all_stories):
    """How many other outlets carry the same story: a big story is everywhere."""
    index = [(norm(s["title"]), s["source"]) for s in all_stories]

    def count(s):
        words = norm(s["title"])
        return len({src for w, src in index if src != s["source"] and same_story(words, [w])})
    return count


def order_by_importance(sections, spares):
    """Today's biggest news first. Your section order still counts: the first section gets
    a 60% head start over the last one, so a quiet day keeps your usual order."""
    n = len(sections)
    rank = []
    for i, ((name, stories), spare) in enumerate(zip(sections, spares)):
        top = sorted((s["importance"] for s in stories), reverse=True)
        base = top[0] + 0.5 * sum(top[1:3]) / max(1, len(top[1:3])) if top else -1
        rank.append((base * (1 + 0.6 * (n - 1 - i) / max(1, n - 1)), i))
    rank.sort(reverse=True)
    sections = [sections[i] for _, i in rank]
    if sections and sections[0][1]:  # the top story is the most important one, not just the first
        top = sections[0][1]
        top.insert(0, top.pop(max(range(len(top)), key=lambda k: top[k]["importance"])))
    return sections, [spares[i] for _, i in rank]


def pick_stories(cfg, section, stories, seen, history=(set(), []), coverage=lambda s: 0):
    now = dt.datetime.now(dt.timezone.utc)
    window = section.get("max_age_hours", cfg.get("max_age_hours", 36))
    cutoff = now - dt.timedelta(hours=window)
    boost = cfg.get("boost_keywords", []) + section.get("boost_keywords", [])
    exclude = cfg.get("exclude_keywords", []) + section.get("exclude_keywords", [])

    outlets = len({s["source"] for s in stories})  # sections with few outlets can't show wide coverage
    ranked = []
    for s in stories:
        text = s["title"] + " " + s["summary"]
        if s["date"] and s["date"] < cutoff:
            continue
        if any(has_kw(text, k) for k in exclude):
            continue
        need = section.get("require_keywords")
        if need and not any(has_kw(text, k) for k in need):
            continue  # off-topic for this section
        age_h = (now - s["date"]).total_seconds() / 3600 if s["date"] else window / 2
        score = 10 - 9 * min(age_h, window) / window  # newer is better
        score += 4 * sum(has_kw(s["title"], k) for k in boost)
        score += 1.5 * sum(has_kw(s["summary"], k) for k in boost)
        score += 1 if len(s["summary"]) > 120 else 0
        cov = coverage(s)
        score += min(3 * cov, 9)  # covered by several outlets = important today
        reach = min(1.0, cov / max(1, min(3, outlets - 1)))  # share of the outlets that could cover it
        s["importance"] = 10 - 9 * min(age_h, window) / window + 10 * reach  # keyword boosts don't count here
        ranked.append((score, s))
    ranked.sort(key=lambda x: x[0], reverse=True)

    chosen, reserve, per_source = [], [], {}
    limit = section.get("stories", 8)
    for _, s in ranked:
        words = norm(s["title"])
        if same_story(words, seen):
            continue  # same story from another outlet
        if already_printed(s, history):
            continue  # printed in a recent edition (follow-ups are allowed)
        if any(same_story(words, [norm(r["title"])]) for r in reserve):
            continue
        s["words"] = section.get("summary_words", cfg.get("summary_words", 40))
        if len(chosen) < limit and per_source.get(s["source"], 0) < max(2, limit // 3):
            chosen.append(s)  # keep sources varied
            seen.append(words)
            per_source[s["source"]] = per_source.get(s["source"], 0) + 1
        elif len(reserve) < limit:
            reserve.append(s)  # used only to fill leftover space on the last page
        elif len(chosen) >= limit:
            break
    return chosen, reserve


# ---------- layout ----------

CSS = """
@import url('https://fonts.googleapis.com/css2?family=Playfair+Display:wght@700;900&family=Source+Serif+4:ital,wght@0,400;0,700;1,400&family=Libre+Franklin:wght@500;700&display=swap');
@page { size: letter; margin: 0.4in 0.45in; }
* { box-sizing: border-box; }
body { font-family: 'Source Serif 4', Georgia, 'DejaVu Serif', serif; color: #111; margin: 0; font-size: 9.2pt; line-height: 1.28; }
.kicker, .src, section h3, .dateline, .box h5 { font-family: 'Libre Franklin', Helvetica, 'DejaVu Sans', sans-serif; }
.mast { text-align: center; border-bottom: 3px double #111; padding-bottom: 3px; }
.mast h1 { font-family: 'Playfair Display', 'Times New Roman', Georgia, serif; font-weight: 900; font-size: 34pt;
           margin: 0 0 2px; line-height: 1.2; letter-spacing: .5px; }
.tag { font-style: italic; font-size: 8.5pt; }
.dateline { display: flex; justify-content: space-between; border-bottom: 1px solid #111; font-size: 7.5pt;
            text-transform: uppercase; letter-spacing: .6px; padding: 3px 0; margin-bottom: 7px; }
.lead { border-bottom: 1px solid #111; padding-bottom: 7px; margin-bottom: 7px; }
.lead h2 { font-family: 'Playfair Display', Georgia, serif; font-weight: 700; font-size: 20pt; line-height: 1.1; margin: 2px 0 4px; }
.lead p { font-size: 10.5pt; margin: 0; }
.kicker { font-size: 7pt; font-weight: bold; text-transform: uppercase; letter-spacing: .8px; }
.boxes { display: flex; gap: 12px; margin-bottom: 8px; }
.box { flex: 1; border: 1px solid #111; padding: 5px 8px; }
.box.wx { flex: 0 0 1.9in; }
.brief { margin-top: -3px; }
.brief .box { padding: 4px 8px; }
.brief .big { font-size: 11.5pt; }
.brief .note { font-size: 7.6pt; color: #333; margin-top: 1px; }
.wx .temp { font-weight: 700; font-size: 16pt; line-height: 1.1; }
.wx .temp span { font-size: 11pt; font-weight: 400; }
.wx .sun { font-size: 7.6pt; margin-top: 2px; }
.box h5 { margin: 0 0 3px; font-size: 7pt; text-transform: uppercase; letter-spacing: 1.2px; }
.box p { margin: 0; }
.box .quote { font-style: italic; font-size: 10pt; }
.box .who { font-size: 8pt; margin-top: 2px; text-align: right; }
.box .tip-title { font-weight: bold; }
.flow { columns: 3; column-gap: 14px; column-rule: 1px solid #bbb; }
section h3 { font-size: 9pt; text-transform: uppercase; letter-spacing: 2px; font-weight: 700;
             border-top: 2px solid #111; border-bottom: 1px solid #111; padding: 2px 0; margin: 2px 0 5px; }
.sh, .head { break-inside: avoid; page-break-inside: avoid; }  /* heading glued to its first story */
.head { break-after: avoid; page-break-after: avoid; }
article { margin-bottom: 5px; padding-bottom: 5px; border-bottom: 1px dotted #999; }
article p { orphans: 2; widows: 2; }  /* text may continue in the next column, never one stray line */
article h4, article .src, .qr { break-after: avoid; page-break-after: avoid; }
article h4 { break-inside: avoid; }
article h4 { font-size: 10.4pt; line-height: 1.16; margin: 0 0 1px; }
article p { margin: 2px 0 0; }
article::after, .lead::after { content: ""; display: block; clear: both; }
.head { display: flex; align-items: flex-start; gap: 5px; }
.ht { flex: 1 1 auto; min-width: 0; }
.qr { flex: none; float: none; }
.lead .qr { float: right; }
.qr-lead { margin-left: 10px; }
.src { font-size: 6.6pt; color: #444; text-transform: uppercase; letter-spacing: .5px; }
a { color: inherit; text-decoration: none; }
footer { font-size: 7pt; text-align: center; color: #555; border-top: 1px solid #111; padding-top: 3px; margin-top: 6px; }
"""

SHORT_CACHE = {}
SETTINGS = {"shorten_links": False}  # set from config.json in main()


def when(d):
    if not d:
        return ""
    return d.astimezone().strftime("%-I:%M %p").lower()


def tidy_url(url):
    """Drop tracking parameters so QR codes stay small and easy to scan."""
    p = urllib.parse.urlsplit(url)
    q = [(k, v) for k, v in urllib.parse.parse_qsl(p.query, keep_blank_values=True)
         if not k.lower().startswith(("utm_", "cmp", "ito", "at_", "fbclid", "gclid", "mc_"))]
    return urllib.parse.urlunsplit((p.scheme, p.netloc, p.path, urllib.parse.urlencode(q), ""))


def shorten(url):
    """Short is.gd link so the QR code has few, large squares. Falls back to the full link."""
    url = tidy_url(url)
    if not SETTINGS["shorten_links"]:
        return url
    if url in SHORT_CACHE:
        return SHORT_CACHE[url]
    try:
        api = "https://is.gd/create.php?format=simple&url=" + urllib.parse.quote(url, safe="")
        short = fetch(api).decode().strip()
        if short.startswith("https://is.gd/"):
            SHORT_CACHE[url] = short
            return short
    except Exception as e:
        log(f"  shortener failed, using full link: {e}")
    return url


@__import__("functools").lru_cache(maxsize=None)  # pages are rendered several times while filling
def qr_img(url, cls="qr"):
    """Inline SVG QR code linking to the story (skipped if segno isn't installed)."""
    try:
        import segno
    except ImportError:
        return ""
    if not url:
        return ""
    qr = segno.make(shorten(url), error="l", micro=False)
    modules = qr.symbol_size(border=1)[0]
    # ~0.29 mm per square (was 0.33, which scanned fine); short links give small codes, long ones a bit bigger
    size = max(0.36, round(modules * 0.0115, 2))
    uri = qr.svg_data_uri(border=1, dark="#000", light="#fff")
    return f'<img class="{cls}" style="width:{size}in;height:{size}in" src="{uri}" alt="">'


def article_html(s, heading=""):
    summ = clean(s["summary"], s.get("words", 40))
    head = ("<div class='head'><div class='ht'>"
            f'<h4><a href="{html.escape(s["link"])}">{html.escape(s["title"])}</a></h4>'
            f'<div class="src">{html.escape(s["source"])} · {when(s["date"])}</div></div>'
            + qr_img(s["link"]) + "</div>")
    if heading:  # section heading never sits alone at a column bottom
        head = f"<div class='sh'><h3>{html.escape(heading)}</h3>{head}</div>"
    return "<article>" + head + (f"<p>{html.escape(summ)}</p>" if summ else "") + "</article>"


def section_html(name, stories):
    """A section flows through the page's columns like a real paper; only stories never split."""
    return "<section>" + article_html(stories[0], name) + "".join(article_html(s) for s in stories[1:]) + "</section>"


def short_date(d):
    return d.astimezone().strftime("%b %-d") if d else ""


def brief_html(commute=None, fx=None, ee=None):
    """Second row of small boxes: commute, exchange rate, Express Entry."""
    parts = []
    if commute:
        how = (f"{commute['mins']} min with traffic now" + (f" ({commute['delay']} min delay)" if commute["delay"] else "")
               if commute["live"] else f"{commute['mins']} min on clear roads · allow extra at rush hour")
        parts.append(f"<div class='box'><h5>Commute · {html.escape(commute['label'])}</h5>"
                     f"<p><b class='big'>{commute['mins']} min</b> · {commute['km']} km</p><p class='note'>{how}</p></div>")
    if fx:
        arrow = "▲" if fx["change"] > 0 else "▼" if fx["change"] < 0 else "■"
        parts.append(f"<div class='box'><h5>{fx['base']} → {fx['quote']}</h5>"
                     f"<p><b class='big'>{fx['sym']}{fx['rate']:.2f}</b> per 1 {fx['base']}</p>"
                     f"<p class='note'>{arrow} {abs(fx['change']):.2f} this week · ECB rate, {dt.date.fromisoformat(fx['date']):%b %-d}</p></div>")
    if ee:
        d, c = ee["latest"], ee["crs"]
        cut = ""
        if c:
            cut = (f"<b class='big'>CRS {c['crs']}</b> " +
                   ("cutoff" if c is d else f"last cutoff ({html.escape(c['what'])}, {short_date(c['date'])})"))
        parts.append(f"<div class='box'><h5>Express Entry · latest draw</h5>"
                     f"<p>{cut}</p><p class='note'>{short_date(d['date'])}: {d['itas']} invitations, "
                     f"{html.escape(d['what'])}.</p></div>")
    return f"<div class='boxes brief'>{''.join(parts)}</div>" if parts else ""


def boxes_html(quote, tip, weather=None, commute=None, fx=None, ee=None):
    parts = []
    if weather:
        rain = f", {weather['rain']}% chance of rain" if weather["rain"] else ""
        parts.append(f"<div class='box wx'><h5>Today · {html.escape(weather['place'])}</h5>"
                     f"<p class='temp'>{weather['hi']}° <span>/ {weather['lo']}°</span></p>"
                     f"<p>{html.escape(weather['sky'])}{rain}.</p>"
                     f"<p class='sun'>Sunrise {weather['sunrise']} · Sunset {weather['sunset']}</p></div>")
    if quote:
        parts.append(f"<div class='box'><h5>Quote of the day</h5><p class='quote'>“{html.escape(quote[0])}”</p>"
                     f"<div class='who'>— {html.escape(quote[1])}</div></div>")
    if tip:
        parts.append(f"<div class='box'><h5>Tip of the day · {html.escape(tip.get('topic', ''))}</h5>"
                     f"<p><span class='tip-title'>{html.escape(tip['title'])}.</span> {html.escape(tip['tip'])}</p></div>")
    top = f"<div class='boxes'>{''.join(parts)}</div>" if parts else ""
    return top + brief_html(commute, fx, ee)


def build_html(cfg, sections, extras=None):
    today = dt.date.today()
    lead_sec, lead = None, None
    for name, stories in sections:
        if stories and (lead is None):
            lead_sec, lead = name, stories.pop(0)
    edition_no = today.toordinal() - dt.date(2026, 1, 1).toordinal() + 1
    parts = [f"<!doctype html><html><head><meta charset='utf-8'><title>{html.escape(cfg['paper_name'])}</title>"
             f"<style>{CSS}</style></head><body>",
             f"<div class='mast'><h1>{html.escape(cfg['paper_name'])}</h1>"
             f"<div class='tag'>{html.escape(cfg.get('tagline', ''))}</div></div>",
             f"<div class='dateline'><span>No. {edition_no}</span><span>{today:%A, %B %-d, %Y} · {dt.datetime.now():%-I:%M %p}</span>"
             f"<span>{sum(len(s) for _, s in sections) + (1 if lead else 0)} stories</span></div>"]
    if lead:
        parts.append(f"<div class='lead'><div class='kicker'>{html.escape(lead_sec)} · Top story</div>"
                     + qr_img(lead["link"], "qr qr-lead") +
                     f"<h2><a href='{html.escape(lead['link'])}'>{html.escape(lead['title'])}</a></h2>"
                     f"<div class='src'>{html.escape(lead['source'])} · {when(lead['date'])}</div>"
                     + f"<p>{html.escape(clean(lead['summary'], 70))}</p></div>")
    parts.append(boxes_html(**(extras or {})))
    parts.append("<div class='flow'>" + "".join(section_html(n, s) for n, s in sections if s) + "</div>")
    parts.append("</body></html>")  # no footer: it once spilled onto a sheet of its own
    return "".join(parts)


def page_count(cfg, sections, extras):
    from weasyprint import HTML
    doc = build_html(cfg, [(n, list(s)) for n, s in sections], extras)
    return len(HTML(string=doc, base_url=str(HERE)).render().pages)


def fill_pages(cfg, sections, spares, extras):
    """Add spare stories, most important sections first, until the last page (and the back
    of the last sheet when printing two-sided) is full without spilling onto a new page."""
    pages = page_count(cfg, sections, extras)
    target = pages + (pages % 2 if cfg.get("two_sided", True) else 0)
    printed = [norm(s["title"]) for _, st in sections for s in st]
    queue = []  # round-robin: 1st spare of every section, then 2nd of every section, ...
    for rnd in range(max((len(sp) for sp in spares), default=0)):
        queue += [(i, sp[rnd]) for i, sp in enumerate(spares) if rnd < len(sp)]
    added, misses, step = 0, 0, 3
    while queue and misses < 4:
        batch, queue = queue[:step], queue[step:]
        batch = [(i, s) for i, s in batch if not same_story(norm(s["title"]), printed)]
        for i, s in batch:
            sections[i][1].append(s)
        if page_count(cfg, sections, extras) <= target:
            added += len(batch)
            printed += [norm(s["title"]) for _, s in batch]
            continue
        for i, s in batch:
            sections[i][1].remove(s)
        if step > 1:  # too much at once: put them back and go one by one
            queue, step = batch + queue, 1
        else:
            misses += 1  # that one is too long; a shorter one may still fit
    log(f"Filled the space with {added} extra stories ({target} pages)")


# ---------- output ----------

def to_pdf(html_path, pdf_path):
    if Path(CHROME).exists():  # on the Mac
        subprocess.run([CHROME, "--headless=new", "--disable-gpu", "--no-pdf-header-footer",
                        "--print-to-pdf-no-header", f"--print-to-pdf={pdf_path}",
                        html_path.as_uri()], check=True, capture_output=True, timeout=120)
    else:  # on the homelab (Linux, in Docker)
        from weasyprint import HTML
        HTML(filename=str(html_path)).write_pdf(str(pdf_path))


def print_pdf(pdf_path, printer, two_sided=True):
    if printer.startswith(("ipp://", "ipps://")):  # driverless: send straight to the printer
        import ipp
        try:
            info = ipp.printer_info(printer)
        except Exception as e:
            log(f"  couldn't read printer capabilities ({e}); trying PWG raster")
            info = {}
        formats = info.get("document-format-supported", [])
        duplex = two_sided and "two-sided-long-edge" in info.get("sides-supported", ["two-sided-long-edge"])
        if "application/pdf" in formats:
            job = ipp.print_pdf(printer, pdf_path, job_name=pdf_path.stem, two_sided=duplex)
            log(f"Sent PDF to printer, job {job}")
            return
        if formats and "image/pwg-raster" not in formats:
            raise RuntimeError(f"printer accepts neither PDF nor PWG raster (it supports: {', '.join(formats)})")
        import raster
        dpis = [r[0] if isinstance(r, tuple) else r
                for r in info.get("pwg-raster-document-resolution-supported", [])]
        dpi = 600 if not dpis or 600 in dpis else max(d for d in dpis if d <= 600) if any(d <= 600 for d in dpis) else dpis[0]
        data, pages = raster.pdf_to_pwg(pdf_path, dpi=dpi, duplex=duplex)
        job = ipp.print_document(printer, data, "image/pwg-raster",
                                 job_name=pdf_path.stem, two_sided=duplex and pages > 1)
        log(f"Sent {pages} page(s) to printer, job {job}")
        return
    cmd = ["lp"] + (["-d", printer] if printer else []) + [str(pdf_path)]  # a CUPS queue (no Docker)
    r = subprocess.run(cmd, capture_output=True, text=True)
    log(r.stdout.strip() or r.stderr.strip())


def prune(keep):
    pdfs = sorted(EDITIONS.glob("20*.pdf"))
    for old in pdfs[:-keep] if keep else []:
        old.unlink(missing_ok=True)
        old.with_suffix(".html").unlink(missing_ok=True)


def report_health(cfg, ok, msg):
    """Heartbeat for a monitor: Uptime Kuma push URL, healthchecks.io, or any URL that takes a GET."""
    url = cfg.get("healthcheck_url", "")
    if not url or "--no-print" in sys.argv:
        return
    if "/api/push/" in url:  # Uptime Kuma
        url = url.split("?")[0] + "?" + urllib.parse.urlencode({"status": "up" if ok else "down", "msg": msg[:200]})
    elif not ok:
        url = url.rstrip("/") + "/fail"  # healthchecks.io convention
    try:
        fetch(url)
    except Exception as e:
        log(f"  health check ping failed: {e}")


def main():
    cfg = json.loads((HERE / "config.json").read_text())
    try:
        code, msg = run(cfg)
    except Exception as e:
        code, msg = 3, f"crashed: {type(e).__name__}: {e}"
        report_health(cfg, False, msg)
        raise
    if code != 64:
        report_health(cfg, code == 0, msg)
    return code


def run(cfg):
    unknown = [a for a in sys.argv[1:] if a != "--no-print"]
    if unknown:
        print(__doc__)
        print(f"Unknown option(s): {' '.join(unknown)}. Nothing was made or printed.")
        return 64, ""
    SETTINGS["shorten_links"] = bool(cfg.get("shorten_links", False))
    EDITIONS.mkdir(exist_ok=True)
    cache_file = HERE / "shortlinks.json"
    try:
        SHORT_CACHE.update(json.loads(cache_file.read_text()))
    except (OSError, ValueError):
        pass
    days = cfg.get("no_repeat_days", 1)
    history = load_history(days)
    log(f"Gathering {sum(len(s['feeds']) for s in cfg['sections'])} feeds…")
    feeds = fetch_all(cfg["sections"])
    coverage = coverage_counter([s for items in feeds.values() for s in items])
    seen, sections, spares = [], [], []
    for sec in cfg["sections"]:
        items = [s for u in sec["feeds"] for s in feeds.get(u, [])]
        stories, spare = pick_stories(cfg, sec, items, seen, history, coverage)
        log(f"  {sec['name']}: {len(stories)} stories (+{len(spare)} spare)")
        sections.append((sec["name"], stories))
        spares.append(spare)
    sections, spares = order_by_importance(sections, spares)
    log("Order today: " + " > ".join(f"{n} ({max(x['importance'] for x in s):.0f})" for n, s in sections if s))
    if not any(s for _, s in sections):
        log("No stories found (offline?). Nothing printed.")
        return 1, "no stories found (offline?)"

    printed = sections
    real_run = "--no-print" not in sys.argv
    stamp = f"{dt.date.today():%Y-%m-%d}" if real_run else "preview"  # tests never overwrite the real edition
    html_path, pdf_path = EDITIONS / f"{stamp}.html", EDITIONS / f"{stamp}.pdf"
    used = load_used()
    kinds = cfg.get("tip_kinds", ["tech", "life"])
    extras = {"quote": quote_of_the_day(used) if cfg.get("quote_of_the_day", True) else None,
              "tip": tip_of_the_day(used, kinds) if cfg.get("tip_of_the_day", True) else None,
              "weather": weather_today(cfg.get("weather")),
              "commute": commute_today(cfg.get("commute")),
              "fx": fx_today(cfg.get("exchange_rate")),
              "ee": express_entry_today() if cfg.get("express_entry") else None}
    if not Path(CHROME).exists():  # WeasyPrint can measure pages, so fill every one
        fill_pages(cfg, sections, spares, extras)
    html_path.write_text(build_html(cfg, [(n, list(s)) for n, s in sections], extras), encoding="utf-8")
    if SHORT_CACHE:
        cache_file.write_text(json.dumps(dict(list(SHORT_CACHE.items())[-600:]), indent=0))
    to_pdf(html_path, pdf_path)
    log(f"Saved {pdf_path}")

    if cfg.get("print") and "--no-print" not in sys.argv:
        import time
        for attempt in range(1, 4):  # printer may be waking from sleep
            try:
                print_pdf(pdf_path, cfg.get("printer", ""), cfg.get("two_sided", True))
                break
            except Exception as e:
                log(f"Print attempt {attempt} failed: {type(e).__name__}: {e}")
                if attempt == 3:
                    log("Giving up on printing; the PDF is still saved.")
                    return 2, f"printing failed: {e}"
                time.sleep(30)
    if real_run:
        save_history(printed)
        save_used(used, extras["quote"], extras["tip"])
    prune(cfg.get("keep_editions", 14))
    n = sum(len(s) for _, s in sections)
    return 0, f"{'printed' if cfg.get('print') else 'saved'} {n} stories"


if __name__ == "__main__":
    sys.exit(main())
