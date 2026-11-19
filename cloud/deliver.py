#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""מסירת תוצאות סריקה דרך טלגרם — לפתרון הענן (GitHub Actions).

משתני סביבה:
  TELEGRAM_TOKEN  — טוקן בוט טלגרם
  TELEGRAM_CHAT   — מזהה צ'אט (chat id)
"""
import argparse
import json
import os
import urllib.request

TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
TOKEN_BACKUP = os.environ.get("TELEGRAM_TOKEN_BACKUP", "")
CHAT = os.environ.get("TELEGRAM_CHAT", "")
STATE = os.path.join("cache", "cloud_watch_state.json")

# גיבוי: שי-1212 ראשי, שי-1213 מופעל רק אם הראשי נופל
TOKENS = [t for t in (TOKEN, TOKEN_BACKUP) if t]


def send(text):
    if not TOKENS or not CHAT:
        print("TELEGRAM_TOKEN/TELEGRAM_CHAT חסרים — מדפיס במקום שליחה")
        print(text)
        return False
    for i, tok in enumerate(TOKENS, 1):
        url = f"https://api.telegram.org/bot{tok}/sendMessage"
        data = json.dumps({"chat_id": CHAT, "text": text,
                           "parse_mode": "HTML"}).encode("utf-8")
        req = urllib.request.Request(url, data=data,
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                if r.status == 200:
                    print(f"נשלח (בוט {i}/{len(TOKENS)})")
                    return True
        except Exception as exc:
            print(f"שליחה נכשלה (בוט {i}/{len(TOKENS)}):", exc)
    return False


def load_watchlist():
    try:
        with open("watchlist.json", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"items": [], "meta": {}}


def fmt_item(item, n):
    rp = item.get("risk_pct")
    tp = item.get("target_pct")
    rr = item.get("rr")
    stop = (f"סטופ ${item['stop']:.2f} (-{rp:.1f}%)" if rp is not None
            else f"סטופ ${item['stop']:.2f}")
    tgt = (f"יעד ${item['target']:.2f} (+{tp:.1f}%)" if tp is not None
           else f"יעד ${item['target']:.2f}")
    ratio = f" | R:R {rr:.0f}" if rr is not None else ""
    return (f"{n}. <b>{item['symbol']}</b> {item['direction']} | "
            f"ציון {item['score']} | ${item['price']:.2f} | "
            f"{stop} | {tgt}{ratio}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["daily", "brief", "watch", "open"], required=True)
    args = ap.parse_args()

    wl = load_watchlist()
    items = wl.get("items", [])
    meta = wl.get("meta", {})

    if args.mode == "watch":
        prev = None
        try:
            with open(STATE, encoding="utf-8") as f:
                prev = json.load(f)
        except Exception:
            pass
        cur = [[i["symbol"], i["direction"], i["score"]] for i in items]
        if prev == cur:
            print("אין שינוי — מדלג על מסירה")
            return
        os.makedirs("cache", exist_ok=True)
        with open(STATE, "w", encoding="utf-8") as f:
            json.dump(cur, f)

    titles = {
        "daily": "📊 <b>סורק נאסדק — דוח יומי</b>",
        "brief": "⭐ <b>מועדפים לקראת הפתיחה</b>",
        "watch": "👁 <b>מעקב נאסדק — שינוי ברשימה</b>",
        "open": "🚀 <b>פתיחת שוק — עדכון כל 5 דקות</b>",
    }
    lines = [f"{titles[args.mode]} | {meta.get('now_il', '')} | "
             f"{meta.get('market_status', '')}"]
    lines.append(f"סרוקו: {meta.get('scanned', '?')} | מועדפים: {len(items)}")
    lines.append("⏱ סגנון: מסחר יומי · טווח החזקה: יום–יומיים מסחר")
    lines.append("")
    if not items:
        lines.append("אין מועדפים חזקים כרגע.")
    for n, item in enumerate(items[:15], 1):
        lines.append(fmt_item(item, n))
    lines.append("")
    lines.append("⚠️ למטרות מחקר בלבד, אינו ייעוץ השקעה.")

    print("נשלח" if send("\n".join(lines)) else "לא נשלח")


if __name__ == "__main__":
    main()
