#!/usr/bin/env python3
"""apple_calibrate.py - Apple 维修价页校准（form_select_cascade）。

对 de/jp/ae/my 各区域：加载 /<语区>/iphone/repair，选 device=iPhone 16 + 首个 model，
读取「预估服务费」区文本与定位器，截图。tr 无页面(404)→标记 unavailable。
输出 JSON：每区域 status / costs_text / cost_rows(尽力结构化) / screenshot。
"""
import argparse, json, sys, re
from datetime import datetime
from pathlib import Path

CHROME = r"C:\Users\Dong\AppData\Local\ms-playwright\chromium-1234\chrome-win64\chrome.exe"
REGIONS = {
    "de": ("https://support.apple.com/de-de/iphone/repair", "de-DE", "EUR", "iPhone 16"),
    "jp": ("https://support.apple.com/ja-jp/iphone/repair", "ja-JP", "JPY", "iPhone 16"),
    "ae": ("https://support.apple.com/en-ae/iphone/repair", "en-AE", "AED", "iPhone 16"),
    "my": ("https://support.apple.com/en-my/iphone/repair", "en-MY", "MYR", "iPhone 16"),
}
# 成本区标题多语匹配
HEAD_RE = re.compile(r"Servicekosten|Service costs|費|التكلف|Kosten|見積|Estim|估计", re.I)


async def calibrate(page, area, url, locale, device):
    await page.goto(url, wait_until="commit")
    await page.wait_for_timeout(6000)
    # 选 device
    dd = page.locator(".device-dropdown")
    await dd.first.wait_for(state="visible", timeout=10000)
    await dd.first.select_option(label=device)
    await page.wait_for_timeout(1500)
    # 选 model（取首个）
    md = page.locator(".model-dropdown")
    await md.first.wait_for(state="visible", timeout=10000)
    opts = await md.first.locator("option").all_inner_texts()
    first_model = [o for o in opts if o.strip()][0] if opts else None
    if first_model:
        await md.first.select_option(label=first_model)
    await page.wait_for_timeout(3500)
    # 读成本区（整页按价格符号配对上一行标签，全语言通用）
    data = await page.evaluate("""() => {
        const priceRe=/[€$¥£AEDRM]\\s?[\\d.,]+|[\\d.,]+\\s?(円|€|\\$|¥|£|AED|RM)|[\\d,]+\\s?円/i;
        const lines=(document.body.innerText||'').split('\\n').map(s=>s.trim()).filter(Boolean);
        const rows=[];
        for(let i=0;i<lines.length;i++){
            const l=lines[i];
            if(priceRe.test(l) && l.length<40){
                const label=(i>0 && !priceRe.test(lines[i-1]))?lines[i-1]:'';
                // 仅保留价格行确含货币符号的（过滤下拉选项噪点）
                if(/[€$¥£]|円|AED|RM/i.test(l)) rows.push({label:label, price:l});
            }
        }
        // 成本区标题
        let head='';
        for(const e of [...document.querySelectorAll('*')]){const t=(e.textContent||'').trim();
            if(/Servicekosten|Service costs|見積もりの|見積|التكلف|估计的|Estim|料金|費用/i.test(t) && t.length<40){head=t;break;}}
        return {found:rows.length>0, heading:head, rows};
    }""")
    shot = f"apple_{area}_cal.png"
    await page.screenshot(path=shot)
    data["screenshot"] = shot
    data["model_selected"] = first_model
    data["url"] = page.url
    return data


async def main(out):
    from playwright.async_api import async_playwright
    results = {}
    async with async_playwright() as p:
        b = await p.chromium.launch(headless=True, executable_path=CHROME, args=["--no-sandbox"])
        pg = await b.new_page()
        for area, (url, locale, cur, dev) in REGIONS.items():
            try:
                await pg.context.clear_cookies()
                r = await calibrate(pg, area, url, locale, dev)
                r["currency"] = cur
                results[area] = {"status": "ok" if r.get("found") else "no_costs", **r}
                print(f"[{area}] {'ok' if r.get('found') else 'no_costs'} model={r.get('model_selected')}", flush=True)
            except Exception as e:
                results[area] = {"status": "error", "error": str(e)[:300]}
                print(f"[{area}] error {e}", flush=True)
        await b.close()
    doc = {"captured_at": datetime.now().isoformat(timespec="seconds"), "results": results}
    if out:
        Path(out).write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"saved -> {out}")
    return doc


if __name__ == "__main__":
    ap = argparse.ArgumentParser(); ap.add_argument("--out", default="apple_cal.json"); args = ap.parse_args()
    import asyncio; asyncio.run(main(args.out))
