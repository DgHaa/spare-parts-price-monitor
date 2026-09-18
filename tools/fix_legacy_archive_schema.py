"""tools/fix_legacy_archive_schema.py - 修复 parts_legacy_archive 的过期表结构（含归一化列回填）。

背景（2026-09-17 发现）：
  归档三表是 archive_oppo_legacy.py 用
  `create table if not exists {t} as select * from {src} where 0` 建的，
  建表时 `parts` 只有 4 列（id/model_id/name/part_type）。之后备件名归一化方案落地，
  `parts` 增到 12 列（canonical_name / canonical_type / canonical_spec / variant /
  lang / norm_rule / norm_conf / norm_version），但**归档表没跟着迁移**。
  于是任何归档写入都会报
    `sqlite3.OperationalError: table parts_legacy_archive has 4 columns but 12 values were supplied`
  —— 即 archive_oppo_legacy.py 今天再跑 --execute 也会失败，属既有隐患。

本脚本把 parts_legacy_archive 重建为与 parts 同构，并保留既有 13836 行；
缺失的 8 个归一化列用项目自带的 crawler.part_norm.normalize 回填
（与 db.upsert_part 用的是同一个归一化器，口径一致），失败则留 NULL。

用法：
  python tools/fix_legacy_archive_schema.py --dry-run
  python tools/fix_legacy_archive_schema.py --execute
"""
import argparse
import shutil
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "spare_parts.db"
T_ARC, T_SRC = "parts_legacy_archive", "parts"


def _cols(cur, t):
    return [x[1] for x in cur.execute(f"PRAGMA table_info({t})")]


def _need_fix(cur):
    a, b = _cols(cur, T_SRC), _cols(cur, T_ARC)
    return [c for c in a if c not in b]


def migrate(cur, dry=True):
    src_cols = _cols(cur, T_SRC)
    arc_cols = _cols(cur, T_ARC)
    missing = [c for c in src_cols if c not in arc_cols]
    n = cur.execute(f"select count(*) from {T_ARC}").fetchone()[0]
    print(f"[info] {T_ARC}: {n} 行，{len(arc_cols)} 列；需补齐列 = {missing}")
    if dry:
        print("[dry-run] 未改库（执行请加 --execute）")
        return
    if not missing:
        print("[execute] 结构已一致，无需修复")
        return
    sys.path.insert(0, str(ROOT))
    from crawler import part_norm as pn
    new_t = T_ARC + "_new"
    cur.execute(f"drop table if exists {new_t}")
    cur.execute(f"create table {new_t} as select * from {T_SRC} where 0")
    rows = cur.execute(f"select * from {T_ARC}").fetchall()
    idx = {c: i for i, c in enumerate(arc_cols)}
    filled = 0
    for r in rows:
        vals = {c: r[idx[c]] if c in idx else None for c in src_cols}
        # 回填归一化列（与 db.upsert_part 同一归一化器；异常则留 NULL，绝不编造）
        try:
            nr = pn.normalize(vals.get("name") or "", vals.get("part_type"))
            vals["canonical_name"] = nr.canonical
            vals["canonical_type"] = nr.canonical_type or vals.get("part_type") or ""
            vals["canonical_spec"] = nr.spec
            vals["variant"] = nr.variant
            vals["lang"] = nr.lang
            vals["norm_rule"] = nr.rule
            vals["norm_conf"] = nr.conf
            vals["norm_version"] = "part_alias.json@v" + pn.alias_version()
            filled += 1
        except Exception:
            pass
        cur.execute(
            f"insert into {new_t}({','.join(src_cols)}) "
            f"values({','.join('?' * len(src_cols))})",
            [vals[c] for c in src_cols])
    cur.execute(f"drop table {T_ARC}")
    cur.execute(f"alter table {new_t} rename to {T_ARC}")
    print(f"[execute] 已重建 {T_ARC}：{len(rows)} 行，{len(src_cols)} 列，"
          f"归一化列回填 {filled}/{len(rows)} 行")


def main(mode):
    c = sqlite3.connect(DB)
    cur = c.cursor()
    if mode == "dry-run":
        migrate(cur, dry=True)
    else:
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        dst = DB.with_suffix(f".db.bak-{ts}")
        shutil.copy2(DB, dst)
        print(f"[backup] {dst.name}")
        migrate(cur, dry=False)
        c.commit()
    c.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--dry-run", action="store_true")
    g.add_argument("--execute", action="store_true")
    a = ap.parse_args()
    main("dry-run" if a.dry_run else "execute")
