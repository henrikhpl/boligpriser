"""
Daily picks: reads villas currently for sale (max 9 mio. kr.) in the same areas as
scraper.py, scores them as investments, and saves docs/listings.json for the dashboard.

Run after scraper.py (it uses docs/data.json for the area price levels).
"""
import json
import re
import statistics
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from bs4 import BeautifulSoup

from scraper import (AREA_ORDER, BASE, DELAY_SECONDS, POSTCODES, Fetcher, address_from_detail,
                     assign_area, describe, parse_coords, save_debug, to_int)

# ---------------------------------------------------------------- settings ---
LISTINGS_FILE = Path("docs/listings.json")
SALES_FILE = Path("docs/data.json")
STARS_FILE = Path("docs/stars.json")          # written by the dashboard when you click ☆

LISTING_PRICE_MAX = 9_000_000
LISTING_PRICE_MIN = 0             # raise (e.g. 4_000_000) to skip very cheap odd cases
NEW_MAX_DAYS_ON_MARKET = 3        # a listing counts as new if it has been for sale this long at most
PICKS_MAX, PICKS_MIN = 10, 5      # if fewer than PICKS_MIN new, top up from the past week
TOP_UP_DAYS = 7
KEEP_INACTIVE_DAYS = 60
BENCHMARK_MONTHS = 3             # widened to 6, 12, then all data if an area has too few sales

# How much each signal counts in the score (they are rescaled if one is missing)
WEIGHTS = {
    "m2": 0.40,          # m² price vs. the area's level (from actual sales)
    "valuation": 0.20,   # asking price vs. offentlig vurdering, compared to other listings
    "plot": 0.15,        # plot size per million kr.
    "rooms": 0.10,       # rooms per million kr.
    "scarcity": 0.15,    # few houses under the price cap for sale in the area
}
CLIP = 0.4               # a signal counts at most ±40 %

# Learning from your stars
TASTE_MAX_SHARE = 0.6        # at most 60 % of the final score comes from learned taste ...
TASTE_STARS_FOR_FULL = 12    # ... reached once you have starred this many houses
MODEL_MIN_STARS = 3          # the preference model starts at this many stars (similarity from 1)

NUM = r"\d{1,3}(?:\.\d{3})+"

# ---------------------------------------------------------------- parsing ---
def flat(text):
    return re.sub(r"\s+", " ", re.sub(r"[\u00a0\u2007\u202f\u200b]", " ", text or "")).strip()


def listing_cards(html):
    """Find each listing card via its 'Se bolig' link and return (url, card text)."""
    soup = BeautifulSoup(html, "html.parser")
    cards = {}
    for a in soup.find_all("a", href=re.compile(r"/adresse/")):
        if flat(a.get_text(" ")) != "Se bolig":
            continue
        node = a
        for _ in range(10):
            node = node.parent
            if node is None:
                break
            text = node.get_text("\n")
            if "M² pris" in text or "M²-pris" in text:
                if text.count("Se bolig") > 1:
                    node = None
                break
        if node is None:
            continue
        href = a["href"]
        url = (href if href.startswith("http") else BASE + href).split("?")[0]
        cards[url] = node.get_text("\n")
    return cards


def parse_card(url, text):
    t = flat(text)
    m_price = re.search(rf"({NUM}) kr\.(?: ?\((-?\d+)%\))?", t)
    if not m_price:
        return None
    m_m2p = re.search(rf"M²[ -]pris ({NUM}) kr", t)
    sizes = [to_int(x) for x in re.findall(r"(\d{1,3}(?:\.\d{3})?) m²", t)]   # lowercase m² only
    rooms = re.search(r"(\d+) vær\.", t)
    days = re.search(r"(\d+) dage? \| (\d+) dage? i alt", t)
    fee = re.search(rf"({NUM}|\d{{3}}) kr\./md", t)
    lines = [flat(l) for l in text.splitlines() if flat(l)]
    address = next((l for l in lines if re.search(r", \d{4} \S", l) and not l.startswith("Villa")), None)
    return {
        "url": url,
        "address": address,
        "price": to_int(m_price.group(1)),
        "price_cut_pct": int(m_price.group(2)) if m_price.group(2) else None,
        "m2_price": to_int(m_m2p.group(1)) if m_m2p else None,
        "living_m2": sizes[0] if sizes else None,
        "plot_m2": sizes[1] if len(sizes) > 1 else None,
        "rooms": int(rooms.group(1)) if rooms else None,
        "owner_cost_month": to_int(fee.group(1)) if fee else None,
        "days_on_market": int(days.group(1)) if days else None,
        "days_total": int(days.group(2)) if days else None,
    }


def parse_listing_detail(html):
    t = flat(BeautifulSoup(html, "html.parser").get_text(" "))
    lat, lon = parse_coords(html)
    valuation = re.search(rf"Ejendomsværdi ?({NUM}) ?kr", t)
    plot = re.search(r"Grund(?:areal|størrelse)? ?:? ?(\d{1,3}(?:\.\d{3})?) ?m²", t)
    year = re.search(r"Byggeår ?(\d{4})", t)
    return {
        "lat": lat, "lon": lon,
        "valuation": to_int(valuation.group(1)) if valuation else None,
        "plot_m2": to_int(plot.group(1)) if plot else None,
        "year_built": int(year.group(1)) if year else None,
        "address": address_from_detail(html),
    }

# ---------------------------------------------------------------- scoring ---
def median(values):
    values = [v for v in values if v is not None]
    return statistics.median(values) if values else None


def area_benchmarks(sales):
    """Expected asking m² price per area, from actual sales (5-10 mio.) in the area."""
    out = {}
    windows = sorted({BENCHMARK_MONTHS, 6, 12}) + [None]      # None = all collected sales
    windows = [w for w in windows if w is None or w >= BENCHMARK_MONTHS]
    for area in AREA_ORDER:
        rows = [s for s in sales if s.get("area") == area and s.get("m2_price")]
        used, months = [], None
        for w in windows:
            if w is None:
                used, months = rows, "all"
                break
            cutoff = (date.today() - timedelta(days=int(w * 30.5))).isoformat()
            used, months = [s for s in rows if s["sale_date"] >= cutoff], w
            if len(used) >= 8:
                break
        if len(used) < 5:
            continue
        m2 = median([s["m2_price"] for s in used])
        below = median([s.get("pct_below") for s in used]) or 0
        out[area] = {"sold_m2": round(m2), "expected_ask_m2": round(m2 / (1 - below / 100)),
                     "n": len(used), "months": months}
    return out


def dk(n):
    """8500000 -> '8.500.000'"""
    return f"{n:,}".replace(",", ".")


def clip(x):
    return max(-CLIP, min(CLIP, x))


def score_listings(pool, bench):
    """Adds 'score' (-100..100) and 'reasons' to each listing in pool."""
    val_ratios = [l["price"] / l["valuation"] for l in pool if l.get("valuation")]
    med_val_ratio = median(val_ratios)
    plot_per_mio = median([l["plot_m2"] / (l["price"] / 1e6) for l in pool if l.get("plot_m2")])
    rooms_per_mio = median([l["rooms"] / (l["price"] / 1e6) for l in pool if l.get("rooms")])
    counts = {a: sum(1 for l in pool if l.get("area") == a) for a in AREA_ORDER}
    med_count = median([c for c in counts.values() if c > 0])

    for l in pool:
        sig, reasons = {}, []
        b = bench.get(l.get("area"))
        if b and l.get("m2_price"):
            sig["m2"] = 1 - l["m2_price"] / b["expected_ask_m2"]
            word = "below" if sig["m2"] >= 0 else "above"
            reasons.append((abs(sig["m2"]) * WEIGHTS["m2"] * (1 if sig["m2"] >= 0 else -1),
                            f"m² price {abs(sig['m2']) * 100:.0f} % {word} the area's level "
                            f"({dk(l['m2_price'])} vs. {dk(b['expected_ask_m2'])} kr./m², "
                            + (f"sales last {b['months']} months)" if b.get("months") != "all"
                               else "all collected sales)")))
        if l.get("valuation") and med_val_ratio:
            ratio = l["price"] / l["valuation"]
            sig["valuation"] = 1 - ratio / med_val_ratio
            reasons.append((sig["valuation"] * WEIGHTS["valuation"],
                            f"asking price is {ratio:.2f}× the public valuation "
                            f"(typical {med_val_ratio:.2f}×)"))
        if l.get("plot_m2") and plot_per_mio:
            sig["plot"] = l["plot_m2"] / (l["price"] / 1e6) / plot_per_mio - 1
            reasons.append((clip(sig["plot"]) * WEIGHTS["plot"],
                            f"{'large' if sig['plot'] > 0 else 'small'} plot for the price: "
                            f"{l['plot_m2']:,} m²".replace(",", ".")))
        if l.get("rooms") and rooms_per_mio:
            sig["rooms"] = l["rooms"] / (l["price"] / 1e6) / rooms_per_mio - 1
            reasons.append((clip(sig["rooms"]) * WEIGHTS["rooms"],
                            f"{l['rooms']} rooms for {l['price'] / 1e6:.1f}".replace(".", ",") + " mio."))
        n_area = counts.get(l.get("area"), 0)
        if med_count and n_area:
            sig["scarcity"] = 1 - n_area / med_count
            if sig["scarcity"] > 0:
                reasons.append((clip(sig["scarcity"]) * WEIGHTS["scarcity"],
                                f"only {n_area} house{'s' if n_area != 1 else ''} under "
                                f"{LISTING_PRICE_MAX / 1e6:.0f} mio. "
                                f"for sale in {l['area']}"))
        if l.get("price_cut_pct"):
            reasons.append((0.005, f"price already reduced {abs(l['price_cut_pct'])} %"))

        used = {k: v for k, v in sig.items() if v is not None}
        if used:
            wsum = sum(WEIGHTS[k] for k in used)
            l["score"] = round(100 * sum(WEIGHTS[k] * clip(v) for k, v in used.items()) / wsum / CLIP)
        else:
            l["score"] = None
        l["signals"] = {k: round(v, 3) for k, v in used.items()}
        pos = sorted([r for r in reasons if r[0] > 0.004], key=lambda r: -r[0])[:3]
        neg = sorted([r for r in reasons if r[0] < -0.02], key=lambda r: r[0])[:1]
        l["reasons"] = [r[1] for r in pos]
        l["warnings"] = [r[1] for r in neg]

# ------------------------------------------------------------------ taste ---
import math

FEATURES = [
    ("sig_m2", "low m² price for the area"),
    ("sig_valuation", "low price vs. public valuation"),
    ("sig_plot", "large plot for the price"),
    ("sig_rooms", "many rooms for the price"),
    ("sig_scarcity", "few listings in the area"),
    ("log_price", "higher asking price"),
    ("living_m2", "larger living area"),
    ("log_plot", "larger plot"),
    ("rooms", "more rooms"),
    ("year_built", "newer house"),
    ("log_days", "longer time on the market"),
    ("price_cut", "price already reduced"),
    ("cost_per_m2", "higher owner costs per m²"),
]


def raw_features(l):
    sig = l.get("signals") or {}
    f = {
        "sig_m2": sig.get("m2"), "sig_valuation": sig.get("valuation"), "sig_plot": sig.get("plot"),
        "sig_rooms": sig.get("rooms"), "sig_scarcity": sig.get("scarcity"),
        "log_price": math.log(l["price"]) if l.get("price") else None,
        "living_m2": l.get("living_m2"),
        "log_plot": math.log(l["plot_m2"]) if l.get("plot_m2") else None,
        "rooms": l.get("rooms"), "year_built": l.get("year_built"),
        "log_days": math.log1p(l["days_total"]) if l.get("days_total") is not None else None,
        "price_cut": 1.0 if l.get("price_cut_pct") else 0.0,
        "cost_per_m2": (l["owner_cost_month"] / l["living_m2"])
        if l.get("owner_cost_month") and l.get("living_m2") else None,
    }
    for a in AREA_ORDER:
        f["area_" + a] = 1.0 if l.get("area") == a else 0.0
    return f


def feature_names():
    return [k for k, _ in FEATURES] + ["area_" + a for a in AREA_ORDER]


def feature_label(name):
    if name.startswith("area_"):
        return "in " + name[5:]
    return dict(FEATURES)[name]


def standardise(items):
    """Returns vectors (missing -> 0 = average) and the names, standardised over items."""
    names = feature_names()
    raws = [raw_features(l) for l in items]
    stats = {}
    for n in names:
        vals = [r[n] for r in raws if r[n] is not None]
        if len(vals) < 2:
            stats[n] = (0.0, 0.0)
            continue
        mu = sum(vals) / len(vals)
        sd = (sum((v - mu) ** 2 for v in vals) / len(vals)) ** 0.5
        stats[n] = (mu, sd)
    vecs = []
    for r in raws:
        vecs.append([((r[n] - stats[n][0]) / stats[n][1]) if r[n] is not None and stats[n][1] > 0 else 0.0
                     for n in names])
    return vecs, names


def fit_preferences(X, y, l2=0.05, steps=800, lr=0.5):
    """Small logistic regression. Stars and non-stars get equal total weight, so a few
    stars still count; l2 keeps the weights modest when there is little to learn from."""
    n_pos = sum(y) or 1
    n_neg = (len(y) - n_pos) or 1
    wt = [0.5 / n_pos if yi else 0.5 / n_neg for yi in y]      # weights sum to 1
    w, b = [0.0] * len(X[0]), 0.0
    for _ in range(steps):
        gw, gb = [l2 * wi for wi in w], 0.0
        for xi, yi, si in zip(X, y, wt):
            z = b + sum(a * c for a, c in zip(w, xi))
            p = 1 / (1 + math.exp(-max(-30, min(30, z))))
            e = (p - yi) * si
            gb += e
            for j, xj in enumerate(xi):
                gw[j] += e * xj
        w = [wi - lr * g for wi, g in zip(w, gw)]
        b -= lr * gb
    return w, b


def percentile_ranks(values):
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    for r, i in enumerate(order):
        ranks[i] = r / max(1, len(values) - 1)
    return ranks


def apply_taste(listings, pool, stars):
    """Blend learned taste into the scores of the pool. Returns a summary for the dashboard."""
    for l in pool:
        l["base_score"] = l.get("score")
    starred = [l for u, l in listings.items() if u in stars and l.get("signals") is not None]
    n = len(starred)
    summary = {"n_stars": len(stars), "n_used": n, "share": 0, "likes": [], "dislikes": []}
    if not n or not pool:
        return summary

    train = [l for l in listings.values() if l.get("signals") is not None]
    X_all, names = standardise(train)
    idx = {id(l): i for i, l in enumerate(train)}
    y = [1 if l["url"] in stars else 0 for l in train]

    # 1) preference model (from MODEL_MIN_STARS stars)
    w = None
    if n >= MODEL_MIN_STARS and len(train) - n >= 10:
        w, b = fit_preferences(X_all, y)
        ranked = sorted(zip(names, w), key=lambda t: -abs(t[1]))
        summary["likes"] = [feature_label(k) for k, v in ranked if v > 0.15][:4]
        summary["dislikes"] = [feature_label(k) for k, v in ranked if v < -0.15][:3]

    # 2) similarity to starred houses (features weighted by what the model found important)
    if w:
        mean_abs = sum(abs(v) for v in w) / len(w) or 1
        fw = [0.25 + abs(v) / mean_abs for v in w]
    else:
        fw = [1.0] * len(names)
    star_vecs = [(X_all[idx[id(l)]], l) for l in starred]

    sims, prefs, nearest = [], [], []
    for l in pool:
        x = X_all[idx[id(l)]]
        best, best_l = 0.0, None
        for sv, sl in star_vecs:
            if sl is l:
                continue
            d2 = sum(wj * (a - c) ** 2 for wj, a, c in zip(fw, x, sv)) / sum(fw)
            sim = math.exp(-d2 / 2)
            if sim > best:
                best, best_l = sim, sl
        sims.append(best)
        nearest.append(best_l)
        if w:
            prefs.append(b + sum(a * c for a, c in zip(w, x)))

    sim_r = percentile_ranks(sims)
    taste = ([0.5 * a + 0.5 * c for a, c in zip(sim_r, percentile_ranks(prefs))] if w else sim_r)
    share = TASTE_MAX_SHARE * min(1.0, n / TASTE_STARS_FOR_FULL)
    summary["share"] = round(share * 100)

    for l, t, near in zip(pool, taste, nearest):
        l["taste"] = round(t * 100)
        base = l["base_score"] if l["base_score"] is not None else 0
        l["score"] = round((1 - share) * base + share * (t * 200 - 100))
        if t >= 0.8 and near is not None and l["url"] not in stars:
            l["reasons"] = [f"similar to a house you starred: {near.get('address') or near['url']}"] \
                + (l.get("reasons") or [])[:2]
    return summary


# ------------------------------------------------------------------- main ---
def load(path, default):
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default


def main():
    today = date.today().isoformat()
    store = load(LISTINGS_FILE, {"listings": {}, "picks": {}})
    listings, picks_by_day = store["listings"], store.get("picks", {})
    sales = load(SALES_FILE, {"sales": []})["sales"]
    stars = load(STARS_FILE, {})
    print(f"{len(stars)} starred houses")
    bench = area_benchmarks(sales)
    print(f"Area price levels from sales: { {a: b['expected_ask_m2'] for a, b in bench.items()} }")

    fetcher = Fetcher()
    try:
        # 1) current listings from each postcode
        active, seen = set(), set()
        for pc in POSTCODES:
            got = 0
            for n in range(1, 20):
                url = f"{BASE}/postnummer/{pc}/tilsalg/villa" + (f"?page={n}" if n > 1 else "")
                status, html = fetcher.get(url, wait_for="text=Se bolig")
                cards = listing_cards(html)
                if not cards and n == 1 and fetcher.mode == "http" and pc == POSTCODES[0]:
                    print(f"No listing cards with plain request: {describe(status, html)}")
                    save_debug("listings_plain", html)
                    fetcher.switch_to_browser()
                    status, html = fetcher.get(url, wait_for="text=Se bolig")
                    cards = listing_cards(html)
                new_urls = [u for u in cards if u not in seen]
                seen.update(new_urls)
                if not new_urls:
                    if n == 1:
                        print(f"  {pc}: no listings read ({describe(status, html)})")
                        save_debug(f"listings_{pc}", html)
                    break
                for u in new_urls:
                    card = parse_card(u, cards[u])
                    if not card or not (LISTING_PRICE_MIN <= card["price"] <= LISTING_PRICE_MAX):
                        continue
                    active.add(u)
                    got += 1
                    old = listings.get(u, {})
                    listings[u] = {**old, **{k: v for k, v in card.items() if v is not None},
                                   "postcode": pc, "first_seen": old.get("first_seen", today),
                                   "last_seen": today}
                time.sleep(DELAY_SECONDS)
            print(f"{pc}: {got} houses for sale under {LISTING_PRICE_MAX:,}".replace(",", "."))

        if not active:
            print("No listings could be read - see the debug files.")
            sys.exit(1)

        # 2) details (valuation, plot, location) for listings we have not looked at yet
        todo = [listings[u] for u in active if not listings[u].get("detail_checked")]
        print(f"Reading {len(todo)} house pages")
        for i, l in enumerate(todo, 1):
            try:
                status, html = fetcher.get(l["url"], wait_for="text=Ejendomsværdi")
                d = parse_listing_detail(html)
            except Exception as exc:
                print(f"  {l.get('address')}: failed ({exc})")
                continue
            for k, v in d.items():
                if v is not None and (k != "plot_m2" or not l.get("plot_m2")):
                    l[k] = v
            l["detail_checked"] = True
            print(f"  [{i}/{len(todo)}] {l.get('address')}: valuation {l.get('valuation')}, "
                  f"plot {l.get('plot_m2')}")
            time.sleep(DELAY_SECONDS)
    finally:
        fetcher.close()

    # 3) areas, scores and today's picks
    for u, l in listings.items():
        l["active"] = u in active
        l["area"] = assign_area(l["postcode"], l.get("lat"), l.get("lon"))
    pool = [listings[u] for u in active if listings[u].get("area")]
    score_listings(pool, bench)
    taste = apply_taste(listings, pool, stars)
    print(f"Taste: {taste}")

    def rank(items):
        return sorted(items, key=lambda l: (l.get("score") is None, -(l.get("score") or 0)))

    picked_before = {u for d, us in picks_by_day.items() if d != today for u in us}
    new = [l for l in pool if l["first_seen"] == today
           and (l.get("days_total") is None or l["days_total"] <= NEW_MAX_DAYS_ON_MARKET)]
    picks = rank(new)[:PICKS_MAX]
    for l in picks:
        l["pick_type"] = "new"
    if len(picks) < PICKS_MIN:
        week = (date.today() - timedelta(days=TOP_UP_DAYS)).isoformat()
        extra = [l for l in pool if l not in picks and l["first_seen"] >= week
                 and l["url"] not in picked_before]
        for l in rank(extra)[:PICKS_MIN - len(picks)]:
            l["pick_type"] = "earlier"
            picks.append(l)
    picks_by_day[today] = [l["url"] for l in picks]
    print(f"Today's picks: {len(picks)} ({len(new)} new listings today)")
    for l in picks:
        print(f"  {l.get('score')}: {l.get('address')} {l['price']:,} - {'; '.join(l['reasons'])}")

    # 4) tidy and save
    cutoff = (date.today() - timedelta(days=KEEP_INACTIVE_DAYS)).isoformat()
    listings = {u: l for u, l in listings.items()
                if l["active"] or l["last_seen"] >= cutoff or u in stars}   # starred kept for learning
    keep_days = sorted(picks_by_day)[-30:]
    out = {
        "updated": datetime.now(timezone.utc).isoformat(timespec="minutes"),
        "price_max": LISTING_PRICE_MAX,
        "benchmarks": bench,
        "taste": taste,
        "picks": {d: picks_by_day[d] for d in keep_days},
        "listings": listings,
    }
    LISTINGS_FILE.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"Saved {len(listings)} listings ({len(active)} for sale now)")


if __name__ == "__main__":
    main()
