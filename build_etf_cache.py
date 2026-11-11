#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Build ETF cache in chunks — משלים את מטמון היסטוריית תעודות הסל במקטעים.
מיועד להרצה חוזרת עד שכל המועמדים נמצאים במטמון (לאחר מכן אוניברסום ה-ETF
נבנה במהירות מהמטמון).

שימוש: python3 build_etf_cache.py [chunk_size]
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import scanner  # noqa: E402


def main():
    limit = int(sys.argv[1]) if len(sys.argv) > 1 else 800
    cands = scanner.fetch_etf_candidates()
    cached = set()
    cache_dir = scanner.CONFIG["cache_dir"]
    for p in os.listdir(cache_dir):
        if p.startswith("etf_") and p.endswith(".json"):
            cached.add(p[4:-5])
    remaining = [c for c in cands if c["symbol"] not in cached]
    print(f"candidates {len(cands)} | cached {len(cached)} | remaining {len(remaining)}")
    chunk = remaining[:limit]
    if not chunk:
        print("ALL CACHED")
        return
    from concurrent.futures import ThreadPoolExecutor
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=scanner.CONFIG["workers"]) as ex:
        res = list(ex.map(
            lambda r: scanner.fetch_history(r["symbol"], refresh=False, assetclass="etf"),
            chunk))
    ok = sum(1 for r in res if r)
    print(f"fetched {ok}/{len(chunk)} in {time.time()-t0:.1f}s")


if __name__ == "__main__":
    main()
