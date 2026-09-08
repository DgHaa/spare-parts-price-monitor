"""tools/probe_more.py —— 补测三件事（决定各品牌机型级链接形态的最后拼图）。

1) OPPO 备件价页是否支持 query 深链（?model= / ?productModel= …）
2) 三星维修价表页（静态整表）能否打开 + 表内是否逐行列出机型（=> 可用 #:~:text= 片段深链）
3) Apple 机型级支持页 slug 规则（support.apple.com/<locale>/iphone-11 是否 200）
"""
import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from crawler.core import launch_browser  # noqa: E402
import executor  # noqa: E402

OUT = ROOT / "output"


async def probe_oppo(browser):
    """OPPO：页面 query 深链候选。判定标准=页面是否直接渲染出该机型的备件价。"""
    res = {}
    model = "OPPO Reno14 5G"
    for qs in ("?model=OPPO%20Reno14%205G", "?productModel=OPPO%20Reno14%205G",
               "#model=OPPO%20Reno14%205G", ""):
        page = await browser.new_page(locale="tr-TR")
        u = f"https://support.oppo.com/tr/spare-parts-price/{qs}"
        try:
            await page.goto(u, wait_until="domcontentloaded", timeout=45000)
            await page.wait_for_timeout(9000)
            info = await page.evaluate(
                """(m) => {const b=document.body.innerText;
                   return {has_model_text: b.includes(m),
                           has_price_digits: /\\d{2,}[.,]?\\d*/.test(b),
                           head: b.slice(0, 260)};}""", model)
            res[f"oppo{qs or '(plain)'}"] = {"url": u, **info}
        except Exception as e:
            res[f"oppo{qs or '(plain)'}"] = {"url": u, "error": f"{type(e).__name__}: {str(e)[:140]}"}
        finally:
            await page.close()
    return res


async def probe_samsung(browser, country, locale):
    """三星：静态整表页 —— 确认可达性、表内机型清单、文本片段定位可行性。"""
    rec = executor.load_record("samsung", country)
    url = (rec or {}).get("entry", {}).get("expect_url")
    page = await browser.new_page(locale=locale)
    out = {"url": url}
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=70000)
        await page.wait_for_timeout(6000)
        grids = await executor._read_all_tables_text(page)
        models = []
        for g in grids:
            for r in (g or [])[1:]:
                if r and len(r) > 1:
                    m = executor._samsung_model_of(r)
                    if m:
                        models.append(m)
        out.update({"reachable": True, "n_tables": len(grids),
                    "n_models": len(models), "models_sample": models[:10]})
        # 文本片段深链验证：直接带 #:~:text= 再开一次，看浏览器是否命中该文本
        if models:
            frag = models[0].replace(" ", "%20")
            u2 = f"{url}#:~:text={frag}"
            p2 = await browser.new_page(locale=locale)
            try:
                await p2.goto(u2, wait_until="domcontentloaded", timeout=70000)
                await p2.wait_for_timeout(5000)
                hit = await p2.evaluate("(m) => document.body.innerText.includes(m)", models[0])
                out["text_fragment"] = {"url": u2, "loads": True, "model_present_in_page": hit}
            except Exception as e:
                out["text_fragment"] = {"url": u2, "loads": False, "error": str(e)[:140]}
            finally:
                await p2.close()
    except Exception as e:
        out.update({"reachable": False, "error": f"{type(e).__name__}: {str(e)[:160]}"})
    finally:
        await page.close()
    return out


def apple_slug(model_name):
    return model_name.lower().replace(" ", "-").replace("(", "").replace(")", "")


async def probe_apple_slugs(browser):
    """Apple：机型级支持页 slug 是否真实存在（HTTP 200 且页面标题含机型）。"""
    import sqlite3
    conn = sqlite3.connect(ROOT / "spare_parts.db")
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """SELECT DISTINCT m.name FROM models m JOIN brands b ON b.id=m.brand_id
           WHERE b.name='apple' AND m.country_code='de' ORDER BY m.name""").fetchall()
    conn.close()
    names = [r["name"] for r in rows]
    page = await browser.new_page(locale="de-DE")
    await page.goto("https://support.apple.com/de-de/iphone/repair",
                    wait_until="domcontentloaded", timeout=45000)
    out = []
    for nm in names:
        u = f"https://support.apple.com/de-de/{apple_slug(nm)}"
        r = await page.evaluate("""async (u) => {
            try { const r = await fetch(u, {method:'GET', redirect:'follow'});
                  const t = await r.text();
                  const m = /<title>([^<]*)<\\/title>/i.exec(t);
                  return {status: r.status, final: r.url, title: m ? m[1].slice(0,90) : null}; }
            catch(e) { return {error: String(e).slice(0,120)}; }
        }""", u)
        out.append({"model": nm, "url": u, **r})
    await page.close()
    return {"total": len(out), "ok_200": sum(1 for x in out if x.get("status") == 200), "detail": out}


async def main():
    which = sys.argv[1] if len(sys.argv) > 1 else "all"
    pw, browser = await launch_browser()
    res = {}
    try:
        if which in ("all", "oppo"):
            res["oppo"] = await probe_oppo(browser)
        if which in ("all", "samsung"):
            res["samsung_tr"] = await probe_samsung(browser, "tr", "tr-TR")
            res["samsung_jp"] = await probe_samsung(browser, "jp", "ja-JP")
        if which in ("all", "apple"):
            res["apple_slugs"] = await probe_apple_slugs(browser)
    finally:
        await browser.close()
        await pw.stop()
    OUT.mkdir(exist_ok=True)
    f = OUT / f"probe_more_{which}.json"
    f.write_text(json.dumps(res, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(res, ensure_ascii=False, indent=2)[:5000], flush=True)
    print(f"\n[saved] {f}", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
