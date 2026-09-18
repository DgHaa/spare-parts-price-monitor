#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""检测「统一定价档」——同一机型·同一国家内，多个不同备件的价格挤在窄带里。

背景（2026-09-17 实证）：
    OPPO 德国把低价配件（卡托 / 按键 / 充电接口 / 扬声器）定价在 €55–70 窄带，
    且**不同机型、不同配件价格完全相同**（A5 2025 与 A6 Pro 5G 的卡托都是 €62）。
    REBORN API 里这些件同属 groupCode=PHONE_OTHER，但各自有独立 retailPrice ——
    即"档位定价"，不是"零件成本定价"。
    若把 €62 直接挂在「卡托」名下，会被误读为"卡托零件成本 €62"，
    进而得出"德国卡托比中国贵 5000%"这种数学正确、归因错误的结论。

    注意：中国等市场按零件成本定价，同机型内备件价差可达数百倍，不会触发本检测。

用法：
    python tools/detect_price_bands.py --brand oppo --country de
    python tools/detect_price_bands.py --brand oppo --country cn   # 应几乎无输出（成本定价）
    python tools/detect_price_bands.py --brand oppo --country de --tol 0.15 --min-parts 3
"""
import argparse
import sqlite3
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "spare_parts.db"


def cluster(items, tol):
    """items=[(name, price)] 已按价升序。贪心聚类：与上一项相对差 <= tol 即同档。"""
    bands = []
    for nm, pr in items:
        if bands and abs(pr - bands[-1][-1][1]) <= bands[-1][-1][1] * tol:
            bands[-1].append((nm, pr))
        else:
            bands.append([(nm, pr)])
    return bands


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--brand", required=True)
    ap.add_argument("--country", required=True)
    ap.add_argument("--tol", type=float, default=0.15, help="同档相对容差，默认 0.15")
    ap.add_argument("--min-parts", type=int, default=5,
                    help="档内至少几个不同备件才判为统一定价档（默认 5，与 /api/price_bands 一致；"
                         "调低到 4 会把中国的配件同价误报为档位定价）")
    a = ap.parse_args()

    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """SELECT m.model_key mk, COALESCE(p.canonical_name, p.name) nm,
                  COALESCE(p.canonical_spec,'') cs, ps.price pr, ps.currency cu
           FROM price_snapshots ps JOIN parts p ON p.id=ps.part_id JOIN models m ON m.id=p.model_id
           JOIN brands b ON b.id=m.brand_id
           WHERE b.name=? AND m.country_code=? AND ps.price>0
           ORDER BY m.model_key, ps.price""",
        (a.brand, a.country)).fetchall()
    conn.close()

    by_model = {}
    for r in rows:
        by_model.setdefault(r["mk"], {})
        # 按 (件名, 规格) 去重：同名不同规格价格不同，合并会失真；同规格多颜色只取一条。
        by_model[r["mk"]].setdefault((r["nm"], r["cs"]), r["pr"])

    n_models, n_bands = 0, 0
    for mk, parts in by_model.items():
        items = sorted(((f"{nm} {cs}".strip(), pr) for (nm, cs), pr in parts.items()),
                       key=lambda x: x[1])
        wide = [b for b in cluster(items, a.tol) if len(b) >= a.min_parts]
        if not wide:
            continue
        n_models += 1
        print(f"\n=== {mk}  （共 {len(items)} 个备件）===")
        for b in wide:
            n_bands += 1
            lo, hi = b[0][1], b[-1][1]
            print(f"   [统一档 {lo:,.2f}–{hi:,.2f} {rows[0]['cu']}] {len(b)} 个不同备件同处一档：")
            for nm, pr in b:
                print(f"        {nm[:26]:28} {pr:>10,.2f}")
    print(f"\n汇总：{a.brand}/{a.country} 共 {len(by_model)} 个机型，"
          f"其中 {n_models} 个存在统一定价档；合计 {n_bands} 个档。")


if __name__ == "__main__":
    main()
