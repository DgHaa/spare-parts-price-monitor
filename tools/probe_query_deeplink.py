"""tools/probe_query_deeplink.py —— 实测 vivo / 小米 的机型级 URL 候选是否真的生效。

已知（netprobe 实测）：
  vivo   选机型 -> POST https://www.vivo.com/my/support/queryPriceByProductId  body: id=3505
         且 DOM 选项自带 data-id（X300 Pro=3505、T1x=1823 …）
  xiaomi 选机型 -> GET  https://api2.service.order.mi.com/repair_price/shop_band_wx_price?class_id=44799

本脚本验证两类"机型级链接"：
  A. 官网页面 + query 参数（?id= / ?productId= / ?class_id= …）能否直接定位到该机型；
  B. 机型级数据接口本身能否作为可回放链接（GET 直开是否返回该机型数据）。
"""
import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from crawler.core import launch_browser  # noqa: E402

OUT = ROOT / "output"
VIVO_ID = "3505"        # X300 Pro @ my
VIVO_MODEL = "X300 Pro"
MI_CLASS = "44799"      # Xiaomi 13 Pro 8GB 陶瓷黑 128GB


async def check_vivo_page(browser, url):
    page = await browser.new_page(locale="ms-MY")
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=40000)
        await page.wait_for_timeout(16000)
        return await page.evaluate(
            """() => {const rows=[...document.querySelectorAll('li.container-box-item')].slice(0,2)
                        .map(r=>(r.querySelector('.item-left-name')?.innerText||'')+'='+
                                (r.querySelector('.item-right-price')?.innerText||''));
               const trig=document.querySelector('#boxSelectModel, .box-select-model');
               return {selected_label: trig ? trig.innerText.trim().slice(0,60) : null,
                       n_price_rows: document.querySelectorAll('li.container-box-item').length,
                       rows};}""")
    except Exception as e:
        return {"error": f"{type(e).__name__}: {str(e)[:160]}"}
    finally:
        await page.close()


async def check_mi_page(browser, url):
    page = await browser.new_page(locale="zh-CN")
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=40000)
        await page.wait_for_timeout(13000)
        return await page.evaluate(
            """() => {const trs=[...document.querySelectorAll('table tr')].slice(0,3)
                        .map(tr=>[...tr.querySelectorAll('td,th')].map(td=>td.innerText.trim()).join('|'));
               return {n_rows: document.querySelectorAll('table tr').length, rows: trs};}""")
    except Exception as e:
        return {"error": f"{type(e).__name__}: {str(e)[:160]}"}
    finally:
        await page.close()


async def raw_get(page, url):
    """在当前页上下文里 GET 一个 URL，返回状态与内容头部（判断接口能否直开）。"""
    return await page.evaluate("""async (u) => {
        try { const r = await fetch(u, {credentials:'include'});
              const t = await r.text();
              return {status: r.status, head: t.slice(0, 500), len: t.length}; }
        catch(e) { return {error: String(e)}; }
    }""", url)


async def main():
    pw, browser = await launch_browser()
    res = {}
    try:
        # ---------- A. vivo 页面 query 候选 ----------
        for qs in (f"?id={VIVO_ID}", f"?productId={VIVO_ID}", f"?model={VIVO_MODEL.replace(' ', '%20')}",
                   f"#id={VIVO_ID}"):
            u = f"https://www.vivo.com/my/support/accessory{qs}"
            res[f"vivo_page_{qs}"] = {"url": u, **(await check_vivo_page(browser, u))}
        # ---------- B. vivo 接口能否 GET 直开 ----------
        p = await browser.new_page(locale="ms-MY")
        await p.goto("https://www.vivo.com/my/support/accessory", wait_until="domcontentloaded", timeout=40000)
        await p.wait_for_timeout(6000)
        res["vivo_api_get"] = await raw_get(p, f"https://www.vivo.com/my/support/queryPriceByProductId?id={VIVO_ID}")
        res["vivo_api_post"] = await p.evaluate("""async (id) => {
            try { const r = await fetch('https://www.vivo.com/my/support/queryPriceByProductId',
                     {method:'POST', credentials:'include',
                      headers:{'Content-Type':'application/x-www-form-urlencoded'},
                      body:'id='+id});
                  const t = await r.text();
                  return {status:r.status, head:t.slice(0,600), len:t.length}; }
            catch(e){ return {error:String(e)}; }
        }""", VIVO_ID)
        await p.close()

        # ---------- C. 小米页面 query 候选 ----------
        for qs in (f"?class_id={MI_CLASS}", f"?classId={MI_CLASS}", f"?goodsId={MI_CLASS}"):
            u = f"https://www.mi.com/service/materialprice{qs}"
            res[f"mi_page_{qs}"] = {"url": u, **(await check_mi_page(browser, u))}
        # ---------- D. 小米接口 GET 直开 ----------
        p2 = await browser.new_page(locale="zh-CN")
        await p2.goto("https://www.mi.com/service/materialprice", wait_until="domcontentloaded", timeout=40000)
        await p2.wait_for_timeout(5000)
        res["mi_api_plain"] = await raw_get(
            p2, f"https://api2.service.order.mi.com/repair_price/shop_band_wx_price?class_id={MI_CLASS}")
        await p2.close()
    finally:
        await browser.close()
        await pw.stop()
    OUT.mkdir(exist_ok=True)
    f = OUT / "query_deeplink_probe.json"
    f.write_text(json.dumps(res, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(res, ensure_ascii=False, indent=2)[:6000], flush=True)
    print(f"\n[saved] {f}", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
