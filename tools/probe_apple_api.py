"""tools/probe_apple_api.py —— 验证 Apple 官方定价 API 能否按"机型 TAG"精确返回。

已知（netprobe 实测）：
  页面加载时请求 https://support.apple.com/ols/api/pricing/products/services/pricing-estimate
                  ?locale=de-de&pricing_type=OOW&parent_tag_id=TAG_1754518739895
  且机型在 DOM 下拉里带官方 TAG（iPhone 11 = TAG_1754518887992）。
本脚本在 support.apple.com 同源内 fetch，检查：
  ① 全量返回结构（是否每机型带 tag id + 价格）
  ② 传入某机型 tag 作为 parent_tag_id 是否只返回该机型（=> 机型级数据链接可用）
"""
import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from crawler.core import launch_browser  # noqa: E402

OUT = ROOT / "output"
LOCALE = "de-de"
ROOT_TAG = "TAG_1754518739895"       # 页面自带的 iPhone 品类根 tag
IPHONE11_TAG = "TAG_1754518887992"   # DOM 下拉里 iPhone 11 的 tag


async def jfetch(page, url):
    return await page.evaluate("""async (u) => {
        try {
            const r = await fetch(u, {credentials: 'include'});
            const t = await r.text();
            let j = null; try { j = JSON.parse(t); } catch(e) {}
            return {status: r.status, json: j, text: j ? null : t.slice(0, 800)};
        } catch (e) { return {error: String(e)}; }
    }""", url)


async def main():
    pw, browser = await launch_browser()
    res = {}
    try:
        page = await browser.new_page(locale="de-DE")
        await page.goto(f"https://support.apple.com/{LOCALE}/iphone/repair",
                        wait_until="domcontentloaded", timeout=40000)
        await page.wait_for_timeout(5000)

        base = "https://support.apple.com/ols/api/pricing/products/services/pricing-estimate"
        # ① 全量（页面原生请求）
        u_all = f"{base}?locale={LOCALE}&pricing_type=OOW&parent_tag_id={ROOT_TAG}"
        r_all = await jfetch(page, u_all)
        res["all"] = {"url": u_all, "status": r_all.get("status"),
                      "keys": list((r_all.get("json") or {}).keys())[:20] if isinstance(r_all.get("json"), dict) else "list",
                      "raw_head": json.dumps(r_all.get("json"), ensure_ascii=False)[:2500] if r_all.get("json") else r_all.get("text")}
        # ② 机型 tag 作为 parent_tag_id
        u_m = f"{base}?locale={LOCALE}&pricing_type=OOW&parent_tag_id={IPHONE11_TAG}"
        r_m = await jfetch(page, u_m)
        res["by_model_tag"] = {"url": u_m, "status": r_m.get("status"),
                               "raw_head": json.dumps(r_m.get("json"), ensure_ascii=False)[:2500] if r_m.get("json") else r_m.get("text")}
        # ③ 试 tag_id / product_tag_id 变体
        for p in ("tag_id", "product_tag_id", "child_tag_id"):
            u = f"{base}?locale={LOCALE}&pricing_type=OOW&{p}={IPHONE11_TAG}"
            r = await jfetch(page, u)
            res[f"variant_{p}"] = {"url": u, "status": r.get("status"),
                                   "raw_head": (json.dumps(r.get("json"), ensure_ascii=False)[:700]
                                                if r.get("json") else r.get("text"))}
        # ④ 页面 query 深链候选：看能否直接把 iPhone 11 选中
        for qs in (f"?services=service&product={IPHONE11_TAG}",
                   f"?services=service&model={IPHONE11_TAG}",
                   f"?services=service&tag={IPHONE11_TAG}",
                   "?services=service&product=iphone-11"):
            p2 = await browser.new_page(locale="de-DE")
            try:
                await p2.goto(f"https://support.apple.com/{LOCALE}/iphone/repair{qs}",
                              wait_until="domcontentloaded", timeout=40000)
                await p2.wait_for_timeout(6000)
                sel = await p2.evaluate(
                    """() => {const d=document.querySelector('.device-dropdown'),
                                    m=document.querySelector('.model-dropdown');
                       const pick = e => e ? (e.options[e.selectedIndex]?.text || null) : null;
                       const body = document.body.innerText;
                       const i = body.indexOf('Batterieservice');
                       return {device: pick(d), model: pick(m),
                               price_area: i>=0 ? body.slice(i, i+180) : null};}""")
                res[f"query{qs}"] = sel
            except Exception as e:
                res[f"query{qs}"] = {"error": str(e)[:200]}
            finally:
                await p2.close()
        await page.close()
    finally:
        await browser.close()
        await pw.stop()
    OUT.mkdir(exist_ok=True)
    f = OUT / "apple_api_probe.json"
    f.write_text(json.dumps(res, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(res, ensure_ascii=False, indent=2)[:7000], flush=True)
    print(f"\n[saved] {f}", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
