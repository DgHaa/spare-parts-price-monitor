#!/usr/bin/env python3
"""oppo_tr_calibrate.py - OPPO 土耳其站备件价格 API 校准脚本（已验证可用）。

用途：
  1) 在真实浏览器(Playwright)内加载 OPPO TR 备件价格页；
  2) 从页面 JS 全局变量推导出真实价格 API 基址（window.SOWAPIPATH / GCSMAPIPATH）；
  3) 在浏览器同源上下文里直接调用价格 API（绕过 CDN 对 curl 的 302 拦截）；
  4) 拉取机型列表(智能手机) + 指定机型备件价，作为 KB 校准锚点。

已知结论（本环境实测）：
  - 价格由内部 API 驱动，非渲染后的 DOM 表：
      GET {SOWAPIPATH}/cnw/v1/GetPartPriceProductInfo?area=tr&language=tr&productType=1
      GET {SOWAPIPATH}/cnw/v1/GetPartPrice?area=tr&language=tr&productType=1&productModel=<model>
    SOWAPIPATH = https://sgp-sow-cms.oppo.com/oppo-server
    （REBORN 的 POST /basic/v1/getProduct|getPartPrice 返回 code:10018，不可用；用旧 GET /cnw/v1/）
  - 返回字段：partName(部件) / partPrice(备件价，TRY，无符号) / partModel / typeName
  - productType 参数被忽略，列表恒返回全部品类；需按 productTypeName=='智能手机' 过滤

依赖：pip install playwright && playwright install chromium
运行：python oppo_tr_calibrate.py [--model "OPPO Ace2"] [--out oppo_tr_api.json]
"""
import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

PY = r"C:\Users\Dong\.workbuddy\binaries\python\envs\default\Scripts\python.exe"
CHROME = r"C:\Users\Dong\AppData\Local\ms-playwright\chromium-1234\chrome-win64\chrome.exe"
URL = "https://support.oppo.com/tr/spare-parts-price/"


async def main(model=None, out=None):
    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, executable_path=CHROME,
                                           args=["--no-sandbox"])
        page = await browser.new_page(locale="tr-TR")
        await page.goto(URL, wait_until="commit")
        await page.wait_for_timeout(4000)

        # 1) 推导真实 API 基址
        globals_ = await page.evaluate("""() => ({
            SOWAPIPATH: window.SOWAPIPATH,
            GCSMAPIPATH: window.GCSMAPIPATH,
            SOWREGION: window.SOWREGION,
            SOWLANGID: window.SOWLANGID
        })""")

        base = globals_.get("SOWAPIPATH") or globals_.get("GCSMAPIPATH")
        if not base:
            await browser.close()
            return {"status": "error", "error": "未能从页面读取 API 基址(SOWAPIPATH/GCSMAPIPATH)"}

        # 2) 机型列表（智能手机）
        prod_url = f"{base}/cnw/v1/GetPartPriceProductInfo?area=tr&language=tr&productType=1"
        products = await page.evaluate(
            """async (u) => {
                const r = await fetch(u, {credentials: 'include'});
                if (!r.ok) return {error: 'HTTP ' + r.status};
                const j = await r.json();
                return j.data || j || {};
            }""", prod_url)
        if isinstance(products, dict) and products.get("error"):
            await browser.close()
            return {"status": "error", "error": products["error"], "globals": globals_}

        # 兼容返回结构：data 可能是数组或 {list:[...]}
        plist = products if isinstance(products, list) else products.get("list", [])
        phones = [x for x in plist if (x.get("productTypeName") or "").find("智能手机") >= 0
                  or (x.get("typeName") or "").find("手机") >= 0]

        # 3) 取价：优先指定 model，否则取前 2 个手机
        targets = []
        if model:
            targets = [m for m in phones if model.lower() in (m.get("productModel") or m.get("name", "")).lower()]
            if not targets:
                targets = [{"productModel": model}]
        if not targets:
            targets = phones[:2]

        rows_out = []
        for t in targets:
            mdl = t.get("productModel") or t.get("name") or model
            price_url = f"{base}/cnw/v1/GetPartPrice?area=tr&language=tr&productType=1&productModel={mdl}"
            data = await page.evaluate(
                """async (u) => {
                    const r = await fetch(u, {credentials: 'include'});
                    if (!r.ok) return {error: 'HTTP ' + r.status};
                    const j = await r.json();
                    return j.data || j || {};
                }""", price_url)
            if isinstance(data, dict) and data.get("error"):
                rows_out.append({"model": mdl, "error": data["error"]})
                continue
            parts = data if isinstance(data, list) else data.get("list", data.get("partList", []))
            rows_out.append({
                "model": mdl,
                "parts": [{"partName": x.get("partName"), "partPrice": x.get("partPrice"),
                           "typeName": x.get("typeName")} for x in parts]
            })

        await browser.close()
        return {
            "status": "ok",
            "captured_at": datetime.now().isoformat(timespec="seconds"),
            "page_url": URL,
            "globals": globals_,
            "api_base": base,
            "phone_count": len(phones),
            "sample_models": [x.get("productModel") or x.get("name") for x in phones[:8]],
            "prices": rows_out,
        }


def run():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default=None)
    ap.add_argument("--out", default="oppo_tr_api.json")
    args = ap.parse_args()

    import asyncio
    res = asyncio.run(main(args.model, args.out))
    print(json.dumps(res, ensure_ascii=False, indent=2))
    if args.out and res.get("status") == "ok":
        Path(args.out).write_text(json.dumps(res, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\nsaved -> {args.out}")
    if res.get("status") != "ok":
        sys.exit(1)


if __name__ == "__main__":
    run()
