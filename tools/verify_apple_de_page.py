"""对 Apple 德国官网维修页取数并截图，核验「Sonstiger Schaden」基准漂移。

背景：KB 人工校准基准（2026-08-27，iPhone 16 Pro Max）为
  Batterieservice 135 / Rückglasschaden 169 / Schaden an der Rückkamera 299 /
  Displayschaden 488,99 / Schaden an Display und Rückglas 585 / Sonstiger Schaden 899
重抓后前 5 条完全一致（含逗号小数 488,99），只有 Sonstiger Schaden 变成 979。
需判断是「解析 bug」还是「Apple 调价」——本脚本用官网实时页面 + 截图定论。

用法：python tools/verify_apple_de_page.py ["iPhone 16 Pro Max"]

⚠️ 已知限制（2026-09-17 实测）：本脚本在**独立会话**里跑 `executor.run_query` 会超过
150s 兜底超时（`discover_models` 能正常返回 31 个机型，卡点在随后的 run_query）。
而同一套 KB 配方在 `crawler/run.py` 正式抓取流程里约 5s/机型、可正常完成。
疑与独立会话缺少抓取流程的页面状态过渡有关，**尚未定位**。
因此：需要"官网人读页截图"这类取证时，**优先直接跑正式抓取流程**
（`python -m crawler.run --brand apple --country de`）并读取其日志/落库结果；
本脚本仅作辅助，失败不影响结论。

本次「Sonstiger Schaden €899→€979 是否官方调价」的结论**不依赖本脚本**，而是由
归档表旧值与 KB `extract.sample` 基准对照得出（见报告 §12.8）。
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8")

from crawler.core import launch_browser, open_page, get_proxy  # noqa: E402
import executor  # noqa: E402

MODEL = "iPhone 16 Pro Max"
ORACLE = {
    "Batterieservice": 135.0,
    "Rückglasshaden": 169.0,
    "Schaden an der Rückkamera": 299.0,
    "Displayschaden": 488.99,
    "Schaden an Display und Rückglas": 585.0,
    "Sonstiger Schaden": 899.0,
}


async def main():
    model = sys.argv[1] if len(sys.argv) > 1 else MODEL
    rec = executor.load_record("apple", "de")
    query = rec["query"]
    url = rec["entry"]["url"]
    print("[proxy]", get_proxy())
    print("[goto]", url)
    pw, browser = await launch_browser()
    try:
        page = await open_page(browser, rec)
        await page.wait_for_timeout(2500)
        # 关键：先跑「机型发现」。抓取流程（crawler/run.py）在 run_query 前必经此步——
        # 它会把品牌/机型下拉展开并选定设备级，漏掉它 run_query 会卡在等下拉出现。
        from crawler.run import discover_models  # noqa: E402

        found = await discover_models(page, rec)
        print(f"[discover] 发现 {len(found)} 个机型；目标={model!r} 在其中: {model in found}")
        rows = await executor.run_query(page, query, model, None, "de")
        out = ROOT / "output" / "apple_de_official_page.png"
        out.parent.mkdir(parents=True, exist_ok=True)
        await page.screenshot(path=str(out), full_page=True)
        print("[shot]", out)
        print(f"\n=== 官网实时抽取（{model}）===")
        for r in rows or []:
            part = r.get("part") or (r.get("cells") or ["", ""])[-2]
            price = r.get("price")
            o = ORACLE.get(str(part), "")
            mark = ""
            if o != "":
                mark = "  OK" if abs((price or 0) - o) < 0.01 else f"  DIFF(基准 {o})"
            print(f"   {str(part)[:34]:36} {price}{mark}")
    finally:
        for c in ([browser.close()] if browser else []) + ([pw.stop()] if pw else []):
            try:
                await c
            except Exception:
                pass


if __name__ == "__main__":
    # 硬超时兜底：官网 SPA/选择器等任一环节卡住时，宁可失败也不挂死（实测曾挂 >3min）
    asyncio.run(asyncio.wait_for(main(), timeout=150))
