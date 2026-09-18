#!/usr/bin/env python3
"""measure_probe_latency.py — 实测 getProduct 探测的冷/热启动耗时，用于定 hedge 值。

hedge 必须**高于**正常耗时，否则每次冷启动都会误触发对冲（多发无谓请求）；
又必须足够低，故障时才能快速回退。故先量出正常区间再定值。

用法：python tools/measure_probe_latency.py --country de --rounds 6
"""
import argparse
import asyncio
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from crawler.core import launch_browser  # noqa: E402,F401
import executor  # noqa: E402
import crawler.run as run  # noqa: E402


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--brand", default="oppo")
    ap.add_argument("--country", default="de")
    ap.add_argument("--rounds", type=int, default=6)
    args = ap.parse_args()

    rec = executor.load_record(args.brand, args.country)
    api = rec["query"]["api"]
    cc = api.get("region_iso") or args.country
    hosts = api.get("candidate_hosts") or [api.get("base_url") or ""]
    hosts = [h.strip().replace("https://", "").rstrip("/") for h in hosts if h]
    path_list = api.get("product_list", "/basic/v1/getProduct")
    path_price = api.get("price_detail", "/basic/v1/getPartPriceNew")

    print(f"=== getProduct 单节点耗时（{args.brand}/{args.country}，"
          f"候选 {len(hosts)} 个，串行 {args.rounds} 轮）===")
    print(f"{'轮次':<6}{'节点':<28}{'耗时':>8}  机型数")
    for i in range(args.rounds):
        for h in hosts:
            api_h = {**api, "base_url":
                     (h if h.startswith("http") else f"https://{h}").rstrip("/") + "/oppo-api"}
            t0 = time.perf_counter()
            res = await executor.reborn_post(api_h, cc, page=None,
                                             path_list=path_list, path_price=path_price,
                                             tries=run._PROBE_TRIES, to=run._PROBE_TIMEOUT)
            el = time.perf_counter() - t0
            n = len((res or {}).get("data") or []) if not res.get("error") else -1
            tag = "(冷启)" if i == 0 else ""
            print(f"{i + 1:<6}{h:<28}{el:>7.2f}s  {n}{tag}  "
                  f"{res.get('error') or ''}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
