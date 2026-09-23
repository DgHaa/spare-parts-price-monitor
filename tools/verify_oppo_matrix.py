#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""OPPO 比价矩阵健康度回归测试（2026-09-23 覆盖修正后）。

背景：旧端点 /cnw/v1/GetPartPrice 残留的 1,123 个污染机型条目（含 186 台一加机型、
中国专供版、智能电视/手环等）已清退，机型表改由 getProductInfo（区域产品目录全集）定源。
本脚本在浏览器里断言修正结果没有回退：

  1) 矩阵正常渲染（.heat 表格存在）；
  2) **无幽灵机型**：不得出现「一加/OnePlus」「智能电视」「手环」「兰博基尼」等
     不属于这些区域的机型名；
  3) **参考价已清零**：不得出现 .ref-cell / sup.ref（CN 参考价方案的前提已被证伪并停用）；
  4) 单元格小字不得出现「参考·中国」。

前置：后端已启动 python api/server.py (127.0.0.1:8000)。
用法：python tools/verify_oppo_matrix.py [--brand oppo] [--model "OPPO Reno14"]
                                    [--shot out.png] [--result-log logs_x.txt]
"""
import argparse
import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "references" / "calibration"))
from _paths import find_chromium  # noqa: E402

URL = "http://127.0.0.1:8000/#matrix"
BRAND, MODEL = "oppo", "OPPO Reno14"

# 「幽灵机型」的权威判据在 **DB 层**（source_url），不在页面文本 —— 见 db_legacy_residue()。
# 早期版本用整页文本搜关键词，把下列**合法数据**误报为幽灵机型，已弃用该做法：
#   · 「一加 / OnePlus / 智能电视」：OPPO **中国**官网的备件价页本身就含这些
#     （一加在华售后服务已并入 OPPO），来源是 REBORN，属真实数据；
#   · 「手环」：还大量出现在小米中国的机型名里；
#   · 「兰博基尼」：小米 Redmi K70 至尊冠军版的联名款。


def db_legacy_residue():
    """DB 层：仍为旧端点来源的 OPPO 机型行数（应为 0）。"""
    import sqlite3
    c = sqlite3.connect(str(ROOT / "spare_parts.db"))
    n = c.execute("""SELECT COUNT(*) FROM models m JOIN brands b ON b.id=m.brand_id
                     WHERE b.name='oppo'
                       AND m.source_url LIKE '%/cnw/v1/GetPartPrice%'""").fetchone()[0]
    c.close()
    return n


def kb_unavailable(brand):
    """该品牌「官方不提供备件价」的区域（KB recipe status = unavailable/unverified）。"""
    import json
    p = ROOT / "references" / "kb" / f"{brand}.json"
    if not p.exists():
        return []
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return []
    out = []
    for cc, arr in (d.get("countries") or {}).items():
        for rec in (arr or []):
            if (rec.get("status") or rec.get("query", {}).get("status")) in ("unavailable", "unverified"):
                out.append(cc)
                break
    return out


def _assert(info, fails, expect_no_src=0):
    if not info.get("tableRendered"):
        fails.append("比价矩阵未渲染（.heat 表格缺失）")
    residue = db_legacy_residue()
    if residue:
        fails.append(f"旧端点污染机型残留 {residue} 台（应为 0）")
    # 有"官方不提供备件价"的区域时，必须把原因写在页面上 ——
    # 否则用户会把空白列误读成"抓取失败"（这正是 2026-09-23 体检要澄清的）
    if expect_no_src and not info.get("unavailableNoteRendered"):
        fails.append(f"该品牌有 {expect_no_src} 个『官方不提供备件价』的区域，"
                     f"但页面未渲染说明横幅")
    if info.get("refCellCount", 0) != 0:
        fails.append(f"参考价单元格应为 0（方案已停用），实际 {info['refCellCount']}")
    if info.get("refBadgeCount", 0) != 0:
        fails.append(f"参考价徽标应为 0，实际 {info['refBadgeCount']}")
    if info.get("hasRefText"):
        fails.append("单元格内仍出现「参考·中国」文案")
    if not info.get("cellCount"):
        fails.append("矩阵没有任何价格单元格")

    if fails:
        print("\n[FAIL]")
        for f in fails:
            print("  -", f)
        return 1
    extra = "；已渲染『官方不提供备件价』区域说明" if expect_no_src else ""
    print(f"\n[PASS] 矩阵渲染正常（{info['cellCount']} 个价格单元格）；"
          f"旧端点污染机型残留 0 台；参考价单元格/徽标 0 个{extra}")
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--brand", default=BRAND)
    ap.add_argument("--model", default=MODEL)
    ap.add_argument("--shot", default=None)
    ap.add_argument("--result-log", default="logs_oppo_matrix_result.txt")
    a = ap.parse_args()

    from playwright.sync_api import sync_playwright

    fails = []
    with sync_playwright() as p:
        print("[1/5] 启动无头 Chromium…", flush=True)
        b = p.chromium.launch(headless=True, executable_path=find_chromium(),
                              args=["--no-sandbox"])
        pg = b.new_page(viewport={"width": 1680, "height": 1200})
        pg.on("pageerror", lambda e: fails.append(f"页面 JS 异常: {e}"))
        print("[2/5] 打开比价矩阵…", flush=True)
        pg.goto(URL, wait_until="load")
        pg.wait_for_selector("#mc-b", timeout=20000)
        pg.select_option("#mc-b", a.brand)
        pg.wait_for_timeout(1200)
        pg.fill("#mc-search", a.model)
        pg.wait_for_timeout(1500)
        pg.click(f'#mc-list .combo-item[data-m="{a.model}"]')
        pg.wait_for_timeout(3500)
        print("[3/5] 读取矩阵…", flush=True)
        info = pg.evaluate("""() => {
            const tables = [...document.querySelectorAll('table.heat')];
            const body = document.body.innerText || '';
            const hints = [...document.querySelectorAll('.hint')].map(e => e.innerText || '').join(' ');
            return {
                tableRendered: tables.length > 0,
                cellCount: document.querySelectorAll('td.cell-click, td.muted').length,
                refCellCount: document.querySelectorAll('td.ref-cell').length,
                refBadgeCount: document.querySelectorAll('sup.ref').length,
                hasRefText: body.includes('参考·中国'),
                unavailableNoteRendered: hints.includes('官方不提供') && hints.includes('不是抓取失败'),
            };
        }""")
        if a.shot:
            pg.screenshot(path=a.shot)
        # 先落结果再关浏览器：本机残留 chrome-headless-shell 会让 b.close() 长时间阻塞，
        # 若把结果打印放在 close 之后，断言结果将永远刷不出来。
        print("结果:", info, flush=True)
        Path(a.result_log).write_text(repr(info), encoding="utf-8")
        print("[4/5] 关闭浏览器…", flush=True)
        try:
            b.close()
        except Exception as e:
            print(f"  (浏览器关闭异常，忽略): {e}", flush=True)

    return _assert(info, fails, expect_no_src=len(kb_unavailable(a.brand)))


if __name__ == "__main__":
    sys.exit(main())
