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


def tip_of_the_day(used):
    """Every tip once before any repeats, and never the same topic two days running."""
    try:
        tips = json.loads((HERE / "tips.json").read_text())
    except (OSError, ValueError):
        return None
    if not tips:
        return None
    fresh = [x for x in tips if x["title"] not in used["tips"]] or tips  # all used: start a new round
    last_topic = next((x["topic"] for x in tips if used["tips"] and x["title"] == used["tips"][-1]), None)
    pool = [x for x in fresh if x["topic"] != last_topic] or fresh
    return pool[dt.date.today().toordinal() % len(pool)]


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
    """Links and title words of stories printed in the previous `days` days (not today's)."""
    try:
        items = json.loads(HISTORY.read_text())
    except (OSError, ValueError):
        items = []
    today = dt.date.today()
    recent = [i for i in items if 0 < (today - dt.date.fromisoformat(i["date"])).days <= days]
    return {tidy_url(i["link"]) for i in recent if i.get("link")}, [norm(i["title"]) for i in recent]


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
    return any(len(words & w) >= max(3, len(words) // 2) for w in others)


def pick_stories(cfg, section, seen, history=(set(), [])):
    stories = []
    with cf.ThreadPoolExecutor(8) as pool:
        futures = {pool.submit(parse_feed, u): u for u in section["feeds"]}
        for f in cf.as_completed(futures):
            try:
                stories += f.result()
            except Exception as e:  # one broken feed shouldn't kill the paper
                log(f"  skipped {futures[f]}: {e}")

    now = dt.datetime.now(dt.timezone.utc)
    window = section.get("max_age_hours", cfg.get("max_age_hours", 36))
    cutoff = now - dt.timedelta(hours=window)
    boost = cfg.get("boost_keywords", []) + section.get("boost_keywords", [])
    exclude = cfg.get("exclude_keywords", []) + section.get("exclude_keywords", [])

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
        ranked.append((score, s))
    ranked.sort(key=lambda x: x[0], reverse=True)

    chosen, reserve, per_source = [], [], {}
    limit = section.get("stories", 8)
    for _, s in ranked:
        words = norm(s["title"])
        if same_story(words, seen):
            continue  # same story from another outlet
        if tidy_url(s["link"]) in history[0] or same_story(words, history[1]):
            continue  # already printed in a recent edition
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
@import url('https://fonts.googleapis.com/css2?family=Old+Standard+TT:wght@700&family=Playfair+Display:wght@700;900&family=Source+Serif+4:ital,wght@0,400;0,700;1,400&family=Libre+Franklin:wght@500;700&display=swap');
@page { size: letter; margin: 0.4in 0.45in; }
* { box-sizing: border-box; }
body { font-family: 'Source Serif 4', Georgia, 'DejaVu Serif', serif; color: #111; margin: 0; font-size: 9.2pt; line-height: 1.28; }
.kicker, .src, section h3, .dateline, .box h5 { font-family: 'Libre Franklin', Helvetica, 'DejaVu Sans', sans-serif; }
.mast { text-align: center; border-bottom: 3px double #111; padding-bottom: 3px; }
.mast h1 { font-family: 'Old Standard TT', 'Times New Roman', Georgia, serif; font-weight: 700; font-size: 46pt;
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


def boxes_html(quote, tip):
    parts = []
    if quote:
        parts.append(f"<div class='box'><h5>Quote of the day</h5><p class='quote'>“{html.escape(quote[0])}”</p>"
                     f"<div class='who'>— {html.escape(quote[1])}</div></div>")
    if tip:
        parts.append(f"<div class='box'><h5>Tip of the day · {html.escape(tip.get('topic', ''))}</h5>"
                     f"<p><span class='tip-title'>{html.escape(tip['title'])}.</span> {html.escape(tip['tip'])}</p></div>")
    return f"<div class='boxes'>{''.join(parts)}</div>" if parts else ""


def build_html(cfg, sections, quote=None, tip=None):
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
    parts.append(boxes_html(quote, tip))
    parts.append("<div class='flow'>" + "".join(section_html(n, s) for n, s in sections if s) + "</div>")
    parts.append("</body></html>")  # no footer: it once spilled onto a sheet of its own
    return "".join(parts)


def page_count(cfg, sections, quote, tip):
    from weasyprint import HTML
    doc = build_html(cfg, [(n, list(s)) for n, s in sections], quote, tip)
    return len(HTML(string=doc, base_url=str(HERE)).render().pages)


def fill_pages(cfg, sections, spares, quote, tip):
    """Add spare stories, most important sections first, until the last page (and the back
    of the last sheet when printing two-sided) is full without spilling onto a new page."""
    pages = page_count(cfg, sections, quote, tip)
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
        if page_count(cfg, sections, quote, tip) <= target:
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


def main():
    unknown = [a for a in sys.argv[1:] if a != "--no-print"]
    if unknown:
        print(__doc__)
        print(f"Unknown option(s): {' '.join(unknown)}. Nothing was made or printed.")
        return 64
    cfg = json.loads((HERE / "config.json").read_text())
    SETTINGS["shorten_links"] = bool(cfg.get("shorten_links", False))
    EDITIONS.mkdir(exist_ok=True)
    cache_file = HERE / "shortlinks.json"
    try:
        SHORT_CACHE.update(json.loads(cache_file.read_text()))
    except (OSError, ValueError):
        pass
    history = load_history(cfg.get("no_repeat_days", 7))
    log(f"Skipping {len(history[0])} stories already printed in the last {cfg.get('no_repeat_days', 7)} days")
    seen, sections, spares = [], [], []
    for sec in cfg["sections"]:
        log(f"Gathering {sec['name']}…")
        stories, spare = pick_stories(cfg, sec, seen, history)
        log(f"  {len(stories)} stories (+{len(spare)} spare)")
        sections.append((sec["name"], stories))
        spares.append(spare)
    if not any(s for _, s in sections):
        log("No stories found (offline?). Nothing printed.")
        return 1

    printed = sections
    real_run = "--no-print" not in sys.argv
    stamp = f"{dt.date.today():%Y-%m-%d}" if real_run else "preview"  # tests never overwrite the real edition
    html_path, pdf_path = EDITIONS / f"{stamp}.html", EDITIONS / f"{stamp}.pdf"
    used = load_used()
    quote = quote_of_the_day(used) if cfg.get("quote_of_the_day", True) else None
    tip = tip_of_the_day(used) if cfg.get("tip_of_the_day", True) else None
    if not Path(CHROME).exists():  # WeasyPrint can measure pages, so fill every one
        fill_pages(cfg, sections, spares, quote, tip)
    html_path.write_text(build_html(cfg, [(n, list(s)) for n, s in sections], quote, tip), encoding="utf-8")
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
                    return 2
                time.sleep(30)
    if real_run:
        save_history(printed)
        save_used(used, quote, tip)
    prune(cfg.get("keep_editions", 14))
    return 0


if __name__ == "__main__":
    sys.exit(main())
