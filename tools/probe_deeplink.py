"""tools/probe_deeplink.py —— 机型级深链能力实测探针（只读，不写库）。

目的：确认每个品牌×国家的官网，在"选中某个具体机型"之后，
     ① location.href 是否变成机型级 URL（有 SPA 路由 / query 参数）；
     ② 若变了，直接 goto 该 URL 能否复现该机型的价格（可回放 = 真正可用的深链）；
     ③ 若没变（纯前端状态），记录候选兜底（文本片段 #:~:text= / 机型支持页）。

用法：python tools/probe_deeplink.py --brand vivo --country my
输出：output/deeplink_probe_<brand>_<country>.json
"""
import argparse
import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from crawler.core import launch_browser, open_page  # noqa: E402
import executor  # noqa: E402

OUT = ROOT / "output"


async def _href(page):
    try:
        return await page.evaluate("() => location.href")
    except Exception:
        return page.url


async def probe_vivo(page, rec, want_model):
    """vivo：SPA 自定义下拉。选机型后读 href。"""
    q = rec.get("query", {})
    await page.wait_for_timeout(int(q.get("pre_wait_ms", 16000)))
    url0 = await _href(page)
    trig = page.locator(q.get("model_trigger", "#boxSelectModel, .box-select-model")).first
    if not await trig.count():
        return {"url_before": url0, "error": "触发器不存在"}
    await trig.evaluate("el => el.click()")
    opt_sel = q.get("model_option", "li.select-model-item")
    for _ in range(16):
        if await page.locator(opt_sel).count() > 0:
            break
        await page.wait_for_timeout(500)
    names = [t.strip() for t in await page.locator(opt_sel).all_inner_texts() if t.strip()]
    chosen = await page.evaluate(
        """(args) => {const [m, sel]=args; const lis=[...document.querySelectorAll(sel)];
           let el = m ? lis.find(x=>(x.innerText||'').trim()===m) : null; if(!el) el=lis[0];
           if(!el) return null; const t=el.innerText.trim(); el.click(); return t;}""",
        [want_model, opt_sel])
    await page.wait_for_timeout(4000)
    url1 = await _href(page)
    # 读一行价做锚点，供回放对比
    sample = await page.evaluate(
        """() => {const r=document.querySelector('li.container-box-item');
           return r ? (r.querySelector('.item-left-name')?.innerText||'')+'|'+(r.querySelector('.item-right-price')?.innerText||'') : null;}""")
    return {"url_before": url0, "url_after": url1, "chosen": chosen,
            "options_sample": names[:5], "price_sample": sample}


async def probe_xiaomi(page, rec, want_model):
    q = rec.get("query", {})
    await page.wait_for_timeout(int(q.get("pre_wait_ms", 13000)))
    url0 = await _href(page)
    leaves = [t.strip() for t in await page.locator("div.type-search-goods").all_inner_texts() if t.strip()]
    target = want_model or (leaves[0] if leaves else None)
    clicked = None
    if target:
        el = page.get_by_text(target, exact=False).first
        if await el.count():
            try:
                await el.click(timeout=8000)
                clicked = target
                await page.wait_for_timeout(4000)
            except Exception as e:
                clicked = f"click_fail: {e}"
    url1 = await _href(page)
    sample = await page.evaluate(
        """() => {const tr=document.querySelector('table tbody tr, table tr');
           return tr ? [...tr.querySelectorAll('td')].map(td=>td.innerText.trim()).join('|') : null;}""")
    return {"url_before": url0, "url_after": url1, "chosen": clicked,
            "options_sample": leaves[:3], "price_sample": sample}


async def probe_apple(page, rec, want_model):
    """Apple：.device-dropdown + .model-dropdown 两级。选完读 href。"""
    url0 = await _href(page)
    await page.wait_for_timeout(3000)
    info = {"url_before": url0}
    dev_opts = await _read_opts(page, ".device-dropdown")
    info["device_options"] = dev_opts[:8]
    # 选择目标机型所属系列
    target = want_model or "iPhone 11"
    series = None
    for o in dev_opts:
        if o.strip().lower() == target.strip().lower():
            series = o
            break
    if not series:
        for o in dev_opts:
            if target.split()[-1] in o:
                series = o
                break
    series = series or (dev_opts[0] if dev_opts else None)
    if series:
        await _pick(page, ".device-dropdown", series)
        await page.wait_for_timeout(2500)
    info["series_picked"] = series
    info["url_after_device"] = await _href(page)
    mod_opts = await _read_opts(page, ".model-dropdown")
    info["model_options"] = mod_opts[:8]
    pick = None
    for o in mod_opts:
        if o.strip().lower() == target.strip().lower():
            pick = o
            break
    pick = pick or (mod_opts[0] if mod_opts else None)
    if pick:
        await _pick(page, ".model-dropdown", pick)
        await page.wait_for_timeout(3500)
    info["model_picked"] = pick
    info["url_after_model"] = await _href(page)
    body = await page.evaluate("() => document.body.innerText.slice(0, 1500)")
    info["body_head"] = body
    return info


async def _read_opts(page, sel):
    el = page.locator(sel).first
    if await el.count() == 0:
        return []
    try:
        if await el.evaluate("e => e.tagName === 'SELECT'"):
            return [t.strip() for t in await el.locator("option").all_inner_texts() if t.strip()]
    except Exception:
        pass
    try:
        await el.click()
        await page.wait_for_timeout(800)
    except Exception:
        pass
    return [t.strip() for t in await page.locator(
        f"{sel} option, {sel} li, {sel} [role=option]").all_inner_texts() if t.strip()]


async def _pick(page, sel, text):
    el = page.locator(sel).first
    try:
        if await el.evaluate("e => e.tagName === 'SELECT'"):
            await el.select_option(label=text, timeout=6000)
            return
    except Exception:
        pass
    try:
        await el.click()
        await page.wait_for_timeout(500)
        await page.locator(f"{sel} li, {sel} [role=option]", has_text=text).first.click(timeout=6000)
    except Exception as e:
        print(f"[warn] pick {sel}={text}: {e}", flush=True)


async def probe_samsung(page, rec, want_model):
    """三星：静态整表。确认表里含机型文本（→ 可用 #:~:text= 片段深链）。"""
    url0 = await _href(page)
    await page.wait_for_timeout(3000)
    grids = await executor._read_all_tables_text(page)
    models = []
    for g in grids:
        for r in (g or [])[1:]:
            if r and len(r) > 1:
                m = executor._samsung_model_of(r)
                if m:
                    models.append(m)
    return {"url_before": url0, "url_after": url0, "n_tables": len(grids),
            "table_models_sample": models[:8], "n_table_models": len(models),
            "note": "静态整表页：URL 不随机型变化"}


async def replay(browser, rec, url, expect_text):
    """把探测到的 URL 重新打开，检查是否自动定位到该机型（可回放性）。"""
    page = await browser.new_page(locale=rec.get("locale", ""))
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=30000)
        await page.wait_for_timeout(9000)
        body = await page.evaluate("() => document.body.innerText")
        return {"url": url, "http_ok": True,
                "contains_model": bool(expect_text and expect_text.lower() in body.lower()),
                "body_head": body[:600]}
    except Exception as e:
        return {"url": url, "http_ok": False, "error": f"{type(e).__name__}: {e}"}
    finally:
        await page.close()


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--brand", required=True)
    ap.add_argument("--country", required=True)
    ap.add_argument("--model", default=None)
    args = ap.parse_args()
    rec = executor.load_record(args.brand, args.country)
    if not rec:
        print("no KB record"); return
    pw, browser = await launch_browser()
    result = {"brand": args.brand, "country": args.country,
              "mode": rec.get("query", {}).get("mode"),
              "entry": rec.get("entry", {}).get("url"),
              "expect": rec.get("entry", {}).get("expect_url")}
    try:
        page = await open_page(browser, rec)
        mode = rec.get("query", {}).get("mode")
        if mode == "vivo_parts_grid":
            r = await probe_vivo(page, rec, args.model)
        elif mode == "xiaomi_material_table":
            r = await probe_xiaomi(page, rec, args.model)
        elif mode == "form_select_cascade":
            r = await probe_apple(page, rec, args.model)
        else:
            r = await probe_samsung(page, rec, args.model)
        result["probe"] = r
        await page.close()
        # 若 URL 变了 → 试回放
        u0, u1 = r.get("url_before"), r.get("url_after") or r.get("url_after_model")
        if u1 and u0 and u1 != u0:
            result["deeplink_changed"] = True
            result["replay"] = await replay(browser, rec, u1,
                                           r.get("chosen") or r.get("model_picked"))
        else:
            result["deeplink_changed"] = False
    finally:
        await browser.close()
        await pw.stop()
    OUT.mkdir(exist_ok=True)
    f = OUT / f"deeplink_probe_{args.brand}_{args.country}.json"
    f.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2)[:3000], flush=True)
    print(f"\n[saved] {f}", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
