"""tools/probe_ua_mode.py - 验证 OPPO CN REBORN 价格是否按 User-Agent 分流。

假设：浏览器(HeadlessChrome UA)取到 950/9，匿名 HTTP(Mozilla UA)取到 850/11（与官网人读页一致）。
若把浏览器上下文的 UA 改成桌面 Chrome 即复现 850/11，则根因为网关按 UA 分流/反爬，
修复方式 = 为 OPPO CN 的 Playwright 上下文显式设置桌面 User-Agent。
"""
import asyncio
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

from crawler.core import launch_browser  # noqa: E402
import executor  # noqa: E402

CODE = "80ab6f7c252b424080fab2d60d1fa428"
BODY = {"region": "CN", "marketingModelCode": CODE, "regionIsoCode2": "cn",
        "isoLanguageCode": "zh-CN", "sourceRoute": "1"}
URL = "https://sow-cms.oppo.com/oppo-api/basic/v1/getPartPriceNew"
DESKTOP_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")


async def main():
    pw, browser = await launch_browser()
    for label, ctxkw in (("default(headless UA)", {}),
                         ("desktop UA", {"user_agent": DESKTOP_UA})):
        ctx = await browser.new_context(viewport={"width": 1280, "height": 800}, **ctxkw)
        pg = await ctx.new_page()
        try:
            await pg.goto("https://support.oppo.com/cn/spare-parts-price/",
                          wait_until="domcontentloaded", timeout=30000)
            await pg.wait_for_timeout(2500)
            res = await executor._page_fetch_post(pg, URL, BODY, 20000, "omit")
            groups = ((res or {}).get("data") or {}).get("partPriceList") or []
            rows = []
            for g in groups:
                for c in (g.get("childList") or [g]):
                    rows.append((c.get("partName"), c.get("retailPrice")))
            print(f"\n=== {label} === 行数={len(rows)}")
            for r in rows[:6]:
                print("   ", r)
            print("   ...(共", len(rows), "条)")
        finally:
            for closer in (pg.close(), ctx.close()):
                try:
                    await asyncio.wait_for(closer, timeout=10)
                except Exception:
                    pass
    for closer in (browser.close(), pw.stop()):
        try:
            await asyncio.wait_for(closer, timeout=20)
        except Exception:
            pass


asyncio.run(main())
os._exit(0)
