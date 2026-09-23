#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""为 OPPO 非 CN 区域「本季无官方本地价」的机型补 CN 官方参考价（B 方案）。

背景（2026-09-23 实测）：OPPO api_reborn getProduct 按区域只返回在售精选机型，
老机型在 de/ae/tr/mx/my/jp 官方无任何备件价（跨区复用 CN code 也被 10028 拒绝）。
但其中多数在 CN 有同名官方价。本脚本用同季度 CN 官方价作参考价回填，并标记
is_reference=1 / reference_region='cn'，绝不参与本地价差放大、CNY 单列、
前端灰色「参考·中国」徽标。

⚠️ 逻辑已抽到 `crawler/reference_prices.py`（季度抓取 run_all 收尾会自动调用，
保证后续季度持续补覆盖）。本脚本只是"手动补历史季度 + 自动备份"的薄壳。

- 预览（默认）：打印每区域将补多少机型/备件/价格行，不写库。
- --apply：先备份 DB，再写库。
- 幂等：本季已有快照的机型跳过（可重复跑）。

用法：
  python tools/backfill_oppo_cn_reference.py                 # 预览（默认季度=本季）
  python tools/backfill_oppo_cn_reference.py --quarter 2026Q3 --apply
"""
import argparse
import datetime as _dt
import os
import shutil
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from crawler.reference_prices import apply_cn_reference, format_stats  # noqa: E402
from db import this_quarter  # noqa: E402

DB = os.path.join(ROOT, "spare_parts.db")
BACKUP_DIR = os.path.join(ROOT, "backups")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--quarter", default=None, help="目标季度（默认=当前季度）")
    ap.add_argument("--brand", default="oppo")
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args()
    quarter = a.quarter or this_quarter()

    stats = apply_cn_reference(quarter, brand=a.brand, dry_run=True)
    print("=== 预览：将回填的 CN 参考价（区域 / 机型数 / 参考价行数）===")
    print(format_stats(stats))

    total_models = sum(d["models"] for d in stats["regions"].values())
    if not a.apply:
        print("\n[预览模式] 未写库。加 --apply 执行（会自动备份 DB）。")
        return 0
    if total_models == 0:
        print("\n[apply] 无需回填（该季度已全部有价或已有参考价），未做任何改动。")
        return 0

    os.makedirs(BACKUP_DIR, exist_ok=True)
    ts = _dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    bak = os.path.join(BACKUP_DIR, f"spare_parts_before_cn_refill_{ts}.db")
    shutil.copy2(DB, bak)
    print(f"\n[apply] 已备份 -> {bak}")

    stats = apply_cn_reference(quarter, brand=a.brand, dry_run=False)
    print(format_stats(stats))
    print(f"[apply] 完成：共补 {total_models} 台机型参考价"
          f"（is_reference=1, reference_region='cn', currency='CNY'）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
