"""截取海报所需真实页面图：总览 / 比价矩阵 / 异动告警。
服务需先起在 http://localhost:8011。输出到 output/shots_poster/*.png
"""
import asyncio
from pathlib import Path
from playwright.async_api import async_playwright

BASE = "http://localhost:8011"
OUT = Path("output/shots_poster")
OUT.mkdir(parents=True, exist_ok=True)


async def go(page, hash_route, wait=3.0):
    await page.goto(BASE + "/#" + hash_route, wait_until="domcontentloaded")
    await page.wait_for_timeout(int(wait * 1000))


async def shot_view(page, route, name, wait=3.0):
    await go(page, route, wait)
    await page.screenshot(path=str(OUT / (name + ".png")), full_page=False)
    print("shot:", name, flush=True)


async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        page = await browser.new_page(viewport={"width": 1366, "height": 900}, device_scale_factor=2)
        await page.goto(BASE + "/", wait_until="domcontentloaded")
        await page.wait_for_timeout(2500)
        # 1) 总览
        await shot_view(page, "overview", "overview", wait=3.0)
        # 2) 比价矩阵（默认 同档位·跨品牌对标）
        await shot_view(page, "matrix", "matrix", wait=3.5)
        # 3) 异动告警
        await shot_view(page, "alerts", "alerts", wait=3.0)
        # 4) 运行监控（alerts 为空时的替换候选）
        await shot_view(page, "monitor", "monitor", wait=4.0)
        await browser.close()
    print("DONE", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
