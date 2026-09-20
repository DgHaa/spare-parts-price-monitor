#!/usr/bin/env python3
"""xiaomi_calibrate.py - 小米备件价格页校准（截图落在 output/evidence/）。"""
import json, re, sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _paths import ensure_evidence, find_chromium  # noqa: E402

CHROME = find_chromium()          # None -> 用 Playwright 自带 Chromium
URL = "https://www.mi.com/service/materialprice"
async def main():
    from playwright.async_api import async_playwright
    out = {}
    async with async_playwright() as p:
        b = await p.chromium.launch(headless=True, executable_path=CHROME, args=["--no-sandbox"])
        pg = await b.new_page()
        try:
            await pg.goto(URL, wait_until="commit")
            await pg.wait_for_timeout(12000)
            # 点系列
            await pg.get_by_text("Xiaomi 13 Pro", exact=False).first.click(timeout=8000)
            await pg.wait_for_timeout(2000)
            # 点具体型号（含 128GB 的变体）
            var = pg.get_by_text("Xiaomi 13 Pro 8GB内存 陶瓷黑 128GB", exact=False).first
            out["variant_count"] = await var.count()
            await var.click(timeout=8000)
            await pg.wait_for_timeout(4000)
            body = await pg.inner_text("body")
            # 维修部件关键词 + 价
            kw = re.compile(r"屏幕|电池|主板|后盖|摄像头|听筒|马达|扬声器|充电|总成|外壳|镜座|内存|字库|校验", re.I)
            out["part_lines"] = [l.strip() for l in body.split("\n") if kw.search(l)][:30]
            out["price_lines"] = [l.strip() for l in body.split("\n") if re.search(r"[¥￥]\s?[\d.,]+|[\d.,]+\s?元", l)][:30]
            # 价格表结构
            out["table_count"] = await pg.locator("table").count()
            if await pg.locator("table").count():
                out["table0"] = (await pg.locator("table").first.inner_text())[:800]
            out["repair_panel"] = await pg.evaluate("""() => {
                const all=[...document.querySelectorAll('[class*=repair],[class*=material],[class*=price-detail],[class*=part],[class*=detail]')];
                return all.slice(0,6).map(e=>({cls:e.className+'', txt:(e.innerText||'').trim().slice(0,300)}));
            }""")
            await pg.screenshot(path=str(ensure_evidence() / "xiaomi_cal3.png"), full_page=True)
        except Exception as e:
            out["error"] = str(e)[:300]
        await b.close()
        return out
if __name__ == "__main__":
    import asyncio
    print(json.dumps(asyncio.run(main()), ensure_ascii=False, indent=2))
