"""tools/probe_network.py —— 抓取"选中机型"这一动作触发的网络请求（找机型级 API / 深链参数）。

思路：SPA 前端 URL 不变，但价格必然来自某个**机型级**请求。把该请求 URL 抓出来，
就得到"这一台机型的实际数据链接"；再检查 DOM 选项元素是否带机型 slug/id，
用于判断官网页面是否存在可构造的机型级深链。

用法：python tools/probe_network.py --brand vivo --country my --model "X300 Pro"
输出：output/netprobe_<brand>_<country>.json
"""
import argparse
import asyncio
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from crawler.core import launch_browser, open_page  # noqa: E402
import executor  # noqa: E402

OUT = ROOT / "output"
STATIC = re.compile(r"\.(png|jpg|jpeg|gif|webp|svg|css|woff2?|ttf|ico|mp4|js)(\?|$)", re.I)


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--brand", required=True)
    ap.add_argument("--country", required=True)
    ap.add_argument("--model", default=None)
    args = ap.parse_args()
    rec = executor.load_record(args.brand, args.country)
    if not rec:
        print("no KB record"); return
    q = rec.get("query", {})
    mode = q.get("mode")
    pw, browser = await launch_browser()
    reqs = []            # 全部请求
    phase = {"v": "load"}

    result = {"brand": args.brand, "country": args.country, "mode": mode}
    try:
        page = await browser.new_page(locale=rec.get("locale", ""))

        def on_req(r):
            u = r.url
            if STATIC.search(u):
                return
            reqs.append({"phase": phase["v"], "method": r.method, "url": u[:600],
                         "rtype": r.resource_type,
                         "post": (r.post_data or "")[:400] if r.method != "GET" else ""})
        page.on("request", on_req)

        url = rec.get("entry", {}).get("expect_url") or rec.get("entry", {}).get("url")
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=40000)
        except Exception as e:
            result["goto_warn"] = f"{type(e).__name__}: {e}"
        await page.wait_for_timeout(int(q.get("pre_wait_ms", 14000)))

        # ---- 选机型前，先 dump DOM 里选项元素的属性（找 slug/id） ----
        if mode == "vivo_parts_grid":
            trig = page.locator(q.get("model_trigger", "#boxSelectModel, .box-select-model")).first
            if await trig.count():
                await trig.evaluate("el => el.click()")
                await page.wait_for_timeout(2500)
            result["option_html"] = await page.evaluate(
                """() => [...document.querySelectorAll('li.select-model-item')].slice(0,4)
                          .map(e => e.outerHTML.slice(0,400))""")
        elif mode == "xiaomi_material_table":
            result["option_html"] = await page.evaluate(
                """() => [...document.querySelectorAll('div.type-search-goods')].slice(0,3)
                          .map(e => e.outerHTML.slice(0,400))""")
        elif mode == "form_select_cascade":
            result["option_html"] = await page.evaluate(
                """() => {const out={};
                   for (const s of ['.device-dropdown','.model-dropdown']) {
                     const el=document.querySelector(s);
                     out[s]= el ? el.outerHTML.slice(0,1200) : null;
                   }
                   out['embedded_json_ids']=[...document.querySelectorAll('script[type*=json]')]
                       .map(e=>e.id||e.getAttribute('data-name')||'(anon)').slice(0,10);
                   return out;}""")

        # ---- 进入"选机型"阶段 ----
        phase["v"] = "select"
        before = len(reqs)
        chosen = None
        if mode == "vivo_parts_grid":
            chosen = await page.evaluate(
                """(args)=>{const [m,sel]=args;const lis=[...document.querySelectorAll(sel)];
                   let el=m?lis.find(x=>(x.innerText||'').trim()===m):null; if(!el) el=lis[0];
                   if(!el) return null; const t=el.innerText.trim(); el.click(); return t;}""",
                [args.model, q.get("model_option", "li.select-model-item")])
        elif mode == "xiaomi_material_table":
            target = args.model
            if not target:
                names = [t.strip() for t in await page.locator("div.type-search-goods").all_inner_texts() if t.strip()]
                target = names[0] if names else None
            if target:
                el = page.get_by_text(target, exact=False).first
                if await el.count():
                    await el.click(timeout=9000)
                    chosen = target
        elif mode == "form_select_cascade":
            target = args.model or "iPhone 11"
            for sel in (".device-dropdown", ".model-dropdown"):
                el = page.locator(sel).first
                if not await el.count():
                    continue
                try:
                    if await el.evaluate("e => e.tagName === 'SELECT'"):
                        await el.select_option(label=target, timeout=6000)
                    else:
                        await el.click(); await page.wait_for_timeout(500)
                        await page.locator(f"{sel} li, {sel} [role=option]",
                                           has_text=target).first.click(timeout=6000)
                except Exception as e:
                    print(f"[warn] pick {sel}: {e}", flush=True)
                await page.wait_for_timeout(2500)
            chosen = target
        await page.wait_for_timeout(6000)

        result["chosen"] = chosen
        result["requests_after_select"] = reqs[before:]
        result["n_requests_total"] = len(reqs)
        # 请求 URL 里含机型名（或其去空格形式）的 —— 最可能就是机型级数据链接
        if chosen:
            keys = {chosen, chosen.replace(" ", ""), chosen.replace(" ", "%20"),
                    chosen.replace(" ", "+")}
            hit = [r for r in reqs if any(k.lower() in r["url"].lower() for k in keys)
                   or any(k.lower() in (r["post"] or "").lower() for k in keys)]
            result["model_keyed_requests"] = hit[:12]
        result["xhr_all"] = [r for r in reqs if r["rtype"] in ("xhr", "fetch")][-25:]
        result["final_url"] = await page.evaluate("() => location.href")
        await page.close()
    finally:
        await browser.close()
        await pw.stop()
    OUT.mkdir(exist_ok=True)
    f = OUT / f"netprobe_{args.brand}_{args.country}.json"
    f.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2)[:6000], flush=True)
    print(f"\n[saved] {f}", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
