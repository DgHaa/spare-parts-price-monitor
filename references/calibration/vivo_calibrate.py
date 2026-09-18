#!/usr/bin/env python3
# vivo 备件价格校准/采集脚本（多区域）
# 机制：support 页 .box-select-model(combobox) 选机型 -> li.select-model-item(role=option)
#       -> 渲染 li.container-box-item 行，含 .item-left-name(部件) + .item-right-price(价格)
# 用法: python vivo_calibrate.py --out vivo_cal.json
import json, argparse, asyncio
CHROME = r"C:\Users\Dong\AppData\Local\ms-playwright\chromium-1234\chrome-win64\chrome.exe"
REGIONS = {
    "my": {"url": "https://www.vivo.com/my/support/accessory", "currency": "MYR", "lang": "ms"},
    "tr": {"url": "https://www.vivo.com/tr/support/accessory", "currency": "TRY", "lang": "tr"},
    "de": {"url": "https://www.vivo.com/de/support/accessory", "currency": "EUR", "lang": "de"},
    "ae": {"url": "https://www.vivo.com/ae/support/accessory", "currency": "AED", "lang": "en"},
}
PREF_MODELS = ["X300 Pro", "X100 Pro", "V40", "V30 Pro", "Y36", "V50", "Y28"]

EXTRACT_JS = """() => {
    const rows=[...document.querySelectorAll('li.container-box-item')];
    const out=[];
    for(const r of rows){
        const nm=r.querySelector('.item-left-name')?.innerText?.trim();
        const pr=r.querySelector('.item-right-price')?.innerText?.trim();
        if(nm && pr) out.push({part:nm, price:pr});
    }
    return out;
}"""

async def run_region(area, info):
    from playwright.async_api import async_playwright
    out = {"area": area, "status": "error", "url": info["url"]}
    async with async_playwright() as p:
        b = await p.chromium.launch(headless=True, executable_path=CHROME, args=["--no-sandbox"])
        pg = await b.new_page()
        try:
            await pg.goto(info["url"], wait_until="commit")
            await pg.wait_for_timeout(15000)
            # 触发器：优先 #boxSelectModel，否则 .box-select-model；用 JS 点击（部分区域 Playwright click 超时）
            trig = pg.locator("#boxSelectModel").first
            if not await trig.count():
                trig = pg.locator(".box-select-model").first
            await trig.evaluate("el => el.click()")
            # 轮询等待选项列表出现（部分区域惰性渲染）
            opts = pg.locator("li.select-model-item")
            for _ in range(12):
                if await opts.count() > 0:
                    break
                await pg.wait_for_timeout(500)
            # 列出真实可选机型
            opt_texts = []
            nc = await opts.count()
            for i in range(nc):
                t = (await opts.nth(i).inner_text()).strip()
                if t:
                    opt_texts.append(t)
            # 选一个真正存在的候选
            chosen = next((m for m in PREF_MODELS if m in opt_texts), None)
            if not chosen and opt_texts:
                chosen = opt_texts[0]
            out["available_models_sample"] = opt_texts[:8]
            sel = pg.locator("li.select-model-item", has_text=chosen).first
            await sel.evaluate("el => el.click()")
            # 轮询等待价格行渲染
            rows = []
            for _ in range(16):
                rows = await pg.evaluate(EXTRACT_JS)
                if rows:
                    break
                await pg.wait_for_timeout(500)
            if not rows:
                # 重试：换下一个候选
                for m in opt_texts[1:6]:
                    if m == chosen:
                        continue
                    sel2 = pg.locator("li.select-model-item", has_text=m).first
                    await sel2.evaluate("el => el.click()")
                    for _ in range(16):
                        rows = await pg.evaluate(EXTRACT_JS)
                        if rows:
                            chosen = m
                            break
                        await pg.wait_for_timeout(500)
                    if rows:
                        break
            out["model_selected"] = chosen
            out["currency"] = info["currency"]
            out["rows"] = rows
            out["row_count"] = len(rows)
            out["status"] = "ok" if rows else "no_rows"
            await pg.screenshot(path=f"vivo_{area}_cal.png")
        except Exception as e:
            out["error"] = str(e)[:300]
        await b.close()
        return out

async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="vivo_cal.json")
    args = ap.parse_args()
    results = {}
    for area, info in REGIONS.items():
        results[area] = await run_region(area, info)
        print(f"[{area}] {results[area]['status']} rows={results[area].get('row_count')} model={results[area].get('model_selected')}")
    json.dump({"results": results}, open(args.out, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    print("saved", args.out)

if __name__ == "__main__":
    asyncio.run(main())
