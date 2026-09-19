"""
Collects sold villas (fri handel, 5-10 mio. kr.) from boligsiden.dk for a set of
Copenhagen-area neighbourhoods and saves them to docs/data.json for the dashboard.

Runs automatically every day via .github/workflows/update.yml.
Local run:  pip install requests beautifulsoup4 playwright && python -m playwright install chromium
            python scraper.py
"""
import json
import re
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import requests
from bs4 import BeautifulSoup

# ---------------------------------------------------------------- settings ---
BASE = "https://www.boligsiden.dk"
DATA_FILE = Path("docs/data.json")
DEBUG_DIR = Path("debug")

PRICE_MIN = 5_000_000
PRICE_MAX = 10_000_000
FIRST_RUN_MONTHS = 24          # how far back the very first run collects
OVERLAP_DAYS = 21              # later runs re-check this many days back (late registrations)
MAX_PAGES_PER_POSTCODE = 150
MAX_DETAIL_PAGES_PER_RUN = 1500
MAX_DETAIL_ATTEMPTS = 3
DELAY_SECONDS = 1.5            # pause between page loads, to be gentle on the site

POSTCODES = ["2500", "2720", "2700", "2400", "2870", "2820", "2610", "2650"]
AREA_ORDER = ["Valby", "Vanløse", "Brønshøj", "Emdrup", "København NV",
              "Dyssegård", "Vangede", "Rødovre", "Hvidovre"]
SIMPLE_AREAS = {"2500": "Valby", "2720": "Vanløse", "2700": "Brønshøj",
                "2870": "Dyssegård", "2610": "Rødovre", "2650": "Hvidovre"}

# Emdrup = houses in 2400 inside this outline (latitude, longitude corners).
# It is an approximation - adjust the corners if houses land in the wrong area.
# Everything else in 2400 counts as "København NV".
EMDRUP_POLYGON = [(55.7335, 12.5190), (55.7335, 12.5560),
                  (55.7200, 12.5560), (55.7175, 12.5190)]

# Lyngbyvejen through Gentofte, south to north (latitude, longitude).
# Vangede = houses in 2820 west of this line; houses east of it are ignored.
LYNGBYVEJ = [(55.7300, 12.5550), (55.7400, 12.5440), (55.7500, 12.5340),
             (55.7600, 12.5250), (55.7700, 12.5150)]

USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36")

# ------------------------------------------------------------------ areas ---
def in_polygon(lat, lon, poly):
    inside = False
    for i in range(len(poly)):
        y1, x1 = poly[i]
        y2, x2 = poly[(i + 1) % len(poly)]
        if (y1 > lat) != (y2 > lat):
            x_cross = x1 + (lat - y1) * (x2 - x1) / (y2 - y1)
            if lon < x_cross:
                inside = not inside
    return inside


def line_lon_at(lat, line):
    if lat <= line[0][0]:
        return line[0][1]
    if lat >= line[-1][0]:
        return line[-1][1]
    for (la1, lo1), (la2, lo2) in zip(line, line[1:]):
        if la1 <= lat <= la2:
            return lo1 + (lat - la1) * (lo2 - lo1) / (la2 - la1)
    return line[-1][1]


def assign_area(postcode, lat, lon):
    if postcode in SIMPLE_AREAS:
        return SIMPLE_AREAS[postcode]
    if lat is None or lon is None:
        return None
    if postcode == "2400":
        return "Emdrup" if in_polygon(lat, lon, EMDRUP_POLYGON) else "København NV"
    if postcode == "2820":
        return "Vangede" if lon < line_lon_at(lat, LYNGBYVEJ) else None
    return None

# ---------------------------------------------------------------- parsing ---
STATUS_PREFIXES = ("Ikke til salg", "Til salg", "Kommer snart", "Solgt")
RE_HANDEL = re.compile(r"Handelstype\s*(.+?)\s*Salgsdato", re.S)
RE_DATE = re.compile(r"Salgsdato\s*(\d{2}-\d{2}-\d{4})")
RE_PRICE = re.compile(r"Pris\s*([\d.]+)")          # capital P: does not match "M²-pris"
RE_M2 = re.compile(r"M²-pris\s*([\d.]+)")
RE_ADDR = re.compile(r"([^\n]*?\S,\s*\d{4}\s[^\n]*?)\s*Villa")
RE_PRICE_LINE = re.compile(r"^(.*?)\s*(\d{1,3}(?:\.\d{3})+)\s*(?:kr\.?)?$")
MONTHS = {"januar", "februar", "marts", "april", "maj", "juni", "juli",
          "august", "september", "oktober", "november", "december"}


def to_int(s):
    digits = re.sub(r"[^\d]", "", s or "")
    return int(digits) if digits else None


def parse_row(href, text):
    """Parse one sale from the 'solgte' list. Returns dict or None."""
    if "Salgsdato" not in text or "Villa" not in text:
        return None
    m_date, m_price = RE_DATE.search(text), RE_PRICE.search(text)
    if not (m_date and m_price):
        return None
    handel = RE_HANDEL.search(text)
    m2 = RE_M2.search(text)
    addr = RE_ADDR.search(text)
    address = addr.group(1).strip() if addr else href.rsplit("/", 1)[-1]
    for p in STATUS_PREFIXES:
        if address.startswith(p):
            address = address[len(p):].strip()
    d, mth, y = m_date.group(1).split("-")
    url = href if href.startswith("http") else BASE + href
    return {
        "url": url.split("?")[0],
        "address": address,
        "handel": handel.group(1).strip() if handel else "",
        "sale_date": f"{y}-{mth}-{d}",
        "sale_price": to_int(m_price.group(1)),
        "m2_price": to_int(m2.group(1)) if m2 else None,
    }


def parse_coords(html):
    patterns = [
        r"sortByDistanceCenter=(\d+\.\d+)(?:,|%2C)(\d+\.\d+)",
        r"marker=(\d+\.\d+)(?:,|%2C)(\d+\.\d+)",
        r"\|(\d+\.\d+)(?:,|%2C)(\d+\.\d+)",
    ]
    for pat in patterns:
        m = re.search(pat, html)
        if m:
            a, b = float(m.group(1)), float(m.group(2))
            lon, lat = (a, b) if a < b else (b, a)   # Denmark: lon ~8-15, lat ~54-58
            if 54 < lat < 58 and 7 < lon < 16:
                return lat, lon
    m_lat = re.search(r'"lat(?:itude)?"\s*:\s*(5[4-7]\.\d+)', html)
    m_lon = re.search(r'"(?:lon|lng|longitude)"\s*:\s*(1?\d\.\d+)', html)
    if m_lat and m_lon:
        return float(m_lat.group(1)), float(m_lon.group(1))
    return None, None


def parse_history(text):
    """Events from 'Boligens historie', newest first: [{'label', 'price'}]."""
    start = text.find("Boligens historie")
    if start < 0:
        return []
    ends = [text.find(k, start + 20) for k in
            ("Vis hele historikken", "Prisudvikling i", "Solgte boliger i området")]
    ends = [e for e in ends if e > 0]
    block = text[start:min(ends)] if ends else text[start:start + 4000]
    events, pending = [], None
    for line in (l.strip() for l in block.splitlines()):
        if (not line or line.lower() in MONTHS or re.fullmatch(r"\d{4}", line)
                or line.lower() in ("kr", "kr.")):
            continue
        m = RE_PRICE_LINE.match(line)
        if m:
            label = m.group(1).strip() or pending
            if label:
                events.append({"label": label, "price": to_int(m.group(2))})
            pending = None
        else:
            pending = line
    return events


def derive_asking(events, sale_price):
    """Last asking price before the sale, and the first asking price of that listing."""
    sold_idx = None
    for i, e in enumerate(events):
        if e["label"].lower().startswith("solgt"):
            if sold_idx is None:
                sold_idx = i
            if abs(e["price"] - sale_price) <= 0.01 * sale_price:
                sold_idx = i
                break
    if sold_idx is None:
        return None, None
    last_ask = first_ask = None
    for e in events[sold_idx + 1:]:
        label = e["label"].lower()
        if label.startswith("solgt"):
            break                      # an older sale - stop
        if last_ask is None:
            last_ask = e["price"]
        if "udbudt" in label or "til salg" in label:
            first_ask = e["price"]
    first_ask = first_ask or last_ask
    if last_ask and not (0.6 * sale_price <= last_ask <= 1.6 * sale_price):
        return None, None              # looks like an unrelated old listing
    return last_ask, first_ask

# --------------------------------------------------------------- fetching ---
class Fetcher:
    """Gets page HTML. Tries plain web requests first, falls back to a real browser."""

    def __init__(self):
        self.mode = "http"
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "da-DK,da;q=0.9,en;q=0.8",
        })
        self._pw = self._browser = self._page = None

    def get(self, url, wait_for=None):
        if self.mode == "http":
            r = self.session.get(url, timeout=40)
            return r.status_code, r.text
        return self._browser_get(url, wait_for)

    def switch_to_browser(self):
        from playwright.sync_api import sync_playwright
        print("Switching to browser mode")
        self.mode = "browser"
        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch()
        ctx = self._browser.new_context(locale="da-DK", user_agent=USER_AGENT,
                                        viewport={"width": 1400, "height": 1000})
        self._page = ctx.new_page()

    def _browser_get(self, url, wait_for):
        page = self._page
        resp = page.goto(url, wait_until="domcontentloaded", timeout=60000)
        for label in ("Afvis alle", "Kun nødvendige", "Tillad alle", "Accepter alle"):
            try:
                page.get_by_role("button", name=re.compile(label, re.I)).first.click(timeout=1500)
                break
            except Exception:
                continue
        if wait_for:
            try:
                page.wait_for_selector(wait_for, timeout=20000)
            except Exception:
                pass
        for _ in range(4):
            page.mouse.wheel(0, 2500)
            page.wait_for_timeout(250)
        return (resp.status if resp else 0), page.content()

    def close(self):
        if self._browser:
            self._browser.close()
            self._pw.stop()


def describe(status, html):
    title = re.search(r"<title[^>]*>(.*?)</title>", html or "", re.S | re.I)
    title = title.group(1).strip()[:100] if title else "(no title)"
    low = (html or "").lower()
    hints = [w for w in ("captcha", "cloudflare", "access denied", "challenge", "blocked",
                         "enable javascript", "robot") if w in low]
    links = low.count("/adresse/")
    return (f"status {status}, {len(html or '')} characters, title '{title}', "
            f"{links} address links" + (f", contains: {', '.join(hints)}" if hints else ""))


def save_debug(name, html):
    DEBUG_DIR.mkdir(exist_ok=True)
    safe = re.sub(r"[^\w-]", "_", name)[:80]
    (DEBUG_DIR / f"{safe}.html").write_text(html or "", encoding="utf-8")


def rows_from_html(html):
    soup = BeautifulSoup(html, "html.parser")
    rows = {}
    for a in soup.select('a[href*="/adresse/"]'):
        row = parse_row(a.get("href", ""), a.get_text("\n"))
        if row:
            rows[row["url"] + "|" + row["sale_date"]] = row
    return list(rows.values())


def list_url(pc, n):
    return f"{BASE}/postnummer/{pc}/solgte/villa" + (f"?page={n}" if n > 1 else "")


def read_list_page(fetcher, url):
    status, html = fetcher.get(url, wait_for='a[href*="/adresse/"]')
    return rows_from_html(html), status, html


def choose_mode(fetcher):
    """Check that the list can be read; switch to the browser if plain requests fail."""
    url = list_url(POSTCODES[0], 1)
    rows, status, html = read_list_page(fetcher, url)
    print(f"Test (plain request): {describe(status, html)} -> {len(rows)} sales read")
    if rows:
        return True
    save_debug("test_plain_request", html)
    fetcher.switch_to_browser()
    rows, status, html = read_list_page(fetcher, url)
    print(f"Test (browser): {describe(status, html)} -> {len(rows)} sales read")
    if rows:
        return True
    save_debug("test_browser", html)
    return False


def crawl_postcode(fetcher, pc, since):
    found = {}
    for n in range(1, MAX_PAGES_PER_POSTCODE + 1):
        url = list_url(pc, n)
        rows, status, html = read_list_page(fetcher, url)
        if not rows:
            if n == 1:
                print(f"  no sales found on {url}: {describe(status, html)}")
                save_debug(f"list_{pc}", html)
            break
        for r in rows:
            if r["sale_date"] >= since:
                found[r["url"] + "|" + r["sale_date"]] = r
        print(f"  page {n}: {len(rows)} rows, oldest {min(r['sale_date'] for r in rows)}")
        if all(r["sale_date"] < since for r in rows):
            break
        time.sleep(DELAY_SECONDS)
    return found


def read_detail(fetcher, url):
    status, html = fetcher.get(url, wait_for="text=Boligens historie")
    lat, lon = parse_coords(html)
    text = BeautifulSoup(html, "html.parser").get_text("\n")
    return lat, lon, parse_history(text), status, html

# ------------------------------------------------------------------- main ---
def load_data():
    if DATA_FILE.exists():
        return json.loads(DATA_FILE.read_text(encoding="utf-8"))
    return {"sales": []}


def write_data(sales):
    for s in sales.values():
        s["area"] = assign_area(s["postcode"], s.get("lat"), s.get("lon"))
        ask, sale = s.get("asking_price"), s["sale_price"]
        s["pct_below"] = round((ask - sale) / ask * 100, 2) if ask else None
    out = {
        "updated": datetime.now(timezone.utc).isoformat(timespec="minutes"),
        "areas": AREA_ORDER,
        "price_range": [PRICE_MIN, PRICE_MAX],
        "sales": sorted(sales.values(), key=lambda s: s["sale_date"], reverse=True),
    }
    DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    DATA_FILE.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")


def main():
    data = load_data()
    sales = {s["id"]: s for s in data.get("sales", [])}
    today = date.today()

    fetcher = Fetcher()
    try:
        if not choose_mode(fetcher):
            print("Boligsiden's sales list could not be read, neither with a plain request nor "
                  "with a browser. See the lines above and the debug files.")
            sys.exit(1)

        # 1) new sales from the lists
        for pc in POSTCODES:
            known = [s["sale_date"] for s in sales.values() if s["postcode"] == pc]
            if known:
                since = (date.fromisoformat(max(known)) - timedelta(days=OVERLAP_DAYS)).isoformat()
            else:
                since = (today - timedelta(days=int(FIRST_RUN_MONTHS * 30.5))).isoformat()
            print(f"{pc}: collecting sales since {since}")
            rows = crawl_postcode(fetcher, pc, since)
            added = 0
            for key, r in rows.items():
                if r["handel"].lower() != "fri handel":
                    continue
                if not (PRICE_MIN <= r["sale_price"] <= PRICE_MAX):
                    continue
                if key in sales:
                    continue
                sales[key] = {**r, "id": key, "postcode": pc, "lat": None, "lon": None,
                              "asking_price": None, "first_asking_price": None,
                              "history_checked": False, "attempts": 0}
                added += 1
            print(f"{pc}: {added} new sales in range")

        # 2) listing price + location from each new sale's own page
        todo = [s for s in sales.values()
                if not s["history_checked"] and s["attempts"] < MAX_DETAIL_ATTEMPTS]
        print(f"Reading {len(todo)} house pages")
        saved_debug = 0
        for i, s in enumerate(todo[:MAX_DETAIL_PAGES_PER_RUN], 1):
            s["attempts"] += 1
            try:
                lat, lon, events, status, html = read_detail(fetcher, s["url"])
            except Exception as exc:
                print(f"  {s['address']}: failed ({exc})")
                continue
            if lat is not None:
                s["lat"], s["lon"] = lat, lon
            if events:
                s["asking_price"], s["first_asking_price"] = derive_asking(events, s["sale_price"])
                s["history_checked"] = True
            elif saved_debug < 3:
                print(f"  no history found: {describe(status, html)}")
                save_debug("detail_" + s["url"].rsplit("/", 1)[-1], html)
                saved_debug += 1
            print(f"  [{i}/{len(todo)}] {s['address']}: sold {s['sale_price']:,}, "
                  f"asking {s['asking_price'] or '-'}")
            if i % 25 == 0:
                write_data(sales)
            time.sleep(DELAY_SECONDS)
    finally:
        fetcher.close()

    write_data(sales)
    print(f"Saved {len(sales)} sales to {DATA_FILE}")


if __name__ == "__main__":
    main()
