"""独立验证 vivo/ae 的 4 台争议机型：官方接口称"无价"，但库里仍有 2026-09-02 旧值。

验证手段刻意与抓取路径**不同源**：用真实浏览器打开备件页、点选机型、读 DOM 网格。
若浏览器也读不到价 → 官方确实没公布，库里的旧值是脏数据，应隔离。
若浏览器能读到价 → 说明我的接口路径对这 4 台不对（data-id 错配），要改抓取而非清数据。

  用法：python tools/verify_vivo_disputed.py
"""
import asyncio
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, "C:/Users/Dong/.workbuddy/skills/spare-parts-price/scripts")
_VENDOR = _ROOT / "vendor"  # 仓库内副本优先（最后插入 = 最先被 import）
if _VENDOR.exists():
    sys.path.insert(0, str(_VENDOR))

TARGETS = ["X200 FE", "X300", "V70 Lite 5G", "T1 Pro 5G"]


async def main():
    import executor
    from crawler.core import launch_browser

    rid, items = executor.vivo_support_page("ae")
    did = {n: i for i, n in items}
    print(f"[page] regionId={rid} SSR 机型={len(items)}")

    # force_direct：走代理时 vivo 备件页 45s 都到不了 domcontentloaded（实测），
    # 而接口路径走直连正常 —— 验证脚本必须与"能出数"的同一条网络路径，否则
    # 失败原因是代理而非页面，等于没验证。
    pw, browser = await launch_browser(force_direct=True)
    page = await browser.new_page(viewport={"width": 1440, "height": 1000})
    await page.goto("https://www.vivo.com/ae/support/accessory",
                    wait_until="domcontentloaded", timeout=45000)
    await page.wait_for_timeout(15000)  # SPA 慢，KB 记录需 ~15s

    for t in TARGETS:
        # 用页面自身的 JS 点选（与 KB 记录的 model_click=js 一致），避开 Playwright 可见性限制
        ok = await page.evaluate(
            """(name) => {
                const lis = [...document.querySelectorAll('li.select-model-item')];
                const el = lis.find(l => (l.textContent || '').trim() === name);
                if (!el) return false;
                el.click();
                return true;
            }""", t)
        await page.wait_for_timeout(6000)
        rows = await page.evaluate(
            """() => [...document.querySelectorAll('li.container-box-item')].map(li => {
                   const n = li.querySelector('.item-left-name');
                   const p = li.querySelector('.item-right-price');
                   return ((n && n.textContent) || '').trim() + '=' + ((p && p.textContent) || '').trim();
               })""")
        print(f"\n[{t}] clicked={ok} DOM 价行={len(rows)}  data_id={did.get(t)}")
        for r in rows[:14]:
            print("   ", r)

    await browser.close()
    await pw.stop()


if __name__ == "__main__":
    asyncio.run(main())
