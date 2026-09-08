"""tools/probe_apple_families.py —— 摸清 Apple 官方定价接口的"机型族 -> parent_tag_id"映射。

已知（probe_apple_api 实测）：
  GET https://support.apple.com/ols/api/pricing/products/services/pricing-estimate
      ?locale=de-de&pricing_type=OOW&parent_tag_id=TAG_1754518739895   -> 200 全量 JSON
  按单机型 tag 查询 -> 404（Apple 不提供单机型接口）

本脚本要回答：
  ① parent_tag_id 是"iPhone 全部"还是"某一代机型族"？
  ② 族列表从哪个接口/内嵌 JSON 拿？能否为库内每个机型定位到它所属的族 tag？
  ③ 200 的那份 JSON 里，单个机型（含各项服务价）的定位路径是什么？
输出：output/apple_families_probe.json
"""
import asyncio
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from crawler.core import launch_browser  # noqa: E402

OUT = ROOT / "output"
LOCALE = "de-de"
PAGE = "https://support.apple.com/de-de/iphone/repair"
KNOWN_TAG = "TAG_1754518739895"
API = "https://support.apple.com/ols/api/pricing/products/services/pricing-estimate"


async def fetch_json(page, url):
    return await page.evaluate("""async (u) => {
        try { const r = await fetch(u, {credentials:'include'});
              const t = await r.text();
              let j = null; try { j = JSON.parse(t); } catch(e) {}
              return {status: r.status, len: t.length, json: j, head: t.slice(0, 400)}; }
        catch(e) { return {error: String(e).slice(0,160)}; }
    }""", url)


def shape(o, depth=0, maxd=4):
    """紧凑描述 JSON 结构。"""
    if depth > maxd:
        return "..."
    if isinstance(o, dict):
        return {k: shape(v, depth + 1, maxd) for k, v in list(o.items())[:18]}
    if isinstance(o, list):
        return [f"list({len(o)})"] + ([shape(o[0], depth + 1, maxd)] if o else [])
    s = str(o)
    return s[:90]


async def main():
    pw, browser = await launch_browser()
    res = {}
    reqs = []
    try:
        page = await browser.new_page(locale="de-DE")
        page.on("request", lambda r: reqs.append({"m": r.method, "u": r.url[:400], "t": r.resource_type}))
        await page.goto(PAGE, wait_until="domcontentloaded", timeout=45000)
        await page.wait_for_timeout(9000)

        # ① 页面加载时命中的 pricing / tag 相关请求
        res["pricing_requests"] = [r for r in reqs if re.search(r"pricing|ols/api|tag", r["u"], re.I)]

        # ② 页面内嵌 JSON 里的 TAG_ 出现情况
        html = await page.content()
        tags = sorted(set(re.findall(r"TAG_\d{10,}", html)))
        res["tags_in_html_count"] = len(tags)
        res["tags_in_html_sample"] = tags[:12]
        # 试着把 "机型名 <-> TAG_" 的邻近关系抓出来
        res["tag_context"] = [m[:220] for m in re.findall(r".{110}TAG_\d{10,}.{60}", html)[:8]]

        # ③ 已知 tag 的 200 JSON 到底是什么粒度
        r = await fetch_json(page, f"{API}?locale={LOCALE}&pricing_type=OOW&parent_tag_id={KNOWN_TAG}")
        res["known_tag_status"] = r.get("status")
        if r.get("json"):
            j = r["json"]
            res["known_tag_top_keys"] = list(j.keys())
            res["known_tag_shape"] = shape(j, maxd=3)
            # 找机型清单
            blob = json.dumps(j, ensure_ascii=False)
            res["known_tag_mentions"] = {
                nm: (nm in blob) for nm in
                ("iPhone 11", "iPhone 12", "iPhone 13", "iPhone 15", "iPhone 17", "iPhone 17 Pro Max")}
            res["known_tag_len"] = r["len"]

        # ④ 页面 DOM 上的机型下拉：选项元素属性里是否带 tag
        res["dom_dropdowns"] = await page.evaluate(
            """() => {const out = {};
               for (const sel of ['.device-dropdown','.model-dropdown','select','[role=listbox]']) {
                 const els = [...document.querySelectorAll(sel)].slice(0,2);
                 out[sel] = els.map(e => e.outerHTML.slice(0, 900));
               }
               return out;}""")

        # ⑤ 主动选一个机型族，看会不会发新的 pricing 请求（带该族 tag）
        before = len(reqs)
        picked = await page.evaluate(
            """() => {const dd = document.querySelector('.device-dropdown');
               if (!dd) return null;
               if (dd.tagName === 'SELECT') { const o=[...dd.options].find(x=>/iPhone 11/i.test(x.textContent));
                 if (o) { dd.value=o.value; dd.dispatchEvent(new Event('change',{bubbles:true})); return o.textContent.trim(); } }
               dd.click(); return 'clicked';}""")
        await page.wait_for_timeout(6000)
        res["picked_device"] = picked
        res["requests_after_pick"] = [r for r in reqs[before:]
                                      if re.search(r"pricing|ols/api|tag", r["u"], re.I)]
        await page.close()
    finally:
        await browser.close()
        await pw.stop()
    OUT.mkdir(exist_ok=True)
    f = OUT / "apple_families_probe.json"
    f.write_text(json.dumps(res, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(res, ensure_ascii=False, indent=2)[:7000], flush=True)
    print(f"\n[saved] {f}", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
