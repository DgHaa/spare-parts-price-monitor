"""verify_status_ui.py - 用真实浏览器校验「运行状态 / 覆盖度」在界面上的呈现。

背景：2026-09-23 把 run_logs.status 的 skipped 拆成 resumed / unavailable / skipped，
并给覆盖矩阵新增 unavailable（官方不提供）格子。数据库改对了不等于界面对——
本脚本起真实 Chromium 打开总览页，断言：

  1. KPI 出现「官方不提供」且计数与接口一致
  2. 覆盖度图例含「官方不提供」
  3. 覆盖矩阵里存在 `.cov-na` 格子，数量与接口的 cov_status=unavailable 一致
  4. 悬停格子的 title 里能看到新的状态文案（resumed / unavailable）

前置：后端已启动（python api/server.py，127.0.0.1:8000）。
用法：python tools/verify_status_ui.py [--url http://127.0.0.1:8000/] [--shot out.png]
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.append(str(ROOT))

from crawler.core import launch_browser  # noqa: E402

FAILS: list[str] = []


def check(name, got, want):
    ok = got == want
    print(f"  {'✓' if ok else '✗'} {name}: got={got!r} want={want!r}")
    if not ok:
        FAILS.append(name)


def check_true(name, got):
    ok = bool(got)
    print(f"  {'✓' if ok else '✗'} {name}: {got!r}")
    if not ok:
        FAILS.append(name)


async def main(url, shot):
    base = url.rstrip("/")
    with urllib.request.urlopen(base + "/api/overview", timeout=15) as r:
        ov = json.load(r)
    n_unav = ov["kpis"]["coverage"].get("unavailable", 0)
    print(f"接口：coverage.unavailable = {n_unav}")

    pw, b = await launch_browser()
    page = await b.new_page(viewport={"width": 1440, "height": 1100}, device_scale_factor=2)
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=25000)
        await page.wait_for_timeout(3000)

        # 1) KPI
        kpi = await page.inner_text(".grid.kpis")
        check_true("KPI 含「官方不提供」", "官方不提供" in kpi)
        # 2) 图例
        legend = await page.inner_text(".cov-legend")
        check_true("图例含「官方不提供」", "官方不提供" in legend)
        # 3) 矩阵格子
        n_na = await page.locator(".cov-cell.cov-na").count()
        check("覆盖矩阵 .cov-na 格子数", n_na, n_unav)
        # 4) 悬停文案
        titles = await page.eval_on_selector_all(
            ".cov-cell[title]", "els => els.map(e => e.getAttribute('title'))")
        joined = " || ".join(titles)
        check_true("悬停文案出现「官网不提供」", "官网不提供" in joined)
        check_true("悬停文案出现「续跑」", "续跑" in joined)
        # 旧文案不该再出现
        check_true("不再出现含糊的「跳过（断点续跑/无新增）」",
                   "跳过（断点续跑/无新增）" not in joined)

        if shot:
            Path(shot).parent.mkdir(parents=True, exist_ok=True)
            await page.screenshot(path=shot, full_page=True)
            print(f"  截图：{shot}")
    finally:
        for closer in (b.close(), pw.stop()):
            try:
                await asyncio.wait_for(closer, timeout=5)
            except Exception:
                pass

    print()
    if FAILS:
        print(f"结果：{len(FAILS)} 项未通过 ✗ → {FAILS}")
        return 1
    print("结果：全部通过 ✓")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://127.0.0.1:8000/")
    ap.add_argument("--shot", default=None)
    a = ap.parse_args()
    sys.exit(asyncio.run(main(a.url, a.shot)))
