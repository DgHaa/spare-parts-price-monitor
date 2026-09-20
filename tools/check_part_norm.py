"""检查归一化覆盖率与漏网基名（dry-run，不写库）。"""
import sqlite3
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8")
from crawler import part_norm as pn  # noqa: E402

DB = ROOT / "spare_parts.db"


def main():
    c = sqlite3.connect(str(DB))
    rows = c.execute(
        "SELECT p.part_type, p.name, COUNT(*) n FROM parts p "
        "JOIN models m ON m.id=p.model_id GROUP BY p.part_type, p.name"
    ).fetchall()
    c.close()

    miss = Counter()
    miss_rows = 0
    hit = 0
    hit_rows = 0
    type_override = Counter()
    spec_examples = []
    for pt, name, n in rows:
        r = pn.normalize(name, pt)
        if r.rule == "fallback":
            miss[(pt, r.base_key)] += n
            miss_rows += n
        else:
            hit += 1
            hit_rows += n
        if r.canonical_type and r.canonical_type != pt:
            type_override[(pt, r.canonical_type)] += 1
        if r.spec and len(spec_examples) < 25:
            spec_examples.append((pt, name, r.canonical, r.spec, r.variant))

    total_rows = hit_rows + miss_rows
    print("名字覆盖: %d/%d (%.1f%%)" % (hit, len(rows), 100.0 * hit / len(rows)))
    print("行覆盖  : %d/%d (%.1f%%)" % (hit_rows, total_rows, 100.0 * hit_rows / total_rows))
    print()
    print("== 漏网基名 (part_type, base_key) 共 %d 个 ==" % len(miss))
    for (pt, bk), n in miss.most_common(80):
        print("   %5d  [%s] %s" % (n, pt, bk))
    print()
    print("== 跨品类归并 ==")
    for (a, b), n in type_override.most_common():
        print("   %5d  %s -> %s" % (n, a, b))
    print()
    print("== 规格解析样例 ==")
    for e in spec_examples:
        print("   [%s] %-34s -> %-14s spec=%-10s var=%s" % e)


if __name__ == "__main__":
    main()
