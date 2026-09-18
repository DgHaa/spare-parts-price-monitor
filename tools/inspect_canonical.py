#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""检查某个规范名（canonical_name）底下混了哪些原始名 —— 用于发现归一化过度合并。

用法：
    python tools/inspect_canonical.py 摄像头镜片
    python tools/inspect_canonical.py --suspect          # 全库扫描可疑规范名
    python tools/inspect_canonical.py --suspect --top 20

判定「可疑」的依据：同一 canonical_name 下
    · 原始名数量 >= 3（可能是杂物袋）
    · 或 CNY 价格 max/min > 5（跨度异常，不同实物被并到一起）
注意：高价差也可能是「不同机型同零件的正常差價」（如旗舰 vs 入门），
      所以 --suspect 只做提示，需人工看原始名再决定是否拆。
"""
import argparse
import sqlite3
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "spare_parts.db"


def inspect(conn, canonical):
    rows = conn.execute(
        """SELECT p.name, p.lang, p.part_type, COUNT(*) n,
                  ROUND(MIN(ps.cny_price),2) mn, ROUND(MAX(ps.cny_price),2) mx
           FROM parts p JOIN price_snapshots ps ON ps.part_id = p.id
           WHERE p.canonical_name = ?
           GROUP BY p.name ORDER BY mx DESC""",
        (canonical,),
    ).fetchall()
    if not rows:
        print(f"（无此规范名：{canonical}）")
        return
    print(f"=== 规范名「{canonical}」下的原始名（{len(rows)} 个）===")
    for name, lang, pt, n, mn, mx in rows:
        print(f"   {str(name)[:44]:46} [{str(lang):3}] pt={str(pt)[:8]:10} n={n:5} ¥{mn}~{mx}")


def suspect(conn, top):
    rows = conn.execute(
        """SELECT p.canonical_name cn, p.canonical_type ct,
                  COUNT(DISTINCT p.name) names, COUNT(*) n,
                  ROUND(MIN(ps.cny_price),2) mn, ROUND(MAX(ps.cny_price),2) mx
           FROM parts p JOIN price_snapshots ps ON ps.part_id = p.id
           WHERE p.canonical_name IS NOT NULL AND ps.cny_price > 0
           GROUP BY 1, 2
           HAVING names >= 3 OR (mn > 0 AND mx / mn > 5)
           ORDER BY (mx / NULLIF(mn,0)) DESC"""
    ).fetchall()
    print(f"=== 可疑规范名（原始名>=3 或 价差>5x）：{len(rows)} 个 ===")
    for cn, ct, names, n, mn, mx in rows[:top]:
        ratio = (mx / mn) if mn else 0
        print(f"   {cn[:24]:26} [{str(ct)[:8]:10}] 原始名{names:3}个 n={n:5} ¥{mn}~{mx}  跨度{ratio:,.0f}x")


def multi(conn, min_names, top):
    """列出原始名 >= min_names 的规范名及其全部原始名（人工看语义是否混杂）。"""
    cns = conn.execute(
        """SELECT p.canonical_name cn, p.canonical_type ct, COUNT(DISTINCT p.name) k
           FROM parts p WHERE p.canonical_name IS NOT NULL
           GROUP BY 1,2 HAVING k >= ? ORDER BY k DESC LIMIT ?""",
        (min_names, top),
    ).fetchall()
    for cn, ct, k in cns:
        names = [r[0] for r in conn.execute(
            "SELECT DISTINCT name FROM parts WHERE canonical_name=? ORDER BY name", (cn,))]
        print(f"\n[{ct}] {cn}  —— {k} 个原始名")
        for n in names:
            print(f"      {n}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("canonical", nargs="?", help="要检查的规范名")
    ap.add_argument("--suspect", action="store_true", help="全库扫描可疑规范名")
    ap.add_argument("--multi", type=int, metavar="N",
                    help="列出原始名 >=N 个的规范名及其全部原始名（查语义混杂）")
    ap.add_argument("--top", type=int, default=25)
    a = ap.parse_args()
    conn = sqlite3.connect(DB)
    if a.suspect:
        suspect(conn, a.top)
    elif a.multi:
        multi(conn, a.multi, a.top)
    elif a.canonical:
        inspect(conn, a.canonical)
    else:
        ap.print_help()
    conn.close()


if __name__ == "__main__":
    main()
