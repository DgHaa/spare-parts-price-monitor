"""tools/capture_oppo_request.py - 抓取 OPPO 中国官网 /spare-parts-price/ 详情页
实际发出的 getProduct / getPartPriceNew 请求与响应，定位 850/11(官网人读) vs 950/9(抓取) 差异。

用法：
    python tools/capture_oppo_request.py
"""
import asyncio
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

from crawler.core import launch_browser

TARGET = ("https://support.oppo.com/cn/spare-parts-price/"
          "#/detail?marketingModelCode=80ab6f7c252b424080fab2d60d1fa428")


async def main():
    pw, browser = await launch_browser(force_direct=False)
    page = await browser.new_page()
    captured = []

    async def on_request(req):
        u = req.url
        if "getPartPriceNew" in u or "getProduct" in u or "GetPartPrice" in u:
            captured.append(("REQ", u, req.method, dict(req.headers), req.post_data))

    async def on_response(resp):
        u = resp.url
        if "getPartPriceNew" in u or "getProduct" in u or "GetPartPrice" in u:
            try:
                body = await resp.body()
                txt = body.decode("utf-8", "replace")
            except Exception as e:  # noqa
                txt = f"<body err {e}>"
            captured.append(("RESP", u, resp.status, txt))

    page.on("request", on_request)
    page.on("response", on_response)

    print("[nav] 打开", TARGET)
    try:
        await page.goto(TARGET, wait_until="domcontentloaded", timeout=30000)
    except Exception as e:  # noqa
        print("  [warn] goto:", type(e).__name__, e)
    # 让 SPA 触发接口请求
    await page.wait_for_timeout(9000)

    if not captured:
        print("[warn] 未捕获到任何 getProduct/getPartPriceNew 请求")

    seen = set()
    for tag, u, a, b, c in captured:
        key = (tag, u, a)
        if key in seen:
            continue
        seen.add(key)
        print("\n" + "=" * 70)
        print(tag, a, u)
        if tag == "REQ":
            print("-- headers --")
            print(json.dumps(b, ensure_ascii=False, indent=1)[:2500])
            print("-- post_data --")
            print(c)
        else:
            print("-- status", a)
            print("-- body (preview) --")
            print(b[:5000])

    await browser.close()
    await pw.stop()


if __name__ == "__main__":
    asyncio.run(main())
