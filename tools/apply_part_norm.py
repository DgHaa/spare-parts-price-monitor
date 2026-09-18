"""落库备件名归一化（方案 A）。

做三件事：
1. 给 `parts` 加归一化列：canonical_name / canonical_type / canonical_spec / variant /
   lang / norm_rule / norm_conf / norm_version。
2. 建 `part_alias` 表并把 `crawler/part_alias.json` 物化进去（可审计、可 SQL 查）。
3. 用 `crawler/part_norm.py` 回填全部 parts。

`parts.name` 与 `parts.part_type` **保持官网原文不动**，用于溯源。

用法：
    python tools/apply_part_norm.py --dry-run     # 只统计，不改库
    python tools/apply_part_norm.py --execute     # 备份 + 建表 + 回填
    python tools/apply_part_norm.py --status      # 查看当前回填情况
"""
from __future__ import annotations

import argparse
import shutil
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.append(str(ROOT))   # 用 append 即可（原 queue.py 遮蔽 stdlib 的问题已于 2026-09-17 改名 issue_queue.py 根治）

sys.stdout.reconfigure(encoding="utf-8")
from crawler import part_norm as pn  # noqa: E402

DB = ROOT / "spare_parts.db"

NEW_COLS = [
    ("canonical_name", "TEXT"),
    ("canonical_type", "TEXT"),
    ("canonical_spec", "TEXT"),
    ("variant", "TEXT"),
    ("lang", "TEXT"),
    ("norm_rule", "TEXT"),
    ("norm_conf", "INTEGER"),
    ("norm_version", "TEXT"),
]


def have_col(c, table, col):
    return any(r[1] == col for r in c.execute(f"PRAGMA table_info({table})"))


def ensure_schema(c):
    added = []
    for col, typ in NEW_COLS:
        if not have_col(c, "parts", col):
            c.execute(f"ALTER TABLE parts ADD COLUMN {col} {typ}")
            added.append(col)
    c.execute(
        """CREATE TABLE IF NOT EXISTS part_alias (
               part_type      TEXT NOT NULL,
               alias_key      TEXT NOT NULL,
               canonical_name TEXT NOT NULL,
               canonical_type TEXT,
               lang           TEXT,
               source         TEXT,
               PRIMARY KEY (part_type, alias_key)
           )"""
    )
    return added


def refresh_alias_table(c):
    c.execute("DELETE FROM part_alias")
    rows = [(pt, ak, cn, ct, lg, "part_alias.json@v" + pn.alias_version())
            for pt, ak, cn, ct, lg in pn.alias_rows()]
    c.executemany(
        "INSERT OR REPLACE INTO part_alias(part_type,alias_key,canonical_name,canonical_type,lang,source)"
        " VALUES(?,?,?,?,?,?)", rows)
    return len(rows)


def backfill(c, verbose=True):
    ver = "part_alias.json@v" + pn.alias_version()
    rows = c.execute("SELECT id, name, part_type FROM parts").fetchall()
    payload = []
    stats = {"alias": 0, "fallback": 0}
    for pid, name, pt in rows:
        r = pn.normalize(name, pt)
        ctype = r.canonical_type or (pt or "")
        payload.append((r.canonical, ctype, r.spec, r.variant, r.lang,
                        r.rule, r.conf, ver, pid))
        stats[r.rule] = stats.get(r.rule, 0) + 1
    c.executemany(
        """UPDATE parts SET canonical_name=?, canonical_type=?, canonical_spec=?,
               variant=?, lang=?, norm_rule=?, norm_conf=?, norm_version=? WHERE id=?""",
        payload)
    return len(payload), stats


def report(c):
    print("\n== 回填统计 ==")
    for r in c.execute("SELECT norm_rule, COUNT(*) FROM parts GROUP BY norm_rule"):
        print("   %-10s %d" % (r[0], r[1]))
    print("   未回填    %d" % c.execute(
        "SELECT COUNT(*) FROM parts WHERE canonical_name IS NULL").fetchone()[0])
    print("\n== 归一化前后「(canonical_type, name)」可分组数变化 ==")
    before = c.execute("SELECT COUNT(*) FROM (SELECT DISTINCT part_type,name FROM parts)").fetchone()[0]
    after = c.execute("SELECT COUNT(*) FROM (SELECT DISTINCT canonical_type,canonical_name,canonical_spec FROM parts)").fetchone()[0]
    print("   归一前: %d" % before)
    print("   归一后: %d  (降幅 %.1f%%)" % (after, 100.0 * (before - after) / before))
    print("\n== 合并力度最大的 15 组 ==")
    q = """SELECT canonical_type, canonical_name, canonical_spec,
                  COUNT(DISTINCT part_type||'|'||name) n_names, COUNT(*) n_rows
           FROM parts GROUP BY canonical_type, canonical_name, canonical_spec
           HAVING n_names > 1 ORDER BY n_names DESC LIMIT 15"""
    for r in c.execute(q):
        print("   %-8s %-16s %-10s %2d 个名字 → %d 行" % r)
    print("\n== 跨品类归并 ==")
    for r in c.execute("""SELECT part_type, canonical_type, COUNT(*) FROM parts
                          WHERE part_type<>canonical_type GROUP BY part_type,canonical_type"""):
        print("   %-8s -> %-8s %d 行" % r)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--execute", action="store_true")
    ap.add_argument("--status", action="store_true")
    a = ap.parse_args()

    if a.status:
        c = sqlite3.connect(DB)
        if not have_col(c, "parts", "canonical_name"):
            print("尚未落库（parts 无 canonical_name 列）")
        else:
            report(c)
        c.close()
        return

    if not (a.dry_run or a.execute):
        ap.error("需指定 --dry-run / --execute / --status")

    # 预演：只跑归一化，不碰库
    c = sqlite3.connect(DB)
    rows = c.execute("SELECT name, part_type FROM parts").fetchall()
    before = len(set(rows))
    triples = set()
    n_alias = 0
    for name, pt in rows:
        r = pn.normalize(name, pt)
        triples.add((r.canonical_type or pt or "", r.canonical, r.spec))
        n_alias += (r.rule == "alias")
    print("[归一化预演] parts=%d  别名命中=%d  可分组键 %d → %d (降幅 %.1f%%)"
          % (len(rows), n_alias, before, len(triples),
             100.0 * (before - len(triples)) / before))
    if a.dry_run:
        c.close()
        print("(--dry-run，未改动数据库)")
        return

    # 备份
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    bak = DB.with_suffix(f".db.bak-norm-{stamp}")
    shutil.copy2(DB, bak)
    print("已备份数据库 →", bak.name)

    added = ensure_schema(c)
    print("新增列:", added or "(已存在)")
    n = refresh_alias_table(c)
    print("part_alias 物化 %d 条" % n)
    cnt, stats = backfill(c)
    print("回填 %d 行 parts，规则分布 %s" % (cnt, stats))
    c.execute("CREATE INDEX IF NOT EXISTS idx_parts_canon ON parts(canonical_type, canonical_name, canonical_spec)")
    c.commit()
    report(c)
    c.close()


if __name__ == "__main__":
    main()
