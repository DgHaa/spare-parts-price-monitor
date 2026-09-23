#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""vivo 区域备件价源核查探针：某区域到底有没有官方备件价。

用途：新增/复核 vivo 某国区域时，判断它属于哪一类，避免把"官方无数据"误判成
"抓取失败"（或反之：把抓取缺陷误判成官方无数据）。

结论分三档：
  · ok        —— 页面有机型，接口返回价表           → 可抓
  · noprice   —— 页面有机型，接口 success 但价表为空 → 官方未配置价格（如 de）
  · no_page   —— 页面 404 / 无机型                   → 无此工具（如 jp）

关键设计：**拦截站点自己发出的请求**。只看我们自己的 HTTP 调用无法排除
"我们的端点/参数写错了"；用真浏览器点选机型、截获站点自身的 XHR，
才能证明"官网自己拿到的也是空"，从而把结论建立在站点行为而非我们的实现上。

用法：
    python -u tools/probe_vivo_region.py de          # 单区域
    python -u tools/probe_vivo_region.py de my       # 连带对照组（推荐：对照区必须能出价）
    python -u tools/probe_vivo_region.py --http-only de   # 只跑 HTTP（快，不开浏览器）

产出：out_vivo_probe_<cc>.png（页面截图，作为结论存证）
前置：无（只读；不写库）
"""
import argparse
import asyncio
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "vendor"))
sys.path.insert(0, str(ROOT / "references" / "calibration"))

PRICE_RE = re.compile(r"(€|EUR|RM|₺|AED|INR|BRL|MXN|\d[\d.,]{2,}\s*(?:€|EUR))")


# ------------------------------------------------------------------ HTTP 快速判定

def http_verdict(country: str) -> dict:
    """不开浏览器：抓备件页 → 逐台打官方接口，统计 ok/noprice/error。"""
    import executor
    try:
        region_id, items = executor.vivo_support_page(country)
    except Exception as e:
        return {"country": country, "verdict": "no_page", "models": 0,
                "detail": f"{type(e).__name__}: {str(e)[:120]}"}
    if not items:
        return {"country": country, "verdict": "no_page", "models": 0,
                "detail": "备件页未解析出机型（404 或无此页）"}
    tally = {"ok": 0, "noprice": 0, "error": 0}
    sample = None
    for did, nm in items:
        rows, err, st = executor.vivo_price_rows(region_id, did, nm)
        tally[st if st in tally else "error"] += 1
        if st == "ok" and sample is None:
            sample = {"model": nm, "rows": len(rows),
                      "first": [r["cells"][1:] for r in rows[:2]],
                      "currency": rows[0].get("vivo_currency_note")}
    verdict = "ok" if tally["ok"] else ("noprice" if tally["noprice"] else "error")
    return {"country": country, "verdict": verdict, "region_id": region_id,
            "models": len(items), "tally": tally, "sample": sample,
            "detail": (f"regionId={region_id}；{len(items)} 台："
                       f"ok={tally['ok']} noprice={tally['noprice']} error={tally['error']}")}


# ------------------------------------------------------------------ 浏览器取证

async def browser_evidence(country: str):
    """真浏览器点选一个机型，拦截站点自身 XHR，读 DOM 里有无价格。"""
    from playwright.async_api import async_playwright
    from _paths import find_chromium

    url = f"https://www.vivo.com/{country}/support/accessory"
    calls = []
    print("=" * 86, flush=True)
    print(f"### {country}  {url}", flush=True)

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            executable_path=find_chromium(), headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"])
        page = await browser.new_page(
            user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/125.0 Safari/537.36"))

        async def on_resp(resp):
            if any(k in resp.url for k in ("Price", "price", "Maint", "maint", "spare")):
                try:
                    body = (await resp.text())[:400]
                except Exception as e:
                    body = f"<{type(e).__name__}>"
                if "vivo.com" in resp.url:      # 只留官网自身，滤掉 GA 等第三方
                    calls.append((resp.status, resp.request.method, resp.url,
                                  (resp.request.post_data or "")[:120], body))

        page.on("response", lambda r: asyncio.create_task(on_resp(r)))

        loaded = False
        for attempt in range(3):
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=60000)
                loaded = True
                break
            except Exception as e:
                print(f"  goto 第 {attempt + 1} 次失败: {type(e).__name__}", flush=True)
                await page.wait_for_timeout(2500)
        if not loaded:
            print("  页面三次均加载失败，放弃该区域", flush=True)
            await browser.close()
            return
        await page.wait_for_timeout(3000)

        # cookie 浮层会挡住点击
        for sel in ("text=Alle Cookies akzeptieren", "text=Accept all cookies",
                    "#onetrust-accept-btn-handler"):
            try:
                el = page.locator(sel).first
                if await el.count():
                    await el.click(timeout=3000)
                    await page.wait_for_timeout(800)
                    break
            except Exception:
                pass

        items = await page.evaluate("""() => [...document.querySelectorAll('.select-model-item')]
            .map(e => ({id: e.dataset.id, name: (e.textContent||'').trim()}))""")
        print(f"  页面机型数 = {len(items)}  样本 = {[i['name'] for i in items[:4]]}", flush=True)
        if not items:
            print("  [判定] no_page：页面无机型", flush=True)
            await browser.close()
            return

        tgt = items[0]
        print(f"  → 选  {tgt['name']} (data-id={tgt['id']})", flush=True)
        for sel in (".box-select-model", ".select-model-icon", ".select-model-text"):
            try:
                await page.click(sel, timeout=6000)
                break
            except Exception:
                pass
        await page.wait_for_timeout(1200)
        loc = page.locator(f'.select-model-item[data-id="{tgt["id"]}"]')
        try:
            await loc.click(timeout=8000)
        except Exception:
            try:
                await loc.click(timeout=8000, force=True)
            except Exception:
                await page.evaluate("""(id) => {
                    const it = document.querySelector('.select-model-item[data-id="'+id+'"]');
                    if (it) it.dispatchEvent(new MouseEvent('click', {bubbles:true}));
                }""", tgt["id"])
        await page.wait_for_timeout(6000)

        dom = await page.evaluate("""() => {
            const nodes = [...document.querySelectorAll('[class*=price], .part-item, table td')]
                .map(e => (e.textContent||'').trim()).filter(Boolean);
            const sel = document.querySelector('.select-model-text');
            return {selected: sel ? sel.textContent.trim() : null,
                    nodes: nodes.slice(0, 20)};
        }""")
        picked = [n for n in dom["nodes"] if PRICE_RE.search(n)]
        print(f"  选中显示 = {dom['selected']!r}", flush=True)
        print(f"  **含价格数字的节点** = {picked[:10]}", flush=True)
        print(f"  --- 站点自身取价请求 {len(calls)} 条 ---", flush=True)
        for st, m, u, post, body in calls[:6]:
            print(f"   [{st}] {m} {u[:100]}", flush=True)
            if post:
                print(f"        POST: {post}", flush=True)
            print(f"        RESP: {body[:240]}", flush=True)
        verdict = "ok" if picked else ("noprice" if calls else "unknown")
        print(f"  [判定] {verdict}", flush=True)

        shot = ROOT / f"out_vivo_probe_{country}.png"
        await page.screenshot(path=str(shot), full_page=True)
        print(f"  截图: {shot.name}", flush=True)
        await browser.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("countries", nargs="+", help="区域码，如 de my")
    ap.add_argument("--http-only", action="store_true", help="只跑 HTTP 判定（不开浏览器）")
    a = ap.parse_args()

    print("### HTTP 快速判定", flush=True)
    for cc in a.countries:
        v = http_verdict(cc)
        print(f"  {cc:<4} -> {v['verdict']:<8} {v['detail']}", flush=True)
        if v.get("sample"):
            print(f"        样本: {v['sample']}", flush=True)

    if not a.http_only:
        for cc in a.countries:
            try:
                asyncio.run(browser_evidence(cc))
            except Exception as e:
                print(f"  [ERR] {cc}: {type(e).__name__}: {str(e)[:200]}", flush=True)


if __name__ == "__main__":
    main()
