"""tools/price_structure.py - 算「品牌×国家」的备件价格结构指标，用于判断跨国价差是否可比。

为什么需要：拿两国同备件相除得出"贵 N 倍"，在**归因上可能是错的**——各国定价模型不同。
实测：德国是「高底价 + 压缩」模型（低值件被抬到 €50–70 底价），而中/墨/土/马按零部件
成本定价。于是「德国卡托比中国贵 49 倍」数学正确、但归因错误（不是成本差）。

两个指标：
  - 动态范围 = 最贵备件 ÷ 最便宜备件（越小 → 越接近统一定价）
  - 底价聚集度 = 落在「最低价 ×1.5」内的不同价位占比（越高 → 越可能存在价格下限）

用法：
  python tools/price_structure.py                 # 全部品牌×国家
  python tools/price_structure.py --brand oppo
  python tools/price_structure.py --min-levels 8   # 只看价位数足够的格子（默认 8）
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "spare_parts.db"
FLOOR_BAND = 1.5  # 「底价带」倍率阈值
sys.stdout.reconfigure(encoding="utf-8")


def collect(conn, brand=None):
    sql = """SELECT b.name bd, m.country_code cc, ps.price pr
             FROM price_snapshots ps
             JOIN parts p ON p.id = ps.part_id
             JOIN models m ON m.id = p.model_id
             JOIN brands b ON b.id = m.brand_id
             WHERE ps.price > 0"""
    args = ()
    if brand:
        sql += " AND b.name = ?"
        args = (brand,)
    g = {}
    for r in conn.execute(sql, args):
        g.setdefault((r["bd"], r["cc"]), set()).add(r["pr"])
    return g


def metrics(levels):
    v = sorted(levels)
    lo, hi = v[0], v[-1]
    floor_n = sum(1 for x in v if x <= lo * FLOOR_BAND)
    return {"n": len(v), "min": lo, "med": v[len(v) // 2], "max": hi,
            "range": hi / lo, "floor_pct": 100.0 * floor_n / len(v)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--brand")
    ap.add_argument("--min-levels", type=int, default=8)
    a = ap.parse_args()

    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    g = collect(conn, a.brand)
    conn.close()

    rows = []
    for (bd, cc), levels in g.items():
        if len(levels) < a.min_levels:
            continue
        rows.append((bd, cc, metrics(levels)))
    rows.sort(key=lambda x: x[2]["range"])

    print(f"{'品牌/国':16}{'不同价位':>8}{'min':>12}{'中位':>12}{'max':>12}{'动态范围':>11}{'底价聚集度':>12}")
    print("-" * 84)
    for bd, cc, m in rows:
        print(f"{bd + '/' + cc:16}{m['n']:>8}{m['min']:>12.2f}{m['med']:>12.2f}"
              f"{m['max']:>12.2f}{m['range']:>10.1f}×{m['floor_pct']:>11.1f}%")

    if rows:
        print()
        print(f"提示：动态范围最小 / 底价聚集度最高的市场，其「跨国价差」多为**定价模型差异**，")
        print(f"不宜直接解读为成本差。参考阈值：动态范围 < 20× 且底价聚集度 > 15% 即可疑。")


if __name__ == "__main__":
    main()
