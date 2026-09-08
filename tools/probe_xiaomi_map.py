"""tools/probe_xiaomi_map.py —— 找小米"机型名 -> class_id"的批量来源。

已知：点某机型后前端发 GET
  https://api2.service.order.mi.com/repair_price/shop_band_wx_price?class_id=44799&callback=__jpN
要为库内 200+ 机型逐个生成机型级链接，必须能**批量**拿到 class_id，
否则只能逐个点击（成本高）。本脚本查三处：
  ① 页面加载时的请求里是否已有"机型列表(含 class_id)"接口
  ② 页面 HTML/内嵌 JSON 是否直接带 class_id
  ③ Vue 根实例数据里是否有机型列表
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


async def main():
    pw, browser = await launch_browser()
    res = {}
    reqs = []
    try:
        page = await browser.new_page(locale="zh-CN")
        page.on("request", lambda r: reqs.append({"m": r.method, "url": r.url[:300], "t": r.resource_type}))
        await page.goto("https://www.mi.com/service/materialprice",
                        wait_until="domcontentloaded", timeout=45000)
        await page.wait_for_timeout(14000)

        res["all_requests"] = [r for r in reqs if not re.search(
            r"\.(png|jpg|jpeg|webp|gif|css|woff2?|svg|ico)(\?|$)", r["url"], re.I)]
        # ① 明显的"列表"类接口
        res["candidate_list_apis"] = [r for r in reqs if re.search(
            r"repair_price|material|class|product|goods|band", r["url"], re.I)]
        # ② HTML 里是否直接含 class_id
        html = await page.content()
        res["html_has_class_id"] = "class_id" in html
        res["html_class_id_hits"] = re.findall(r".{60}class_id.{60}", html)[:5]
        res["html_len"] = len(html)
        # ③ 尝试从 Vue 实例 / 全局变量捞机型列表
        res["globals"] = await page.evaluate(
            """() => {const keys = Object.keys(window).filter(k => /nuxt|initial|state|data|goods|class/i.test(k));
               const out = {};
               for (const k of keys.slice(0, 12)) {
                 try { const v = window[k];
                       out[k] = typeof v === 'object' ? JSON.stringify(v).slice(0, 300) : String(v).slice(0,120); }
                 catch(e) { out[k] = 'ERR'; }
               }
               return out;}""")
        res["vue_probe"] = await page.evaluate(
            """() => {const app = document.querySelector('#app') || document.body.firstElementChild;
               const v = app && (app.__vue__ || app.__vue_app__);
               if (!v) return 'no vue instance';
               try { const d = v.$data || (v._instance && v._instance.data) || {};
                     return JSON.stringify(d).slice(0, 1200); } catch(e) { return 'ERR ' + e; }}""")
        # ④ 直接试"全量列表"候选端点（同源 fetch）
        for u in ("https://api2.service.order.mi.com/repair_price/shop_band_wx_class?callback=cb",
                  "https://api2.service.order.mi.com/repair_price/shop_band_wx_list?callback=cb",
                  "https://api2.service.order.mi.com/repair_price/class_list?callback=cb"):
            res[f"try_{u.rsplit('/', 1)[-1]}"] = await page.evaluate(
                """async (u) => {try{const r=await fetch(u);const t=await r.text();
                   return {status:r.status, head:t.slice(0,300)};}catch(e){return {error:String(e).slice(0,120)};}}""", u)
        await page.close()
    finally:
        await browser.close()
        await pw.stop()
    OUT.mkdir(exist_ok=True)
    f = OUT / "xiaomi_map_probe.json"
    f.write_text(json.dumps(res, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(res, ensure_ascii=False, indent=2)[:5000], flush=True)
    print(f"\n[saved] {f}", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
