#!/usr/bin/env python3
"""מדידת תוצאות האותות — מעקב אחרי ביצועי סיגנלים בפועל.

שתי פעולות:
  snapshot  — מעתיק את קובץ האותות האחרון ל־tracking/snapshots/<תאריך>.json
              (מיום הסריקה, לא מהקובץ עצמו). עבר עבור כל יום סריקה.
  score     — משווה כל סיגנל מול נרות יומיים שאחרי יום הכניסה ומחשב R בפועל:
              הגעה ליעד (+2R), סטופ (־1R), גאפ פתיחה, או "עדיין פתוח".

החישוב שמרני: אם באותו נר נגעו גם הסטופ וגם היעד — נזכה בסטופ.
נתונים מגיעים מ־scanner.fetch_history (מטמון על הדיסק), כך שהציון עובד גם
כשהרשת תקועה — מספיק מטמון ישן.

פלט: tracking/outcomes.jsonl (רשומה לכל עסקה) + tracking/summary.md + מסוף.
"""
import argparse
import datetime as dt
import glob
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import scanner  # CONFIG, fetch_history, israel_now

TRACK_DIR = "tracking"
SNAP_DIR = os.path.join(TRACK_DIR, "snapshots")
OUTCOMES = os.path.join(TRACK_DIR, "outcomes.jsonl")
SUMMARY = os.path.join(TRACK_DIR, "summary.md")


# --------------------------------------------------------------------------
# עזרים
# --------------------------------------------------------------------------
def parse_day(s):
    """תאריך נר (MM/DD/YYYY) או טקסט חופשי -> dt.date או None."""
    if not s:
        return None
    s = str(s).strip()
    for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%m/%d/%y"):
        try:
            return dt.datetime.strptime(s[:10], fmt).date()
        except ValueError:
            continue
    return None


def parse_stamp_day(meta):
    """יום הסריקה מתוך meta['now_il'] ('2026-11-19 03:22')."""
    now = (meta or {}).get("now_il") or ""
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return dt.datetime.strptime(now.strip(), fmt).date()
        except ValueError:
            continue
    return None


def newest_signals():
    files = sorted(glob.glob(os.path.join(scanner.CONFIG["out_dir"], "signals_*.json")))
    return files[-1] if files else None


def load_signals(path):
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    sigs = data.get("signals") or []
    trades = data.get("trades") or []
    items = []
    for i, s in enumerate(sigs):
        t = trades[i] if i < len(trades) else None
        if not t:
            continue
        items.append({
            "symbol": s.get("symbol"),
            "name": s.get("name"),
            "sector": s.get("sector"),
            "direction": s.get("direction"),
            "score": s.get("score"),
            "quality": s.get("quality"),
            "atr": s.get("atr"),
            "adx": s.get("adx"),
            "entry": t.get("entry"),
            "stop": t.get("stop"),
            "target": t.get("target"),
            "rr": t.get("rr"),
            "risk_pct": t.get("risk_pct"),
            "target_pct": t.get("target_pct"),
        })
    return (data.get("meta") or {}), items


# --------------------------------------------------------------------------
# snapshot
# --------------------------------------------------------------------------
def cmd_snapshot(args):
    path = args.file or newest_signals()
    if not path or not os.path.exists(path):
        print("⚠️  לא נמצא קובץ אותות.")
        return 1
    meta, items = load_signals(path)
    day = parse_stamp_day(meta)
    if day is None:
        print(f"⚠️  אין מתאריך סריקה בקובץ: {path}")
        return 1
    os.makedirs(SNAP_DIR, exist_ok=True)
    out = os.path.join(SNAP_DIR, f"{day.isoformat()}.json")
    payload = {"source": os.path.basename(path), "day": day.isoformat(),
               "meta": meta, "items": items}
    with open(out, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    print(f"📌 snapshot נשמר: {out} | יום {day} | {len(items)} אותות")
    return 0


# --------------------------------------------------------------------------
# סימולציית תוצאה
# --------------------------------------------------------------------------
def simulate(item, hist, max_days):
    """מחזיר רשומת תוצאה לסיגנל אחד על בסיס נרות יומיים אחרי יום הכניסה."""
    entry = item.get("entry")
    stop = item.get("stop")
    target = item.get("target")
    day = parse_day(item.get("_day"))
    direction = item.get("direction")
    if None in (entry, stop, target) or day is None:
        return None
    risk = abs(entry - stop)
    if risk <= 0:
        return None

    bars = []
    for b in hist or []:
        d = parse_day(b.get("date"))
        if d is None or d <= day:
            continue
        bars.append((d, b))
        if len(bars) >= max_days:
            break
    if not bars:
        return None

    mfe = mae = 0.0
    for idx, (d, b) in enumerate(bars, 1):
        o, h, l, c = b.get("open"), b.get("high"), b.get("low"), b.get("close")
        long_ = direction == "LONG"

        def r_of(px):
            if px is None:
                return 0.0
            return ((px - entry) / risk) if long_ else ((entry - px) / risk)

        # גאפ פתיחה: הפתיחה כבר מחוץ לסטופ
        if (long_ and o is not None and o <= stop) or \
           (not long_ and o is not None and o >= stop):
            return {
                "result": "stop_gap", "exit": round(o, 2), "days": idx,
                "r": round(r_of(o), 2), "mfe": round(max(mfe, r_of(h)), 2),
                "mae": round(min(mae, r_of(l)), 2), "exit_day": d.isoformat(),
            }

        hit_stop = (long_ and l is not None and l <= stop) or \
                   (not long_ and l is not None and l >= stop)
        hit_target = (long_ and h is not None and h >= target) or \
                     (not long_ and h is not None and h <= target)
        # שמרני: נגיעה בשניהם באותו נר — מניחים סטופ
        if hit_stop:
            return {
                "result": "stop", "exit": round(stop, 2), "days": idx,
                "r": -1.0, "mfe": round(max(mfe, r_of(h)), 2),
                "mae": -1.0, "exit_day": d.isoformat(),
            }
        if hit_target:
            return {
                "result": "target", "exit": round(target, 2), "days": idx,
                "r": round(r_of(target), 2), "mfe": round(r_of(target), 2),
                "mae": round(min(mae, r_of(l)), 2), "exit_day": d.isoformat(),
            }

        mfe = max(mfe, r_of(h), r_of(c), 0.0)
        mae = min(mae, r_of(l), r_of(c), 0.0)

    last_day, last = bars[-1]
    return {
        "result": "running", "exit": round(last.get("close"), 2),
        "days": len(bars), "r": round(r_of(last.get("close")), 2),
        "mfe": round(mfe, 2), "mae": round(mae, 2),
        "exit_day": last_day.isoformat(),
    }


# --------------------------------------------------------------------------
# score
# --------------------------------------------------------------------------
def cmd_score(args):
    snaps = sorted(glob.glob(os.path.join(SNAP_DIR, "*.json")))
    if not snaps:
        print("⚠️  אין snapshots. הרץ קודם: python tracker.py snapshot")
        return 1
    rows = []
    skipped = []
    for sp in snaps:
        with open(sp, "r", encoding="utf-8") as f:
            data = json.load(f)
        day = data.get("day")
        for it in data.get("items") or []:
            sym = it.get("symbol")
            hist = scanner.fetch_history(sym, refresh=args.refresh,
                                         assetclass="etf"
                                         if it.get("sector") == "ETF" else "stocks")
            rec = simulate(dict(it, _day=day), hist, args.max_days)
            if not rec:
                if not hist:
                    skipped.append(sym)
                continue
            rec.update({"symbol": sym, "day": day,
                        "direction": it.get("direction"),
                        "score": it.get("score"),
                        "sector": it.get("sector"),
                        "adx": it.get("adx")})
            rows.append(rec)

    os.makedirs(TRACK_DIR, exist_ok=True)
    with open(OUTCOMES, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    if skipped:
        print(f"⚠️  לא נמצאה היסטוריה עבור: {', '.join(sorted(set(skipped)))}")
    _print_summary(rows)
    _write_summary_md(rows)
    return 0


def _metrics(rows):
    n = len(rows)
    if not n:
        return None
    tgt = [r for r in rows if r["result"] == "target"]
    stp = [r for r in rows if r["result"].startswith("stop")]
    run = [r for r in rows if r["result"] == "running"]
    rs = [r["r"] for r in rows]
    return {
        "n": n,
        "targets": len(tgt),
        "target_pct": 100.0 * len(tgt) / n,
        "stops": len(stp),
        "running": len(run),
        "avg_r": sum(rs) / n,
        "median_days": sorted(r["days"] for r in rows)[n // 2],
        "best_r": max(rs),
        "worst_r": min(rs),
    }


def _print_summary(rows):
    m = _metrics(rows)
    print("\n" + "=" * 70)
    print("מדידת תוצאות אותות")
    print("=" * 70)
    if not m:
        print("אין נתונים.")
        return
    print(f"עסקאות: {m['n']} | הגעה ליעד: {m['targets']} ({m['target_pct']:.0f}%) | "
          f"סטופ: {m['stops']} | פתוח: {m['running']}")
    print(f"ממוצע R: {m['avg_r']:+.2f} | ממוצע ימים: {m['median_days']} | "
          f"טוב ביותר: {m['best_r']:+.1f}R | גרוע ביותר: {m['worst_r']:+.1f}R")
    # לפי כיוון
    for dirn in ("LONG", "SHORT"):
        sub = [r for r in rows if r["direction"] == dirn]
        mm = _metrics(sub)
        if mm:
            print(f"  {dirn:5}: {mm['n']:3} עסקאות | {mm['target_pct']:4.0f}% יעד | "
                  f"ממוצע {mm['avg_r']:+.2f}R")
    # לפי ציון
    buckets = {}
    for r in rows:
        key = r.get("score")
        buckets.setdefault(key, []).append(r)
    print("\nלפי ציון:")
    for sc in sorted(buckets, reverse=True):
        mm = _metrics(buckets[sc])
        print(f"   ציון {str(sc):>2}: {mm['n']:3} עסקאות | {mm['target_pct']:4.0f}% יעד | "
              f"ממוצע {mm['avg_r']:+.2f}R")
    print(f"\n📄 {OUTCOMES}")


def _write_summary_md(rows):
    m = _metrics(rows)
    lines = ["# מדידת תוצאות אותות", ""]
    if not m:
        lines.append("אין נתונים עדיין.")
    else:
        lines += [
            f"- עסקאות: **{m['n']}**",
            f"- הגעה ליעד (+2R): **{m['targets']}** ({m['target_pct']:.0f}%)",
            f"- סטופ: **{m['stops']}** · פתוח: **{m['running']}**",
            f"- ממוצע R: **{m['avg_r']:+.2f}**",
            f"- ממוצע ימים עד יציאה: **{m['median_days']}**",
            "",
            "| יום | סימול | כיוון | ציון | תוצאה | R | ימים |",
            "|---|---|---|---|---|---|---|",
        ]
        for r in rows:
            lines.append(
                f"| {r['day']} | {r['symbol']} | {r['direction']} | {r.get('score')} | "
                f"{r['result']} | {r['r']:+.2f} | {r['days']} |")
    with open(SUMMARY, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def main():
    ap = argparse.ArgumentParser(description="מדידת תוצאות אותות")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("snapshot", help="שומר את אותות היום למדידה")
    s.add_argument("--file", default=None, help="קובץ signals_*.json (ברירת מחדל: האחרון)")
    sc = sub.add_parser("score", help="מחשב תוצאות R לכל ה־snapshots")
    sc.add_argument("--max-days", type=int, default=5,
                    help="כמה נרות יומיים לבדוק אחרי הכניסה (ברירת מחדל 5)")
    sc.add_argument("--refresh", action="store_true",
                    help="נתק מטמון ושלף היסטוריה מחדש (איטי, דורש רשת)")
    args = ap.parse_args()
    if args.cmd == "snapshot":
        return cmd_snapshot(args)
    return cmd_score(args)


if __name__ == "__main__":
    raise SystemExit(main())
