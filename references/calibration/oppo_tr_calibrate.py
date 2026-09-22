#!/usr/bin/env python3
"""oppo_tr_calibrate.py - OPPO 土耳其站备件价格 API 校准脚本。

!! 已知不可信，默认拒绝运行（2026-09-22 修订）!!
  本脚本标定的 /cnw/v1/GetPartPrice 是 OPPO **遗留端点**，服务端**完全忽略 area 参数**：
  tr 拿到的与其他区域完全相同，都是中国大陆 CNY 价目表（实测 6 国 1844 个公共
  (机型,备件) 键价格指纹全等）。故「partPrice(备件价,TRY,无符号)」这一结论是错的——
  那个数字是人民币，被误当成了里拉。历史上它产出了 13836 行污染数据，并在比价页
  制造 37 倍假价差（+3614%）。清退见 tools/purge_oppo_area_param_pollution.py。
  另：本文档原先记的「REBORN 接口返回 code:10018 不可用」**已被证伪**——REBORN
  （POST /basic/v1/getProduct + getPartPriceNew）现行可用，且是唯一可信信源，
  见 references/kb/oppo.json 的 api_reborn 配置。
  确需复现历史结论，请显式加 --i-know-this-endpoint-is-broken。

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
    【2026-09-22 更正】partPrice 实际是**人民币(CNY)**，不是里拉——area 参数被服务端忽略。
  - productType 参数被忽略，列表恒返回全部品类；需按 productTypeName=='智能手机' 过滤

依赖：pip install playwright && playwright install chromium
运行：python oppo_tr_calibrate.py [--model "OPPO Ace2"] [--out oppo_tr_api.json]
"""
import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _paths import ensure_evidence, find_chromium  # noqa: E402

CHROME = find_chromium()          # None -> 用 Playwright 自带 Chromium
HERE = Path(__file__).resolve().parent
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


_LEGACY_BANNER = """[已知不可信 — 默认拒绝运行]
本脚本标定的 OPPO 遗留端点 /cnw/v1/GetPartPrice 服务端**忽略 area 参数**，tr 拿到的
是与中国大陆完全相同的人民币价目表（2026-09-22 实测 6 国 1844 个公共 (机型,备件)
键价格指纹全等）。用它只会把「CNY 被误标成 TRY」反向"验证"成正确结论。
现行正确信源：REBORN 接口 POST /basic/v1/getProduct + getPartPriceNew。
确需复现历史结论，加 --i-know-this-endpoint-is-broken 显式确认。
"""


def run():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default=None)
    ap.add_argument("--out", default=str(HERE / "oppo_tr_api.json"))
    ap.add_argument("--i-know-this-endpoint-is-broken", action="store_true",
                    help="确认已知该端点不可信（area 被忽略），仍要运行以复现历史结论")
    args = ap.parse_args()
    if not args.i_know_this_endpoint_is_broken:
        print(_LEGACY_BANNER)
        return 2

    import asyncio
    res = asyncio.run(main(args.model, args.out))
    print(json.dumps(res, ensure_ascii=False, indent=2))
    if args.out and res.get("status") == "ok":
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(res, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\nsaved -> {out}")
    if res.get("status") != "ok":
        sys.exit(1)


if __name__ == "__main__":
    sys.exit(run())
