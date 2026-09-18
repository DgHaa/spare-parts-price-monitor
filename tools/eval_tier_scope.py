#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""评估「模式②（跨品牌同档位均价）」在不同分组口径下的表现。

用法：
    python tools/eval_tier_scope.py                # 键数对比 + 摄像头拆分效果
    python tools/eval_tier_scope.py --tier 高端     # 指定档位
    python tools/eval_tier_scope.py --cat 摄像头     # 指定品类看拆分后的规范名

背景：模式②原按 canonical_type（品类）平均，会把 ¥10 的「摄像头镜片」和
      ¥2600 的「后置潜望长焦摄像头」混算，均值无意义。本工具用于评估
      改为 canonical_name（规范件名）后的分组规模与可读性。
"""
import argparse
import sqlite3
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "spare_parts.db"

EXPRS = [
    ("canonical_type（现状·品类）", "COALESCE(p.canonical_type, p.part_type)"),
    ("canonical_name（建议·规范件名）", "COALESCE(p.canonical_name, p.name)"),
    ("name + spec", "COALESCE(p.canonical_name,p.name) || '|' || COALESCE(p.canonical_spec,'')"),
]


def tiers_overview(conn):
    """各档位下，两种口径的组数，以及「跨品牌可比」（>=2 品牌有数据）的组数。"""
    print("=== 各档位分组规模（模式② 实际按 tier 过滤）===")
    print(f"   {'档位':10} {'品牌':>4} {'品类级':>7} {'件名级':>7} {'其中>=2品牌可比':>15}")
    for (tier,) in conn.execute(
            "SELECT DISTINCT tier FROM models WHERE tier IS NOT NULL ORDER BY tier"):
        brands = conn.execute(
            """SELECT COUNT(DISTINCT b.name) FROM price_snapshots ps
               JOIN parts p ON p.id=ps.part_id JOIN models m ON m.id=p.model_id
               JOIN brands b ON b.id=m.brand_id WHERE m.tier=?""", (tier,)).fetchone()[0]
        ncat = conn.execute(
            """SELECT COUNT(DISTINCT COALESCE(p.canonical_type,p.part_type)) FROM price_snapshots ps
               JOIN parts p ON p.id=ps.part_id JOIN models m ON m.id=p.model_id WHERE m.tier=?""",
            (tier,)).fetchone()[0]
        rows = conn.execute(
            """SELECT COALESCE(p.canonical_name,p.name) cn, COUNT(DISTINCT b.name) nb
               FROM price_snapshots ps JOIN parts p ON p.id=ps.part_id JOIN models m ON m.id=p.model_id
               JOIN brands b ON b.id=m.brand_id WHERE m.tier=?
               GROUP BY 1""", (tier,)).fetchall()
        nname = len(rows)
        ncross = len([r for r in rows if r[1] >= 2])
        print(f"   {tier:10} {brands:>4} {ncat:>7} {nname:>7} {ncross:>15}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tier")
    ap.add_argument("--cat")
    ap.add_argument("--tiers", action="store_true", help="各档位分组规模概览")
    a = ap.parse_args()
    conn = sqlite3.connect(DB)

    if a.tiers:
        tiers_overview(conn)
        conn.close()
        return

    where = "WHERE 1=1"
    args = []
    if a.tier:
        where += " AND m.tier=?"
        args.append(a.tier)

    print("=== 分组键数对比 ===")
    for label, expr in EXPRS:
        n = conn.execute(
            f"""SELECT COUNT(DISTINCT {expr}) FROM price_snapshots ps
                JOIN parts p ON p.id=ps.part_id JOIN models m ON m.id=p.model_id {where}""",
            args,
        ).fetchone()[0]
        print(f"   {label:30} {n:5} 个键")

    if a.cat:
        print(f"\n=== 品类「{a.cat}」下的规范件名拆分 ===")
        rows = conn.execute(
            f"""SELECT COALESCE(p.canonical_name,p.name) cn, COALESCE(p.canonical_spec,'') cs,
                       COUNT(*) n, ROUND(AVG(ps.cny_price),2) avg, ROUND(MIN(ps.cny_price),2) mn,
                       ROUND(MAX(ps.cny_price),2) mx
                FROM price_snapshots ps JOIN parts p ON p.id=ps.part_id JOIN models m ON m.id=p.model_id
                {where} AND COALESCE(p.canonical_type,p.part_type)=?
                GROUP BY 1,2 ORDER BY avg DESC""",
            args + [a.cat],
        ).fetchall()
        print(f"   {'规范件名':26} {'规格':11} {'n':>5} {'均价':>10} {'最低':>9} {'最高':>10}")
        for cn, cs, n, avg, mn, mx in rows:
            print(f"   {cn[:24]:26} {(cs or '-')[:9]:11} {n:5} ¥{avg:>9,.2f} ¥{mn:>8,.2f} ¥{mx:>9,.2f}")
        if rows:
            lo = min(r[4] for r in rows)
            hi = max(r[5] for r in rows)
            print(f"\n   拆分后最贵/最便宜 = {hi/lo:,.0f}x（拆分前按品类混算无法体现此差异）")
    conn.close()


if __name__ == "__main__":
    main()
