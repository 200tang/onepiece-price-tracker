"""Daily buy-watch signals for One Piece singles.

Reads the saved daily prices (data/prices/*.csv), the card catalog, and recent
tournament decklists from Limitless TCG, and flags:

  low       - price at its lowest in the window and well below its average
  meta      - cards used in recent tournament decks, with the cheapest printing,
              the cost of a playset (4 copies) and how close it is to its low
  settling  - new-set cards whose price has fallen a lot since release and has
              now held steady for several days
  pullback  - cards that climbed strongly and have just dipped back a little

These are price facts, not predictions. Market price comes from recent sales and
can be noisy for cards that rarely sell.
"""
from __future__ import annotations

import csv
import datetime as dt
import json
import statistics
import sys
import time
from collections import defaultdict
from pathlib import Path

import requests

DATA = Path(__file__).resolve().parent.parent / "data"
USER_AGENT = "onepiece-price-tracker/1.0 (personal daily price tracker; GitHub Actions)"
LIMITLESS = "https://play.limitlesstcg.com/api"

PRICE_MIN, PRICE_MAX = 5.0, 200.0   # the range the owner wants alerts for
WINDOW = 60                         # days of history used for lows and averages
META_DAYS = 14                      # tournaments from the last two weeks
META_MIN_PLAYERS = 16
META_MAX_EVENTS = 40
NEW_SET_DAYS = 45
FEE_RATE, FEE_FIXED = 0.13, 0.30    # rough TCGplayer seller fees (commission + payment processing)


# ---------- data loading ----------

def load_history() -> tuple[list[str], dict[str, dict[str, float]]]:
    days = sorted(p.stem for p in (DATA / "prices").glob("*.csv"))
    hist = {}
    for d in days:
        with (DATA / "prices" / f"{d}.csv").open() as f:
            hist[d] = {f"{r['product_id']}|{r['printing']}": float(r["market_price"]) for r in csv.DictReader(f)}
    return days, hist


def load_catalog() -> dict[str, dict]:
    with (DATA / "catalog.csv").open() as f:
        return {r["product_id"]: r for r in csv.DictReader(f)}


def load_released() -> dict[str, str]:
    path = DATA / "groups.csv"
    if not path.exists():
        return {}
    with path.open() as f:
        return {r["group_id"]: r["released"] for r in csv.DictReader(f)}


# ---------- tournament meta ----------

def fetch_meta(today: dt.date) -> dict | None:
    s = requests.Session()
    s.headers["User-Agent"] = USER_AGENT

    def get(url):
        for attempt in range(3):
            r = s.get(url, timeout=60)
            if r.status_code == 429:
                time.sleep(int(r.headers.get("Retry-After", "30")))
                continue
            r.raise_for_status()
            if r.headers.get("X-RateLimit-Remaining") == "0":
                time.sleep(int(r.headers.get("X-RateLimit-Reset-After", r.headers.get("Retry-After", "30"))))
            return r.json()
        raise RuntimeError(f"rate limited: {url}")

    cutoff = today - dt.timedelta(days=META_DAYS)
    events = []
    for page in range(1, 8):
        batch = get(f"{LIMITLESS}/tournaments?game=OP&limit=100&page={page}")
        if not batch:
            break
        for t in batch:
            when = dt.date.fromisoformat(t["date"][:10])
            if when >= cutoff and (t.get("players") or 0) >= META_MIN_PLAYERS:
                events.append(t)
        if dt.date.fromisoformat(batch[-1]["date"][:10]) < cutoff:
            break
        time.sleep(1)
    events = sorted(events, key=lambda t: -t["players"])[:META_MAX_EVENTS]

    usage = defaultdict(lambda: {"name": "", "decks": 0, "copies": 0, "winning_decks": 0})
    leaders = defaultdict(int)
    decks = winning = 0
    for t in events:
        time.sleep(1.5)
        try:
            standings = get(f"{LIMITLESS}/tournaments/{t['id']}/standings")
        except Exception as e:  # one bad event shouldn't stop the rest
            print(f"skip {t['id']}: {e}", file=sys.stderr)
            continue
        for st in standings:
            dl = st.get("decklist")
            if not dl or not isinstance(dl, dict):
                continue
            rec = st.get("record") or {}
            won = (rec.get("wins") or 0) > (rec.get("losses") or 0)
            decks += 1
            winning += won
            lead = dl.get("leader")
            if lead:
                leaders[f"{lead['set']}-{lead['number']}|{lead['name']}"] += 1
            seen = set()
            for part in ("character", "event", "stage"):
                for c in dl.get(part) or []:
                    num = f"{c['set']}-{c['number']}"
                    u = usage[num]
                    u["name"] = c["name"]
                    u["copies"] += c.get("count", 1)
                    if num not in seen:
                        u["decks"] += 1
                        u["winning_decks"] += won
                        seen.add(num)
    if not decks:
        return None
    return {
        "since": cutoff.isoformat(), "events": len(events), "decks": decks, "winning_decks": winning,
        "cards": {k: {**v, "share": round(v["decks"] / decks * 100, 1),
                      "avg_copies": round(v["copies"] / v["decks"], 1)} for k, v in usage.items()},
        "leaders": sorted(([k.split("|")[0], k.split("|")[1], n] for k, n in leaders.items()), key=lambda x: -x[2])[:15],
    }


# ---------- signals ----------

def breakeven(price: float) -> tuple[float, float]:
    need = (price + FEE_FIXED) / (1 - FEE_RATE)
    return round(need, 2), round((need - price) / price * 100, 1)


WEEK_AGO: dict[str, float] = {}


def card_row(key, catalog, price, **extra):
    pid, printing = key.split("|", 1)
    c = catalog[pid]
    be, be_pct = breakeven(price)
    wk = WEEK_AGO.get(key)
    return {"card": c["name"], "number": c["number"], "set": c["set"], "rarity": c["rarity"],
            "printing": printing, "now": round(price, 2), "week_ago": round(wk, 2) if wk else None,
            "breakeven": be, "breakeven_pct": be_pct, "url": c["url"], **extra}


def compute(days, hist, catalog, released, meta):
    today = days[-1]
    now = hist[today]
    wk_day = max((d for d in days if d <= (dt.date.fromisoformat(today) - dt.timedelta(days=7)).isoformat()), default=None)
    WEEK_AGO.clear()
    WEEK_AGO.update(hist[wk_day] if wk_day else {})
    window_days = [d for d in days[:-1] if d >= (dt.date.fromisoformat(today) - dt.timedelta(days=WINDOW)).isoformat()]
    series = defaultdict(list)
    for d in window_days:
        for k, v in hist[d].items():
            series[k].append((d, v))

    def released_on(key):
        c = catalog.get(key.split("|")[0])
        return released.get(c["group_id"], "") if c and c.get("group_id") else ""

    new_cut = (dt.date.fromisoformat(today) - dt.timedelta(days=NEW_SET_DAYS)).isoformat()
    in_range = lambda p: PRICE_MIN <= p <= PRICE_MAX

    lows, settling, pullbacks = [], [], []
    for key, price in now.items():
        if key.split("|")[0] not in catalog:
            continue
        s = series.get(key, [])
        vals = [v for _, v in s]
        rel = released_on(key)
        is_new = bool(rel) and rel >= new_cut

        # New set settling: fell a lot since release, steady for the last 5 days.
        if is_new and in_range(price) and rel <= today:
            since = [v for d, v in s if d >= rel]
            if len(since) >= 7:
                peak = max(since)
                last5 = since[-4:] + [price]
                if peak >= 1.4 * price and max(last5) <= 1.08 * min(last5):
                    settling.append(card_row(key, catalog, price, peak=round(peak, 2),
                                             off_peak_pct=round((price - peak) / peak * 100, 1),
                                             released=rel, steady_days=5))
            continue  # new cards fall naturally; keep them out of the other lists

        if len(vals) < 40 or not in_range(price):
            continue
        lo, avg = min(vals), statistics.fmean(vals)

        # At its lowest in the window and well under its average.
        if price <= lo and price <= 0.8 * avg:
            lows.append(card_row(key, catalog, price, low=round(lo, 2), avg=round(avg, 2),
                                 vs_avg_pct=round((price - avg) / avg * 100, 1)))

        # Climbed strongly, then dipped back a little but stayed above where it started.
        start = statistics.fmean(vals[:5])
        recent = [v for d, v in s if d >= (dt.date.fromisoformat(today) - dt.timedelta(days=30)).isoformat()]
        if recent:
            peak30 = max(recent)
            if peak30 >= 1.3 * start and price <= 0.88 * peak30 and price >= 1.1 * start:
                pullbacks.append(card_row(key, catalog, price, start=round(start, 2), peak=round(peak30, 2),
                                          rise_pct=round((peak30 - start) / start * 100, 1),
                                          off_peak_pct=round((price - peak30) / peak30 * 100, 1)))

    lows.sort(key=lambda r: r["vs_avg_pct"])
    settling.sort(key=lambda r: r["off_peak_pct"])
    pullbacks.sort(key=lambda r: -r["rise_pct"])

    # Meta board: cheapest printing of each tournament card, with playset cost.
    board = []
    if meta:
        by_number = defaultdict(list)
        for k in now:
            c = catalog.get(k.split("|")[0])
            if c:
                by_number[c["number"]].append(k)
        for num, u in sorted(meta["cards"].items(), key=lambda kv: -kv[1]["decks"]):
            if u["share"] < 3 or u["decks"] < 10:
                break
            keys = by_number.get(num)
            if not keys:
                continue
            k = min(keys, key=lambda x: now[x])
            price = now[k]
            vals = [v for _, v in series.get(k, [])]
            lo = min(vals + [price])
            row = card_row(k, catalog, price, share=u["share"], avg_copies=u["avg_copies"],
                           playset=round(4 * price, 2), low=round(lo, 2),
                           vs_low_pct=round((price - lo) / lo * 100, 1) if lo else 0.0,
                           history_days=len(vals))
            row["at_low"] = len(vals) >= 20 and price <= 1.05 * lo
            board.append(row)
            if len(board) >= 40:
                break

    meta_lows = [r for r in board if r["at_low"] and r["now"] >= PRICE_MIN]
    return {
        "date": today, "window_days": len(window_days), "price_range": [PRICE_MIN, PRICE_MAX],
        "fees": {"rate": FEE_RATE, "fixed": FEE_FIXED},
        "meta": None if not meta else {k: meta[k] for k in ("since", "events", "decks", "winning_decks", "leaders")},
        "low": lows[:15], "meta_low": meta_lows[:15], "settling": settling[:15], "pullback": pullbacks[:10],
        "meta_board": board,
    }


def write_outputs(result: dict) -> None:
    sdir = DATA / "signals"
    sdir.mkdir(parents=True, exist_ok=True)
    for name in (f"{result['date']}.json", "latest.json"):
        (sdir / name).write_text(json.dumps(result, indent=1))
    rows = []
    for sig in ("low", "meta_low", "settling", "pullback", "meta_board"):
        for i, r in enumerate(result[sig], 1):
            rows.append({"key": f"{sig}-{i}", "signal": sig, "rank": i, **r})
    ddir = DATA / "dashboard"
    ddir.mkdir(parents=True, exist_ok=True)
    (ddir / "signals.json").write_text(json.dumps(rows))
    m = result["meta"] or {}
    (ddir / "signals_summary.json").write_text(json.dumps([{
        "date": result["date"], "window_days": result["window_days"],
        "price_min": result["price_range"][0], "price_max": result["price_range"][1],
        "fee_rate_pct": round(result["fees"]["rate"] * 100), "fee_fixed": result["fees"]["fixed"],
        "meta_since": m.get("since"), "meta_events": m.get("events"), "meta_decks": m.get("decks"),
    }]))


def main() -> int:
    days, hist = load_history()
    catalog, released = load_catalog(), load_released()
    try:
        meta = fetch_meta(dt.date.fromisoformat(days[-1]))
    except Exception as e:
        print(f"Tournament data unavailable today: {e}", file=sys.stderr)
        meta = None
    if meta:
        mdir = DATA / "meta"
        mdir.mkdir(parents=True, exist_ok=True)
        (mdir / "latest.json").write_text(json.dumps(meta, indent=1))
    result = compute(days, hist, catalog, released, meta)
    write_outputs(result)
    print(f"{result['date']}: low {len(result['low'])}, meta_low {len(result['meta_low'])}, "
          f"settling {len(result['settling'])}, pullback {len(result['pullback'])}, meta board {len(result['meta_board'])}"
          + (f" from {meta['decks']} decks in {meta['events']} events" if meta else " (no tournament data)"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
