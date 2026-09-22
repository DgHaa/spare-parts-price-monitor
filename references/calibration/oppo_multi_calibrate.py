#!/usr/bin/env python3
"""oppo_multi_calibrate.py - OPPO 多区域备件价格 API 校准（基于已验证的 TR 方案，跨域复用 sgp 节点）。

!! 已知不可信，默认拒绝运行（2026-09-22 修订）!!
  本脚本依据的是 OPPO **遗留端点** /cnw/v1/GetPartPrice。该端点服务端**完全忽略 area
  参数**：de/mx/my/jp/ae 各国拿到的都是同一份中国大陆 CNY 价目表（实测 6 国 1844 个
  公共 (机型,备件) 键的价格集合指纹全部相等）。用它标定，只会把「CN 价被误标成本地
  币种」这个错误反向“验证”成正确结论——历史上正是它产出了 13836 行污染数据，并在
  比价页制造出 37 倍的假价差（+3614%）。
  现行正确信源是 REBORN 接口 POST /basic/v1/getProduct + getPartPriceNew，见
  references/kb/oppo.json 的 api_reborn 配置；数据清退见
  tools/purge_oppo_area_param_pollution.py。
  确需复现历史结论，请显式加 --i-know-this-endpoint-is-broken。

关键结论（本环境实测，**其中「用 area 参数切换区域」一条已被证伪**）：
  - 各区域页的支持页(spare-parts-price) curl 均 200，但部分区域 headless 下 SPA 未注入 SOWAPIPATH；
  - 真实价格 API 由 **sgp-sow-cms.oppo.com/oppo-server** 统一承载，用 area 参数切换区域，
    par-sow-cms(巴黎) 等区域节点从沙箱不可达(000/504)。
  => 加载可靠的 TR 页拿到 sgp 基址，之后跨域 fetch 时只换 area 即可覆盖 de/mx/my/jp/ae。

用法：python oppo_multi_calibrate.py [--out oppo_multi_api.json]
"""
import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _paths import find_chromium  # noqa: E402

CHROME = find_chromium()          # None -> 用 Playwright 自带 Chromium
HERE = Path(__file__).resolve().parent
TR_URL = "https://support.oppo.com/tr/spare-parts-price/"
# area -> 候选 language（按优先级），与 TR 验证一致：language 用区域码字符串
AREAS = {
    "de": ["de", "en"],
    "mx": ["es", "es-mx", "en"],
    "my": ["ms", "en-my", "en", "zh"],
    "jp": ["ja", "ja-jp", "en"],
    "ae": ["ar", "en-ae", "en"],
}


async def main(out):
    from playwright.async_api import async_playwright
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, executable_path=CHROME, args=["--no-sandbox"])
        page = await browser.new_page(locale="tr-TR")
        await page.goto(TR_URL, wait_until="commit")
        await page.wait_for_timeout(4500)
        g = await page.evaluate("""() => ({
            SOWAPIPATH: window.SOWAPIPATH, GCSMAPIPATH: window.GCSMAPIPATH,
            SOWREGION: window.SOWREGION, SOWLANGID: window.SOWLANGID})""")
        base = g.get("SOWAPIPATH") or g.get("GCSMAPIPATH")
        print(f"base={base} globals={json.dumps(g,ensure_ascii=False)}", flush=True)
        if not base:
            await browser.close()
            return {"status": "error", "error": "TR 页未注入 SOWAPIPATH"}
        results = {}
        for area, langs in AREAS.items():
            rec = {"area": area, "status": "error"}
            for lang in langs:
                prod_url = f"{base}/cnw/v1/GetPartPriceProductInfo?area={area}&language={lang}&productType=1"
                try:
                    products = await page.evaluate("""async (u) => {
                        const r = await fetch(u, {credentials:'include'});
                        if (!r.ok) return {error:'HTTP '+r.status};
                        const j = await r.json(); return j.data || j || {};
                    }""", prod_url)
                except Exception as e:
                    rec = {"area": area, "status": "error", "error": str(e)}
                    continue
                if isinstance(products, dict) and products.get("error"):
                    rec = {"area": area, "status": "error", "error": products["error"]}
                    continue
                plist = products if isinstance(products, list) else products.get("list", [])
                phones = [x for x in plist if "智能手机" in (x.get("productTypeName") or "")
                          or "手机" in (x.get("typeName") or "")
                          or "phone" in (x.get("productTypeName") or "").lower()]
                if not phones and plist:
                    phones = plist
                if phones:
                    rows = []
                    for t in phones[:2]:
                        mdl = t.get("productModel") or t.get("name") or t.get("model")
                        purl = f"{base}/cnw/v1/GetPartPrice?area={area}&language={lang}&productType=1&productModel={mdl}"
                        d = await page.evaluate("""async (u) => {
                            const r = await fetch(u, {credentials:'include'});
                            if (!r.ok) return {error:'HTTP '+r.status};
                            const j = await r.json(); return j.data || j || {};
                        }""", purl)
                        parts = d if isinstance(d, list) else d.get("list", d.get("partList", []))
                        rows.append({"model": mdl, "parts": [
                            {"partName": x.get("partName"), "partPrice": x.get("partPrice"),
                             "typeName": x.get("typeName")} for x in parts]})
                    rec = {"area": area, "status": "ok", "lang_used": lang,
                           "phone_count": len(phones),
                           "sample_models": [x.get("productModel") or x.get("name") for x in phones[:8]],
                           "prices": rows}
                    break
            print(f"[{area}] {rec['status']} lang={rec.get('lang_used')}", flush=True)
            results[area] = rec
        await browser.close()
    doc = {"captured_at": datetime.now().isoformat(timespec="seconds"),
           "api_base": base, "tr_globals": g, "results": results}
    if out:
        op = Path(out)
        op.parent.mkdir(parents=True, exist_ok=True)
        op.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"saved -> {op}")
    return doc


_LEGACY_BANNER = """[已知不可信 — 默认拒绝运行]
本脚本基于 OPPO 遗留端点 /cnw/v1/GetPartPrice 标定。该端点服务端**忽略 area 参数**，
de/mx/my/jp/ae 各国返回的都是同一份中国大陆 CNY 价目表（2026-09-22 实测 6 国 1844 个
公共 (机型,备件) 键价格指纹全等）。用它标定只会把「CN 价被误标成本地币种」反向
"验证"成正确——历史上正是它产出了 13836 行污染数据。
现行正确信源：REBORN 接口，见 references/kb/oppo.json 的 api_reborn 配置。
确需复现历史结论，加 --i-know-this-endpoint-is-broken 显式确认。
"""


def run():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(HERE / "oppo_multi_api.json"))
    ap.add_argument("--i-know-this-endpoint-is-broken", action="store_true",
                    help="确认已知该端点不可信（area 被忽略），仍要运行以复现历史结论")
    args = ap.parse_args()
    if not args.i_know_this_endpoint_is_broken:
        print(_LEGACY_BANNER)
        return 2
    import asyncio
    asyncio.run(main(args.out))


if __name__ == "__main__":
    sys.exit(run())
