"""对 OPPO 德国官网备件价详情页截图，作为"高价廉价件"质疑的官方取证。

背景：库里 oppo/de 的 `Card tray`=€65、`Keys`=€65、`Speaker`=€57，比中国区（¥10 级）
高 20 倍，曾被怀疑是解析 bug。经直连 REBORN 接口核对，retailPrice 原文即 65.00 EUR
（discountRetailPrice=None，laborCostAmount=0），故怀疑转为"德国官方定价如此"。
本脚本渲染官网人读页截图，把结论钉死在官方页面上。

用法：python tools/shot_oppo_de_page.py [marketingModelCode]

⚠️ 已知限制（2026-09-17 实测）：对本页（support.oppo.com SPA）导航用
`domcontentloaded` 会挂死 >3 分钟；已改为 `commit`（与 crawler/core 一致），
但该 SPA 渲染依赖异步取价，仍可能较慢或不稳定。
OPPO 各国价格**优先用官方 API 原文核验**（`executor.reborn_post` + `flatten_reborn_parts`，
走 `par-sow-cms` / `sgp-sow-cms` 节点，秒级返回且与页面同源同值）——
本轮「德国廉价件是否有价格下限」的结论正是这样得出的（见报告 §12.5）。
本脚本仅用于需要人读页截图留档时。
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8")

from crawler.core import launch_browser, get_proxy  # noqa: E402

CODE = "5afbbfc9cdd241b18e725ede1b17894f"  # OPPO Find X9 (DE)
OUT = ROOT / "output" / "oppo_de_findx9_official.png"


async def main():
    code = sys.argv[1] if len(sys.argv) > 1 else CODE
    url = ("https://support.oppo.com/de/spare-parts-price/"
           f"#/detail?marketingModelCode={code}")
    print("[proxy]", get_proxy())
    print("[goto]", url)
    pw, browser = await launch_browser()
    try:
        page = await browser.new_page(locale="de-DE")
        # 用 commit 而非 domcontentloaded：SPA 常因长轮询卡在 load 事件（实测挂死 >3min）
        await page.goto(url, wait_until="commit", timeout=45000)
        await page.wait_for_timeout(9000)  # SPA 取价渲染
        text = await page.evaluate("() => document.body.innerText || ''")
        OUT.parent.mkdir(parents=True, exist_ok=True)
        await page.screenshot(path=str(OUT), full_page=True)
        print("[shot]", OUT)
        keep = [ln for ln in text.splitlines()
                if any(k in ln for k in ("Card tray", "Keys", "Speaker", "Screen",
                                         "Battery", "EUR", "Preis", "65", "57"))]
        print("[page text 摘要]")
        for ln in keep[:40]:
            print("   ", ln.strip())
    finally:
        try:
            await browser.close()
            await pw.stop()
        except Exception:
            pass


if __name__ == "__main__":
    asyncio.run(main())
