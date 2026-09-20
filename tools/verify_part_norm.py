"""归一化回归验证：找"错误合并"信号 + 确认目标合并生效。

错误合并的判据：同一 (机型, 国家) 内，归一到同一 (canonical_type, canonical_name,
canonical_spec) 的多条原文价，最高/最低 > 3 倍 —— 说明把不同部件或不同规格并到了一行。
"""
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
DB = str(Path(__file__).resolve().parents[1] / "spare_parts.db")
c = sqlite3.connect(DB)

print("=" * 78)
print("1) 归一化总览")
print("=" * 78)
before = c.execute("SELECT COUNT(*) FROM (SELECT DISTINCT part_type,name FROM parts)").fetchone()[0]
after = c.execute("SELECT COUNT(*) FROM (SELECT DISTINCT canonical_type,canonical_name,canonical_spec FROM parts)").fetchone()[0]
print("   可分组键: %d -> %d  (降幅 %.1f%%)" % (before, after, 100.0 * (before - after) / before))
print("   未回填行:", c.execute("SELECT COUNT(*) FROM parts WHERE canonical_name IS NULL").fetchone()[0])
print("   低置信度(fallback)行:", c.execute("SELECT COUNT(*) FROM parts WHERE norm_conf=0").fetchone()[0])

print()
print("=" * 78)
print("2) 错误合并嫌疑：同 (机型,国家) 内归一组价差 > 3 倍")
print("=" * 78)
q = """
SELECT b.name, m.name, m.country_code, p.canonical_type, p.canonical_name,
       p.canonical_spec, MIN(ps.cny_price), MAX(ps.cny_price), COUNT(*) n,
       GROUP_CONCAT(DISTINCT p.part_type||'/'||p.name)
FROM price_snapshots ps
JOIN parts p ON p.id=ps.part_id
JOIN models m ON m.id=p.model_id
JOIN brands b ON b.id=m.brand_id
WHERE ps.cny_price IS NOT NULL AND ps.cny_price > 0
GROUP BY m.id, p.canonical_type, p.canonical_name, p.canonical_spec
HAVING MAX(ps.cny_price) / MIN(ps.cny_price) > 3
ORDER BY MAX(ps.cny_price) / MIN(ps.cny_price) DESC
"""
rows = c.execute(q).fetchall()
print("   可疑组数: %d  (总归一组 %d)" % (len(rows), after))
for r in rows[:25]:
    print("   %5.1fx  [%s/%s/%s %s] %s | %.0f~%.0f CNY (%d条)" %
          (r[7] / r[6] if r[6] else 0, r[0], r[1], r[2], r[4], r[5] or '-', r[6], r[7], r[8]))
    print("           原文: %s" % (r[9][:150]))

print()
print("=" * 78)
print("3) 目标合并验证")
print("=" * 78)
targets = [
    ("屏幕", "屏幕组件"), ("摄像头", "前置摄像头"), ("摄像头", "后置主摄像头"),
    ("电池", "电池"), ("其他", "卡托"), ("其他", "听筒"), ("数据线", "数据线"),
]
for ct, cn in targets:
    r = c.execute("""SELECT COUNT(DISTINCT p.name), COUNT(DISTINCT m.country_code),
                            GROUP_CONCAT(DISTINCT m.country_code)
                     FROM parts p JOIN models m ON m.id=p.model_id
                     WHERE p.canonical_type=? AND p.canonical_name=?""", (ct, cn)).fetchone()
    print("   %-6s/%-10s : 原文名 %3d 个, 覆盖 %d 国 [%s]" % (ct, cn, r[0], r[1], r[2]))

print()
print("=" * 78)
print("4) 规格必须保持独立（主板）")
print("=" * 78)
for r in c.execute("""SELECT canonical_spec, COUNT(*) FROM parts
                      WHERE canonical_type='主板' GROUP BY canonical_spec ORDER BY canonical_spec"""):
    print("   主板 spec=%-10s %d 行" % (r[0] or '-', r[1]))

print()
print("=" * 78)
print("5) 颜色变体合并是否丢失价格（取 min 的行）")
print("=" * 78)
q2 = """
SELECT b.name, m.name, p.canonical_name, MIN(ps.cny_price), MAX(ps.cny_price),
       COUNT(DISTINCT p.name), GROUP_CONCAT(DISTINCT p.name)
FROM price_snapshots ps JOIN parts p ON p.id=ps.part_id JOIN models m ON m.id=p.model_id
JOIN brands b ON b.id=m.brand_id
WHERE p.variant <> '' AND ps.cny_price IS NOT NULL
GROUP BY m.id, p.canonical_type, p.canonical_name, p.canonical_spec
HAVING MAX(ps.cny_price) > MIN(ps.cny_price)
"""
rows2 = c.execute(q2).fetchall()
print("   同机型同部件但变体间价格不同的组: %d" % len(rows2))
for r in rows2[:10]:
    print("   [%s %s] %s: %.0f~%.0f CNY | %s" % (r[0], r[1], r[2], r[3], r[4], (r[6] or '')[:80]))
c.close()
