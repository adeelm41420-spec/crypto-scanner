#!/usr/bin/env python3
"""
Crypto Scanner (mobile alerts via Telegram)
-------------------------------------------
- Market cap $500M se upar ke altcoins (CoinGecko), stablecoins/wrapped tokens aur BTC nahi.
- Daily candle close par support/resistance (swing levels) nikalta hai (candles: Crypto.com).
- Coin news RSS feeds se (CoinDesk, Cointelegraph, Decrypt).
- Alerts Telegram par (phone par notification).

Chalane ka tareeqa:
    python scanner.py daily     # daily close ke baad: poora scan + news
    python scanner.py hourly    # har ghante: level cross hone par alert + nayi news
Environment variables: TELEGRAM_TOKEN, TELEGRAM_CHAT_ID (zaroori), COINGECKO_KEY (optional)
"""
import json, os, re, sys, time, urllib.parse, urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime

TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
CHAT = os.environ.get("TELEGRAM_CHAT_ID", "")
CG_KEY = os.environ.get("COINGECKO_KEY", "")

MIN_MCAP = 500_000_000
MAX_COINS = 150
NEAR_PCT = 1.5        # level se itna % qareeb = alert
CLUSTER_PCT = 1.5     # itne qareeb ke levels ek zone
PIVOT_K = 3           # swing low/high: dono taraf 3 candles
EXCLUDE = {"BTC"}
SKIP_NAME = re.compile(r"usd|tether|wrapped|staked|steth|bridged|dollar|euro|gold|peg", re.I)
FEEDS = ["https://www.coindesk.com/arc/outboundfeeds/rss/",
         "https://cointelegraph.com/rss",
         "https://decrypt.co/feed"]
CDC = "https://api.crypto.com/exchange/v1/public/get-candlestick"


def get(url, headers=None, tries=3):
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "crypto-scanner", **(headers or {})})
            with urllib.request.urlopen(req, timeout=25) as r:
                return r.read()
        except Exception:
            if i == tries - 1:
                raise
            time.sleep(4)


def send(text):
    print(text, "\n")
    if not (TOKEN and CHAT):
        return
    chunk = ""
    for line in text.split("\n") + [None]:
        if line is None or len(chunk) + len(line) > 3500:
            if chunk:
                body = urllib.parse.urlencode({"chat_id": CHAT, "text": chunk,
                                               "disable_web_page_preview": "true"}).encode()
                try:
                    urllib.request.urlopen(urllib.request.Request(
                        f"https://api.telegram.org/bot{TOKEN}/sendMessage", data=body), timeout=20).read()
                except Exception as e:
                    print("Telegram error:", e)
            chunk = ""
        if line is not None:
            chunk += line + "\n"


# ---------- coins ----------
def top_coins():
    rows = []
    for page in (1, 2):
        qs = urllib.parse.urlencode({"vs_currency": "usd", "order": "market_cap_desc",
                                     "per_page": 250, "page": page})
        h = {"x-cg-demo-api-key": CG_KEY} if CG_KEY else {}
        got = json.loads(get("https://api.coingecko.com/api/v3/coins/markets?" + qs, h))
        rows += got
        if not got or (got[-1].get("market_cap") or 0) < MIN_MCAP:
            break
        time.sleep(3)
    out = []
    for r in rows:
        sym, mc = r["symbol"].upper(), r.get("market_cap") or 0
        if mc >= MIN_MCAP and sym not in EXCLUDE and not SKIP_NAME.search(r["name"] + " " + sym):
            out.append({"sym": sym, "name": r["name"], "mcap": mc})
    return out[:MAX_COINS]


def candles(sym, tf, count):
    qs = urllib.parse.urlencode({"instrument_name": f"{sym}_USDT", "timeframe": tf, "count": count})
    rows = json.loads(get(f"{CDC}?{qs}", tries=2))["result"]["data"]
    out = [{"t": int(r["t"]) / 1000, "low": float(r.get("l", r.get("low"))),
            "high": float(r.get("h", r.get("high"))), "close": float(r.get("c", r.get("close")))}
           for r in rows]
    return sorted(out, key=lambda c: c["t"])


# ---------- support / resistance ----------
def pivots(c, k, key, pick):
    v = [x[key] for x in c]
    return [v[i] for i in range(k, len(v) - k) if v[i] == pick(v[i - k:i + k + 1])]


def cluster(vals, tol=CLUSTER_PCT):
    zs = []
    for x in sorted(vals):
        if zs and (x - zs[-1][0] / zs[-1][1]) / x * 100 <= tol:
            zs[-1][0] += x
            zs[-1][1] += 1
        else:
            zs.append([x, 1])
    return [(s / n, n) for s, n in zs]


def get_levels(closed):
    return (cluster(pivots(closed, PIVOT_K, "low", min)),
            cluster(pivots(closed, PIVOT_K, "high", max)))


def fmt(p):
    return f"{p:,.5f}" if p < 1 else f"{p:,.4f}" if p < 10 else f"{p:,.2f}"


def money(m):
    return f"${m/1e9:.1f}B" if m >= 1e9 else f"${m/1e6:.0f}M"


def daily_events(closed):
    """Aakhri band hui daily candle ke hisaab se events."""
    sup, res = get_levels(closed)
    p, q = closed[-1]["close"], closed[-2]["close"]
    ev = []
    broke = [(s, n) for s, n in sup if q >= s > p]
    if broke:
        ev.append(("breakdown",) + max(broke))
    else:
        near = [(s, n) for s, n in sup if 0 <= (p - s) / s * 100 <= NEAR_PCT]
        if near:
            ev.append(("support",) + max(near))
    up = [(r, n) for r, n in res if q <= r < p]
    if up:
        ev.append(("breakout",) + max(up))
    else:
        near = [(r, n) for r, n in res if 0 <= (r - p) / p * 100 <= NEAR_PCT]
        if near:
            ev.append(("resistance",) + min(near))
    return p, ev


# ---------- news ----------
def fetch_news():
    items = []
    for url in FEEDS:
        try:
            root = ET.fromstring(get(url))
        except Exception as e:
            print("Feed error:", url, e)
            continue
        for it in root.iter("item"):
            try:
                d = parsedate_to_datetime(it.findtext("pubDate"))
                if d.tzinfo is None:
                    d = d.replace(tzinfo=timezone.utc)
            except Exception:
                continue
            items.append((d, (it.findtext("title") or "").strip(), (it.findtext("link") or "").strip()))
    return sorted(items, reverse=True)


def news_for(coin, items, since):
    name = re.compile(r"\b" + re.escape(coin["name"]) + r"\b", re.I)
    sym = re.compile(r"\b" + re.escape(coin["sym"]) + r"\b") if len(coin["sym"]) >= 3 else None
    return [(t, l) for d, t, l in items if d >= since and (name.search(t) or (sym and sym.search(t)))]


# ---------- modes ----------
def run_daily(coins, items, today):
    groups = {"breakdown": [], "support": [], "breakout": [], "resistance": []}
    scanned = skipped = 0
    for c in coins:
        try:
            closed = [x for x in candles(c["sym"], "1D", 300) if x["t"] < today]
            if len(closed) < 40:
                skipped += 1
                continue
            p, ev = daily_events(closed)
            scanned += 1
            for kind, lv, n in ev:
                dist = (p - lv) / lv * 100
                nws = news_for(c, items, datetime.now(timezone.utc) - timedelta(hours=48))
                tail = ("\n   📰 " + nws[0][0][:110]) if nws else ""
                groups[kind].append(f"{c['sym']} ({money(c['mcap'])}) close ${fmt(p)} | level ${fmt(lv)} "
                                    f"({dist:+.1f}%, {n}x test){tail}")
        except Exception:
            skipped += 1
        time.sleep(0.15)
    titles = {"breakdown": "🔴 Support daily close par TOOTA", "support": "🟢 Support ke qareeb",
              "breakout": "🚀 Resistance daily close par TOOTA (breakout)", "resistance": "🔵 Resistance ke qareeb"}
    msg = [f"📊 DAILY CLOSE SCAN ({datetime.now(timezone.utc):%d %b %Y})",
           f"{scanned} coins scan hue (mcap > $500M), {skipped} skip (Crypto.com par nahi/kam data)", ""]
    for k in ("breakdown", "support", "breakout", "resistance"):
        if groups[k]:
            msg += [titles[k]] + groups[k] + [""]
    if not any(groups.values()):
        msg.append("Aaj koi coin level ke qareeb nahi mila.")
    msg.append("Ye buy/sell signal nahi hai. Chart khud confirm karein.")
    send("\n".join(msg))
    since = datetime.now(timezone.utc) - timedelta(hours=24)
    lines = []
    for c in coins:
        for t, l in news_for(c, items, since)[:1]:
            lines.append(f"{c['sym']}: {t}\n{l}")
    if lines:
        send("📰 NEWS (24 ghante, tracked coins)\n\n" + "\n\n".join(lines[:15]))


def run_hourly(coins, items, today):
    alerts = []
    for c in coins:
        try:
            closed = [x for x in candles(c["sym"], "1D", 300) if x["t"] < today]
            if len(closed) < 40:
                continue
            h = candles(c["sym"], "1h", 5)
            q, p = h[-2]["close"], h[-1]["close"]
            sup, res = get_levels(closed)
            for s, n in sup:
                if q >= s > p:
                    alerts.append(f"🔴 {c['sym']} support ${fmt(s)} ({n}x) ke neechay gaya | ab ${fmt(p)}")
            for r, n in res:
                if q <= r < p:
                    alerts.append(f"🚀 {c['sym']} resistance ${fmt(r)} ({n}x) ke upar gaya | ab ${fmt(p)}")
        except Exception:
            pass
        time.sleep(0.15)
    if alerts:
        send("⚡ LEVEL CROSS (pichla ghanta)\n" + "\n".join(alerts))
    since = datetime.now(timezone.utc) - timedelta(minutes=75)
    lines = []
    for c in coins:
        for t, l in news_for(c, items, since)[:1]:
            lines.append(f"{c['sym']}: {t}\n{l}")
    if lines:
        send("📰 NAYI NEWS\n\n" + "\n\n".join(lines[:10]))


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "daily"
    now = datetime.now(timezone.utc)
    today = datetime(now.year, now.month, now.day, tzinfo=timezone.utc).timestamp()
    coins, items = top_coins(), fetch_news()
    (run_daily if mode == "daily" else run_hourly)(coins, items, today)


if __name__ == "__main__":
    main()
