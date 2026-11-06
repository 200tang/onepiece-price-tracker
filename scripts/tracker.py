"""Daily One Piece TCG singles price tracker.

Downloads TCGplayer market prices for every One Piece Card Game single from
TCGCSV (https://tcgcsv.com, which republishes TCGplayer prices once a day around
20:00 UTC), saves a daily snapshot, and works out the biggest movers since the
previous day and over the past week.

Outputs (all under data/):
  catalog.csv                 every single: product id, name, set, number, rarity, link
  prices/YYYY-MM-DD.csv       market price of every single on that day
  latest.csv                  spreadsheet: every single with today's, yesterday's and
                              last week's price and the changes
  movers/latest.json          top risers/fallers (read by the daily alert + dashboard)
  movers/YYYY-MM-DD.json      the same, kept per day
"""
from __future__ import annotations

import csv
import datetime as dt
import io
import json
import os
import sys
import tempfile
import time
from pathlib import Path

import requests

CATEGORY = 68  # One Piece Card Game on TCGplayer
BASE = "https://tcgcsv.com"
USER_AGENT = "onepiece-price-tracker/1.0 (personal daily price tracker; GitHub Actions)"
DATA = Path(__file__).resolve().parent.parent / "data"
MIN_PRICE = 2.0      # % lists ignore cards under this price on either day (bulk noise)
MIN_CHANGE = 0.50    # ...and moves smaller than this many dollars
TOP_PCT = 15
TOP_USD = 10

session = requests.Session()
session.headers["User-Agent"] = USER_AGENT


def get_json(path: str):
    for attempt in range(4):
        r = session.get(BASE + path, timeout=60)
        if r.status_code == 200:
            try:
                return r.json()
            except ValueError:
                pass
        time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"Could not fetch {path}: HTTP {r.status_code} {r.text[:120]!r}")


def fetch_today() -> tuple[str, dict, dict]:
    """Return (date, catalog, prices) where prices maps 'productId|printing' -> market price."""
    updated = session.get(BASE + "/last-updated.txt", timeout=30).text.strip()
    date = updated[:10]
    groups = get_json(f"/tcgplayer/{CATEGORY}/groups")["results"]
    catalog, prices = {}, {}
    for g in groups:
        gid = g["groupId"]
        set_code = g.get("abbreviation") or g["name"]
        for p in get_json(f"/tcgplayer/{CATEGORY}/{gid}/products")["results"]:
            ext = {e["name"]: e["value"] for e in p.get("extendedData") or []}
            if not ext.get("Number"):
                continue  # sealed product, not a single card
            catalog[str(p["productId"])] = {
                "name": p["name"], "set": set_code, "set_name": g["name"],
                "number": ext["Number"], "rarity": ext.get("Rarity", ""), "url": p["url"],
            }
        time.sleep(0.2)
        for r in get_json(f"/tcgplayer/{CATEGORY}/{gid}/prices")["results"]:
            pid = str(r["productId"])
            if pid in catalog and r.get("marketPrice") is not None:
                prices[f"{pid}|{r['subTypeName']}"] = r["marketPrice"]
        time.sleep(0.2)
    return date, catalog, prices


def fetch_archive(date: str) -> dict | None:
    """Prices for a past day from TCGCSV's daily archive (used to backfill history)."""
    import py7zr

    r = session.get(f"{BASE}/archive/tcgplayer/prices-{date}.ppmd.7z", timeout=300)
    if r.status_code != 200:
        return None
    prices = {}
    with tempfile.TemporaryDirectory() as tmp:
        with py7zr.SevenZipFile(io.BytesIO(r.content)) as z:
            wanted = [n for n in z.getnames() if n.startswith(f"{date}/{CATEGORY}/") and n.endswith("/prices")]
            z.extract(path=tmp, targets=wanted)
        for name in wanted:
            for row in json.loads(Path(tmp, name).read_text())["results"]:
                if row.get("marketPrice") is not None:
                    prices[f"{row['productId']}|{row['subTypeName']}"] = row["marketPrice"]
    return prices


def save_prices(date: str, prices: dict) -> None:
    out = DATA / "prices" / f"{date}.csv"
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["product_id", "printing", "market_price"])
        for key in sorted(prices):
            pid, sub = key.split("|", 1)
            w.writerow([pid, sub, prices[key]])


def load_prices(date: str) -> dict | None:
    path = DATA / "prices" / f"{date}.csv"
    if not path.exists():
        return None
    with path.open() as f:
        return {f"{r['product_id']}|{r['printing']}": float(r["market_price"]) for r in csv.DictReader(f)}


def prices_on_or_before(date: str, days_back: int, allow_fetch: bool) -> tuple[str, dict] | tuple[None, None]:
    """Find the saved (or archived) snapshot nearest to `days_back` days before `date`."""
    base = dt.date.fromisoformat(date)
    for extra in range(4):
        d = (base - dt.timedelta(days=days_back + extra)).isoformat()
        p = load_prices(d)
        if p is None and allow_fetch:
            p = fetch_archive(d)
            if p:
                save_prices(d, p)
        if p:
            return d, p
    return None, None


def movers(now: dict, before: dict, catalog: dict) -> dict:
    rows = []
    for key, price in now.items():
        old = before.get(key)
        if not old or old <= 0:
            continue
        pid, sub = key.split("|", 1)
        c = catalog[pid]
        rows.append({
            "card": c["name"], "number": c["number"], "set": c["set"], "rarity": c["rarity"],
            "printing": sub, "was": round(old, 2), "now": round(price, 2),
            "change": round(price - old, 2), "change_pct": round((price - old) / old * 100, 1),
            "url": c["url"],
        })
    pct_pool = [r for r in rows if min(r["was"], r["now"]) >= MIN_PRICE and abs(r["change"]) >= MIN_CHANGE]
    return {
        "compared": len(rows),
        "gain": sorted([r for r in pct_pool if r["change"] > 0], key=lambda r: -r["change_pct"])[:TOP_PCT],
        "drop": sorted([r for r in pct_pool if r["change"] < 0], key=lambda r: r["change_pct"])[:TOP_PCT],
        "gain_usd": sorted([r for r in rows if r["change"] > 0], key=lambda r: -r["change"])[:TOP_USD],
        "drop_usd": sorted([r for r in rows if r["change"] < 0], key=lambda r: r["change"])[:TOP_USD],
    }


def build_outputs(date: str, catalog: dict, now: dict, allow_fetch: bool) -> dict:
    d1, p1 = prices_on_or_before(date, 1, allow_fetch)
    d7, p7 = prices_on_or_before(date, 7, allow_fetch)
    result = {
        "date": date,
        "singles_priced": len(now),
        "rules": {"min_price": MIN_PRICE, "min_change": MIN_CHANGE},
        "day": dict(since=d1, **movers(now, p1, catalog)) if p1 else None,
        "week": dict(since=d7, **movers(now, p7, catalog)) if p7 else None,
        "most_valuable": [
            {"card": catalog[k.split("|")[0]]["name"], "number": catalog[k.split("|")[0]]["number"],
             "set": catalog[k.split("|")[0]]["set"], "rarity": catalog[k.split("|")[0]]["rarity"],
             "printing": k.split("|", 1)[1], "now": round(v, 2), "url": catalog[k.split("|")[0]]["url"]}
            for k, v in sorted(now.items(), key=lambda kv: -kv[1])[:10]
        ],
    }
    mdir = DATA / "movers"
    mdir.mkdir(parents=True, exist_ok=True)
    for name in (f"{date}.json", "latest.json"):
        (mdir / name).write_text(json.dumps(result, indent=1))

    # Full spreadsheet of every single.
    with (DATA / "latest.csv").open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["card", "number", "set", "rarity", "printing", "price_today",
                    f"price_{d1 or 'prev_day'}", "change_1d", "change_1d_pct",
                    f"price_{d7 or 'week_ago'}", "change_7d", "change_7d_pct", "tcgplayer_link"])
        for key in sorted(now, key=lambda k: (catalog[k.split('|')[0]]["set"], catalog[k.split('|')[0]]["number"])):
            pid, sub = key.split("|", 1)
            c, p = catalog[pid], now[key]
            o1 = (p1 or {}).get(key)
            o7 = (p7 or {}).get(key)
            ch = lambda o: ("", "") if not o else (round(p - o, 2), round((p - o) / o * 100, 1))
            w.writerow([c["name"], c["number"], c["set"], c["rarity"], sub, p,
                        o1 or "", *ch(o1), o7 or "", *ch(o7), c["url"]])
    return result


def save_catalog(catalog: dict) -> None:
    with (DATA / "catalog.csv").open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["product_id", "name", "set", "set_name", "number", "rarity", "url"])
        for pid, c in sorted(catalog.items(), key=lambda kv: int(kv[0])):
            w.writerow([pid, c["name"], c["set"], c["set_name"], c["number"], c["rarity"], c["url"]])


def main() -> int:
    DATA.mkdir(exist_ok=True)
    date, catalog, now = fetch_today()
    if len(now) < 1000:
        print(f"Only {len(now)} prices came back; not saving a partial day.", file=sys.stderr)
        return 1
    save_catalog(catalog)
    save_prices(date, now)
    result = build_outputs(date, catalog, now, allow_fetch=True)
    print(f"{date}: {len(now)} singles priced; "
          f"day vs {result['day'] and result['day']['since']}, week vs {result['week'] and result['week']['since']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
