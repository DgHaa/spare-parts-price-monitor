"""诊断：小米/cn 本季仍为 brand_entry（未用官方 API 价覆盖）的 135 台，能否匹配到 mi.com class_id。

输出：
  - 能匹配到 cid 的台数（之前大概率只是 429 失败，重抓即可覆盖）
  - 匹配不到的台数（机型名不在官网清单，需改进匹配或如实保留 brand_entry）
"""
import sqlite3
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from crawler.model_links import norm, match_official_name  # noqa: E402
from tools.refetch_failing import xiaomi_class_map          # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "spare_parts.db"
Q = "2026Q3"


def main():
    cmap = xiaomi_class_map()
    nmap = {norm(k): (k, v) for k, v in cmap.items()}
    print(f"mi.com 实时机型清单：{len(cmap)} 条，归一化后 {len(nmap)} 条", flush=True)

    c = sqlite3.connect(DB); c.row_factory = sqlite3.Row
    # 本季快照里 source_url_kind='brand_entry' 的小米/cn 机型（= 缺口）
    rows = c.execute("""SELECT DISTINCT m.id, m.name, m.base_model
        FROM models m JOIN brands b ON b.id=m.brand_id
        JOIN parts pt ON pt.model_id=m.id
        JOIN price_snapshots ps ON ps.part_id=pt.id
        WHERE b.name='xiaomi' AND m.country_code='cn' AND ps.quarter=? AND ps.source_url_kind='brand_entry'
        ORDER BY m.name""", (Q,)).fetchall()
    c.close()
    print(f"缺口机型（brand_entry）：{len(rows)} 台\n", flush=True)

    matched, unmatched = [], []
    for m in rows:
        hit, by = match_official_name({"name": m["name"], "base_model": m["base_model"] or m["name"]}, nmap)
        if hit:
            matched.append((m["name"], hit[1], by))
        else:
            unmatched.append(m["name"])

    print(f"=== 可匹配到 class_id（重抓可覆盖）：{len(matched)} 台 ===")
    for name, cid, by in matched[:20]:
        print(f"  {name}  -> cid={cid} ({by})")
    if len(matched) > 20:
        print(f"  ...(其余 {len(matched)-20} 台省略)")
    print(f"\n=== 匹配不到（机型名不在官网清单）：{len(unmatched)} 台 ===")
    for name in unmatched:
        print(f"  {name}")
    print(f"\n结论：可回填 {len(matched)} 台，不可回填 {len(unmatched)} 台")


if __name__ == "__main__":
    main()
