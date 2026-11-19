#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
NASDAQ Daily Scanner
====================
סורק את המניות הכי נוזליות בנאסדק ומפיק המלצות עסקאות יומיות (סווינג 1-5 ימי מסחר)
מאסטרטגיית מגמה/מומנטום מרובת-אותות:
    מחיר מול SMA50/SMA200  +  RSI(14)  +  MACD(12,26,9)  +  אישור מחזור  +  מבנה מחיר

מקור נתונים: Nasdaq Public API (סריקה + היסטוריה). ללא מפתח API.
תלויות: requests בלבד (stdlib בשאר). Python 3.7+.

שימוש:
    python scanner.py                       # סריקה מלאה + דוח יומי
    python scanner.py --universe 100        # סריקה מהירה לבדיקה
    python scanner.py --refresh             # דלג על מטמון
    python scanner.py --watch --interval 1800   # מעקב שוטף בשעות המסחר
"""

import argparse
import datetime as dt
import json
import math
import os
import re
import sys
import threading
import time

import requests

# זמן ישראל — הענן (GitHub Actions) רץ ב־UTC, ולכן האזור מוגדר כאן במפורש.
try:
    from zoneinfo import ZoneInfo
    _IL_TZ = ZoneInfo("Asia/Jerusalem")
except Exception:  # pragma: no cover - Windows ללא חבילת tzdata
    _IL_TZ = None


def israel_now():
    """שעון ישראל אמיתי (כולל שעון קיץ/חורף), בלי תלות באזור החיצוני."""
    return dt.datetime.now(_IL_TZ) if _IL_TZ else dt.datetime.now()

# --------------------------------------------------------------------------
# הגדרות
# --------------------------------------------------------------------------
CONFIG = {
    "universe_size": 2000,       # כמה מניות לסרוק (לפי מחזור דולרי)
    "min_price": 5.0,            # מחיר מינימלי
    "min_market_cap": 500e6,     # שווי שוק מינימלי
    "min_dollar_volume": 20e6,   # מחזור דולרי יומי מינימלי
    "etf_min_price": 5.0,        # מחיר מינימלי לתעודות סל
    "history_days": 500,         # כמה ימי מסחר היסטוריה לדרוש
    "history_limit": 300,
    "cache_ttl": 3 * 3600,       # זמן תוקף מטמון (שניות)
    "etf_universe_file": "etf_universe.json",
    "etf_universe_ttl": 7 * 86400,  # אוניברסום תעודות סל נבנה פעם בשבוע
    "workers": 12,               # מספר בקשות מקביליות
    "timeout": 12,               # timeout לבקשה (שניות)
    "backoff": 1.2,              # מכפיל המתנה בין נסיונות חוזרים
    "top_n": 15,                 # כמה המלצות להציג בדוח
    "score_threshold": 10,       # סף ציון להמלצה
    "atr_stop_mult": 1.5,        # סטופ = 1.5 * ATR
    "reward_risk": 2.0,          # יעד = 2R
    "risk_per_trade": 0.01,      # סיכון 1% מהתיק לעסקה
    "portfolio_examples": [10000, 100000],
    "out_dir": "reports",
    "cache_dir": "cache",
    "watchlist_file": "watchlist.json",
    "favorites_min_score": 12,   # סף ציון למועדפים מול הפתיחה
    "favorites_limit": 20,       # גודל רשימת המועדפים
    "favorites_max_per_sector": 4,  # מגוון: לא יותר מ־4 מאותו סקטור
    # שער איתקנות: מועדפים מובילים רק כאלה שיש להם מגמה אמיתית (ADX) —
    # מסנן עסקאות רכות שצורכות מחזור אבל אין להן כיוון.
    "favorites_min_adx": 25.0,
    # --- מסנני איכות סיגנל (שיפור אסטרטגיה) ---
    "adx_min": 20.0,             # ADX מינימלי — עוצמת מגמה (מסנן שוק לטיני/רוח)
    "max_atr_pct": 8.0,          # ATR% מקסימלי — תנודתיות סבירה (מסנן סיכון מוגזם)
    "vol_trend_days": 5,         # מחזור 5 ימים מול 20 — אישור מחזור עולה
    "regime_enabled": True,      # מסנן מצב שוק (לא לסרוק נגד המגמה הרחבה)
    "regime_symbol": "SPY",      # סימבול מצב שוק
}

BASE = "https://api.nasdaq.com/api"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

# מניעת סריקה של מניות לא רצויות (מועדונים, אגרות, כתרים, קרנות, יחידות)
BAD_NAME_PATTERNS = [
    re.compile(r"\bpreferred\b"), re.compile(r"\bwarrants?\b"),
    re.compile(r"\bdepositary\b"), re.compile(r"\bfund"), re.compile(r"\btrusts?\b"),
    re.compile(r"\betfs?\b"), re.compile(r"\bnote"), re.compile(r"\bunits?\b"),
    re.compile(r"\brights?\b"), re.compile(r"\bbond"), re.compile(r"\btreasur"),
    re.compile(r"\bindex"), re.compile(r"\bacquisitions?\b"), re.compile(r"\bspacs?\b"),
]

_tls = threading.local()


def session():
    """requests.Session לכל חוט (thread-safe)."""
    s = getattr(_tls, "s", None)
    if s is None:
        s = requests.Session()
        s.headers.update({
            "User-Agent": UA,
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "en-US,en;q=0.9",
        })
        _tls.s = s
    return s


# --------------------------------------------------------------------------
# עזרים כלליים
# --------------------------------------------------------------------------
def parse_money(s):
    """ממיר '$1,234.56' / '1234' / '' ל-float (או None)."""
    if s is None:
        return None
    s = str(s).replace("$", "").replace(",", "").replace("%", "").strip()
    if s in ("", "null", "None", "--"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def get_json(url, params=None, retries=5, timeout=None, backoff=None):
    """בקשה עם נסיונות חוזרים על כישלון/ריתוק."""
    timeout = timeout or CONFIG.get("timeout", 12)
    backoff = backoff or CONFIG.get("backoff", 1.2)
    for attempt in range(retries):
        try:
            r = session().get(url, params=params, timeout=timeout)
            if r.status_code == 200:
                return r.json()
            if r.status_code in (429, 500, 502, 503, 504):
                time.sleep(backoff ** attempt)
                continue
            return None
        except Exception:
            time.sleep(backoff ** attempt)
    return None


def fmt_money(v, dec=2):
    if v is None:
        return "-"
    if abs(v) >= 1e9:
        return f"${v/1e9:.2f}B"
    if abs(v) >= 1e6:
        return f"${v/1e6:.1f}M"
    return f"${v:,.{dec}f}"


def fmt_pct(v, dec=2):
    if v is None:
        return "-"
    return f"{v:+.{dec}f}%"


# --------------------------------------------------------------------------
# שליפת נתונים
# --------------------------------------------------------------------------
def company_key(name):
    """מפתח חברה לדה-דופליקציה (GOOGL/GOOG, FOXA/FOX וכו')."""
    n = name.lower()
    n = re.split(r"\bclass\b|\bcommon\b|\binc\b|\bcorp\b|\bcorporation\b|"
                 r"\bltd\b|\bplc\b|\bholdings\b|\bco\b", n)[0]
    n = re.sub(r"[^a-z0-9 ]", " ", n)
    return re.sub(r"\s+", " ", n).strip()


def fetch_stocks(limit=None):
    """כל המניות האמריקאיות (NASDAQ + NYSE + אחרות), מסוננות ומדורגות לפי מחזור דולרי."""
    url = f"{BASE}/screener/stocks"
    params = {"tableonly": "true", "limit": "1000", "offset": "0", "download": "true"}
    d = get_json(url, params)
    if not d or not d.get("data") or not d["data"].get("rows"):
        return []
    rows = d["data"]["rows"]
    out = []
    for r in rows:
        sym = (r.get("symbol") or "").strip()
        name = (r.get("name") or "").strip()
        price = parse_money(r.get("lastsale"))
        vol = parse_money(r.get("volume"))
        mcap = parse_money(r.get("marketCap"))
        if not sym or not re.fullmatch(r"[A-Z]{1,5}(\.[A-Z])?", sym):
            continue
        low = name.lower()
        if any(p.search(low) for p in BAD_NAME_PATTERNS):
            continue
        if price is None or vol is None or price < CONFIG["min_price"]:
            continue
        if mcap is not None and mcap < CONFIG["min_market_cap"]:
            continue
        dollar_vol = price * vol
        if dollar_vol < CONFIG["min_dollar_volume"]:
            continue
        out.append({
            "symbol": sym,
            "name": name,
            "sector": (r.get("sector") or "").strip(),
            "industry": (r.get("industry") or "").strip(),
            "price": price,
            "change_pct": parse_money(r.get("pctchange")),
            "market_cap": mcap,
            "volume": vol,
            "dollar_volume": dollar_vol,
            "assetclass": "stocks",
        })
    out.sort(key=lambda x: x["dollar_volume"], reverse=True)
    # דה-דופליקציה לפי חברה — נשמר המחזור הנוזל יותר (BRK.B/BRK.A, GOOGL/GOOG)
    seen = set()
    deduped = []
    for r in out:
        key = company_key(r["name"])
        if key and key in seen:
            continue
        seen.add(key)
        deduped.append(r)
    return deduped[: (limit or CONFIG["universe_size"])]


def fetch_etf_candidates():
    """תעודות סל מסדרת ה-ETF של Nasdaq (ללא נתוני נזילות — מסוננות בהמשך)."""
    url = f"{BASE}/screener/etf"
    params = {"tableonly": "true", "limit": "1000", "offset": "0", "download": "true"}
    d = get_json(url, params)
    if not d or not d.get("data"):
        return []
    rows = (d["data"].get("data") or {}).get("rows") or []
    out = []
    for r in rows:
        sym = (r.get("symbol") or "").strip()
        name = (r.get("companyName") or "").strip()
        price = parse_money(r.get("lastSalePrice"))
        chg = parse_money(r.get("percentageChange"))
        if not sym or not re.fullmatch(r"[A-Z]{1,5}", sym):
            continue
        if price is None or price < CONFIG["etf_min_price"]:
            continue
        out.append({
            "symbol": sym, "name": name, "sector": "ETF", "industry": "",
            "price": price, "change_pct": chg, "market_cap": None,
            "volume": 0, "dollar_volume": 0, "assetclass": "etf",
        })
    return out


def fetch_etf_universe(refresh=False, limit=None):
    """אוניברסום תעודות הסל: נבנה פעם אחת (נזילות לפי היסטוריה) ונשמר במטמון.
    המחירים השוטפים מתעדכנים בכל הרצה מהסריקה."""
    cache = CONFIG["etf_universe_file"]
    items = []
    if not refresh and os.path.exists(cache):
        try:
            with open(cache, "r", encoding="utf-8") as f:
                data = json.load(f)
            if time.time() - data.get("ts", 0) < CONFIG["etf_universe_ttl"]:
                items = data["items"]
        except Exception:
            items = []
    if not items:
        cands = fetch_etf_candidates()
        built = []

        def work(row):
            hist = fetch_history(row["symbol"], refresh=False, assetclass="etf")
            if not hist:
                return None
            dv = hist[-1]["close"] * hist[-1]["volume"]
            if dv < CONFIG["min_dollar_volume"]:
                return None
            r = dict(row)
            r["dollar_volume"] = dv
            r["volume"] = hist[-1]["volume"]
            return r

        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=CONFIG["workers"]) as ex:
            for r in ex.map(work, cands):
                if r:
                    built.append(r)
        built.sort(key=lambda x: x["dollar_volume"], reverse=True)
        try:
            with open(cache, "w", encoding="utf-8") as f:
                json.dump({"ts": time.time(), "items": built}, f, ensure_ascii=False, indent=2)
        except Exception:
            pass
        items = built
    # עדכון מחירים שוטפים מהסריקה (המטמון עשוי להכיל מחירים ישנים)
    live = {r["symbol"]: r for r in fetch_etf_candidates()}
    for r in items:
        l = live.get(r["symbol"])
        if l:
            r["price"] = l["price"]
            r["change_pct"] = l["change_pct"]
    if limit:
        items = items[:limit]
    return items


def cache_path(sym, assetclass="stocks"):
    prefix = "" if assetclass == "stocks" else f"{assetclass}_"
    return os.path.join(CONFIG["cache_dir"], f"{prefix}{sym}.json")


def fetch_history(sym, refresh=False, assetclass="stocks"):
    """היסטוריית יומי למניה/תעודת סל, עם מטמון על הדיסק."""
    cp = cache_path(sym, assetclass)
    if not refresh and os.path.exists(cp):
        try:
            with open(cp, "r", encoding="utf-8") as f:
                c = json.load(f)
            if time.time() - c.get("ts", 0) < CONFIG["cache_ttl"]:
                return c["data"]
        except Exception:
            pass

    end = dt.date.today()
    start = end - dt.timedelta(days=CONFIG["history_days"])
    url = f"{BASE}/quote/{sym}/historical"
    params = {
        "assetclass": assetclass,
        "fromdate": start.isoformat(),
        "todate": end.isoformat(),
        "limit": str(CONFIG["history_limit"]),
    }
    d = get_json(url, params)
    if not d or not d.get("data"):
        return None
    table = d["data"].get("tradesTable") or {}
    rows = table.get("rows") or []
    hist = []
    for r in rows:
        c = parse_money(r.get("close"))
        o = parse_money(r.get("open"))
        h = parse_money(r.get("high"))
        l = parse_money(r.get("low"))
        v = parse_money(r.get("volume"))
        if c is None or v is None:
            continue
        hist.append({
            "date": r.get("date"),
            "open": o, "high": h, "low": l, "close": c,
            "volume": v or 0,
        })
    hist.reverse()  # הכן מהישן לחדש
    if len(hist) < 210:
        return None
    try:
        os.makedirs(CONFIG["cache_dir"], exist_ok=True)
        with open(cp, "w", encoding="utf-8") as f:
            json.dump({"ts": time.time(), "data": hist}, f)
    except Exception:
        pass
    return hist


def fetch_market_status():
    d = get_json(f"{BASE}/quote/QQQ/info", {"assetclass": "etf"})
    if not d or not d.get("data"):
        return "Unknown", None
    data = d["data"]
    return data.get("marketStatus") or "Unknown", data.get("primaryData", {}).get("lastTradeTimestamp")


def fetch_regime(symbol=None, refresh=False):
    """מצב שוק לפי SPY: bull / bear / neutral — מגמת השוק הרחב.

    bull  : מחיר מעל SMA50 ו-SMA50 מעל SMA200 (מגמת עלייה)
    bear  : מחיר מתחת ל-SMA50 ו-SMA50 מתחת ל-SMA200 (מגמת ירידה)
    neutral: כל מצב אחר
    """
    sym = symbol or CONFIG.get("regime_symbol", "SPY")
    hist = fetch_history(sym, refresh=refresh, assetclass="etf")
    if not hist:
        return None
    closes = [h["close"] for h in hist]
    sma50 = sma(closes, 50)
    sma200 = sma(closes, 200)
    last = closes[-1]
    if sma50 is None or sma200 is None:
        return None
    if last > sma50 > sma200:
        return "bull"
    if last < sma50 < sma200:
        return "bear"
    return "neutral"


# --------------------------------------------------------------------------
# אינדיקטורים (טהור, ללא pandas)
# --------------------------------------------------------------------------
def sma(vals, n):
    if len(vals) < n:
        return None
    return sum(vals[-n:]) / n


def ema_series(vals, n):
    if len(vals) < n:
        return []
    k = 2.0 / (n + 1)
    e = sum(vals[:n]) / n
    out = [e]
    for v in vals[n:]:
        e = v * k + e * (1 - k)
        out.append(e)
    return out


def rsi(vals, n=14):
    """RSI עם חילון של ווילדר."""
    if len(vals) < n + 1:
        return None
    gains, losses = [], []
    for i in range(1, len(vals)):
        d = vals[i] - vals[i - 1]
        gains.append(max(d, 0.0))
        losses.append(max(-d, 0.0))
    avg_g = sum(gains[:n]) / n
    avg_l = sum(losses[:n]) / n
    for i in range(n, len(gains)):
        avg_g = (avg_g * (n - 1) + gains[i]) / n
        avg_l = (avg_l * (n - 1) + losses[i]) / n
    if avg_l == 0:
        return 100.0
    return 100.0 - 100.0 / (1.0 + avg_g / avg_l)


def macd(vals, fast=12, slow=26, signal=9):
    """מחזיר (macd_line, signal, hist, hist_prev)."""
    if len(vals) < slow + signal:
        return None
    ef = ema_series(vals, fast)
    es = ema_series(vals, slow)
    ef_al = ef[len(ef) - len(es):]  # יישור לאותו אורך
    line = [a - b for a, b in zip(ef_al, es)]
    sig = ema_series(line, signal)
    if not sig:
        return None
    hist = [m - s for m, s in zip(line[-len(sig):], sig)]
    return line[-1], sig[-1], hist[-1], (hist[-2] if len(hist) > 1 else hist[-1])


def atr(highs, lows, closes, n=14):
    """ממוצע טווח אמיתי (Wilder)."""
    if len(closes) < n + 1:
        return None
    trs = []
    for i in range(1, len(closes)):
        trs.append(max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1]),
        ))
    a = sum(trs[:n]) / n
    for t in trs[n:]:
        a = (a * (n - 1) + t) / n
    return a


def adx(highs, lows, closes, n=14):
    """ADX (Wilder) — עוצמת מגמה 0-100 (גבוה = מגמה חזקה, נמוך = שוק לטיני)."""
    if len(closes) < 2 * n + 1:
        return None
    trs, pdm, ndm = [], [], []
    for i in range(1, len(closes)):
        up = highs[i] - highs[i - 1]
        dn = lows[i - 1] - lows[i]
        pdm.append(up if up > dn and up > 0 else 0.0)
        ndm.append(dn if dn > up and dn > 0 else 0.0)
        trs.append(max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1]),
        ))
    atr_w = sum(trs[:n])
    pdi_w = sum(pdm[:n])
    ndi_w = sum(ndm[:n])
    dxs = []
    for i in range(n, len(trs)):
        atr_w = atr_w - atr_w / n + trs[i]
        pdi_w = pdi_w - pdi_w / n + pdm[i]
        ndi_w = ndi_w - ndi_w / n + ndm[i]
        if atr_w > 0:
            pdi = 100.0 * pdi_w / atr_w
            ndi = 100.0 * ndi_w / atr_w
            s = pdi + ndi
            dxs.append(100.0 * abs(pdi - ndi) / s if s > 0 else 0.0)
    if len(dxs) < n:
        return None
    a = sum(dxs[:n]) / n
    for d in dxs[n:]:
        a = (a * (n - 1) + d) / n
    return a


# --------------------------------------------------------------------------
# ניתוח וציון
# --------------------------------------------------------------------------
def _ind_cache_path(sym, bar_date, assetclass="stocks"):
    prefix = "" if assetclass == "stocks" else f"{assetclass}_"
    return os.path.join(CONFIG["cache_dir"],
                        f"ind_{prefix}{sym}_{(bar_date or 'na').replace('/', '-')}.json")


def _compute_indicators(closes, highs, lows, vols):
    """אינדיקטורים יומיים — תלויים רק בנרות סגורים, ולכן נשמרים ליום מסחר אחד."""
    n = len(closes)
    last = closes[-1]
    m = macd(closes)
    vol20 = sma(vols, 20) or 0
    vol5 = sma(vols, 5) or 0
    a = atr(highs, lows, closes, 14)
    hi20 = max(highs[-20:])
    lo20 = min(lows[-20:])
    return {
        "last": last, "sma20": sma(closes, 20), "sma50": sma(closes, 50),
        "sma200": sma(closes, 200), "rsi14": rsi(closes, 14),
        "macd_line": m[0] if m else None, "macd_sig": m[1] if m else None,
        "macd_hist": m[2] if m else None, "macd_hist_prev": m[3] if m else None,
        "atr": a, "adx": adx(highs, lows, closes, 14),
        "vol_last": vols[-1],
        "vol_ratio": (vols[-1] / vol20) if vol20 else 0.0,
        "vol_trend": (vol5 / vol20) if vol20 else 0.0,
        "atr_pct": (a / last * 100.0) if (a and last) else None,
        "hi252": (max(highs[-252:]) if n >= 252 else max(highs)),
        "lo252": (min(lows[-252:]) if n >= 252 else min(lows)),
        "hi20": hi20, "lo20": lo20,
        "pos20": ((last - lo20) / (hi20 - lo20)) if hi20 > lo20 else 0.5,
        "ret20": (last / closes[-21] - 1) if n >= 21 else 0.0,
        "ret60": (last / closes[-61] - 1) if n >= 61 else 0.0,
    }


def analyze(row, hist, regime=None):
    closes = [h["close"] for h in hist]
    n = len(closes)
    if n < 210:
        return None

    # אינדיקטורים יומיים: נשמרים לפי תאריך הנר האחרון, כך שסריקה חוזרת באותו יום
    # עולה רק טעינת הערכים (במקום חישוב מחדש) — חלון נתונים קצר בהרבה.
    assetclass = row.get("assetclass", "stocks")
    bar_date = (hist[-1].get("date") or "").strip()
    ind = None
    if bar_date:
        cp = _ind_cache_path(row["symbol"], bar_date, assetclass)
        try:
            if time.time() - os.path.getmtime(cp) < CONFIG["cache_ttl"]:
                with open(cp, "r", encoding="utf-8") as f:
                    ind = json.load(f)
        except Exception:
            ind = None
    if not ind:
        ind = _compute_indicators(closes, [h["high"] for h in hist],
                                  [h["low"] for h in hist],
                                  [h["volume"] for h in hist])
        if bar_date:
            try:
                os.makedirs(CONFIG["cache_dir"], exist_ok=True)
                with open(_ind_cache_path(row["symbol"], bar_date, assetclass),
                          "w", encoding="utf-8") as f:
                    json.dump(ind, f)
            except Exception:
                pass

    last = ind["last"]
    sma50 = ind["sma50"]
    sma200 = ind["sma200"]
    rsi14 = ind["rsi14"]
    m_line, m_hist, m_hist_prev = ind["macd_line"], ind["macd_hist"], ind["macd_hist_prev"]
    a = ind["atr"]
    adx_v = ind["adx"]
    vol_ratio = ind["vol_ratio"]
    vol_trend = ind["vol_trend"]
    atr_pct = ind["atr_pct"]
    pos20 = ind["pos20"]
    ret20 = ind["ret20"]
    hi252 = ind["hi252"]
    lo252 = ind["lo252"]
    ret60 = ind["ret60"]

    # --- ציון לונג ---
    long_score, long_reasons = 0, []
    if last > sma50:
        long_score += 2; long_reasons.append("מחיר מעל SMA50")
    if sma50 > sma200:
        long_score += 2; long_reasons.append("SMA50 מעל SMA200")
    if last > sma200:
        long_score += 1
    if 50 <= rsi14 <= 70:
        long_score += 2; long_reasons.append("RSI מומנטום בריא")
    if rsi14 > 75:
        long_score -= 2; long_reasons.append("RSI קיצוני")
    if m_line is not None and m_line > 0:
        long_score += 2; long_reasons.append("MACD חיובי")
    if m_hist is not None and m_hist > 0 and m_hist > m_hist_prev:
        long_score += 1; long_reasons.append("MACD עולה")
    if vol_ratio >= 1.5:
        long_score += 2; long_reasons.append("מחזור גבוה")
    elif vol_ratio >= 1.2:
        long_score += 1
    if pos20 >= 0.8:
        long_score += 1; long_reasons.append("קרוב לשיא 20 יום")
    if last >= hi252 * 0.9:
        long_score += 1
    if ret20 >= 0.05:
        long_score += 1
    if last < sma50:
        long_score -= 3

    # --- ציון שורט ---
    short_score, short_reasons = 0, []
    if last < sma50:
        short_score += 2; short_reasons.append("מחיר מתחת ל-SMA50")
    if sma50 < sma200:
        short_score += 2; short_reasons.append("SMA50 מתחת ל-SMA200")
    if last < sma200:
        short_score += 1
    if 30 <= rsi14 <= 50:
        short_score += 2; short_reasons.append("RSI שולי מכירה")
    if rsi14 < 25:
        short_score -= 2; short_reasons.append("RSI קיצוני")
    if m_line is not None and m_line < 0:
        short_score += 2; short_reasons.append("MACD שלילי")
    if m_hist is not None and m_hist < 0 and m_hist < m_hist_prev:
        short_score += 1; short_reasons.append("MACD יורד")
    if vol_ratio >= 1.5:
        short_score += 2; short_reasons.append("מחזור גבוה")
    elif vol_ratio >= 1.2:
        short_score += 1
    if pos20 <= 0.2:
        short_score += 1; short_reasons.append("קרוב לשפל 20 יום")
    if last <= lo252 * 1.1:
        short_score += 1
    if ret20 <= -0.05:
        short_score += 1
    if last > sma50:
        short_score -= 3

    thr = CONFIG["score_threshold"]
    if long_score >= thr and long_score >= short_score and last > sma50:
        direction, score, reasons = "LONG", long_score, long_reasons
    elif short_score >= thr and last < sma50:
        direction, score, reasons = "SHORT", short_score, short_reasons
    else:
        return None

    # --- מסנני איכות סיגנל (שיפור אסטרטגיה) ---
    # 1) מצב שוק: לא לסרוק נגד המגמה הרחבה (SPY)
    if regime and CONFIG.get("regime_enabled", True):
        if regime == "bear" and direction == "LONG":
            return None
        if regime == "bull" and direction == "SHORT":
            return None
    # 2) עוצמת מגמה: ADX נמוך = שוק לטיני, סיגנל מומנטום מטעה
    adx_min = CONFIG.get("adx_min", 0.0) or 0.0
    if adx_v is not None and adx_min and adx_v < adx_min:
        return None
    # 3) תנודתיות: ATR% גבוה מדי = סטופ רחב וסיכון מוגזם
    max_atr = CONFIG.get("max_atr_pct", 0.0) or 0.0
    if atr_pct is not None and max_atr and atr_pct > max_atr:
        return None
    # 4) מחזור עולה: אישור שהמחזור גדל ב-5 הימים האחרונים מול 20
    vt_days = CONFIG.get("vol_trend_days", 0) or 0
    if vt_days and vol_trend < 1.0:
        return None

    live_price = row.get("price") or last  # מחיר שוטף מהסריקה (לכניסה)

    # מדד איכות רציף לדירוג משנה כשהציון זהה
    vol_q = min(vol_ratio, 3.0)
    mom_q = max(min(ret20, 0.30), -0.30) / 0.30
    rsi_ideal = 60.0 if direction == "LONG" else 40.0
    rsi_q = (1.0 - abs(rsi14 - rsi_ideal) / 40.0) if rsi14 is not None else 0.0
    rsi_q = max(min(rsi_q, 1.0), -1.0)
    dist_q = ((last - sma50) if direction == "LONG" else (sma50 - last)) / (a or (last * 0.02))
    dist_q = max(min(dist_q, 0.20), -0.20) / 0.20
    adx_q = (adx_v / 40.0) if adx_v is not None else 0.0
    adx_q = max(min(adx_q, 1.0), 0.0)
    voltr_q = max(min(vol_trend, 2.0), 0.0) / 2.0
    atr_q = 1.0 - (atr_pct / 10.0) if atr_pct is not None else 0.0
    atr_q = max(min(atr_q, 1.0), -1.0)
    quality = (vol_q * 1.5 + mom_q * 2.0 + rsi_q * 2.0 + pos20 * 1.5 + dist_q * 2.0
               + adx_q * 1.5 + voltr_q * 1.0 + atr_q * 1.0)

    return {
        "symbol": row["symbol"],
        "name": row["name"],
        "sector": row["sector"],
        "price": live_price,
        "close": last,
        "change_pct": row.get("change_pct"),
        "market_cap": row.get("market_cap"),
        "volume": ind["vol_last"],
        "dollar_volume": row.get("dollar_volume"),
        "direction": direction,
        "score": score,
        "quality": round(quality, 2),
        "reasons": reasons,
        "rsi": rsi14,
        "macd_hist": m_hist,
        "vol_ratio": vol_ratio,
        "atr": a,
        "atr_pct": round(atr_pct, 2) if atr_pct is not None else None,
        "adx": round(adx_v, 1) if adx_v is not None else None,
        "vol_trend": round(vol_trend, 2),
        "regime": regime,
        "sma50": sma50,
        "sma200": sma200,
        "ret20": ret20,
        "ret60": ret60,
        "pos20": pos20,
        "closes": closes[-60:],
    }


def build_trade(signal, portfolio):
    entry = signal["price"]
    a = signal["atr"] or entry * 0.02
    mult = CONFIG["atr_stop_mult"]
    rr = CONFIG["reward_risk"]
    if signal["direction"] == "LONG":
        stop = entry - mult * a
        risk = entry - stop
        target = entry + rr * risk
    else:
        stop = entry + mult * a
        risk = stop - entry
        target = entry - rr * risk
    if risk <= 0:
        return None
    risk_pct = risk / entry * 100
    if risk_pct > 10.0:
        return None  # סיכון מוגזם — ללא המלצה
    target_pct = abs(target - entry) / entry * 100
    shares = int(portfolio * CONFIG["risk_per_trade"] / risk)
    return {
        "entry": round(entry, 2),
        "stop": round(stop, 2),
        "target": round(target, 2),
        "risk_pct": round(risk_pct, 2),
        "target_pct": round(target_pct, 2),
        "rr": rr,
        "shares": shares,
        "risk_amount": round(portfolio * CONFIG["risk_per_trade"], 0),
    }


# --------------------------------------------------------------------------
# מועדפים / רשימת מעקב
# --------------------------------------------------------------------------
def load_watchlist():
    try:
        with open(CONFIG["watchlist_file"], "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"items": [], "meta": {}}


def save_watchlist(favs, meta):
    items = []
    for s, t in favs:
        items.append({
            "symbol": s["symbol"], "name": s["name"], "sector": s["sector"],
            "direction": s["direction"], "score": s["score"],
            "quality": s.get("quality"), "price": s["price"],
            "close": s.get("close"), "change_pct": s.get("change_pct"),
            "rsi": s["rsi"], "entry": t["entry"], "stop": t["stop"],
            "target": t["target"], "risk_pct": t["risk_pct"],
            "target_pct": t["target_pct"], "rr": t["rr"], "shares": t["shares"],
        })
    data = {"items": items, "meta": meta, "updated": time.time()}
    try:
        with open(CONFIG["watchlist_file"], "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception:
        pass
    return items


def pick_favorites(signals, trades, limit=None, gap_exclude=5.0):
    """מועדפים לקראת הפתיחה: ציון גבוה + ללא פער קיצוני + מגוון סקטורות.

    gap_exclude: אחוז פער נגד כיוון העסקה שמזניק את המניה מהרשימה.
    """
    if limit is None:
        limit = CONFIG.get("favorites_limit", 20)
    max_per_sector = CONFIG.get("favorites_max_per_sector", 4)
    sector_count = {}
    out = []
    for s, t in zip(signals, trades):
        if t is None:
            continue
        if s["score"] < CONFIG["favorites_min_score"]:
            continue
        # שער איתקנות: מגמה חזקה בלבד (ADX) — מסנן עסקאות רכות ללא כיוון
        min_adx = CONFIG.get("favorites_min_adx", 0.0) or 0.0
        if min_adx and s.get("adx") is not None and s["adx"] < min_adx:
            continue
        # מגוון: לא יותר מ־N מניות מאותו סקטור
        sec = s.get("sector") or ""
        if max_per_sector and sector_count.get(sec, 0) >= max_per_sector:
            continue
        chg = s.get("change_pct") or 0.0
        if s["direction"] == "LONG" and chg <= -gap_exclude:
            continue  # פער ירידה — לא לרדוף
        if s["direction"] == "SHORT" and chg >= gap_exclude:
            continue  # פער עלייה — לא לרדוף
        s = dict(s)
        if s["direction"] == "LONG" and chg >= 8.0:
            s["quality"] = s.get("quality", 0.0) - (chg - 8.0) / 5.0
            s["gap_flag"] = True
        elif s["direction"] == "SHORT" and chg <= -8.0:
            s["quality"] = s.get("quality", 0.0) - (-8.0 - chg) / 5.0
            s["gap_flag"] = True
        sector_count[sec] = sector_count.get(sec, 0) + 1
        out.append((s, t))
    out.sort(key=lambda x: (x[0]["score"], x[0].get("quality", 0.0)), reverse=True)
    return out[:limit]


def diff_watchlist(prev, cur):
    """מחזיר רשימת שינויים בין שתי רשימות מועדפים."""
    changes = []
    p = {i["symbol"]: i for i in prev.get("items", [])}
    c = {i["symbol"]: i for i in cur}
    for sym, item in c.items():
        if sym not in p:
            changes.append(
                f"🆕 {sym} נכנס למועדפים — {item['direction']} ציון {item['score']} "
                f"@ ${item['price']:.2f} (סגירה ${item.get('close', 0):.2f})")
        else:
            d = item["score"] - p[sym]["score"]
            if d >= 2:
                changes.append(
                    f"⬆️ {sym} החמיץ ציון {p[sym]['score']}→{item['score']} "
                    f"({item['direction']} @ ${item['price']:.2f})")
            elif d <= -2:
                changes.append(
                    f"⬇️ {sym} החליש ציון {p[sym]['score']}→{item['score']} "
                    f"@ ${item['price']:.2f}")
    for sym, item in p.items():
        if sym not in c:
            changes.append(f"❌ {sym} יצא מהמועדפים (ציון {item['score']}→—)")
    return changes


# --------------------------------------------------------------------------
# מסרים
# --------------------------------------------------------------------------
def sparkline(values, width=110, height=30, color="#34d399"):
    if not values or len(values) < 2:
        return ""
    lo, hi = min(values), max(values)
    rng = (hi - lo) or 1.0
    step = width / (len(values) - 1)
    pts = []
    for i, v in enumerate(values):
        x = i * step
        y = height - 2 - (v - lo) / rng * (height - 4)
        pts.append(f"{x:.1f},{y:.1f}")
    return (f'<svg class="spark" viewBox="0 0 {width} {height}" '
            f'preserveAspectRatio="none" width="{width}" height="{height}">'
            f'<polyline points="{" ".join(pts)}" fill="none" stroke="{color}" '
            f'stroke-width="1.6" stroke-linejoin="round" stroke-linecap="round"/>'
            f'</svg>')


def render_html(signals, meta, trades, etf_top=None):
    etf_section = ""
    if etf_top:
        er = []
        for i, (s, t) in enumerate(etf_top, 1):
            er.append(
                f'<tr><td>{i}</td><td class="sym">{s["symbol"]}'
                f'<div class="nm">{s["name"][:34]}</div></td>'
                f'<td class="num">${s["price"]:.2f}<div class="sub">סגירה ${s["close"]:.2f}</div></td>'
                f'<td class="num">{s["direction"]}</td><td class="num">{s["score"]}</td>'
                f'<td class="num">{s["rsi"]:.0f}</td><td class="num">${t["stop"]:.2f}</td>'
                f'<td class="num">${t["target"]:.2f}</td></tr>')
        etf_section = (
            '\n  <h2 style="margin:22px 0 10px;font-size:18px">📊 מובילי תעודות סל</h2>\n'
            '  <div class="table-wrap"><table>\n'
            '    <thead><tr><th>#</th><th>תעודת סל</th><th>מחיר</th><th>כיוון</th>'
            '<th>ציון</th><th>RSI</th><th>סטופ</th><th>יעד</th></tr></thead>\n'
            f'    <tbody>{"".join(er)}</tbody>\n  </table></div>')
    now = meta["now_il"]
    status = meta["market_status"]
    status_color = "#34d399" if status.lower() == "open" else "#f59e0b"
    longs = [s for s in signals if s["direction"] == "LONG"]
    shorts = [s for s in signals if s["direction"] == "SHORT"]

    rows_html = []
    for i, (s, t) in enumerate(zip(signals, trades), 1):
        if t is None:
            continue
        d = s["direction"]
        dcolor = "#34d399" if d == "LONG" else "#f87171"
        spark_color = "#34d399" if d == "LONG" else "#f87171"
        chg = s.get("change_pct")
        chg_color = "#34d399" if (chg or 0) >= 0 else "#f87171"
        rows_html.append(f"""
        <tr>
          <td class="rank">{i}</td>
          <td class="sym">{s['symbol']}<div class="nm">{s['name'][:34]}</div><div class="sec">{s['sector']}</div></td>
          <td class="num">${s['price']:.2f}<div class="sub">סגירה ${s['close']:.2f}</div><div class="chg" style="color:{chg_color}">{fmt_pct(chg)}</div></td>
          <td class="num"><span class="badge" style="background:{dcolor}22;color:{dcolor};border:1px solid {dcolor}55">{d}</span><div class="score">ציון {s['score']}</div></td>
          <td class="num">{s['rsi']:.0f}</td>
          <td class="num">{s['macd_hist']:+.3f}</td>
          <td class="num">{s['vol_ratio']:.2f}x</td>
          <td class="num">${t['entry']:.2f}</td>
          <td class="num" style="color:#f87171">${t['stop']:.2f}<div class="sub">-{t['risk_pct']:.1f}%</div></td>
          <td class="num" style="color:#34d399">${t['target']:.2f}<div class="sub">+{t['target_pct']:.1f}%</div></td>
          <td class="num">1:{t['rr']:.0f}</td>
          <td class="num">{t['shares']}</td>
          <td>{sparkline(s['closes'], color=spark_color)}</td>
        </tr>""")

    return f"""<!DOCTYPE html>
<html lang="he" dir="rtl">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>סורק נאסדק — {now}</title>
<style>
  :root {{ color-scheme: dark; }}
  * {{ box-sizing: border-box; }}
  body {{ margin: 0; background: #0b0f17; color: #e5e7eb;
         font-family: "Segoe UI", "Heebo", Tahoma, Arial, sans-serif; font-size: 14px; }}
  .wrap {{ max-width: 1280px; margin: 0 auto; padding: 24px; }}
  h1 {{ font-size: 24px; margin: 0 0 4px; }}
  .meta {{ color: #9ca3af; margin-bottom: 18px; }}
  .meta b {{ color: #e5e7eb; }}
  .cards {{ display: flex; gap: 12px; flex-wrap: wrap; margin-bottom: 18px; }}
  .card {{ background: #111827; border: 1px solid #1f2937; border-radius: 10px;
          padding: 14px 18px; min-width: 150px; }}
  .card .k {{ color: #9ca3af; font-size: 12px; }}
  .card .v {{ font-size: 20px; font-weight: 700; margin-top: 4px; }}
  .table-wrap {{ overflow-x: auto; border-radius: 10px; }}
  table {{ width: 100%; border-collapse: collapse; background: #111827;
          border: 1px solid #1f2937; border-radius: 10px; }}
  th {{ background: #0f172a; color: #9ca3af; font-weight: 600; font-size: 12px;
       padding: 10px 8px; text-align: right; white-space: nowrap; }}
  td {{ padding: 10px 8px; border-top: 1px solid #1f2937; vertical-align: middle; }}
  tr:hover td {{ background: #0f172a; }}
  .num {{ text-align: left; font-variant-numeric: tabular-nums; white-space: nowrap; }}
  .rank {{ color: #6b7280; font-weight: 700; }}
  .sym {{ font-weight: 700; font-size: 15px; }}
  .nm {{ color: #9ca3af; font-weight: 400; font-size: 11px; }}
  .sec {{ color: #6b7280; font-size: 10px; }}
  .chg {{ font-size: 11px; }}
  .sub {{ font-size: 10px; color: #9ca3af; }}
  .score {{ font-size: 10px; color: #9ca3af; }}
  .badge {{ padding: 2px 8px; border-radius: 999px; font-size: 11px; font-weight: 700; }}
  .spark {{ display: block; }}
  .note {{ color: #6b7280; font-size: 12px; margin-top: 16px; line-height: 1.7; }}
  .disc {{ background: #1f293755; border: 1px solid #374151; border-radius: 8px;
          padding: 10px 14px; color: #9ca3af; font-size: 12px; margin-top: 18px; }}
</style>
</head>
<body>
<div class="wrap">
  <h1>📈 סורק נאסדק — המלצות יומיות</h1>
  <div class="meta">
    תאריך: <b>{now}</b> &nbsp;•&nbsp; סטטוס שוק: <b style="color:{status_color}">{status}</b> &nbsp;•&nbsp; מצב שוק: <b>{meta.get('regime') or 'N/A'}</b>
    &nbsp;•&nbsp; סרוקו: <b>{meta['scanned']}</b> &nbsp;•&nbsp; עברו סינון: <b>{meta['passed']}</b>
    &nbsp;•&nbsp; לונג: <b style="color:#34d399">{len(longs)}</b> &nbsp;•&nbsp; שורט: <b style="color:#f87171">{len(shorts)}</b>
  </div>
  <div class="cards">
    <div class="card"><div class="k">ממוצע ציון</div><div class="v">{meta['avg_score']:.1f}</div></div>
    <div class="card"><div class="k">סף המלצה</div><div class="v">{CONFIG['score_threshold']}</div></div>
    <div class="card"><div class="k">סיכון לעסקה</div><div class="v">1%</div></div>
    <div class="card"><div class="k">יעד</div><div class="v">2R</div></div>
    <div class="card"><div class="k">טווח זמן</div><div class="v">יומי (1-5 ימים)</div></div>
  </div>
  <div class="table-wrap">
  <table>
    <thead><tr>
      <th>#</th><th>מניה</th><th>מחיר</th><th>כיוון</th><th>RSI</th>
      <th>MACD</th><th>מחזור</th><th>כניסה</th><th>סטופ</th><th>יעד</th>
      <th>R:R</th><th>היקף*</th><th>מגמה 60י</th>
    </tr></thead>
    <tbody>{''.join(rows_html)}</tbody>
  </table>
  </div>{etf_section}
  <div class="note">
    * היקף מוצע לתיק של $10,000 בסיכון 1% לעסקה. הסטופ מחושב כ-1.5×ATR מתחת/מעל הכניסה,
    והיעד 2R. סף ציון: {CONFIG['score_threshold']}. האותות מבוססים על סגירת הבר היומי האחרון,
    והמחירונים המוצגים הם שוטפים — לאסטרטגיה יומית מומלץ לבצע בפתיחה הבאה או בנסיגה לרמת הכניסה.
  </div>
  <div class="disc">
    ⚠️ המידע מוצג למטרות מחקר ולימוד בלבד ואינו מהווה ייעוץ השקעה או המלצה לקנות/למכור נייר ערך.
    ביצוע העסקאות באחריותך בלבד. ביצועי עבר אינם מעידים על תוצאות עתידיות.
  </div>
</div>
</body>
</html>"""


def render_markdown(signals, meta, trades, etf_top=None):
    lines = [
        f"# 📈 סורק נאסדק — {meta['now_il']}",
        "",
        f"**סטטוס שוק:** {meta['market_status']} • **מצב שוק:** {meta.get('regime') or 'N/A'} • "
        f"**סרוקו:** {meta['scanned']} • "
        f"**לונג:** {len([s for s in signals if s['direction']=='LONG'])} • "
        f"**שורט:** {len([s for s in signals if s['direction']=='SHORT'])}",
        "",
        "| # | מניה | מחיר | כיוון | ציון | RSI | MACD | מחזור | כניסה | סטופ | יעד | יעד % | R:R |",
        "|---|------|------|-------|------|-----|------|-------|-------|------|-----|-------|-----|",
    ]
    for i, (s, t) in enumerate(zip(signals, trades), 1):
        if t is None:
            continue
        lines.append(
            f"| {i} | **{s['symbol']}** {s['name'][:22]} | ${s['price']:.2f} | "
            f"{s['direction']} | {s['score']} | {s['rsi']:.0f} | "
            f"{s['macd_hist']:+.3f} | {s['vol_ratio']:.2f}x | ${t['entry']:.2f} | "
            f"${t['stop']:.2f} (-{t['risk_pct']:.1f}%) | ${t['target']:.2f} | "
            f"+{t['target_pct']:.1f}% | {t['rr']:.1f} |"
        )
    if etf_top:
        lines += [
            "",
            "### 📊 מובילי תעודות סל",
            "",
            "| # | תעודת סל | מחיר | כיוון | ציון | סטופ | יעד |",
            "|---|----------|------|-------|------|------|-----|",
        ]
        for i, (s, t) in enumerate(etf_top, 1):
            lines.append(
                f"| {i} | **{s['symbol']}** | ${s['price']:.2f} | {s['direction']} | "
                f"{s['score']} | ${t['stop']:.2f} | ${t['target']:.2f} |")
    lines += [
        "",
        "**אותות:** " + "; ".join(f"{s['symbol']}: {', '.join(s['reasons'])}" for s in signals[:6]),
        "",
        "⚠️ למטרות מחקר בלבד, אינו ייעוץ השקעה.",
    ]
    return "\n".join(lines)


# --------------------------------------------------------------------------
# הרצה
# --------------------------------------------------------------------------
def run_scan(args, save=True, verbose=True):
    os.makedirs(CONFIG["out_dir"], exist_ok=True)
    os.makedirs(CONFIG["cache_dir"], exist_ok=True)

    stocks = fetch_stocks(args.universe)
    etfs = fetch_etf_universe(refresh=args.rebuild, limit=args.etf_limit)
    universe = stocks + etfs
    if not universe:
        if verbose:
            print("⚠️  שליפת הרשימה נכשלה (בדוק חיבור).")
        return None

    status, ts = fetch_market_status()
    regime = fetch_regime(refresh=args.refresh) if CONFIG.get("regime_enabled", True) else None
    if verbose:
        print(f"📡 רשימה: {len(stocks)} מניות + {len(etfs)} תעודות סל | "
              f"סטטוס שוק: {status} ({ts}) | מצב שוק: {regime or 'N/A'}")

    signals = []
    scanned = 0
    lock = threading.Lock()

    def work(row):
        nonlocal scanned
        hist = fetch_history(row["symbol"], refresh=args.refresh,
                             assetclass=row.get("assetclass", "stocks"))
        scanned += 1
        if not hist:
            return None
        return analyze(row, hist, regime)

    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=CONFIG["workers"]) as ex:
        for res in ex.map(work, universe):
            if res:
                signals.append(res)

    signals.sort(key=lambda s: (s["score"], s.get("quality", 0.0)), reverse=True)

    if args.debug and verbose:
        all_scores = []
        for row in universe:
            h = fetch_history(row["symbol"], refresh=False,
                              assetclass=row.get("assetclass", "stocks"))
            if not h:
                continue
            r = analyze(row, h, regime)
            if r:
                all_scores.append((r["score"], r["quality"], r["symbol"], r["direction"]))
        all_scores.sort(reverse=True)
        from collections import Counter
        hist = Counter(s for s, *_ in all_scores)
        print("\n[DEBUG] התפלגות ציונים:")
        for sc in sorted(hist, reverse=True):
            print(f"   ציון {sc:2}: {'#' * min(hist[sc], 60)} {hist[sc]}")
        print(f"[DEBUG] סה\"כ עוברי סף: {len(all_scores)} מתוך {len(universe)}")
        print("[DEBUG] מובילים 20:")
        for sc, q, sym, d in all_scores[:20]:
            print(f"   {sym:6} {d:5} ציון {sc:2} איכות {q:5.2f}")

    all_signals = list(signals)
    signals = signals[: args.top]

    portfolio = 10000
    trades = [build_trade(s, portfolio) for s in signals]
    kept = [(s, t) for s, t in zip(signals, trades) if t is not None]
    signals = [s for s, _ in kept]
    trades = [t for _, t in kept]

    all_trades = [build_trade(s, portfolio) for s in all_signals]

    now_il = israel_now().strftime("%Y-%m-%d %H:%M")
    avg = sum(s["score"] for s in signals) / len(signals) if signals else 0
    meta = {
        "now_il": now_il,
        "market_status": status,
        "regime": regime,
        "scanned": len(universe),
        "passed": len(signals),
        "avg_score": avg,
    }

    html_path = md_path = json_path = None
    if save:
        stamp = israel_now().strftime("%Y%m%d_%H%M")
        html_path = os.path.join(CONFIG["out_dir"], f"report_{stamp}.html")
        md_path = os.path.join(CONFIG["out_dir"], f"report_{stamp}.md")
        json_path = os.path.join(CONFIG["out_dir"], f"signals_{stamp}.json")

        payload = {
            "meta": meta,
            "signals": [{k: v for k, v in s.items() if k != "closes"} for s in signals],
            "trades": trades,
        }
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        etf_top = [(s, t) for s, t in zip(all_signals, all_trades)
                   if s.get("sector") == "ETF" and t is not None][:5]
        with open(html_path, "w", encoding="utf-8") as f:
            f.write(render_html(signals, meta, trades, etf_top))
        with open(md_path, "w", encoding="utf-8") as f:
            f.write(render_markdown(signals, meta, trades, etf_top))

    # פלט למסוף
    if verbose:
        print(f"\n{'='*78}")
        print(f"סורק נאסדק — {now_il} | שוק: {status} | סרוקו {len(universe)} | המלצות: {len(signals)}")
        print(f"{'='*78}")
        for i, (s, t) in enumerate(zip(signals, trades), 1):
            if t is None:
                continue
            print(f"{i:2}. {s['symbol']:6} {s['direction']:5} ציון {s['score']:2} | "
                  f"מחיר ${s['price']:.2f} (סגירה ${s['close']:.2f}) | RSI {s['rsi']:.0f} | "
                  f"סטופ ${t['stop']:.2f} (-{t['risk_pct']:.1f}%) | "
                  f"יעד ${t['target']:.2f} (+{t['target_pct']:.1f}%) | {t['shares']} מניות")
        if save:
            print(f"\n📄 דוח: {html_path}")
            print(f"📄 מרקדאון: {md_path}")
            print(f"📄 JSON: {json_path}")

    return {"signals": signals, "trades": trades, "meta": meta,
            "paths": {"html": html_path, "md": md_path, "json": json_path}}


def run_brief(args):
    """סיכום מועדפים לקראת הפתיחה."""
    res = run_scan(args, save=False, verbose=False)
    if not res:
        print("⚠️  הסריקה נכשלה.")
        return None
    favs = pick_favorites(res["signals"], res["trades"])
    meta = res["meta"]
    items = save_watchlist(favs, meta)
    print(f"\n{'='*78}")
    print(f"⭐ מועדפים לקראת הפתיחה — {meta['now_il']} | שוק: {meta['market_status']} | "
          f"מתוך {meta['scanned']} מניות")
    print(f"{'='*78}")
    if not favs:
        print("אין מועדפים חזקים כרגע (ציון >= 12, ללא פער קיצוני).")
    for i, (s, t) in enumerate(favs, 1):
        gap = " ⚠️פער" if s.get("gap_flag") else ""
        print(f"{i:2}. {s['symbol']:6} {s['direction']:5} ציון {s['score']:2}{gap} | "
              f"מחיר ${s['price']:.2f} (סגירה ${s['close']:.2f}, {fmt_pct(s.get('change_pct'))}) | "
              f"סטופ ${t['stop']:.2f} (-{t['risk_pct']:.1f}%) | יעד ${t['target']:.2f} (+{t['target_pct']:.1f}%) | "
              f"R:R {t['rr']:.0f} | {t['shares']} מניות")
    print(f"\n📄 רשימת מעקב: {CONFIG['watchlist_file']}")
    return {"favorites": favs, "meta": meta, "items": items}


def run_watch(args):
    """מעקב שוטף 24/7: מדפיס רק שינויים ברשימת המועדפים."""
    prev = load_watchlist()
    print(f"👁  מצב מעקב — סריקה כל {args.interval} שניות (Ctrl+C לעצור)")
    while True:
        try:
            res = run_scan(args, save=False, verbose=False)
            if res:
                favs = pick_favorites(res["signals"], res["trades"])
                items = save_watchlist(favs, res["meta"])
                cur = {"items": items, "meta": res["meta"]}
                changes = diff_watchlist(prev, cur)
                if changes:
                    print(f"\n🔄 {res['meta']['now_il']} | שוק: {res['meta']['market_status']}")
                    for c in changes:
                        print("  " + c)
                else:
                    print(f"⏳ {res['meta']['now_il']} — אין שינויים ({len(items)} מועדפים)")
                prev = cur
            print(f"⏳ הסריקה הבאה בעוד {args.interval} שניות...")
            time.sleep(args.interval)
        except KeyboardInterrupt:
            print("\nעצירה.")
            break


def main():
    ap = argparse.ArgumentParser(description="NASDAQ Daily Scanner")
    ap.add_argument("--universe", type=int, default=CONFIG["universe_size"])
    ap.add_argument("--etf-limit", type=int, default=0, help="סיכול מספר תעודות סל (0 = הכל)")
    ap.add_argument("--top", type=int, default=CONFIG["top_n"])
    ap.add_argument("--refresh", action="store_true", help="דלג על מטמון")
    ap.add_argument("--rebuild", action="store_true", help="בנה מחדש את אוניברסום תעודות הסל")
    ap.add_argument("--debug", action="store_true", help="הדפס התפלגות ציונים")
    ap.add_argument("--watch", action="store_true", help="מעקב שוטף 24/7")
    ap.add_argument("--brief", action="store_true", help="סיכום מועדפים לקראת הפתיחה")
    ap.add_argument("--interval", type=int, default=900, help="מרווח סריקה (שניות)")
    ap.add_argument("--ttl", type=int, default=CONFIG["cache_ttl"], help="תוקף מטמון (שניות)")
    args = ap.parse_args()

    CONFIG["cache_ttl"] = args.ttl

    if args.brief:
        run_brief(args)
    elif args.watch:
        run_watch(args)
    else:
        run_scan(args)


if __name__ == "__main__":
    main()
