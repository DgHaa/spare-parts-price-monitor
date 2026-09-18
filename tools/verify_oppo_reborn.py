"""tools/verify_oppo_reborn.py - 冒烟验证 OPPO 新一代 REBORN 备件价抓取链路。

校验两件事（不写库）：
  1) executor.run_query(mode=api_reborn) 单机型取价 —— 即生产代码路径；
  2) crawler.run.discover_and_price_via_reborn 批量发现+取价（可用 --max 限制机型数）。
用法：
    python tools/verify_oppo_reborn.py --country cn --model "OPPO Pad 5"
    python tools/verify_oppo_reborn.py --country cn --max 5
"""
import argparse
import asyncio
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

from crawler.core import launch_browser, open_page  # noqa: E402
import executor  # noqa: E402
from crawler import run as crawl_run  # noqa: E402


async def main(brand, country, model, max_models, direct):
    rec = executor.load_record(brand, country)
    if not rec:
        print(f"[fail] 无 KB 记录 {brand}/{country}")
        return 1
    mode = rec.get("query", {}).get("mode")
    print(f"[kb] {brand}/{country} mode={mode} currency={rec.get('currency')}")
    if mode != "api_reborn":
        print(f"[fail] 期望 mode=api_reborn，实际 {mode}")
        return 1

    api_cfg = rec.get("query", {}).get("api", {})
    use_http = api_cfg.get("fetch_mode") == "http"
    page = None
    pw = browser = None
    if use_http:
        # http 模式下服务端 urllib 直发即可，无需浏览器（也避免无代理时导航卡死）
        print("[browser] fetch_mode=http，跳过浏览器启动")
    else:
        force_direct = direct or bool(api_cfg.get("bypass_proxy"))
        pw, browser = await launch_browser(force_direct=force_direct)
        page = await open_page(browser, rec, goto_url=rec.get("entry", {}).get("url"))
    rc = 0
    try:
        if model:
            print(f"\n=== [1] 单机型取价: {model} ===")
            rows = await executor.run_query(page, rec.get("query", {}), model, None, country)
            ok = [r for r in rows if r.get("price") is not None]
            print(f"  返回 {len(rows)} 行，其中有价 {len(ok)} 行")
            for r in ok[:12]:
                print(f"    {r['part']:<22} {r['price']} "
                      f"(material={r.get('material_fee')}, labor={r.get('labor_fee')})")
            if not ok:
                print("  [fail] 未取到任何价格")
                rc = 1
        print(f"\n=== [2] 批量发现+取价 (max={max_models or 'all'}) ===")
        out = await crawl_run.discover_and_price_via_reborn(page, rec, country, max_models=max_models)
        print(f"  [result] {len(out)} 个机型取到价")
        for m, rows, human in out:
            print(f"    {m}: {len(rows)} 条价  {human}")
        if not out:
            print("  [fail] 批量发现取到 0 个机型")
            rc = 1
    finally:
        for closer in (page.close() if page else None,
                       browser.close() if browser else None,
                       pw.stop() if pw else None):
            if closer is None:
                continue
            try:
                await asyncio.wait_for(closer, timeout=20)
            except Exception:
                pass
    return rc


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--brand", default="oppo")
    ap.add_argument("--country", default="cn")
    ap.add_argument("--model", default="")
    ap.add_argument("--max", type=int, default=0)
    ap.add_argument("--direct", action="store_true", help="强制直连（忽略代理）")
    a = ap.parse_args()
    code = asyncio.run(main(a.brand, a.country, a.model, a.max or None, a.direct))
    print(f"\n[exit] {code}")
    # os._exit 不 flush 缓冲：必须先手动 flush，否则 [result]/逐机型行会丢失
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(code)
