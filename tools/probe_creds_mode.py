"""tools/probe_creds_mode.py - 判定 OPPO CN REBORN 取价是否受 session cookie 影响。

背景：官网人读页与直连匿名 HTTP 均显示 Pad 5 屏幕 850 / 主板 16G512G 2900（共 11 条备件），
但浏览器内 credentials:'include' 的 fetch 返回屏幕 950 / 9 条。本脚本在同一页面上下文里
依次用 omit / include 发同一 POST，定位差异是否来自 cookie。
"""
import asyncio
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

from crawler.core import launch_browser, open_page  # noqa: E402
import executor  # noqa: E402
from playwright.async_api import async_playwright  # noqa: E402

CODE = "80ab6f7c252b424080fab2d60d1fa428"
BODY = {"region": "CN", "marketingModelCode": CODE, "regionIsoCode2": "cn",
        "isoLanguageCode": "zh-CN", "sourceRoute": "1"}
URL = "https://sow-cms.oppo.com/oppo-api/basic/v1/getPartPriceNew"


async def main():
    pw, browser = await launch_browser()
    pg = await browser.new_page(viewport={"width": 1280, "height": 800})
    try:
        await pg.goto("https://support.oppo.com/cn/spare-parts-price/",
                      wait_until="domcontentloaded", timeout=30000)
        await pg.wait_for_timeout(3000)
        print("cookies on page:", (await pg.evaluate("() => document.cookie"))[:200])
        for cred in ("omit", "include"):
            res = await executor._page_fetch_post(pg, URL, BODY, 20000, cred)
            groups = ((res or {}).get("data") or {}).get("partPriceList") or []
            rows = []
            for g in groups:
                for c in (g.get("childList") or [g]):
                    rows.append((c.get("partName"), c.get("retailPrice"), c.get("laborCostAmount")))
            print(f"\n=== credentials={cred} ===  行数={len(rows)}")
            for r in rows:
                print("   ", r)
    finally:
        for closer in (pg.close(), browser.close(), pw.stop()):
            try:
                await asyncio.wait_for(closer, timeout=20)
            except Exception:
                pass


asyncio.run(main())
os._exit(0)
