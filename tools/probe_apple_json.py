"""tools/probe_apple_json.py —— 落盘 Apple 官方定价 JSON（4 个语区）并确认"单机型价格是否就在这份 JSON 里"。

关键结论待验证：repair 页加载时只发两个定价请求（OOW / AC+），选机型不再发请求 =>
全部机型价格在这一份 JSON 内，故这份 JSON 的 URL + 机型 tag 路径 就是"该机型的实际数据链接"。
输出：output/apple_pricing_<locale>.json + output/apple_json_probe.json（结构摘要）
"""
import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from crawler.core import launch_browser  # noqa: E402

OUT = ROOT / "output"
API = "https://support.apple.com/ols/api/pricing/products/services/pricing-estimate"
ROOT_TAG = "TAG_1754518739895"          # iPhone 根产品（repair 页实测）
# 库内 4 个国家 -> Apple 语区 + repair 页
LOCALES = {
    "de": ("de-de", "https://support.apple.com/de-de/iphone/repair"),
    "jp": ("ja-jp", "https://support.apple.com/ja-jp/iphone/repair"),
    "ae": ("en-ae", "https://support.apple.com/en-ae/iphone/repair"),
    "my": ("en-my", "https://support.apple.com/en-my/iphone/repair"),
}


async def main():
    pw, browser = await launch_browser()
    summary = {}
    try:
        for cc, (locale, page_url) in LOCALES.items():
            page = await browser.new_page(locale=locale)
            try:
                await page.goto(page_url, wait_until="domcontentloaded", timeout=45000)
                await page.wait_for_timeout(4000)
                url = f"{API}?locale={locale}&pricing_type=OOW&parent_tag_id={ROOT_TAG}"
                r = await page.evaluate("""async (u) => {
                    try { const res = await fetch(u, {credentials:'include'});
                          const t = await res.text();
                          return {status: res.status, text: t}; }
                    catch(e) { return {error: String(e).slice(0,160)}; }
                }""", url)
                if r.get("status") != 200:
                    summary[cc] = {"url": url, "status": r.get("status"), "error": r.get("error"),
                                   "head": (r.get("text") or "")[:200]}
                    continue
                data = json.loads(r["text"])
                (OUT / f"apple_pricing_{cc}.json").write_text(
                    json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
                fams = data.get("products", [])
                # 展开：族 -> 变体 -> 服务价
                flat = []
                for f in fams:
                    for ch in (f.get("childrenProducts") or []):
                        svc = ch.get("services") or ch.get("servicePricing") or ch.get("pricing") or []
                        flat.append({
                            "family": f.get("product_loc_title"),
                            "family_tag": f.get("product_tag_id"),
                            "model": ch.get("product_loc_title"),
                            "model_eng": ch.get("product_eng_title"),
                            "model_tag": ch.get("product_tag_id"),
                            "n_services": len(svc) if isinstance(svc, list) else "n/a",
                            "child_keys": list(ch.keys()),
                            "svc_sample": (svc[:2] if isinstance(svc, list) else str(svc)[:200]),
                        })
                summary[cc] = {
                    "url": url, "status": 200, "n_families": len(fams),
                    "n_models": len(flat),
                    "family_titles": [f.get("product_loc_title") for f in fams],
                    "sample_models": flat[:3],
                    "all_model_titles": [x["model"] for x in flat],
                }
            except Exception as e:
                summary[cc] = {"error": f"{type(e).__name__}: {str(e)[:200]}"}
            finally:
                await page.close()
    finally:
        await browser.close()
        await pw.stop()
    OUT.mkdir(exist_ok=True)
    f = OUT / "apple_json_probe.json"
    f.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2)[:7000], flush=True)
    print(f"\n[saved] {f}", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
